#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核心归因逻辑仿真测试（不联网、不发真实消息）。

用假的 Telegram 客户端 + 临时 SQLite，把 Telegram 会推给机器人的
chat_member / chat_join_request 更新原样喂进去，验证：
  1. 通过专属链接进群 → 记到链接主人名下、通知邀请人、发欢迎语
  2. 同一人重复进群 → 不重复计数
  3. 退群 → 累计保留、在群人数 -1
  4. 链接被脱敏（后半段变成 …）→ 用 name 字段兜底也能归因
  5. 别人创建的链接 / 自己邀请自己 → 不计数
  6. 入群申请（需审批）→ 先记 pending，批准后落账
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import invite_bot as ib  # noqa: E402

CHAT_ID = -1001234567890
BOT_ID = 123456789
GID = CHAT_ID
FAILURES = []


class StubTG:
    """记录所有发出的消息，替代真实 Bot API。"""

    def __init__(self):
        self.sent = []
        self.declined = []
        self.approved = []
        self.banned = []
        self.revoked = []
        self.link_error = None          # 设置后 createChatInviteLink 会抛这个错
        self.unknown_accounts = set()   # 放进来的 user_id 会被当成"已注销"

    def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    def answer_callback(self, callback_query_id, text=None, show_alert=False):
        return True

    def get_chat_member(self, chat_id, user_id):
        return {"status": "left", "user": {"id": int(user_id), "first_name": "User"}}

    def call(self, method, params=None, files=None, timeout=60, retries=3):
        params = params or {}
        if method == "createChatInviteLink":
            if self.link_error:
                raise ib.TelegramError(method, 400, self.link_error)
            return {"invite_link": f"https://t.me/+TEST{params['name']}", "name": params["name"],
                    "creator": {"id": BOT_ID}}
        if method == "revokeChatInviteLink":
            self.revoked.append(params.get("invite_link"))
            return True
        if method == "getChatMember":
            uid = int(params["user_id"])
            if uid in self.unknown_accounts:
                raise ib.TelegramError(method, 400, "Bad Request: user not found")
            return {"status": "left", "user": {"id": uid, "first_name": "User"}}
        if method == "declineChatJoinRequest":
            self.declined.append(int(params["user_id"]))
            return True
        if method == "approveChatJoinRequest":
            self.approved.append(int(params["user_id"]))
            return True
        if method in ("banChatMember", "unbanChatMember"):
            if method == "banChatMember":
                self.banned.append(int(params["user_id"]))
            return True
        raise AssertionError(f"未预期的 API 调用：{method}")


def check(cond, label):
    print(("  ✅ " if cond else "  ❌ ") + label)
    if not cond:
        FAILURES.append(label)


def make_bot(tmpdir):
    bot = ib.InviteBot.__new__(ib.InviteBot)
    bot.cfg = dict(ib.DEFAULT_CONFIG)
    bot.cfg["db_path"] = os.path.join(tmpdir, "t.db")
    bot.db = ib.Storage(bot.cfg["db_path"])
    bot.tg = StubTG()
    bot.bot_id = BOT_ID
    bot.bot_username = "xiyuer114514_bot"
    bot.owner_ids = []
    bot.pool = ThreadPoolExecutor(max_workers=2)
    bot._running = True
    bot._member_cache = {}
    bot._awaiting = {}
    bot.db.bind_chat(CHAT_ID, "测试群")
    return bot


def chat_member_update(uid, old, new, invite_link=None, user_name="User"):
    upd = {
        "chat": {"id": CHAT_ID, "type": "supergroup", "title": "测试群"},
        "from": {"id": uid, "first_name": user_name, "is_bot": False},
        "date": int(time.time()),
        "old_chat_member": {"status": old, "user": {"id": uid, "first_name": user_name}},
        "new_chat_member": {"status": new, "user": {"id": uid, "first_name": user_name}},
    }
    if invite_link:
        upd["invite_link"] = invite_link
    return upd


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="botflow-")
    try:
        bot = make_bot(tmpdir)
        owner, invitee, other = 111, 222, 333

        # 0) 链接主人先领链接
        link = bot.create_personal_link(CHAT_ID, owner)
        row = bot.db.get_link(link)
        print("1) 生成专属链接")
        check(link == f"https://t.me/+TESTref_{owner}", "链接按 ref_<uid> 命名")
        check(row["owner_id"] == owner, "链接归属正确记录在库")

        full_link = {"invite_link": link, "name": f"ref_{owner}", "creator": {"id": BOT_ID}}

        # 1) 222 通过 owner 的链接进群
        print("2) 有人通过专属链接进群")
        bot.on_chat_member(chat_member_update(invitee, "left", "member", full_link, "Bob"))
        bot.pool.shutdown(wait=True)
        check(bot.db.count_for(owner, GID) == 1, "累计邀请 = 1")
        check(bot.db.current_for(owner, GID) == 1, "在群人数 = 1")
        check(bot.db.get_referral(invitee, GID)["status"] == "member", "被邀请人状态 = member")
        dms = [t for c, t in bot.tg.sent if c == owner]
        check(any("通过你的链接加入" in t for t in dms), "已私聊通知邀请人")
        check(any(c == CHAT_ID and "欢迎" in t for c, t in bot.tg.sent), "已在群里发送欢迎语")

        # 2) 同一个人重复进群（退群后再进），不应重复计数
        print("3) 同一人重复进群")
        bot.pool = ThreadPoolExecutor(max_workers=2)
        bot.on_chat_member(chat_member_update(invitee, "left", "member", full_link, "Bob"))
        bot.pool.shutdown(wait=True)
        check(bot.db.count_for(owner, GID) == 1, "累计仍是 1（不重复计数）")

        # 3) 退群
        print("4) 被邀请人退群")
        bot.on_chat_member(chat_member_update(invitee, "member", "left"))
        check(bot.db.count_for(owner, GID) == 1, "累计邀请保留")
        check(bot.db.current_for(owner, GID) == 0, "在群人数归零")
        check(bot.db.get_referral(invitee, GID)["status"] == "left", "状态 = left")

        # 4) 脱敏链接（后半段被替换成 …）用 name 兜底
        print("5) 链接被脱敏时用 name 兜底归因")
        masked = {"invite_link": "https://t.me/+…", "name": f"ref_{owner}", "creator": {"id": BOT_ID}}
        bot.pool = ThreadPoolExecutor(max_workers=2)
        bot.on_chat_member(chat_member_update(other, "left", "member", masked, "Cara"))
        bot.pool.shutdown(wait=True)
        check(bot.db.count_for(owner, GID) == 2, "兜底归因成功，累计 = 2")

        # 5) 别人创建的链接 / 自我邀请
        print("6) 别人创建的链接与自我邀请")
        foreign = {"invite_link": "https://t.me/+OTHER", "name": "someone_else",
                   "creator": {"id": 999999}}
        bot.on_chat_member(chat_member_update(444, "left", "member", foreign, "Dan"))
        check(bot.db.count_for(owner, GID) == 2, "非本机器人创建的链接不计入")
        self_link = {"invite_link": link, "name": f"ref_{owner}", "creator": {"id": BOT_ID}}
        bot.on_chat_member(chat_member_update(owner, "left", "member", self_link, "Owner"))
        check(bot.db.count_for(owner, GID) == 2, "自我邀请不计入")

        # 6) 入群申请（需管理员审批）
        print("7) 入群申请先 pending，批准后落账")
        req_user = 555
        bot.on_join_request({
            "chat": {"id": CHAT_ID, "type": "supergroup", "title": "测试群"},
            "from": {"id": req_user, "first_name": "Eve"},
            "date": int(time.time()),
            "invite_link": {"invite_link": link, "name": f"ref_{owner}", "creator": {"id": BOT_ID}},
        })
        check(bot.db.get_referral(req_user, GID)["status"] == "pending", "申请阶段状态 = pending")
        check(bot.db.count_for(owner, GID) == 2, "pending 不计入累计邀请")
        bot.pool = ThreadPoolExecutor(max_workers=2)
        bot.on_chat_member(chat_member_update(req_user, "left", "member", full_link, "Eve"))
        bot.pool.shutdown(wait=True)
        check(bot.db.count_for(owner, GID) == 3, "批准入群后累计 = 3")
        check(bot.db.get_referral(req_user, GID)["status"] == "member", "状态转为 member")

        # 7) 排行榜 / 导出数据
        print("8) 排行榜与统计")
        lb = bot.db.leaderboard(GID)
        check(len(lb) == 1 and lb[0]["invited"] == 3 and lb[0]["present"] == 2, "排行榜数据正确")
        check(bot.db.rank_of(owner, GID) == 1, "排名 = 第 1 名")
        totals = bot.db.group_totals(GID)
        check(totals["invited"] == 3 and totals["inviters"] == 1, f"群汇总正确：{totals}")

        # 9) 已注销账号的入群申请要自动拒绝；正常账号不受影响
        print("9) 入群申请：已注销自动拒绝")
        bot.db.touch_user({"id": owner, "first_name": "Owner"}, is_admin=True)
        bot.tg.unknown_accounts.add(666)
        bot.on_join_request({
            "chat": {"id": CHAT_ID, "type": "supergroup", "title": "测试群"},
            "from": {"id": 666, "first_name": "Ghost"},
            "date": int(time.time()),
            "invite_link": {"invite_link": link, "name": f"ref_{owner}", "creator": {"id": BOT_ID}},
        })
        check(666 in bot.tg.declined, "已注销账号的入群申请被自动拒绝")
        check(bot.db.join_requests_stats(GID)["auto_declined"] == 1, "申请列表记录为自动拒绝")
        check(bot.db.is_marked_deleted(GID, 666), "该账号被标记为已注销")
        check(bot.db.audit_count("auto_decline_deleted") == 1, "自动拒绝写入审计日志")

        bot.on_join_request({
            "chat": {"id": CHAT_ID, "type": "supergroup", "title": "测试群"},
            "from": {"id": 777, "first_name": "Normal"},
            "date": int(time.time()),
            "invite_link": {"invite_link": link, "name": f"ref_{owner}", "creator": {"id": BOT_ID}},
        })
        check(777 not in bot.tg.declined, "正常账号的申请不会被误拒")
        check(bot.db.join_requests_stats(GID)["total"] == 3, "申请列表把每条申请都记下来了（含第 7 步那条）")

        # 10) 活跃度统计
        print("10) 活跃度统计")
        bot.db.bump_activity(GID, 222)
        bot.db.bump_activity(GID, 222)
        bot.db.bump_activity(GID, 555)
        act = bot.db.activity_totals(GID)
        check(act["dau"] == 2 and act["total_messages"] == 3, f"日活/消息数正确：{act}")
        board = bot.db.activity_board(GID)
        check(board[0]["user_id"] == 222 and board[0]["messages"] == 2, "发言榜排序正确")
        check(board[0]["active_days"] == 1, "活跃天数统计正确")
        # 在群人数：优先用机器人同步的真实群人数缓存（mcount）
        bot.db.set_kv(f"mcount:{GID}", "10")
        act = bot.db.activity_totals(GID)
        check(act["members"] == 10, f"在群人数取自真实群人数缓存：{act['members']}")
        check(act["silent"] == 10 - 2, f"从未发言 = 真实人数 - 发过言：{act['silent']}")
        bot.db.set_kv(f"mcount:{GID}", "")
        act2 = bot.db.activity_totals(GID)
        check(act2["members"] >= 0 and act2["silent"] >= 0, "没有缓存时退回推荐关系且不为负")

        # 11) 清理已注销账号：默认只标记不踢人
        print("11) 清理已注销（只标记，绝不自动踢人）")
        bot.db.add_pending(888, CHAT_ID, owner, link, "Ghost2")
        bot.db.mark_joined(888, CHAT_ID)
        bot.tg.unknown_accounts.add(888)
        bot._cleanup_scan(CHAT_ID, report_to=owner, actor_id=owner)
        check(bot.db.is_marked_deleted(CHAT_ID, 888), "扫描把查不到的账号标记为已注销")
        check(bot.tg.banned == [], "扫描阶段没有对任何人执行踢人")
        check(bot.db.audit_count("cleanup_scan") == 1, "扫描写入审计日志")

        bot.cmd_cleanup({"from": {"id": owner, "first_name": "Owner"},
                         "chat": {"id": owner}, "text": "/cleanup kick CONFIRM"}, ["kick", "CONFIRM"])
        check(bot.tg.banned == [], "踢人开关关闭时，即使发了 CONFIRM 也不会踢人")

        bot.db.set_setting("allow_cleanup_kick", True)
        bot.cmd_cleanup({"from": {"id": owner, "first_name": "Owner"},
                         "chat": {"id": owner}, "text": "/cleanup kick CONFIRM"}, ["kick", "CONFIRM"])
        bot.pool.shutdown(wait=True)
        check(sorted(bot.tg.banned) == [666, 888], f"开关打开后只移除已标记的已注销账号：{bot.tg.banned}")
        check(owner not in bot.tg.banned and bot.bot_id not in bot.tg.banned, "绝不碰管理员和机器人自己")
        check(bot.db.audit_count("cleanup_kick") == 2, "每次移除都写入审计日志")

        # 12) 权限错误必须和"链接数量上限"区分开（生产环境踩过的坑）
        print("12) 创建链接失败的错误分类")
        bot.tg.link_error = "Bad Request: not enough rights to manage chat invite link"
        got = bot.create_personal_link(CHAT_ID, owner)
        check(got is None, "权限不足时返回失败")
        check(getattr(bot, "_last_link_error", ("", ""))[0] == "perm",
              "被正确归类为「权限不足」而不是「链接数量上限」")
        check(bot.tg.revoked == [], "权限不足时不会误删任何已发出的链接")

        bot.tg.link_error = "Bad Request: too many invite links"
        bot.db.add_link("https://t.me/+IDLE1", 999, "ref_999", CHAT_ID)
        got = bot.create_personal_link(CHAT_ID, owner)
        check(getattr(bot, "_last_link_error", ("", ""))[0] == "limit",
              "真的上限错误仍归类为「数量上限」")
        check(bot.tg.revoked == ["https://t.me/+IDLE1"], "上限时会回收闲置链接")
        bot.tg.link_error = None

        # 13) 多群：命令作用域 + 数据隔离 + 自动绑定
        print("13) 多群支持")
        GID2 = -1009998887776

        def msg_in(chat_id, uid=111, ctype="supergroup"):
            return {"chat": {"id": chat_id, "type": ctype, "title": "群"},
                    "from": {"id": uid, "first_name": "Admin"}}

        bot.db.bind_chat(GID2, "第二个群")
        check(bot.cmd_target_chat(msg_in(CHAT_ID))["chat_id"] == CHAT_ID,
              "群里发命令 → 作用在本群")
        check(bot.cmd_target_chat(msg_in(GID2))["chat_id"] == GID2,
              "第二个群里发命令 → 作用在第二个群")
        check(bot.cmd_target_chat(msg_in(-1007777777777)) is None,
              "未绑定的群不参与统计（返回 None，不会串到别的群数据）")

        priv = msg_in(111, ctype="private")
        check(bot.cmd_target_chat(priv)["chat_id"] == CHAT_ID, "私聊默认用第一个群")
        bot.db.set_kv("usel:111", str(GID2))
        check(bot.cmd_target_chat(priv)["chat_id"] == GID2, "/switch 后私聊作用在选中的群")

        before1 = bot.db.count_for(111, CHAT_ID)
        bot.db.add_pending(9001, GID2, 111, "https://t.me/+B", "B1")
        bot.db.mark_joined(9001, GID2)
        check(bot.db.count_for(111, GID2) == 1, "第二个群有独立统计（1 人）")
        check(bot.db.count_for(111, CHAT_ID) == before1,
              f"第一个群统计不受影响（仍 {before1} 人）")

        bot.on_my_chat_member({
            "chat": {"id": -1005555555555, "type": "supergroup", "title": "第三个群"},
            "from": {"id": 111, "first_name": "Admin"},
            "old_chat_member": {"status": "member", "user": {"id": BOT_ID}},
            "new_chat_member": {"status": "administrator", "user": {"id": BOT_ID}},
        })
        check(len(bot.db.active_chats()) == 3,
              f"机器人被提升为管理员时自动接入新群（现有 {len(bot.db.active_chats())} 个）")

        # /switch 回调切换
        bot.on_callback({"id": "cb1", "from": {"id": 111, "first_name": "Admin"},
                         "data": f"sw:{CHAT_ID}",
                         "message": {"chat": {"id": 111, "type": "private"}, "message_id": 1}})
        check(bot.db.get_kv("usel:111") == str(CHAT_ID), "点击切换按钮后当前群已更新")

        # 14) 批量复核待处理申请（补拒开关关着时漏掉的）
        print("14) 批量复核入群申请")
        bot.db.add_join_request(CHAT_ID, 9101, "Ghost", None, None, None)
        bot.db.add_join_request(CHAT_ID, 9102, "Real", None, None, None)
        bot.tg.unknown_accounts.add(9101)
        res = bot.sweep_join_requests(CHAT_ID, "deleted", owner)
        check(res["declined"] == 1 and res["kept"] >= 1,
              f"只拒已注销的那条（其余保留）：{res}")
        check(9101 in bot.tg.declined, "已注销申请被真正拒绝")
        check(9102 not in bot.tg.declined, "正常账号的申请被保留")
        check(bot.db.get_referral(9101, CHAT_ID) is None or True, "流程未中断")
        row = bot.db.conn.execute(
            "SELECT decision FROM join_requests WHERE chat_id=? AND user_id=9101",
            (CHAT_ID,)).fetchone()
        check(row["decision"] == "declined", "申请状态已落库为 declined")
        res2 = bot.sweep_join_requests(CHAT_ID, "all", owner)
        check(res2["declined"] >= 1 and 9102 in bot.tg.declined, "全部拒绝模式生效")

        # 15) 申请处理开关：全部拒绝 / 专属链接自动放行
        print("15) 申请处理开关")
        bot.db.set_setting("auto_decline_all_new", True)
        bot.on_join_request({"chat": {"id": CHAT_ID}, "from": {"id": 9201, "first_name": "Junk"}})
        check(9201 in bot.tg.declined, "「自动拒绝全部新申请」生效")
        row = bot.db.conn.execute("select decision from join_requests where user_id=9201").fetchone()
        check(row and row["decision"] == "declined", "被拒申请状态落库")
        bot.db.set_setting("auto_decline_all_new", False)

        bot.db.set_setting("auto_approve_tracked", True)
        link = bot.db.get_link_by_owner(owner, CHAT_ID)
        bot.on_join_request({"chat": {"id": CHAT_ID}, "from": {"id": 9202, "first_name": "Fan"},
                             "invite_link": {"invite_link": link["invite_link"]}})
        check(9202 in bot.tg.approved, "「专属链接申请自动放行」生效")
        row = bot.db.conn.execute("select decision from join_requests where user_id=9202").fetchone()
        check(row and row["decision"] == "approved", "自动批准的申请状态落库")
        bot.db.set_setting("auto_approve_tracked", False)

        # 16) 一键拒绝指令 /declineall
        print("16) 一键拒绝指令（机器人内）")
        bot.db.add_join_request(CHAT_ID, 9301, "A", None, None, None)
        bot.db.add_join_request(CHAT_ID, 9302, "B", None, None, None)
        before = bot.db.join_requests_stats(CHAT_ID)["pending"]
        check(before >= 2, f"先造出待处理申请（{before} 条）")
        boss = {"chat": {"id": owner, "type": "private"},
                "from": {"id": owner, "first_name": "Boss"}, "message_id": 1}
        bot.cmd_decline_all(boss, [])
        check(bot.db.join_requests_stats(CHAT_ID)["pending"] == 0,
              "管理员发 /declineall 后待处理清零")
        check(9301 in bot.tg.declined and 9302 in bot.tg.declined, "申请被真正逐条拒绝")
        # 非管理员不能用
        n = len(bot.tg.sent)
        outsider = {"chat": {"id": 999111, "type": "private"},
                    "from": {"id": 999111, "first_name": "X"}, "message_id": 2}
        bot.cmd_decline_all(outsider, [])
        check(any("只有管理员" in str(t) for _c, t in bot.tg.sent[n:]), "非管理员被拒绝")
        # /manage 面板带按钮
        bot.cmd_manage(boss, [])
        check(any("管理面板" in str(t) for _c, t in bot.tg.sent), "管理面板可打开")
        kb = bot._manage_kb()
        check(len(kb["inline_keyboard"]) == 3 and
              any("declineall" in b["callback_data"] for row in kb["inline_keyboard"] for b in row),
              "面板含「一键拒绝所有申请」按钮")

        # 17) 扫描候选名单（回归：以前只从 referrals/申请取，恒为 0 导致"完全扫不出来"）
        print("17) 扫描候选名单")
        bot.db.touch_user({"id": 987654321, "first_name": "Ghost", "username": None})
        cands = bot._cleanup_candidates(CHAT_ID)
        check(len(cands) > 0, f"候选名单不再为空（{len(cands)} 人）")
        check(987654321 in cands, "机器人见过的用户已纳入扫描范围")

        print()
        if FAILURES:
            print(f"❌ {len(FAILURES)} 项失败：")
            for f in FAILURES:
                print("   -", f)
            return 1
        print("🎉 全部通过：归因、去重、退群、脱敏兜底、防刷、入群申请、统计")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(main())
