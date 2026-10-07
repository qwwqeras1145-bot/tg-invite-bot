#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新功能仿真测试：审计日志 / 留存分析 / 质量分 / 漏斗 / 网页排行榜（含鉴权与签名链接）。

不联网，全部本地跑。
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import invite_bot as ib  # noqa: E402

FAILURES = []
GID = -1009876543210
GID2 = -1009998887776
PASSWORD = "test-pw-123"


def check(cond, label):
    print(("  ✅ " if cond else "  ❌ ") + label)
    if not cond:
        FAILURES.append(label)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def seed(db):
    """造一批可控数据：A 稳定留存，B 拉来的人第 2 天就跑。"""
    db.bind_chat(GID, "测试群")
    for uid, name in ((1, "Alice"), (2, "Bob"), (9, "Me")):
        db.touch_user({"id": uid, "first_name": name}, is_admin=(uid == 9))
    db.add_link("https://t.me/+ALICE", 1, "ref_1", GID)
    now = int(time.time())
    ten_days = now - 10 * 86400

    # Alice 拉了 4 人：3 人留存，1 人第 2 天跑
    for i, uid in enumerate((101, 102, 103, 104)):
        db.add_pending(uid, GID, 1, "https://t.me/+ALICE", f"A{i}")
        db.mark_joined(uid, GID)
        # 把加入时间挪到 10 天前，并补上"当时确实在群"的流水
        db.conn.execute("UPDATE referrals SET joined_at=? WHERE invitee_id=? AND chat_id=?",
                        (ten_days, uid, GID))
        db.conn.execute("UPDATE status_history SET ts=? WHERE invitee_id=? AND chat_id=? AND status='member'",
                        (ten_days, uid, GID))
        if uid == 104:  # 第 2 天退群，没有任何 +1 天之后的在群记录
            db.conn.execute("UPDATE referrals SET status='left', left_at=? WHERE invitee_id=? AND chat_id=?",
                            (ten_days + 2 * 86400, uid, GID))
            db.conn.execute(
                "INSERT INTO status_history(invitee_id,chat_id,status,inviter_id,ts) VALUES(?,?,'left',1,?)",
                (uid, GID, ten_days + 2 * 86400))
        else:  # 第 1、7、14 天都还在
            for d in (1, 7, 14):
                db.conn.execute(
                    "INSERT INTO status_history(invitee_id,chat_id,status,inviter_id,ts) VALUES(?,?,'member',1,?)",
                    (uid, GID, ten_days + d * 86400))
    db.conn.commit()

    # Bob 只拉 1 人，且第 2 天就跑（用来验证质量分会把样本少的拉向 50 分）
    db.add_link("https://t.me/+BOB", 2, "ref_2", GID)
    db.add_pending(201, GID, 2, "https://t.me/+BOB", "B0")
    db.mark_joined(201, GID)
    db.conn.execute("UPDATE referrals SET joined_at=?, status='left', left_at=? WHERE invitee_id=201",
                    (ten_days, ten_days + 2 * 86400))
    db.conn.execute("UPDATE status_history SET ts=? WHERE invitee_id=201 AND status='member'", (ten_days,))
    db.conn.execute("INSERT INTO status_history(invitee_id,chat_id,status,inviter_id,ts) VALUES(201,?,'left',2,?)",
                    (GID, ten_days + 2 * 86400))
    db.conn.commit()

    # 第二个群：完全独立的数据，用来验证多群切换不会串
    db.bind_chat(GID2, "第二群")
    db.touch_user({"id": 30, "first_name": "Zeta", "username": "zeta"})
    db.add_link("https://t.me/+ZETA", 30, "ref_30", GID2)
    db.add_pending(230, GID2, 30, "https://t.me/+ZETA", "Z1")
    db.mark_joined(230, GID2)
    db.bump_activity(GID2, 230)
    db.record_audit(30, "Zeta", "bind_chat", target=str(GID2), chat_id=GID2, detail="第二群")


def test_audit(db):
    print("1) 审计日志")
    base_all = db.audit_count()
    base_bind = db.audit_count("bind_chat")
    db.record_audit(9, "Me", "bind_chat", target=str(GID), chat_id=GID, detail="测试群")
    db.record_audit(9, "Me", "set_welcome", detail="新的欢迎语")
    db.record_audit(None, "web", "web_login_fail", target="1.2.3.4")
    check(db.audit_count() == base_all + 3, "总条数 +3 正确")
    check(db.audit_count("bind_chat") == base_bind + 1, "按 action 过滤计数正确")
    rows = db.audit_page(limit=10)
    check(rows[0]["action"] == "web_login_fail", "按时间倒序")
    check(set(db.audit_actions()) == {"bind_chat", "set_welcome", "web_login_fail"}, "action 去重列表正确")
    check(db.audit_page(limit=10, action="bind_chat")[0]["detail"] == "测试群", "按 action 筛选内容正确")


def test_retention(db):
    print("2) 留存 / 质量分 / 漏斗")
    r1 = db.retention_cohort(GID, 1)
    r7 = db.retention_cohort(GID, 7)
    check(r1["matured"] == 5, f"1 日样本 = 5（加入满 1 天的人）→ 实得 {r1['matured']}")
    check(r1["retained"] == 5, f"1 日留存 = 5（两位都是第 2 天才退群）→ 实得 {r1['retained']}")
    check(abs(r1["rate"] - 1.0) < 1e-9, "1 日留存率 = 100%")
    check(r7["matured"] == 5, f"7 日样本 = 5 → 实得 {r7['matured']}")
    check(r7["retained"] == 3, f"7 日留存 = 3（第 2 天退群的两人不算）→ 实得 {r7['retained']}")
    check(abs(r7["rate"] - 0.6) < 1e-9, "7 日留存率 = 60%")
    check(db.retention_cohort(GID, 30)["matured"] == 0, "30 天样本为 0 时不算留存率")

    scores = db.quality_scores(GID, days=7)
    by_id = {s["inviter_id"]: s for s in scores}
    check(by_id[1]["matured"] == 4 and by_id[1]["retained"] == 3, "Alice 成熟样本 4 / 留存 3")
    check(by_id[2]["matured"] == 1 and by_id[2]["retained"] == 0, "Bob 成熟样本 1 / 留存 0")
    # 贝叶斯平滑：Alice 100*(3+2.5)/(4+5)=61.1，Bob 100*(0+2.5)/(1+5)=41.7
    check(abs(by_id[1]["score"] - 61.1) < 0.15, f"Alice 质量分 ≈61.1 → {by_id[1]['score']}")
    check(abs(by_id[2]["score"] - 41.7) < 0.15, f"Bob 质量分被拉向 50 → {by_id[2]['score']}")
    check(scores[0]["inviter_id"] == 1, "质量分榜首是留存更好的 Alice")

    f = db.funnel(GID)
    check(f["applied"] == 5, f"漏斗-申请人数 = 5 → {f['applied']}")
    check(f["joined"] == 5, f"漏斗-入群人数 = 5 → {f['joined']}")
    check(f["retained_7d"] == 3, f"漏斗-7 日留存 = 3 → {f['retained_7d']}")

    churn = {r["inviter_id"]: r for r in db.churn_board(GID)}
    check(churn[1]["churned"] == 1 and churn[2]["churned"] == 1, "掉人榜统计正确")
    check(db.avg_lifetime_days(GID) and abs(db.avg_lifetime_days(GID) - 2.0) < 0.1, "平均在群时长 ≈2 天")


class Resp:
    def __init__(self, code, body: bytes, headers):
        self.code = code
        self.body = body.decode("utf-8", "replace")
        self.headers = headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """测试需要看到 302 上的 Set-Cookie，所以禁止自动跟随跳转。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


_PANEL = None          # 当前测试用的面板实例（用来给 POST 自动带 CSRF 令牌）


def http(url, data=None, cookie_header=None, host=None) -> Resp:
    # 已登录的 POST 自动补 CSRF 令牌（真实浏览器是表单里的隐藏字段）
    if data is not None and cookie_header and _PANEL is not None:
        try:
            raw = data.decode() if isinstance(data, bytes) else str(data)
            if "csrf=" not in raw:
                tok = _PANEL.csrf_for_cookie(cookie_header)
                data = (raw + "&csrf=" + tok).encode()
        except Exception:
            pass
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if cookie_header:
        req.add_header("Cookie", cookie_header)
    if host:
        req.add_header("Host", host)
    try:
        with _OPENER.open(req, timeout=10) as r:
            return Resp(r.status, r.read(), dict(r.headers))
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.read(), dict(e.headers))


def test_web(tmpdir):
    print("3) 网页排行榜（鉴权 / 签名 / 页面）")
    import webpanel
    db = ib.Storage(os.path.join(tmpdir, "web.db"))
    seed(db)
    port = free_port()
    cfg = dict(ib.DEFAULT_CONFIG)
    cfg.update({"web_port": port, "web_bind_host": "127.0.0.1", "web_password": PASSWORD,
                "web_site_title": "单元测试榜", "web_public_host": "127.0.0.1",
                "web_fallback_port": 0, "web_emergency_path": "/emergency"})
    panel = webpanel.WebPanel(db, cfg, log_fn=lambda *a, **k: None)
    panel.start()
    globals()["_PANEL"] = panel
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if http(base + "/healthz").code == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)

    try:
        check(http(base + "/healthz").body.startswith("ok"), "健康检查可用（无需密码）")

        # 域名白名单：拿 IP / 陌生域名来访问必须被拒（防 IP 扫描）
        hosts = panel._allowed_hosts()
        check("127.0.0.1" in hosts, "白名单包含本机（供 healthz 使用）")
        r = http(base + "/healthz", host="203.0.113.7")
        check(r.code == 421, f"用服务器 IP 当 Host 访问 → 421 拒绝（实得 {r.code}）")
        r = http(base + "/healthz", host="evil.example.com")
        check(r.code == 421, f"用陌生域名当 Host 访问 → 421 拒绝（实得 {r.code}）")
        r = http(base + "/healthz", host="127.0.0.1")
        check(r.code == 200, "白名单内 Host 正常放行")

        r = http(base + "/")
        check(r.code == 200 and "用 Telegram 登录" in r.body, "未登录时显示登录页（Telegram/账号/邮箱）")
        check("127.0.0.1" not in r.body, "登录页不显示服务器地址")

        # 错误密码
        bad = http(base + "/emergency", urllib.parse.urlencode({"password": "wrong"}).encode())
        check("不正确" in bad.body, "错误密码被拒绝")

        # 应急入口隐藏：登录页上没有入口，旧公开路径不再接受口令
        login_page = http(base + "/login?m=pw")
        check("应急" not in login_page.body, "登录页不再显示应急入口")
        old_path = http(base + "/login/emergency",
                        urllib.parse.urlencode({"password": PASSWORD}).encode())
        check(not (old_path.headers.get("Set-Cookie") or "").startswith(webpanel.SESSION_COOKIE + "="),
              "旧公开路径 /login/emergency 已失效（不发会话）")
        check(panel.pw_hash and panel.pw_hash != PASSWORD, "应急口令只存加盐哈希（非明文）")

        # 正确密码 → 拿到会话 cookie
        r = http(base + "/emergency", urllib.parse.urlencode({"password": PASSWORD}).encode())
        cookie = (r.headers.get("Set-Cookie") or "").split(";")[0]
        check(r.code == 302 and cookie.startswith(webpanel.SESSION_COOKIE + "="), "正确密码发放签名会话 Cookie")

        r = http(base + "/", cookie_header=cookie)
        check(r.code == 200 and "累计邀请" in r.body, "登录后进入后台概览")
        check("sidebar" in r.body and "邀请排行榜" in r.body and "审计日志" in r.body,
              "侧边栏分组菜单已渲染")
        check("cdn." not in r.body and "googleapis" not in r.body and "unpkg" not in r.body,
              "页面不依赖任何外部 CDN（隧道环境也能打开）")

        r = http(base + "/leaderboard", cookie_header=cookie)
        check(r.code == 200 and "Alice" in r.body, "排行榜页含成员昵称")
        check("127.0.0.1" not in r.body, "排行榜页面不显示 IP")

        # 多群切换
        r = http(base + f"/leaderboard?g={GID2}", cookie_header=cookie)
        check(r.code == 200 and "Zeta" in r.body and "Alice" not in r.body,
              "?g= 切到第二个群，且不显示第一个群的人")
        r = http(base + f"/?g={GID2}", cookie_header=cookie)
        setck = r.headers.get("Set-Cookie") or ""
        check("dshb_group=" in setck, "切换群后写入 Cookie")
        check(f"g={GID2}" in r.body, "导航/切换链接带上当前群参数")
        ck2 = cookie + "; " + setck.split(";")[0]      # 会话 Cookie + 群 Cookie
        r = http(base + "/", cookie_header=ck2)
        check("Zeta" in r.body, "下次访问默认还是上次选的那个群")
        r = http(base + f"/?g={GID}", cookie_header=cookie)
        check('class="gs"' in r.body, "多群时顶部出现「群切换」下拉框")
        r = http(base + f"/activity?g={GID2}", cookie_header=cookie)
        check(r.code == 200 and "Zeta" not in r.body or "成员" in r.body, "活跃页支持按群切换")
        r = http(base + f"/u/{panel.personal_token(1)}?g={GID2}")
        check(r.code == 200, "个人页支持 ?g= 指定群（不崩）")

        r = http(base + "/logs", cookie_header=cookie)
        check(r.code == 200 and "审计日志" in r.body and "web_login_ok" in r.body,
              "审计日志页可访问且含内容")
        r = http(base + "/logs?export=csv", cookie_header=cookie)
        check(r.code == 200 and "attachment" in (r.headers.get("Content-Disposition") or ""),
              "审计日志可导出 CSV")

        r = http(base + "/quality", cookie_header=cookie)
        check(r.code == 200 and "质量分" in r.body, "质量分页可访问")
        r = http(base + "/retention", cookie_header=cookie)
        check(r.code == 200 and "留存率" in r.body and "入群漏斗" in r.body, "留存页可访问（含漏斗）")
        r = http(base + "/churn", cookie_header=cookie)
        check(r.code == 200 and "掉人榜" in r.body, "掉人榜页可访问")

        # 篡改 cookie 必须失效
        forged = webpanel.SESSION_COOKIE + "=" + str(int(time.time()) + 9999) + "." + "f" * 32
        r = http(base + "/", cookie_header=forged)
        check("用 Telegram 登录" in r.body, "伪造会话 Cookie 被拒绝")

        # 个人签名页
        tok = panel.personal_token(1)
        r = http(base + f"/u/{tok}")
        check(r.code == 200 and "Alice" in r.body, "个人页（签名链接）免密码可访问")
        check("https://t.me/+ALICE" in r.body, "个人页显示专属邀请链接")
        check("127.0.0.1" not in r.body, "个人页不显示 IP")
        r = http(base + "/u/1-deadbeefdeadbeefdeadbeefdeadbeef")
        check(r.code == 403, "篡改签名的个人链接返回 403")
        r = http(base + "/u/999-" + panel.sign(999))
        check(r.code == 200, "未邀请过的人也能打开自己的页面")

        # 二维码（依赖 qrgen，可选）
        try:
            r = http(base + f"/u/{tok}/qr.svg")
            check(r.code == 200 and r.body.lstrip().startswith("<svg"),
                  "个人页二维码可生成（qrgen 可用）")
        except Exception as e:
            check(False, f"二维码接口异常：{e}")

        # 新增页面：活跃 / 申请 / 已注销 / 设置
        r = http(base + "/activity", cookie_header=cookie)
        check(r.code == 200 and "日活跃" in r.body and "发言条数" in r.body and "💬" in r.body,
              "活跃数据页可访问（含表格与发言条数标记）")
        db.bump_activity(GID, 1)
        r = http(base + "/activity", cookie_header=cookie)
        check("📅 活跃" in r.body and "条</span>" in r.body,
              "每行成员格内带「💬N条 / 📅活跃N天」徽章（窄屏也可见）")
        check("overflow-x:auto" in r.body, "表格容器可横向滚动（手机不再裁掉列）")
        r = http(base + "/requests", cookie_header=cookie)
        check(r.code == 200 and "入群申请" in r.body and "已注销" in r.body, "入群申请页可访问（含已注销统计）")
        r = http(base + "/deleted", cookie_header=cookie)
        check(r.code == 200 and "只标记" in r.body and "已禁止" in r.body,
              "已注销页可访问，且默认显示「移除已禁止」")
        r = http(base + "/settings", cookie_header=cookie)
        check(r.code == 200 and "入群欢迎语" in r.body and "保存开关", "设置页可访问")

        # 开关落库 + 审计
        keep_panel = globals().get("_PANEL")
        globals()["_PANEL"] = None          # 模拟"没有 CSRF 令牌"的请求
        r = http(base + "/settings", urllib.parse.urlencode({"welcome_enabled": "on"}).encode(),
                 cookie_header=cookie)
        check(r.code == 403, f"没有 CSRF 令牌的 POST 被拒绝（实得 {r.code}）")
        globals()["_PANEL"] = keep_panel
        before = db.get_setting("welcome_enabled", True)
        r = http(base + "/settings", urllib.parse.urlencode({"welcome_enabled": "on"}).encode(),
                 cookie_header=cookie)
        check(r.code == 302, "保存开关返回跳转")
        db2 = ib.Storage(os.path.join(tmpdir, "web.db"))
        check(db2.get_setting("welcome_enabled", True) in (True, "true", "True"),
              "勾选的开关写入数据库")
        # 不勾 = 关闭
        http(base + "/settings", urllib.parse.urlencode({}).encode(), cookie_header=cookie)
        check(db2.get_setting("welcome_enabled", True) in (False, "false", "False"),
              "取消勾选后开关变为关闭")
        check(db2.audit_count("setting_change") >= 1, "开关改动写入审计日志")

        # 踢人防误操作：没开开关 → 403；开关开了但确认词错 → 400
        r = http(base + "/deleted", urllib.parse.urlencode({"action": "kick", "confirm": "CONFIRM"}).encode(),
                 cookie_header=cookie)
        check(r.code == 403, "未打开「允许踢人」时，移除请求被 403 拒绝")
        db2.set_setting("allow_cleanup_kick", True)
        r = http(base + "/deleted", urllib.parse.urlencode({"action": "kick", "confirm": "no"}).encode(),
                 cookie_header=cookie)
        check(r.code == 400, "确认词不是 CONFIRM 时被 400 拒绝")
        # 扫描接口：本测试没传 bot，应安全地报 503 而不是崩
        r = http(base + "/deleted", urllib.parse.urlencode({"action": "scan"}).encode(),
                 cookie_header=cookie)
        check(r.code == 503, "没有机器人句柄时扫描接口安全返回 503")

        r = http(base + "/settings", cookie_header=cookie)
        check("auto_decline_all_new" in r.body and "auto_approve_tracked" in r.body,
              "设置页有「自动拒绝全部新申请」「专属链接自动放行」开关")
        r = http(base + "/requests", cookie_header=cookie)
        check("sweep_deleted" in r.body and "sweep_all" in r.body,
              "申请页有「批量拒绝已注销」「全部拒绝」按钮")

        # 登录限速（先确保应急入口是开的——上面那次空表单提交把它关掉了）
        db2.set_setting("web_allow_password_login", True)
        for _ in range(12):
            http(base + "/emergency", urllib.parse.urlencode({"password": "nope"}).encode())
        r = http(base + "/emergency", urllib.parse.urlencode({"password": PASSWORD}).encode())
        check("尝试次数过多" in r.body, "连续错误密码触发登录限速")

        # 审计：登录成功/失败都留痕
        check(db.audit_count("web_login_ok") >= 1 and db.audit_count("web_login_fail") >= 5,
              "网页登录成功/失败均写入审计日志")
    finally:
        panel.stop()


def test_unbound(tmpdir):
    """回归测试：还没绑定群组时，所有页面都必须正常响应（曾经这里直接空响应）。"""
    print("4) 未绑定群组时的页面")
    import webpanel
    db = ib.Storage(os.path.join(tmpdir, "unbound.db"))   # 故意不 bind_chat
    port = free_port()
    cfg = dict(ib.DEFAULT_CONFIG)
    cfg.update({"web_port": port, "web_bind_host": "127.0.0.1", "web_password": PASSWORD,
                "web_public_host": "127.0.0.1", "web_fallback_port": 0,
                "web_emergency_path": "/emergency",
                "db_path": os.path.join(tmpdir, "web.db")})
    panel = webpanel.WebPanel(db, cfg, log_fn=lambda *a, **k: None)
    panel.start()
    globals()["_PANEL"] = panel
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if http(base + "/healthz").code == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    try:
        r = http(base + "/emergency", urllib.parse.urlencode({"password": PASSWORD}).encode())
        cookie = (r.headers.get("Set-Cookie") or "").split(";")[0]
        for path in ("/", "/leaderboard", "/activity", "/quality", "/retention", "/churn",
                     "/requests", "/deleted", "/logs", "/settings"):
            r = http(base + path, cookie_header=cookie)
            ok = r.code == 200 and len(r.body) > 500
            check(ok, f"未绑定群组时 {path} 正常响应（HTTP {r.code}，{len(r.body)}B）")
        tok = panel.personal_token(999)
        r = http(base + f"/u/{tok}")
        check(r.code == 200 and len(r.body) > 300, f"未绑定群组时个人页也正常（HTTP {r.code}）")
    finally:
        panel.stop()


class StubBot:
    """网页测试用的机器人替身：只实现网页会调用到的几个方法。"""

    def __init__(self, db):
        self.db = db
        self.tasks = []
        self._avatar = None

    def set_avatar(self, data):
        self._avatar = data

    def get_avatar_bytes(self, uid):
        return self._avatar

    def submit(self, fn, *args, **kwargs):
        self.tasks.append(getattr(fn, "__name__", str(fn)))
        return True

    def approve_link_request(self, req_id, actor=0):
        req = self.db.conn.execute("SELECT * FROM link_requests WHERE id=?", (req_id,)).fetchone()
        if not req or req["status"] != "pending":
            return False, "不存在或已处理"
        self.db.add_link(f"https://t.me/+APPROVED{req['user_id']}", req["user_id"],
                         f"ref_{req['user_id']}", req["chat_id"])
        self.db.decide_link_request(req_id, "approved", actor, "管理员批准")
        return True, "ok"

    def reject_link_request(self, req_id, actor=0, note=""):
        return self.db.decide_link_request(req_id, "rejected", actor, note or "管理员驳回")


def test_accounts(tmpdir):
    """账号体系：注册 / 用户名密码登录 / 分权 / Telegram 登录。"""
    print("5) 账号体系（每人独立账号）")
    import webpanel
    db = ib.Storage(os.path.join(tmpdir, "acc.db"))
    seed(db)
    port = free_port()
    cfg = dict(ib.DEFAULT_CONFIG)
    cfg.update({"web_port": port, "web_bind_host": "127.0.0.1", "web_password": PASSWORD,
                "web_public_host": "127.0.0.1", "web_fallback_port": 0,
                "web_emergency_path": "/emergency", "reg_mode": "invite",
                "db_path": os.path.join(tmpdir, "acc.db")})
    panel = webpanel.WebPanel(db, cfg, log_fn=lambda *a, **k: None, bot=StubBot(db))
    panel.start()
    globals()["_PANEL"] = panel
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if http(base + "/healthz").code == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    try:
        r = http(base + "/register")
        check(r.code == 200 and "注册账号" in r.body, "注册页可打开")

        r = http(base + "/register", urllib.parse.urlencode(
            {"username": "boss", "email": "boss@example.com", "password": "super-secret-1"}).encode())
        ck = (r.headers.get("Set-Cookie") or "").split(";")[0]
        check(r.code == 302 and "dshb_sess=" in ck, "首个注册账号自动成为超管并登录")
        acc = db.get_account_by_username("boss")
        check(acc is not None and acc["role"] == "owner", "首个账号角色 = owner")
        u = panel._resolve_user(ck)
        check(bool(u) and u["role"] == "owner", "会话解析出 owner 身份")
        r = http(base + "/admin/accounts", cookie_header=ck)
        check(r.code == 200 and "账号管理" in r.body and "邀请码" in r.body, "超管能进账号管理页")
        check("超级管理员" in r.body and "角色与权限对照" in r.body,
              "账号页用中文显示「超级管理员」并给出权限对照表")
        # owner 拥有全部权限：所有受限页面都能进
        for path in ("/logs", "/deleted", "/settings", "/requests", "/linkreq", "/admin/accounts"):
            r = http(base + path, cookie_header=ck)
            check(r.code == 200, f"超级管理员访问 {path} 正常（实得 {r.code}）")
        # 回归：生成邀请码（曾经因为跳转地址含中文导致 500）
        r = http(base + "/admin/accounts", urllib.parse.urlencode(
            {"action": "newcode", "note": "测试备注"}).encode(), cookie_header=ck)
        check(r.code == 302, f"生成邀请码不再报错（实得 {r.code}）")
        check("Location" in r.headers, "生成后有正确的跳转头")
        check(any(c["note"] == "测试备注" for c in db.invite_codes(5)), "邀请码已落库")
        r = http(base + "/settings", cookie_header=ck)
        check(r.code == 200, "超管能进设置页")

        r = http(base + "/login", urllib.parse.urlencode({"ident": "boss", "password": "wrong"}).encode())
        check("不正确" in r.body, "密码错误被拒绝")
        r = http(base + "/login", urllib.parse.urlencode(
            {"ident": "boss", "password": "super-secret-1"}).encode())
        ck_boss = (r.headers.get("Set-Cookie") or "").split(";")[0]
        check(r.code == 302 and "dshb_sess=" in ck_boss, "用户名+密码登录成功")
        r = http(base + "/login", urllib.parse.urlencode(
            {"ident": "boss@example.com", "password": "super-secret-1"}).encode())
        check(r.code == 302, "邮箱+密码登录成功")

        r = http(base + "/register", urllib.parse.urlencode(
            {"username": "nobody", "password": "another-secret-1"}).encode())
        check("邀请码" in r.body, "邀请码模式下无码不能注册")
        code = db.create_invite_code("测试", None)
        r = http(base + "/register", urllib.parse.urlencode(
            {"username": "member1", "password": "another-secret-1", "code": code}).encode())
        ck_m = (r.headers.get("Set-Cookie") or "").split(";")[0]
        check(r.code == 302, "带邀请码可以注册")
        m = db.get_account_by_username("member1")
        check(m is not None and m["role"] == "member", "普通账号角色 = member")

        r = http(base + "/", cookie_header=ck_m)
        check(r.code == 200 and "我的" in r.body, "成员访问首页 → 进个人中心")
        r = http(base + "/me", cookie_header=ck_m)
        check(r.code == 200 and "账号" in r.body, "成员能看「我的」")
        for path in ("/settings", "/logs", "/deleted", "/admin/accounts", "/leaderboard"):
            r = http(base + path, cookie_header=ck_m)
            check(r.code == 403, f"成员访问 {path} 被拒（403，实得 {r.code}）")

        db.set_account_status(m["id"], "disabled")
        r = http(base + "/me", cookie_header=ck_m)
        check("用 Telegram 登录" in r.body, "账号被禁用后旧会话立即失效")
        db.set_account_status(m["id"], "active")

        db.create_login_nonce("NONCE123")
        r = http(base + "/login/poll?n=NONCE123")
        check("false" in r.body, "未确认时轮询返回未完成")
        db.confirm_login_nonce("NONCE123", 424242, "TgUser", "tguser")
        r = http(base + "/login/poll?n=NONCE123")
        ck_tg = (r.headers.get("Set-Cookie") or "").split(";")[0]
        check('"ok":true' in r.body and "dshb_sess=" in ck_tg, "确认后轮询拿到会话")
        check(db.get_account_by_tg(424242) is not None, "Telegram 首次登录自动建号")
        r = http(base + "/me", cookie_header=ck_tg)
        check(r.code == 200, "Telegram 会话可用")

        r = http(base + "/me/pw", urllib.parse.urlencode(
            {"old": "super-secret-1", "new": "brand-new-secret-9"}).encode(), cookie_header=ck_boss)
        check(r.code == 302, "改密码成功")
        r = http(base + "/admin/accounts", cookie_header=ck_boss)
        check("用 Telegram 登录" in r.body, "改密码后旧会话失效")
        r = http(base + "/login", urllib.parse.urlencode(
            {"ident": "boss", "password": "brand-new-secret-9"}).encode())
        check(r.code == 302, "新密码可以登录")
        ck_admin = (r.headers.get("Set-Cookie") or "").split(";")[0]   # 改密码后重新登录的管理员会话

        db.set_setting("reg_mode", "closed")
        r = http(base + "/register")
        check("注册已关闭" in r.body, "reg_mode=closed 时关闭注册")
        db.set_setting("reg_mode", "invite")

        # --- 新权限模型：成员只能看数据，审批类没有入口；链接改为申请制 ---
        m = db.get_account_by_username("member1")
        db.set_account_status(m["id"], "active")
        r = http(base + "/login", urllib.parse.urlencode(
            {"ident": "member1", "password": "another-secret-1"}).encode())
        ck_m2 = (r.headers.get("Set-Cookie") or "").split(";")[0]
        for path in ("/requests", "/linkreq", "/settings", "/deleted", "/logs", "/admin/accounts"):
            r = http(base + path, cookie_header=ck_m2)
            check(r.code == 403, f"普通成员访问 {path} → 403（实得 {r.code}）")

        db.link_telegram(m["id"], 555001, "MemberOne")
        db.set_group_admin(GID, 555001, "member")     # 只是普通群成员，不是群管理员
        u = panel._resolve_user(ck_m2)
        check(bool(u) and GID in u["groups"], "登录后能读到「和机器人同在的群」（普通成员身份）")
        check(bool(u) and u["role"] == "member", "身份仍是普通成员（不是管理员）")
        r = http(base + f"/activity?g={GID}", cookie_header=ck_m2)
        check(r.code == 200, "普通成员可以看活跃数据")
        r = http(base + f"/leaderboard?g={GID}", cookie_header=ck_m2)
        check(r.code == 200, "普通成员可以看排行榜")

        r = http(base + "/me", cookie_header=ck_m2)
        check("申请专属邀请链接" in r.body, "成员在「我的」看到的是「申请」按钮，不是生成")
        r = http(base + "/me/linkreq", urllib.parse.urlencode({"g": GID}).encode(), cookie_header=ck_m2)
        check(r.code == 302, "提交链接申请")
        pend = db.pending_link_requests()
        check(len(pend) == 1 and pend[0]["user_id"] == 555001, "申请进入待审批队列")

        r = http(base + "/linkreq", cookie_header=ck_admin)
        check(r.code == 200 and "链接申请" in r.body, "管理员能看到链接申请页")
        rid = pend[0]["id"]
        r = http(base + "/linkreq", urllib.parse.urlencode({"id": rid, "action": "approve"}).encode(),
                 cookie_header=ck_admin)
        check(r.code == 302, "管理员批准申请")
        check(db.pending_link_requests() == [], "批准后队列清空")
        req = db.conn.execute("SELECT * FROM link_requests WHERE id=?", (rid,)).fetchone()
        check(req["status"] == "approved" and req["handled_by"] is not None,
              "申请状态=approved 且记录了处理人")
        r = http(base + "/me", cookie_header=ck_m2)
        check("https://t.me" in r.body, "批准后成员能看到自己的链接")

        # --- 新功能：邀请码有效期 / 作废；账号搜索 ---
        c1 = db.create_invite_code("短期", None, ttl_days=1)
        check(db.use_invite_code(c1, 9901) is True, "有效期内的邀请码可用")
        c2 = db.create_invite_code("过期", None, ttl_days=1)
        db.conn.execute("UPDATE invite_codes SET expires_at=? WHERE code=?",
                        (int(time.time()) - 60, c2))
        db.conn.commit()
        check(db.use_invite_code(c2, 9902) is False, "过期邀请码不可用")
        c3 = db.create_invite_code("作废", None, ttl_days=7)
        check(db.revoke_invite_code(c3) is True and db.use_invite_code(c3, 9903) is False,
              "作废的邀请码立即失效")
        r = http(base + "/admin/accounts?q=boss", cookie_header=ck_admin)
        check(r.code == 200 and "boss" in r.body, "账号搜索可用")
        r = http(base + "/admin/accounts", cookie_header=ck_admin)
        check("作废" in r.body and "到期" in r.body, "邀请码列表显示状态/到期/作废按钮")

        # --- 两步验证（TOTP）：RFC 6238 向量 + 登录拦截 ---
        import invite_bot as _ib
        import base64 as _b64
        _sec = _b64.b32encode(b"12345678901234567890").decode().rstrip("=")
        check(_ib.totp_code(_sec, at=59, digits=8) == "94287082", "TOTP 符合 RFC 6238 向量 (T=59)")
        check(_ib.totp_code(_sec, at=20000000000, digits=8) == "65353130", "TOTP 符合 RFC 6238 向量 (T=2e10)")

        r = http(base + "/me/2fa", cookie_header=ck_admin)
        check(r.code == 200 and "开启两步验证" in r.body, "两步验证页可打开并给出二维码/密钥")
        pending = db.get_kv(f"totp_pending:{db.get_account_by_username('boss')['id']}") or ""
        check(len(pending) >= 16, "服务端生成了待确认密钥")
        code = _ib.totp_code(pending)
        r = http(base + "/me/2fa/on", urllib.parse.urlencode({"code": code}).encode(),
                 cookie_header=ck_admin)
        check(r.code == 200 and "已开启" in r.body and "恢复码" in r.body, "输对动态码即开启并给出恢复码")
        acc_boss2 = db.get_account_by_username("boss")
        check(db.get_totp(acc_boss2["id"])["totp_enabled"] == 1, "数据库标记为已开启两步验证")
        backups = json.loads(db.get_totp(acc_boss2["id"])["totp_backup"] or "[]")
        check(len(backups) == 8, "生成了 8 个恢复码（只存哈希）")
        check(not any(len(b) < 20 for b in backups), "恢复码是哈希存储，不存明文")

        # 密码登录被两步验证拦住
        r = http(base + "/login", urllib.parse.urlencode(
            {"ident": "boss", "password": "brand-new-secret-9"}).encode())
        check(r.code == 302 and "2fa" in (r.headers.get("Location") or "").lower(),
              f"开启后密码登录被拦到两步验证页（实得 {r.code} {r.headers.get('Location')}）")
        half = (r.headers.get("Set-Cookie") or "").split(";")[0]
        check("dshb_2fa=" in half, "下发了临时验证凭证")
        r = http(base + "/login/2fa", urllib.parse.urlencode({"code": "000000"}).encode(),
                 cookie_header=half)
        check("不正确" in r.body, "错误动态码被拒绝")
        r = http(base + "/login/2fa", urllib.parse.urlencode(
            {"code": _ib.totp_code(db.get_totp(acc_boss2["id"])["totp_secret"])}).encode(),
            cookie_header=half)
        ck_2fa = (r.headers.get("Set-Cookie") or "").split(";")[0]
        check(r.code == 302 and "dshb_sess=" in ck_2fa, "正确动态码换到正式会话")
        r = http(base + "/admin/accounts", cookie_header=ck_2fa)
        check(r.code == 200, "两步验证登录后的会话可用")

        # 恢复码也能登录（一次性）
        first_backup = None
        for c in _ib.new_backup_codes(8):
            first_backup = c
            break
        db.set_totp(acc_boss2["id"], db.get_totp(acc_boss2["id"])["totp_secret"], True,
                    json.dumps([_ib.hash_backup_code(first_backup)]))
        r = http(base + "/login", urllib.parse.urlencode(
            {"ident": "boss", "password": "brand-new-secret-9"}).encode())
        half2 = (r.headers.get("Set-Cookie") or "").split(";")[0]
        r = http(base + "/login/2fa", urllib.parse.urlencode({"code": first_backup}).encode(),
                 cookie_header=half2)
        check(r.code == 302 and "dshb_sess=" in (r.headers.get("Set-Cookie") or ""), "恢复码可以登录")
        r = http(base + "/login/2fa", urllib.parse.urlencode({"code": first_backup}).encode(),
                 cookie_header=half2)
        check("不正确" in r.body, "同一个恢复码不能重复使用")

        # 关闭两步验证（要密码）
        r = http(base + "/me/2fa/off", urllib.parse.urlencode(
            {"password": "brand-new-secret-9"}).encode(), cookie_header=ck_2fa)
        check(r.code == 302 and db.get_totp(acc_boss2["id"])["totp_enabled"] == 0,
              "输密码可关闭两步验证")

        # --- 页脚稳定运行时长 ---
        r = http(base + "/", cookie_header=ck_admin)
        check("uptime" in r.body and "已稳定运行" in r.body, "页脚显示「稳定运行天数/时分秒」")

        # --- 有实权的群管理员 vs 空壳管理员 ---
        acc_g = db.create_account(username="gadmin", password="group-admin-pass1")
        db.link_telegram(acc_g, 666001, "GAdmin")
        db.set_group_admin(GID, 666001, "administrator",
                           "can_invite_users,can_delete_messages,can_restrict_members")
        r = http(base + "/login", urllib.parse.urlencode(
            {"ident": "gadmin", "password": "group-admin-pass1"}).encode())
        ck_g = (r.headers.get("Set-Cookie") or "").split(";")[0]
        u = panel._resolve_user(ck_g)
        check(bool(u) and GID in u["admin_groups"], "有实权的群管理员被识别为「管理」")
        r = http(base + "/linkreq", cookie_header=ck_g)
        check(r.code == 200, "有实权的群管理员能进审批页")

        db.set_group_admin(GID, 666001, "administrator", "")   # 空壳管理员：没有任何权限
        u = panel._resolve_user(ck_g)
        check(bool(u) and not u["admin_groups"], "空壳管理员（无权限）不算「管理」")
        r = http(base + "/linkreq", cookie_header=ck_g)
        check(r.code == 403, "空壳管理员进不了审批页（403）")
        r = http(base + f"/requests", cookie_header=ck_g)
        check(r.code == 403, "空壳管理员进不了入群申请页（403）")

        # --- 申请列表分页 / 真头像 / CSV 注入防护 ---
        for i in range(35):
            db.add_join_request(GID, 700000 + i, f"PageUser{i}", None, None, None)
        r = http(base + "/requests", cookie_header=ck_admin)
        check(r.code == 200 and "第 1 / 2 页" in r.body, "申请列表第一页（35 条 → 2 页）")
        r2 = http(base + "/requests?page=1", cookie_header=ck_admin)
        check(r2.code == 200 and "第 2 / 2 页" in r2.body, "申请列表第二页可翻")
        check("PageUser34" in r2.body and "PageUser0" not in r2.body, "第二页显示的是后 5 条")
        r3 = http(base + "/requests?d=pending&page=1", cookie_header=ck_admin)
        check(r3.code == 200, "按状态筛选 + 翻页组合正常")

        for i in range(35):
            db.mark_deleted(GID, 800000 + i, "test")
        r = http(base + "/deleted", cookie_header=ck_admin)
        check(r.code == 200 and "第 1 / 2 页" in r.body, "已注销列表第一页")
        r = http(base + "/deleted?page=1", cookie_header=ck_admin)
        check(r.code == 200 and "第 2 / 2 页" in r.body, "已注销列表第二页可翻")

        stub = globals().get("_PANEL").bot
        r = http(base + "/avatar/700001")
        check(r.code == 404, "没有头像的用户返回 404（前端退回字母头像）")
        stub.set_avatar(b"\xff\xd8\xff\xe0FAKEJPEG")
        r = http(base + "/avatar/700001")
        check(r.code == 200 and r.headers.get("Content-Type") == "image/jpeg",
              "有头像时返回 JPEG 200")
        r = http(base + "/avatar/700001")
        check(r.code == 200, "第二次命中磁盘缓存")
        r = http(base + "/requests", cookie_header=ck_admin)
        check('src="/avatar/' in r.body, "申请列表行内嵌入了真头像 <img>")
        stub.set_avatar(None)

        import webpanel as _wp
        check(_wp.WebPanel._csv_safe("=1+1") == "'=1+1" and _wp.WebPanel._csv_safe("@x") == "'@x"
              and _wp.WebPanel._csv_safe("正常") == "正常", "CSV 公式注入被中和")

        # --- 申请页默认页签 = 待处理（与 Telegram 客户端一致）---
        r = http(base + "/requests", cookie_header=ck_admin)
        check("当前没有待处理申请" not in r.body and "PageUser" in r.body,
              "有待处理时默认显示待处理列表")
        db.conn.execute("UPDATE join_requests SET decision='stale' WHERE chat_id=? AND decision='pending'",
                        (GID,))
        db.conn.commit()
        r = http(base + "/requests", cookie_header=ck_admin)
        check("当前没有待处理申请" in r.body, "没有待处理时显示空状态（不再显示历史当申请）")
        r = http(base + "/requests?d=all", cookie_header=ck_admin)
        check("PageUser" in r.body, "「全部记录」页签仍能看到历史留档")
    finally:
        panel.stop()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    tmpdir = tempfile.mkdtemp(prefix="botweb-")
    try:
        db = ib.Storage(os.path.join(tmpdir, "t.db"))
        seed(db)
        test_audit(db)
        test_retention(db)
        test_web(tmpdir)
        test_unbound(tmpdir)
        test_accounts(tmpdir)
        print()
        if FAILURES:
            print(f"❌ {len(FAILURES)} 项失败：")
            for f in FAILURES:
                print("   -", f)
            return 1
        print("🎉 全部通过：审计日志 / 留存 / 质量分 / 漏斗 / 网页鉴权 / 签名页 / 二维码 / 限速")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
