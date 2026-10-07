#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给机器人设置头像 / 名称 / 简介 / 命令菜单。

依据 Bot API 10.3：
  * setMyProfilePhoto(photo)  —— photo 为 InputProfilePhoto，静态图用
    {"type":"static","photo":"attach://<字段名>"}，图片必须重新上传（不可复用 file_id）。
  * setMyName / setMyDescription / setMyShortDescription / setMyCommands

用法：
    python3 setup_bot_profile.py [config.json] [avatar.jpg]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import uuid

try:  # Windows 控制台默认 GBK，避免打印 emoji 时崩掉
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

COMMANDS = [
    ("start", "开始使用 / 主菜单"),
    ("link", "领取我的专属邀请链接"),
    ("stats", "查看我的邀请战绩"),
    ("list", "查看我邀请进群的人"),
    ("top", "群邀请排行榜"),
    ("web", "我的网页战绩页（含二维码）"),
    ("switch", "切换当前群"),
    ("manage", "管理面板（管理员）"),
    ("jm", "禁漫下载（/jm 帮助）"),
    ("help", "使用帮助"),
]

GROUP_COMMANDS = [
    ("link", "领取我的专属邀请链接"),
    ("top", "群邀请排行榜"),
    ("stats", "我的邀请战绩"),
    ("active", "群活跃数据"),
    ("manage", "管理面板（管理员）"),
    ("declineall", "一键拒绝所有待处理申请（管理员）"),
    ("jm", "禁漫下载（/jm 帮助）"),
    ("groups", "查看所有已绑定的群（管理员）"),
    ("bind", "绑定本群（管理员）"),
    ("panel", "发布领取链接面板（管理员）"),
    ("cleanup", "扫描已注销账号（管理员）"),
    ("logs", "审计日志（管理员）"),
    ("settings", "功能开关（管理员）"),
]


def call(token: str, method: str, params: dict, files: dict | None = None):
    boundary = "----prof" + uuid.uuid4().hex
    if files:
        body = bytearray()
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            body += str(v).encode("utf-8") + b"\r\n"
        for k, (fn, content, ctype) in files.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                     f'filename="{fn}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
            body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        data = bytes(body)
        ctype = f"multipart/form-data; boundary={boundary}"
    else:
        data = json.dumps(params, ensure_ascii=False).encode()
        ctype = "application/json"
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=data, method="POST", headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=90) as r:
        payload = json.loads(r.read().decode("utf-8", "replace"))
    if not payload.get("ok"):
        raise RuntimeError(f"{method} 失败：{payload.get('description')}")
    return payload.get("result")


def main() -> int:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "config.json")
    avatar = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "bot_avatar.jpg")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    token = cfg["bot_token"]
    me = call(token, "getMe", {})
    print("机器人：", me.get("first_name"), "@" + (me.get("username") or ""))

    if os.path.exists(avatar):
        with open(avatar, "rb") as f:
            img = f.read()
        call(token, "setMyProfilePhoto", {"photo": {"type": "static", "photo": "attach://avatar"}},
             files={"avatar": ("avatar.jpg", img, "image/jpeg")})
        print("✅ 头像已更新")
    else:
        print("⚠️  未找到头像文件，跳过：", avatar)

    call(token, "setMyName", {"name": "xiyuer_bot"})
    call(token, "setMyShortDescription", {
        "short_description": "给我一条专属邀请链接，帮你统计你邀请了多少人进群。"})
    call(token, "setMyDescription", {"description": (
        "我是群邀请统计机器人。\n\n"
        "• /link 领取你的专属群邀请链接\n"
        "• /stats 查看你邀请了多少人\n"
        "• /top 查看群邀请排行榜\n\n"
        "把机器人设为群管理员后，在群里发 /bind 即可启用。")})
    call(token, "setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
    call(token, "setMyCommands", {
        "commands": [{"command": c, "description": d} for c, d in GROUP_COMMANDS],
        "scope": {"type": "all_group_chats"}})
    print("✅ 名称 / 简介 / 命令菜单已更新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
