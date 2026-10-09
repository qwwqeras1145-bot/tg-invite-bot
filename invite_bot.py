#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xiyuer114514_bot
================
Telegram「群专属邀请链接 + 邀请人数统计」机器人。

核心原理（依据 Telegram Bot API 10.3 官方文档）：
  * Bot API **没有** getChatInviteImporters 这类"查询某链接拉了多少人"的接口
    （该方法是 MTProto 客户端方法，机器人不可用）。
  * 唯一可靠的归因方式是：机器人给每个人 createChatInviteLink 生成一条
    独立邀请链接（name 里带 owner 的 user_id），然后监听 chat_member 更新。
    ChatMemberUpdated.invite_link 字段会告诉你"这个人是用哪条链接进来的"，
    只对"通过邀请链接加入"的事件出现；chat_join_request.invite_link 同理
    （用于需要管理员审批入群的情况）。
  * 前提条件：机器人必须是群管理员，且 getUpdates 的 allowed_updates 里
    显式包含 "chat_member" / "chat_join_request"（默认不推送这两种更新）。

纯标准库实现，无第三方依赖。
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import html
import io
import json
import os
import re
import signal
import sqlite3
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

VERSION = "1.6.0"
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BOT_DIR, "config.json")

ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "chat_member",
    "chat_join_request",
    "my_chat_member",
]

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
_LOG_LOCK = threading.Lock()


def log(msg: str, level: str = "INFO") -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    with _LOG_LOCK:
        print(line, flush=True)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "bot_token": "",
    "api_base": "https://api.telegram.org",
    "db_path": os.path.join(BOT_DIR, "data", "bot.db"),
    "owner_ids": [],
    "poll_timeout": 50,
    "link_name_prefix": "ref_",
    "recycle_batch": 25,
    "notify_inviter_on_join": True,
    "welcome_enabled": True,
    "welcome_text": (
        "👋 欢迎 {invitee} 加入 {chat}！\n"
        "由 {inviter} 邀请，这是 TA 邀请的第 <b>{count}</b> 位成员。"
    ),
    "panel_text": (
        "📣 <b>群邀请排行榜</b>\n\n"
        "点击下面的按钮，领取你自己的<b>专属邀请链接</b>，\n"
        "把链接分享给好友，机器人会自动统计你邀请了多少人。"
    ),
    "share_text": "我发现了「{chat}」这个群，快来看看！",
    "log_chat_id": 0,
    # 质量分（贝叶斯平滑留存率）
    "quality_prior_weight": 5.0,
    "quality_prior_rate": 0.5,
    # 网页排行榜
    "web_enabled": True,
    "web_bind_host": "0.0.0.0",
    "web_port": 8080,
    "web_fallback_port": 18080,
    "web_fallback_host": "127.0.0.1",
    "web_allowed_hosts": [],
    # 账号体系
    "web_allow_password_login": True,
    "reg_mode": "invite",
    "group_admin_min_rights": "can_invite_users",
    "jm_enabled": True,
    "smtp_host": "",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",
    "smtp_tls": True,
    "web_password": "",
    "web_site_title": "邀请排行榜",
    "web_public_leaderboard": False,
    "web_public_host": "",
    "web_base_url": "",
    "web_use_tunnel_url": True,
    "web_tunnel_log": "/var/log/invite-bot-tunnel.log",
    "web_tls_cert": "",
    "web_tls_key": "",
    # 运行时开关（可在网页设置页修改，DB 值优先）
    "activity_enabled": True,
    "log_leave_enabled": True,
    "auto_decline_deleted": True,
    "auto_decline_all_new": False,
    "auto_approve_tracked": False,
    "auto_sync_requests": True,
    "auto_bind_enabled": True,
    "cleanup_notify_admin": True,
    "allow_cleanup_kick": False,
    "welcome_on_join_request": False,
    "milestone_notify_enabled": True,
    "auto_backup_enabled": True,
    "weekly_report_enabled": True,
}


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["bot_token"] = os.environ.get("BOT_TOKEN") or cfg.get("bot_token") or ""
    if os.environ.get("BOT_DB_PATH"):
        cfg["db_path"] = os.environ["BOT_DB_PATH"]
    return cfg


# ---------------------------------------------------------------------------
# Telegram API 客户端
# ---------------------------------------------------------------------------
class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: int = 0):
        super().__init__(f"{method}: [{code}] {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after

    @property
    def is_conflict(self) -> bool:
        return self.code == 409

    def has(self, *needles: str) -> bool:
        d = self.description.lower()
        return any(n.lower() in d for n in needles)


class Telegram:
    """极简 Bot API 客户端：JSON / multipart 上传、429 退避、网络重试。"""

    def __init__(self, token: str, api_base: str = "https://api.telegram.org"):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self._me: dict | None = None
        self._lock = threading.Lock()
        self._last_call = 0.0

    # -- 基础 --------------------------------------------------------------
    def url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    @staticmethod
    def _encode_multipart(fields: dict, files: dict):
        boundary = "----dsh" + uuid.uuid4().hex
        buf = io.BytesIO()
        for k, v in fields.items():
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
            buf.write(str(v).encode("utf-8"))
            buf.write(b"\r\n")
        for k, (filename, content, ctype) in files.items():
            buf.write(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
            )
            buf.write(content)
            buf.write(b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        return buf.getvalue(), f"multipart/form-data; boundary={boundary}"

    def call(self, method: str, params: dict | None = None, files: dict | None = None,
             timeout: int = 60, retries: int = 3):
        params = dict(params or {})
        attempt = 0
        while True:
            attempt += 1
            if files:
                body, ctype = self._encode_multipart(params, files)
            else:
                body = json.dumps(params, ensure_ascii=False).encode("utf-8")
                ctype = "application/json"
            req = urllib.request.Request(self.url(method), data=body, method="POST",
                                         headers={"Content-Type": ctype})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
                if payload.get("ok"):
                    return payload.get("result")
                code = payload.get("error_code", 0)
                desc = payload.get("description", "unknown error")
                retry_after = int(payload.get("parameters", {}).get("retry_after", 0) or 0)
                if retry_after and attempt <= retries:
                    log(f"{method} 触发限流，等待 {retry_after}s", "WARN")
                    time.sleep(retry_after + 1)
                    continue
                raise TelegramError(method, code, desc, retry_after)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
                code = payload.get("error_code", e.code)
                desc = payload.get("description", raw[:300])
                retry_after = int(payload.get("parameters", {}).get("retry_after", 0) or 0)
                if (retry_after or e.code in (429, 500, 502, 503, 504)) and attempt <= retries:
                    wait = retry_after or min(2 ** attempt, 15)
                    log(f"{method} HTTP {e.code}，{wait}s 后重试（{attempt}/{retries}）", "WARN")
                    time.sleep(wait)
                    continue
                raise TelegramError(method, code, desc, retry_after)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt <= retries:
                    wait = min(2 ** attempt, 15)
                    log(f"{method} 网络异常 {e}，{wait}s 后重试（{attempt}/{retries}）", "WARN")
                    time.sleep(wait)
                    continue
                raise

    # -- 便捷方法 ----------------------------------------------------------
    def get_me(self, use_cache: bool = True) -> dict:
        if use_cache and self._me:
            return self._me
        self._me = self.call("getMe")
        return self._me

    def get_updates(self, offset: int, timeout: int = 50) -> list:
        res = self.call("getUpdates", {
            "offset": offset,
            "timeout": timeout,
            "limit": 100,
            "allowed_updates": ALLOWED_UPDATES,
        }, timeout=timeout + 25)
        return res or []

    def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None,
                     disable_notification=False, reply_to_message_id=None, link_preview=True):
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
            "link_preview_options": {"is_disabled": not link_preview},
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        if reply_to_message_id:
            params["reply_to_message_id"] = reply_to_message_id
            params["allow_sending_without_reply"] = True
        return self.call("sendMessage", params)

    def edit_message_text(self, chat_id, message_id, text, parse_mode="HTML", reply_markup=None):
        return self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": parse_mode, "reply_markup": reply_markup,
        })

    def answer_callback(self, callback_query_id, text=None, show_alert=False):
        return self.call("answerCallbackQuery", {
            "callback_query_id": callback_query_id, "text": text,
            "show_alert": show_alert,
        })

    def send_document(self, chat_id, filename, content: bytes, caption=None):
        return self.call("sendDocument", {"chat_id": chat_id, "caption": caption},
                         files={"document": (filename, content, "text/csv")})

    def get_chat_member(self, chat_id, user_id):
        try:
            return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id}, retries=1)
        except TelegramError as e:
            if e.has("user not found", "participant", "chat not found"):
                return None
            raise


# ---------------------------------------------------------------------------
# 存储层（SQLite，所有写操作都在主线程）
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    bound_at  INTEGER NOT NULL,
    seq       INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS links (
    invite_link TEXT PRIMARY KEY,
    owner_id    INTEGER NOT NULL,
    name        TEXT,
    chat_id     INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    uses        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_links_owner ON links(owner_id, revoked);
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_name ON links(name);
CREATE TABLE IF NOT EXISTS referrals (
    invitee_id  INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    inviter_id  INTEGER NOT NULL,
    invite_link TEXT,
    joined_at   INTEGER,
    left_at     INTEGER,
    status      TEXT NOT NULL DEFAULT 'pending',
    invitee_name TEXT,
    PRIMARY KEY (invitee_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_ref_inviter ON referrals(inviter_id, chat_id, status);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    chat_id  INTEGER,
    user_id  INTEGER,
    detail   TEXT
);
-- 成员状态流水：用于精确计算「加入满 N 天后是否还在群」的留存
CREATE TABLE IF NOT EXISTS status_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invitee_id INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    status     TEXT NOT NULL,
    inviter_id INTEGER,
    ts         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sh_member ON status_history(invitee_id, chat_id, status, ts);
CREATE INDEX IF NOT EXISTS idx_sh_ts ON status_history(ts);
-- 审计日志：所有敏感操作留痕
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    actor_id   INTEGER,
    actor_name TEXT,
    action     TEXT NOT NULL,
    target     TEXT,
    chat_id    INTEGER,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action);
-- 活跃度统计：只记数量和时间，不存消息内容
CREATE TABLE IF NOT EXISTS activity (
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    messages    INTEGER NOT NULL DEFAULT 0,
    first_ts    INTEGER,
    last_ts     INTEGER,
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_activity_msg ON activity(chat_id, messages DESC);
-- 活跃天数：每人每天一行，用于统计"活跃天数"
CREATE TABLE IF NOT EXISTS active_days (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id, day)
);
CREATE INDEX IF NOT EXISTS idx_active_days ON active_days(chat_id, day);
-- 入群申请记录（含被自动拒绝的）
CREATE TABLE IF NOT EXISTS join_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    user_name   TEXT,
    username    TEXT,
    invite_link TEXT,
    inviter_id  INTEGER,
    requested_at INTEGER NOT NULL,
    decision    TEXT NOT NULL DEFAULT 'pending',
    decided_at  INTEGER,
    decided_by  TEXT,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jr ON join_requests(chat_id, requested_at DESC);
-- 已注销 / 查不到的账号（只做标记，绝不自动踢人）
CREATE TABLE IF NOT EXISTS deleted_accounts (
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    detected_at INTEGER NOT NULL,
    evidence    TEXT,
    kicked      INTEGER NOT NULL DEFAULT 0,
    kicked_at   INTEGER,
    kicked_by   INTEGER,
    PRIMARY KEY (chat_id, user_id)
);
-- 网页登录：用 Telegram 账号登录的一次性凭证
CREATE TABLE IF NOT EXISTS login_nonces (
    nonce      TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    used_at    INTEGER,
    user_id    INTEGER,
    first_name TEXT,
    username   TEXT,
    code       TEXT
);
CREATE INDEX IF NOT EXISTS idx_nonce_created ON login_nonces(created_at);
-- 群管理员权限缓存（网页分权用：群管理员只能看自己所在的群）
CREATE TABLE IF NOT EXISTS group_admins (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    status     TEXT,
    checked_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_ga_user ON group_admins(user_id);
-- 邀请链接申请（普通成员不能自己生成，要管理员批准）
CREATE TABLE IF NOT EXISTS link_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    user_name   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    requested_at INTEGER NOT NULL,
    handled_at  INTEGER,
    handled_by  INTEGER,
    note        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lr_pending ON link_requests(chat_id, user_id)
    WHERE status='pending';
-- 网页账号体系（每人独立账号：Telegram / 用户名 / 邮箱 三种方式）
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT UNIQUE,
    email          TEXT UNIQUE,
    pw_hash        TEXT,
    pw_salt        TEXT,
    pw_iter        INTEGER,
    pw_ver         INTEGER NOT NULL DEFAULT 1,
    tg_user_id     INTEGER UNIQUE,
    display_name   TEXT,
    role           TEXT NOT NULL DEFAULT 'member',
    status         TEXT NOT NULL DEFAULT 'active',
    email_verified INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER,
    last_login     INTEGER,
    last_ip        TEXT
);
CREATE TABLE IF NOT EXISTS invite_codes (
    code       TEXT PRIMARY KEY,
    note       TEXT,
    created_by INTEGER,
    created_at INTEGER NOT NULL,
    used_by    INTEGER,
    used_at    INTEGER
);
CREATE TABLE IF NOT EXISTS email_tokens (
    token      TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    used_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_et_account ON email_tokens(account_id, kind);
CREATE TABLE IF NOT EXISTS auth_attempts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT NOT NULL,
    key      TEXT,
    ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts ON auth_attempts(kind, key, ts);
"""


class Storage:
    """SQLite 存储。

    注意：sqlite3 连接不能跨线程使用，而发送通知/欢迎语的线程池任务也会读库，
    因此这里给每个线程开一条独立连接（WAL + busy_timeout，读写互不阻塞）。
    """

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._local = threading.local()
        self._conn()  # 建表

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.commit()
            self._local.conn = conn
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """老库升级：补上 chats.seq（群显示顺序，与 chat_id 大小无关）。"""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(chats)")}
        if "seq" not in cols:
            conn.execute("ALTER TABLE chats ADD COLUMN seq INTEGER")
            conn.execute("UPDATE chats SET seq = bound_at * 1000 + abs(chat_id) % 1000 "
                         "WHERE seq IS NULL")
        icols = {r["name"] for r in conn.execute("PRAGMA table_info(invite_codes)")}
        if "expires_at" not in icols:
            conn.execute("ALTER TABLE invite_codes ADD COLUMN expires_at INTEGER")
        if "revoked" not in icols:
            conn.execute("ALTER TABLE invite_codes ADD COLUMN revoked INTEGER DEFAULT 0")
        gcols = {r["name"] for r in conn.execute("PRAGMA table_info(group_admins)")}
        if "rights" not in gcols:
            # 存群管理员的具体权限（判断是不是"有实权的管理"）
            conn.execute("ALTER TABLE group_admins ADD COLUMN rights TEXT")
        acols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
        if "totp_secret" not in acols:
            conn.execute("ALTER TABLE accounts ADD COLUMN totp_secret TEXT")
            conn.execute("ALTER TABLE accounts ADD COLUMN totp_enabled INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE accounts ADD COLUMN totp_backup TEXT")
        ncols = {r["name"] for r in conn.execute("PRAGMA table_info(login_nonces)")}
        if "code" not in ncols:
            # 跨设备登录兜底：Telegram 里显示的 6 位确认码
            conn.execute("ALTER TABLE login_nonces ADD COLUMN code TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nonce_code ON login_nonces(code)")

    def _next_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM chats").fetchone()
        return int(row["m"] or 0) + 1

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn()

    # -- kv ---------------------------------------------------------------
    def get_kv(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))
        self.conn.commit()

    # -- users ------------------------------------------------------------
    def touch_user(self, user: dict, is_admin: bool | None = None) -> None:
        if not user or user.get("is_bot"):
            return
        now = int(time.time())
        uid = int(user["id"])
        self.conn.execute(
            """INSERT INTO users(user_id, username, first_name, is_admin, created_at, last_seen)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username=excluded.username,
                   first_name=excluded.first_name,
                   last_seen=excluded.last_seen""",
            (uid, user.get("username"), user.get("first_name"), 1 if is_admin else 0, now, now))
        if is_admin:
            self.conn.execute("UPDATE users SET is_admin=1 WHERE user_id=?", (uid,))
        self.conn.commit()

    def get_user(self, user_id: int):
        return self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def is_admin(self, user_id: int) -> bool:
        row = self.get_user(user_id)
        return bool(row and row["is_admin"])

    def set_admin(self, user_id: int, flag: bool) -> None:
        self.conn.execute("UPDATE users SET is_admin=? WHERE user_id=?", (1 if flag else 0, user_id))
        self.conn.commit()

    def admin_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM users WHERE is_admin=1").fetchone()["c"]

    def all_admin_ids(self) -> list:
        return [r["user_id"] for r in self.conn.execute("SELECT user_id FROM users WHERE is_admin=1")]

    def all_user_ids(self) -> list:
        return [r["user_id"] for r in self.conn.execute("SELECT user_id FROM users")]

    def find_user_by_username(self, username: str):
        return self.conn.execute(
            "SELECT * FROM users WHERE lower(username)=lower(?)", (username.lstrip("@"),)).fetchone()

    # -- chats ------------------------------------------------------------
    def bind_chat(self, chat_id: int, title: str) -> None:
        now = int(time.time())
        existing = self.conn.execute("SELECT seq FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        seq = existing["seq"] if (existing and existing["seq"] is not None) else self._next_seq()
        self.conn.execute(
            """INSERT INTO chats(chat_id,title,bound_at,seq,is_active) VALUES(?,?,?,?,1)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, is_active=1""",
            (chat_id, title, now, seq))
        self.conn.commit()

    def unbind_chat(self, chat_id: int) -> None:
        self.conn.execute("UPDATE chats SET is_active=0 WHERE chat_id=?", (chat_id,))
        self.conn.commit()

    def get_chat(self, chat_id: int):
        return self.conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()

    def links_of_owner(self, owner_id: int) -> list:
        return list(self.conn.execute(
            "SELECT chat_id, invite_link FROM links WHERE owner_id=? AND revoked=0 ORDER BY created_at",
            (owner_id,)))

    def main_chat(self):
        """默认群 = 最早绑定的那个（按 seq，与 chat_id 大小无关）。"""
        return self.conn.execute(
            "SELECT * FROM chats WHERE is_active=1 ORDER BY COALESCE(seq, 999999999), bound_at LIMIT 1"
        ).fetchone()

    def active_chats(self) -> list:
        return list(self.conn.execute(
            "SELECT * FROM chats WHERE is_active=1 ORDER BY COALESCE(seq, 999999999), bound_at"))

    def update_chat_title(self, chat_id: int, title: str) -> None:
        self.conn.execute("UPDATE chats SET title=? WHERE chat_id=?", (title, chat_id))
        self.conn.commit()

    # -- links ------------------------------------------------------------
    def get_link_by_owner(self, owner_id: int, chat_id: int):
        return self.conn.execute(
            "SELECT * FROM links WHERE owner_id=? AND chat_id=? AND revoked=0", (owner_id, chat_id)).fetchone()

    def get_link(self, invite_link: str):
        return self.conn.execute("SELECT * FROM links WHERE invite_link=?", (invite_link,)).fetchone()

    def get_link_by_name(self, name: str):
        return self.conn.execute("SELECT * FROM links WHERE name=?", (name,)).fetchone()

    def add_link(self, invite_link: str, owner_id: int, name: str, chat_id: int) -> None:
        self.conn.execute(
            """INSERT INTO links(invite_link,owner_id,name,chat_id,created_at,revoked,uses)
               VALUES(?,?,?,?,?,0,0)
               ON CONFLICT(invite_link) DO UPDATE SET
                   owner_id=excluded.owner_id, name=excluded.name,
                   chat_id=excluded.chat_id, revoked=0""",
            (invite_link, owner_id, name, chat_id, int(time.time())))
        self.conn.commit()

    def revoke_link_row(self, invite_link: str) -> None:
        self.conn.execute("UPDATE links SET revoked=1 WHERE invite_link=?", (invite_link,))
        self.conn.commit()

    def bump_link_uses(self, invite_link: str) -> None:
        self.conn.execute("UPDATE links SET uses=uses+1 WHERE invite_link=?", (invite_link,))
        self.conn.commit()

    def idle_links(self, limit: int) -> list:
        """挑"没有任何有效邀请"的闲置链接，用于触发上限时回收。"""
        return list(self.conn.execute(
            """SELECT l.* FROM links l
               LEFT JOIN referrals r ON r.inviter_id = l.owner_id AND r.chat_id = l.chat_id
               WHERE l.revoked = 0
               GROUP BY l.invite_link
               HAVING COUNT(r.invitee_id) = 0
               ORDER BY l.created_at ASC LIMIT ?""", (limit,)))

    def mark_links_revoked_for_owner(self, owner_id: int, chat_id: int) -> None:
        self.conn.execute("UPDATE links SET revoked=1 WHERE owner_id=? AND chat_id=?", (owner_id, chat_id))
        self.conn.commit()

    def link_owner_id(self, invite_link: str):
        row = self.get_link(invite_link)
        return row["owner_id"] if row else None

    # -- referrals --------------------------------------------------------
    def get_referral(self, invitee_id: int, chat_id: int):
        return self.conn.execute(
            "SELECT * FROM referrals WHERE invitee_id=? AND chat_id=?", (invitee_id, chat_id)).fetchone()

    def add_pending(self, invitee_id: int, chat_id: int, inviter_id: int, invite_link: str, name: str) -> bool:
        existing = self.get_referral(invitee_id, chat_id)
        if existing:
            return False
        self.conn.execute(
            """INSERT INTO referrals(invitee_id, chat_id, inviter_id, invite_link, joined_at, status, invitee_name)
               VALUES(?,?,?,?,NULL,'pending',?)""",
            (invitee_id, chat_id, inviter_id, invite_link, name))
        self.conn.commit()
        self.record_status(invitee_id, chat_id, "pending", inviter_id)
        return True

    def mark_joined(self, invitee_id: int, chat_id: int) -> bool:
        row = self.get_referral(invitee_id, chat_id)
        if not row:
            return False
        self.conn.execute(
            "UPDATE referrals SET status='member', joined_at=COALESCE(joined_at, ?), left_at=NULL "
            "WHERE invitee_id=? AND chat_id=?", (int(time.time()), invitee_id, chat_id))
        self.conn.commit()
        self.record_status(invitee_id, chat_id, "member", row["inviter_id"])
        return True

    def mark_left(self, invitee_id: int, chat_id: int, status: str = "left") -> bool:
        row = self.get_referral(invitee_id, chat_id)
        if not row:
            return False
        self.conn.execute(
            "UPDATE referrals SET status=?, left_at=? WHERE invitee_id=? AND chat_id=?",
            (status, int(time.time()), invitee_id, chat_id))
        self.conn.commit()
        self.record_status(invitee_id, chat_id, status, row["inviter_id"])
        return True

    # -- 状态流水（留存计算的基础） -----------------------------------------
    def record_status(self, invitee_id: int, chat_id: int, status: str, inviter_id=None) -> None:
        self.conn.execute(
            "INSERT INTO status_history(invitee_id, chat_id, status, inviter_id, ts) VALUES(?,?,?,?,?)",
            (invitee_id, chat_id, status, inviter_id, int(time.time())))
        self.conn.commit()

    # -- 审计日志 ----------------------------------------------------------
    def record_audit(self, actor_id, actor_name, action: str, target=None,
                     chat_id=None, detail=None) -> None:
        self.conn.execute(
            "INSERT INTO audit(ts, actor_id, actor_name, action, target, chat_id, detail) "
            "VALUES(?,?,?,?,?,?,?)",
            (int(time.time()), actor_id, actor_name, action, target, chat_id, detail))
        self.conn.commit()

    def audit_count(self, action: str | None = None) -> int:
        if action:
            return self.conn.execute("SELECT COUNT(*) c FROM audit WHERE action=?", (action,)).fetchone()["c"]
        return self.conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]

    def audit_page(self, limit: int = 10, offset: int = 0, action: str | None = None) -> list:
        if action:
            return list(self.conn.execute(
                "SELECT * FROM audit WHERE action=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (action, limit, offset)))
        return list(self.conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)))

    def audit_actions(self) -> list:
        return [r["action"] for r in self.conn.execute(
            "SELECT action, COUNT(*) c FROM audit GROUP BY action ORDER BY c DESC")]

    # -- 账号体系 ----------------------------------------------------------
    def create_account(self, username=None, email=None, password=None, display_name=None,
                       role="member", tg_user_id=None, email_verified=0):
        if username and self.get_account_by_username(username):
            raise ValueError("用户名已被占用")
        if email and self.get_account_by_email(email):
            raise ValueError("邮箱已被注册")
        if tg_user_id and self.get_account_by_tg(tg_user_id):
            raise ValueError("这个 Telegram 已绑定过账号")
        if not (username or email or tg_user_id):
            raise ValueError("至少要有用户名、邮箱或 Telegram 之一")
        pw_hash = pw_salt = None
        iters = None
        if password:
            pw_hash, pw_salt, iters = hash_password(password)
        cur = self.conn.execute(
            """INSERT INTO accounts(username,email,pw_hash,pw_salt,pw_iter,tg_user_id,
                                    display_name,role,status,email_verified,created_at)
               VALUES(?,?,?,?,?,?,?,?,'active',?,?)""",
            (username, email, pw_hash, pw_salt, iters, tg_user_id,
             display_name or username or email or f"tg{tg_user_id}", role, email_verified,
             int(time.time())))
        self.conn.commit()
        return cur.lastrowid

    def _acct(self, row):
        return row

    def get_account(self, acc_id: int):
        return self.conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()

    def get_account_by_username(self, username: str):
        if not username:
            return None
        return self.conn.execute("SELECT * FROM accounts WHERE lower(username)=lower(?)",
                                 (username,)).fetchone()

    def get_account_by_email(self, email: str):
        if not email:
            return None
        return self.conn.execute("SELECT * FROM accounts WHERE lower(email)=lower(?)",
                                 (email,)).fetchone()

    def get_account_by_tg(self, tg_user_id: int):
        if not tg_user_id:
            return None
        return self.conn.execute("SELECT * FROM accounts WHERE tg_user_id=?",
                                 (tg_user_id,)).fetchone()

    def account_count(self, role: str | None = None) -> int:
        if role:
            return self.conn.execute("SELECT COUNT(*) c FROM accounts WHERE role=?",
                                     (role,)).fetchone()["c"]
        return self.conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]

    def list_accounts(self, limit: int = 100, offset: int = 0, q: str | None = None) -> list:
        sql = ("""SELECT a.*, (SELECT COUNT(*) FROM group_admins g
                                WHERE g.user_id = a.tg_user_id
                                  AND g.status IN ('creator','administrator')) AS admin_groups
                 FROM accounts a""")
        args: list = []
        if q:
            sql += " WHERE lower(COALESCE(a.username,'')) LIKE ? OR lower(COALESCE(a.email,'')) LIKE ?" \
                   " OR CAST(a.tg_user_id AS TEXT) LIKE ? OR lower(COALESCE(a.display_name,'')) LIKE ?"
            like = f"%{q.lower()}%"
            args += [like, like, like, like]
        sql += " ORDER BY a.id LIMIT ? OFFSET ?"
        args += [limit, offset]
        return list(self.conn.execute(sql, args))

    def touch_login(self, acc_id: int, ip: str) -> None:
        self.conn.execute("UPDATE accounts SET last_login=?, last_ip=? WHERE id=?",
                          (int(time.time()), ip, acc_id))
        self.conn.commit()

    def set_password(self, acc_id: int, password: str) -> None:
        pw_hash, pw_salt, iters = hash_password(password)
        self.conn.execute(
            "UPDATE accounts SET pw_hash=?, pw_salt=?, pw_iter=?, pw_ver=pw_ver+1 WHERE id=?",
            (pw_hash, pw_salt, iters, acc_id))
        self.conn.commit()

    def set_account_role(self, acc_id: int, role: str) -> None:
        self.conn.execute("UPDATE accounts SET role=? WHERE id=?", (role, acc_id))
        self.conn.commit()

    def set_username(self, acc_id: int, username: str | None) -> None:
        """设置/更换用户名（供已登录用户自助绑定用户名密码登录）。"""
        username = (username or "").strip() or None
        if username:
            other = self.get_account_by_username(username)
            if other and other["id"] != acc_id:
                raise ValueError("用户名已被占用")
        self.conn.execute("UPDATE accounts SET username=? WHERE id=?", (username, acc_id))
        self.conn.commit()

    def set_email(self, acc_id: int, email: str | None, verified: int = 0) -> None:
        """绑定/更换邮箱（未验证邮箱不能用于找回密码）。"""
        email = (email or "").strip() or None
        if email:
            other = self.get_account_by_email(email)
            if other and other["id"] != acc_id:
                raise ValueError("这个邮箱已被其他账号使用")
        self.conn.execute("UPDATE accounts SET email=?, email_verified=? WHERE id=?",
                          (email, 1 if verified else 0, acc_id))
        self.conn.commit()

    def has_password(self, acc_id: int) -> bool:
        row = self.get_account(acc_id)
        return bool(row and row["pw_hash"])

    # -- 二次验证（TOTP） ---------------------------------------------------
    def set_totp(self, acc_id: int, secret: str | None, enabled: bool,
                 backup_json: str | None = None) -> None:
        self.conn.execute(
            "UPDATE accounts SET totp_secret=?, totp_enabled=?, totp_backup=? WHERE id=?",
            (secret, 1 if enabled else 0, backup_json, acc_id))
        self.conn.commit()

    def get_totp(self, acc_id: int):
        return self.conn.execute(
            "SELECT totp_secret, totp_enabled, totp_backup FROM accounts WHERE id=?",
            (acc_id,)).fetchone()

    def consume_backup_code(self, acc_id: int, code: str) -> bool:
        """用掉一个恢复码（一次性）。"""
        row = self.get_totp(acc_id)
        if not row or not row["totp_backup"]:
            return False
        try:
            hashes = json.loads(row["totp_backup"])
        except Exception:
            return False
        h = hash_backup_code(code)
        if h in hashes:
            hashes.remove(h)
            self.conn.execute("UPDATE accounts SET totp_backup=? WHERE id=?",
                              (json.dumps(hashes), acc_id))
            self.conn.commit()
            return True
        return False

    def set_account_status(self, acc_id: int, status: str) -> None:
        self.conn.execute("UPDATE accounts SET status=?, pw_ver=pw_ver+1 WHERE id=?",
                          (status, acc_id))
        self.conn.commit()

    def link_telegram(self, acc_id: int, tg_user_id: int, name: str | None = None) -> None:
        self.conn.execute(
            "UPDATE accounts SET tg_user_id=?, display_name=COALESCE(?, display_name) WHERE id=?",
            (tg_user_id, name, acc_id))
        self.conn.commit()

    def unlink_telegram(self, acc_id: int) -> None:
        self.conn.execute("UPDATE accounts SET tg_user_id=NULL WHERE id=?", (acc_id,))
        self.conn.commit()

    def set_email_verified(self, acc_id: int, email: str | None = None) -> None:
        if email:
            self.conn.execute("UPDATE accounts SET email=?, email_verified=1 WHERE id=?",
                              (email, acc_id))
        else:
            self.conn.execute("UPDATE accounts SET email_verified=1 WHERE id=?", (acc_id,))
        self.conn.commit()

    # 邀请码
    def create_invite_code(self, note: str | None, created_by: int | None,
                           ttl_days: int = 7) -> str:
        code = uuid.uuid4().hex[:12]
        exp = int(time.time()) + ttl_days * 86400 if ttl_days else None
        self.conn.execute(
            "INSERT INTO invite_codes(code,note,created_by,created_at,expires_at,revoked) "
            "VALUES(?,?,?,?,?,0)", (code, note, created_by, int(time.time()), exp))
        self.conn.commit()
        return code

    def use_invite_code(self, code: str, acc_id: int) -> bool:
        row = self.conn.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()
        if not row or row["used_at"] or row["revoked"]:
            return False
        exp = row["expires_at"] if "expires_at" in row.keys() else None
        if exp and int(time.time()) > int(exp):
            return False
        self.conn.execute("UPDATE invite_codes SET used_by=?, used_at=? WHERE code=?",
                          (acc_id, int(time.time()), code))
        self.conn.commit()
        return True

    def revoke_invite_code(self, code: str) -> bool:
        cur = self.conn.execute(
            "UPDATE invite_codes SET revoked=1 WHERE code=? AND used_at IS NULL", (code,))
        self.conn.commit()
        return bool(cur.rowcount)

    def invite_code_info(self, code: str):
        return self.conn.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()

    def invite_codes(self, limit: int = 50) -> list:
        return list(self.conn.execute(
            "SELECT * FROM invite_codes ORDER BY created_at DESC LIMIT ?", (limit,)))

    # 邮箱令牌（验证 / 找回密码）
    def create_email_token(self, acc_id: int, kind: str, ttl: int = 86400) -> str:
        token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        self.conn.execute("DELETE FROM email_tokens WHERE account_id=? AND kind=?", (acc_id, kind))
        self.conn.execute(
            "INSERT INTO email_tokens(token,account_id,kind,created_at) VALUES(?,?,?,?)",
            (token, acc_id, kind, int(time.time())))
        self.conn.commit()
        return token

    def consume_email_token(self, token: str, kind: str):
        row = self.conn.execute("SELECT * FROM email_tokens WHERE token=? AND kind=?",
                                (token, kind)).fetchone()
        if not row or row["used_at"] or int(time.time()) - row["created_at"] > 86400:
            return None
        self.conn.execute("UPDATE email_tokens SET used_at=? WHERE token=?",
                          (int(time.time()), token))
        self.conn.commit()
        return row["account_id"]

    # 登录限速
    def note_attempt(self, kind: str, key: str) -> None:
        self.conn.execute("INSERT INTO auth_attempts(kind,key,ts) VALUES(?,?,?)",
                          (kind, key, int(time.time())))
        self.conn.execute("DELETE FROM auth_attempts WHERE ts < ?", (int(time.time()) - 86400,))
        self.conn.commit()

    def count_attempts(self, kind: str, key: str, window: int = 900) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM auth_attempts WHERE kind=? AND key=? AND ts>=?",
            (kind, key, int(time.time()) - window)).fetchone()["c"]

    # -- 网页登录凭证 / 群权限缓存 ------------------------------------------
    def create_login_nonce(self, nonce: str, ttl: int = 900, code: str | None = None) -> None:
        now = int(time.time())
        self.conn.execute("DELETE FROM login_nonces WHERE created_at < ?", (now - 86400,))
        self.conn.execute(
            "INSERT OR REPLACE INTO login_nonces(nonce, created_at, code) VALUES(?,?,?)",
            (nonce, now, code))
        self.conn.commit()

    def get_login_nonce(self, nonce: str, ttl: int = 900):
        row = self.conn.execute("SELECT * FROM login_nonces WHERE nonce=?", (nonce,)).fetchone()
        if not row or int(time.time()) - row["created_at"] > ttl:
            return None
        return row

    def get_login_nonce_by_code(self, code: str, ttl: int = 900):
        """按 6 位确认码取最新一条已确认的登录凭证（跨设备兜底登录用）。"""
        code = (code or "").strip()
        if not code:
            return None
        row = self.conn.execute(
            """SELECT * FROM login_nonces WHERE code=? AND used_at IS NOT NULL
               ORDER BY created_at DESC LIMIT 1""", (code,)).fetchone()
        if not row or int(time.time()) - row["created_at"] > ttl:
            return None
        return row

    def confirm_login_nonce(self, nonce: str, user_id: int, first_name: str, username) -> bool:
        row = self.get_login_nonce(nonce)
        if not row or row["used_at"]:
            return False
        self.conn.execute(
            "UPDATE login_nonces SET used_at=?, user_id=?, first_name=?, username=? WHERE nonce=?",
            (int(time.time()), user_id, first_name, username, nonce))
        self.conn.commit()
        return True

    def set_group_admin(self, chat_id: int, user_id: int, status: str,
                        rights: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO group_admins(chat_id,user_id,status,rights,checked_at) VALUES(?,?,?,?,?)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET status=excluded.status,
                   rights=excluded.rights, checked_at=excluded.checked_at""",
            (chat_id, user_id, status, rights, int(time.time())))
        self.conn.commit()

    def group_admin_status(self, chat_id: int, user_id: int):
        return self.conn.execute(
            "SELECT status, checked_at FROM group_admins WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)).fetchone()

    def admin_groups_of(self, user_id: int, max_age: int = 6 * 3600) -> list:
        """这个用户**有实权**的管理群（群主，或带指定权限的管理员）。

        空壳管理员（没有任何权限的 administrator）不算 —— 避免"挂个名就拿到后台权限"。
        """
        cutoff = int(time.time()) - max_age
        need = self.get_setting("group_admin_min_rights", "can_invite_users")
        rows = self.conn.execute(
            """SELECT chat_id, status, rights FROM group_admins
               WHERE user_id=? AND status IN ('creator','administrator') AND checked_at >= ?""",
            (user_id, cutoff))
        out = []
        for r in rows:
            if r["status"] == "creator":
                out.append(r["chat_id"])
                continue
            rights = set((r["rights"] or "").split(",")) - {""}
            if need == "any":
                if rights:
                    out.append(r["chat_id"])
            else:
                required = {x.strip() for x in str(need).split(",") if x.strip()}
                if required and required.issubset(rights):
                    out.append(r["chat_id"])
        return out

    def member_groups_of(self, user_id: int, max_age: int = 6 * 3600) -> list:
        """这个用户**在群里**（含管理员/普通成员）的群 —— 登录后按这个显示群列表。"""
        cutoff = int(time.time()) - max_age
        return [r["chat_id"] for r in self.conn.execute(
            """SELECT chat_id FROM group_admins
               WHERE user_id=? AND status IN ('creator','administrator','member','restricted')
                 AND checked_at >= ?""", (user_id, cutoff))]

    def is_admin_of(self, chat_id: int, user_id: int) -> bool:
        """是不是"有实权"的群管理员（群主或带权限的管理员）。"""
        row = self.conn.execute(
            "SELECT status, rights FROM group_admins WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)).fetchone()
        if not row:
            return False
        if row["status"] == "creator":
            return True
        if row["status"] != "administrator":
            return False
        rights = set((row["rights"] or "").split(",")) - {""}
        need = self.get_setting("group_admin_min_rights", "can_invite_users")
        if need == "any":
            return bool(rights)
        required = {x.strip() for x in str(need).split(",") if x.strip()}
        return bool(required) and required.issubset(rights)

    # -- 邀请链接申请 -------------------------------------------------------
    def request_link(self, chat_id: int, user_id: int, user_name: str) -> bool:
        existing = self.conn.execute(
            "SELECT 1 FROM link_requests WHERE chat_id=? AND user_id=? AND status='pending'",
            (chat_id, user_id)).fetchone()
        if existing:
            return False
        self.conn.execute(
            """INSERT INTO link_requests(chat_id,user_id,user_name,status,requested_at)
               VALUES(?,?,?,'pending',?)""",
            (chat_id, user_id, user_name, int(time.time())))
        self.conn.commit()
        return True

    def pending_link_requests(self, chat_id: int | None = None, limit: int = 100) -> list:
        sql = """SELECT lr.*, c.title AS chat_title FROM link_requests lr
                 LEFT JOIN chats c ON c.chat_id = lr.chat_id
                 WHERE lr.status='pending'"""
        args: list = []
        if chat_id is not None:
            sql += " AND lr.chat_id=?"
            args.append(chat_id)
        sql += " ORDER BY lr.requested_at LIMIT ?"
        args.append(limit)
        return list(self.conn.execute(sql, args))

    def link_requests_count(self, chat_id: int | None = None) -> int:
        if chat_id is None:
            return self.conn.execute(
                "SELECT COUNT(*) c FROM link_requests WHERE status='pending'").fetchone()["c"]
        return self.conn.execute(
            "SELECT COUNT(*) c FROM link_requests WHERE status='pending' AND chat_id=?",
            (chat_id,)).fetchone()["c"]

    def my_link_request(self, chat_id: int, user_id: int):
        return self.conn.execute(
            """SELECT * FROM link_requests WHERE chat_id=? AND user_id=?
               ORDER BY id DESC LIMIT 1""", (chat_id, user_id)).fetchone()

    def decide_link_request(self, req_id: int, status: str, by: int, note: str | None = None) -> bool:
        row = self.conn.execute("SELECT * FROM link_requests WHERE id=?", (req_id,)).fetchone()
        if not row or row["status"] != "pending":
            return False
        self.conn.execute(
            "UPDATE link_requests SET status=?, handled_at=?, handled_by=?, note=? WHERE id=?",
            (status, int(time.time()), by, note, req_id))
        self.conn.commit()
        return True


    # -- 网页可改的设置（DB 里的值优先于 config.json） ---------------------
    def get_setting(self, key: str, default=None):
        v = self.get_kv(f"set:{key}")
        if v is None:
            return default
        if v in ("true", "True", "1"):
            return True
        if v in ("false", "False", "0"):
            return False
        return v

    def set_setting(self, key: str, value) -> None:
        self.set_kv(f"set:{key}", "true" if value is True else "false" if value is False else str(value))

    def all_settings(self) -> dict:
        out = {}
        for r in self.conn.execute("SELECT key, value FROM kv WHERE key LIKE 'set:%'"):
            k = r["key"][4:]
            out[k] = ("true" if r["value"] in ("true", "1", "True") else
                      "false" if r["value"] in ("false", "0", "False") else r["value"])
        return out

    # -- 活跃度 ------------------------------------------------------------
    def bump_activity(self, chat_id: int, user_id: int, ts: int | None = None) -> None:
        """只累加计数和时间，不记录任何消息内容。"""
        ts = ts or int(time.time())
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        self.conn.execute(
            """INSERT INTO activity(chat_id, user_id, messages, first_ts, last_ts)
               VALUES(?,?,1,?,?)
               ON CONFLICT(chat_id, user_id) DO UPDATE SET
                   messages = messages + 1, last_ts = excluded.last_ts""",
            (chat_id, user_id, ts, ts))
        self.conn.execute(
            "INSERT OR IGNORE INTO active_days(chat_id, user_id, day) VALUES(?,?,?)",
            (chat_id, user_id, day))
        self.conn.commit()

    def activity_board(self, chat_id: int, limit: int = 20, offset: int = 0,
                       q: str | None = None) -> list:
        rows = list(self.conn.execute(
            """SELECT a.user_id, a.messages, a.first_ts, a.last_ts,
                      (SELECT COUNT(*) FROM active_days d
                        WHERE d.chat_id = a.chat_id AND d.user_id = a.user_id) AS active_days,
                      (SELECT u.first_name FROM users u WHERE u.user_id = a.user_id) AS first_name,
                      (SELECT r.inviter_id FROM referrals r
                        WHERE r.chat_id = a.chat_id AND r.invitee_id = a.user_id) AS inviter_id,
                      (SELECT r.status FROM referrals r
                        WHERE r.chat_id = a.chat_id AND r.invitee_id = a.user_id) AS member_status,
                      (SELECT 1 FROM deleted_accounts da
                        WHERE da.chat_id = a.chat_id AND da.user_id = a.user_id) AS is_deleted
               FROM activity a WHERE a.chat_id = ?
               ORDER BY a.messages DESC, a.last_ts DESC
               LIMIT ? OFFSET ?""", (chat_id, limit, offset)))
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in (r["first_name"] or "").lower()]
        return rows

    def activity_totals(self, chat_id: int) -> dict:
        now = int(time.time())

        def _active_since(seconds: int) -> int:
            day = time.strftime("%Y-%m-%d", time.gmtime(now - seconds))
            return self.conn.execute(
                "SELECT COUNT(DISTINCT user_id) c FROM active_days WHERE chat_id=? AND day>=?",
                (chat_id, day)).fetchone()["c"]

        spoken = self.conn.execute(
            "SELECT COUNT(*) c FROM activity WHERE chat_id=?", (chat_id,)).fetchone()["c"]
        spoken_members = self.conn.execute(
            "SELECT COUNT(DISTINCT user_id) c FROM activity WHERE chat_id=?", (chat_id,)).fetchone()["c"]
        tracked = self.conn.execute(
            """SELECT COUNT(*) c FROM referrals WHERE chat_id=?
               AND status IN ('member','left','kicked')""", (chat_id,)).fetchone()["c"]
        # 真实群人数：机器人每分钟同步到 kv（mcount:<gid>）；没有就退回推荐关系里的在群数
        members = 0
        try:
            members = int(self.get_kv(f"mcount:{chat_id}") or 0)
        except (TypeError, ValueError):
            members = 0
        if members <= 0:
            members = self.conn.execute(
                "SELECT COUNT(*) c FROM referrals WHERE chat_id=? AND status='member'",
                (chat_id,)).fetchone()["c"]
        silent = max(0, members - spoken_members)
        total_msgs = self.conn.execute(
            "SELECT COALESCE(SUM(messages),0) s FROM activity WHERE chat_id=?",
            (chat_id,)).fetchone()["s"]
        return {"dau": _active_since(86400), "wau": _active_since(7 * 86400),
                "mau": _active_since(30 * 86400), "spoken": spoken, "tracked": tracked,
                "members": members, "silent": silent, "total_messages": total_msgs}

    # -- 入群申请 ----------------------------------------------------------
    def add_join_request(self, chat_id: int, user_id: int, user_name: str, username: str | None,
                         invite_link: str | None, inviter_id: int | None) -> int:
        cur = self.conn.execute(
            """INSERT INTO join_requests(chat_id, user_id, user_name, username, invite_link,
                                         inviter_id, requested_at, decision)
               VALUES(?,?,?,?,?,?,?,'pending')""",
            (chat_id, user_id, user_name, username, invite_link, inviter_id, int(time.time())))
        self.conn.commit()
        return cur.lastrowid

    def decide_join_request(self, chat_id: int, user_id: int, decision: str,
                            decided_by: str = "bot", reason: str | None = None) -> int:
        cur = self.conn.execute(
            """UPDATE join_requests SET decision=?, decided_at=?, decided_by=?, reason=?
               WHERE chat_id=? AND user_id=? AND decision='pending'""",
            (decision, int(time.time()), decided_by, reason, chat_id, user_id))
        self.conn.commit()
        return cur.rowcount

    def join_requests_page(self, chat_id: int, limit: int = 20, offset: int = 0,
                           decision: str | None = None) -> list:
        if decision:
            return list(self.conn.execute(
                """SELECT * FROM join_requests WHERE chat_id=? AND decision=?
                   ORDER BY requested_at DESC LIMIT ? OFFSET ?""",
                (chat_id, decision, limit, offset)))
        return list(self.conn.execute(
            """SELECT * FROM join_requests WHERE chat_id=?
               ORDER BY requested_at DESC LIMIT ? OFFSET ?""", (chat_id, limit, offset)))

    def join_requests_count(self, chat_id: int, decision: str | None = None) -> int:
        if decision:
            return self.conn.execute(
                """SELECT COUNT(*) c FROM join_requests WHERE chat_id=? AND decision=?""",
                (chat_id, decision)).fetchone()["c"]
        return self.conn.execute(
            "SELECT COUNT(*) c FROM join_requests WHERE chat_id=?", (chat_id,)).fetchone()["c"]

    def join_requests_stats(self, chat_id: int) -> dict:
        row = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(CASE WHEN decision='pending' THEN 1 END) AS pending,
                      COUNT(CASE WHEN decision='approved' THEN 1 END) AS approved,
                      COUNT(CASE WHEN decision='declined' THEN 1 END) AS declined,
                      COUNT(CASE WHEN decision='stale' THEN 1 END) AS stale
               FROM join_requests WHERE chat_id=?""", (chat_id,)).fetchone()
        auto = self.conn.execute(
            """SELECT COUNT(*) c FROM join_requests WHERE chat_id=?
               AND decision='declined' AND decided_by='auto'""", (chat_id,)).fetchone()["c"]
        return {"total": row["total"], "pending": row["pending"], "approved": row["approved"],
                "declined": row["declined"], "stale": row["stale"], "auto_declined": auto}

    # -- 已注销 / 查不到的账号 ----------------------------------------------
    def mark_deleted(self, chat_id: int, user_id: int, evidence: str) -> bool:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO deleted_accounts(chat_id, user_id, detected_at, evidence)
               VALUES(?,?,?,?)""", (chat_id, user_id, int(time.time()), evidence))
        self.conn.commit()
        return bool(cur.rowcount)

    def is_marked_deleted(self, chat_id: int, user_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM deleted_accounts WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)).fetchone() is not None

    def list_deleted(self, chat_id: int, limit: int = 500) -> list:
        return list(self.conn.execute(
            """SELECT d.*,
                      (SELECT r.inviter_id FROM referrals r
                        WHERE r.chat_id=d.chat_id AND r.invitee_id=d.user_id) AS inviter_id,
                      (SELECT u.first_name FROM users u WHERE u.user_id=d.user_id) AS first_name
               FROM deleted_accounts d WHERE d.chat_id=?
               ORDER BY d.detected_at DESC LIMIT ?""", (chat_id, limit)))

    def deleted_page(self, chat_id: int, limit: int = 30, offset: int = 0) -> list:
        """已注销账号分页（网页用，避免一次捞几百条）。"""
        return list(self.conn.execute(
            """SELECT d.*,
                      (SELECT r.inviter_id FROM referrals r
                        WHERE r.chat_id=d.chat_id AND r.invitee_id=d.user_id) AS inviter_id,
                      (SELECT u.first_name FROM users u WHERE u.user_id=d.user_id) AS first_name
               FROM deleted_accounts d WHERE d.chat_id=?
               ORDER BY d.detected_at DESC LIMIT ? OFFSET ?""", (chat_id, limit, offset)))

    def deleted_stats(self, chat_id: int) -> dict:
        row = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(CASE WHEN kicked=1 THEN 1 END) AS kicked
               FROM deleted_accounts WHERE chat_id=?""", (chat_id,)).fetchone()
        return {"total": row["total"], "kicked": row["kicked"]}

    def mark_kicked(self, chat_id: int, user_id: int, by: int) -> None:
        self.conn.execute(
            "UPDATE deleted_accounts SET kicked=1, kicked_at=?, kicked_by=? WHERE chat_id=? AND user_id=?",
            (int(time.time()), by, chat_id, user_id))
        self.conn.commit()

    # -- 留存 / 质量分析 ---------------------------------------------------
    def retention_cohort(self, chat_id: int, days: int) -> dict:
        """加入满 days 天的成员里，第 days 天时仍在群的比例。

        判定口径（比"当天必须有打卡记录"更准确）：
          * 现在仍在群 → 视为第 N 天留存（他加入早于 N 天前）；
          * 已退群/被移出 → 首次退群时间 >= 加入时间 + N 天 才算留存；
          * 样本只包含加入时间已满 N 天的人，未满 N 天的不参与统计。
        """
        now = int(time.time())
        cutoff = now - days * 86400
        row = self.conn.execute(
            """SELECT COUNT(*) AS matured,
                      COALESCE(SUM(CASE
                          WHEN r.status = 'member' THEN 1
                          WHEN (SELECT MIN(h.ts) FROM status_history h
                                WHERE h.invitee_id = r.invitee_id AND h.chat_id = r.chat_id
                                  AND h.status IN ('left','kicked')) >= r.joined_at + ?
                          THEN 1 ELSE 0 END), 0) AS retained
               FROM referrals r
               WHERE r.chat_id = ? AND r.joined_at IS NOT NULL AND r.joined_at <= ?""",
            (days * 86400, chat_id, cutoff)).fetchone()
        matured = row["matured"] or 0
        retained = row["retained"] or 0
        return {"days": days, "matured": matured, "retained": retained,
                "rate": (retained / matured) if matured else None}

    def retention_by_inviter(self, chat_id: int, days: int = 7, limit: int = 10) -> list:
        now = int(time.time())
        cutoff = now - days * 86400
        return list(self.conn.execute(
            """SELECT r.inviter_id,
                      COUNT(*) AS matured,
                      COALESCE(SUM(CASE
                          WHEN r.status = 'member' THEN 1
                          WHEN (SELECT MIN(h.ts) FROM status_history h
                                WHERE h.invitee_id = r.invitee_id AND h.chat_id = r.chat_id
                                  AND h.status IN ('left','kicked')) >= r.joined_at + ?
                          THEN 1 ELSE 0 END), 0) AS retained
               FROM referrals r
               WHERE r.chat_id = ? AND r.joined_at IS NOT NULL AND r.joined_at <= ?
               GROUP BY r.inviter_id
               HAVING matured >= 1
               ORDER BY retained DESC, matured DESC
               LIMIT ?""", (days * 86400, chat_id, cutoff, min(limit, 500))))

    def avg_lifetime_days(self, chat_id: int) -> float | None:
        row = self.conn.execute(
            """SELECT AVG(left_at - joined_at) AS v FROM referrals
               WHERE chat_id=? AND left_at IS NOT NULL AND joined_at IS NOT NULL""",
            (chat_id,)).fetchone()
        v = row["v"] if row else None
        return (v / 86400.0) if v else None

    def quality_scores(self, chat_id: int, days: int = 7, prior_weight: float = 5.0,
                       prior_rate: float = 0.5) -> list:
        """拉新质量分：贝叶斯平滑后的 7 日留存率（0-100）。

        score = 100 * (retained + prior_rate*prior_weight) / (matured + prior_weight)
        样本少的人会被拉向 50 分，避免"只拉 1 人且没跑"就霸榜。
        """
        out = []
        for r in self.retention_by_inviter(chat_id, days=days, limit=500):
            matured = r["matured"] or 0
            retained = r["retained"] or 0
            score = 100.0 * (retained + prior_rate * prior_weight) / (matured + prior_weight)
            invited = self.count_for(r["inviter_id"], chat_id)
            present = self.current_for(r["inviter_id"], chat_id)
            out.append({
                "inviter_id": r["inviter_id"], "matured": matured, "retained": retained,
                "invited": invited, "present": present,
                "raw_rate": (retained / matured) if matured else None,
                "churned": max(0, invited - present),
                "score": round(score, 1),
            })
        out.sort(key=lambda x: (-x["score"], -x["retained"], -x["invited"]))
        return out

    def funnel(self, chat_id: int) -> dict:
        """入群漏斗：申请过 → 真的进过群 → 7 日后仍在群。"""
        applied = self.conn.execute(
            "SELECT COUNT(DISTINCT invitee_id) c FROM status_history WHERE chat_id=? AND status='pending'",
            (chat_id,)).fetchone()["c"]
        joined = self.conn.execute(
            "SELECT COUNT(*) c FROM referrals WHERE chat_id=? AND joined_at IS NOT NULL",
            (chat_id,)).fetchone()["c"]
        now = int(time.time())
        matured_row = self.conn.execute(
            "SELECT COUNT(*) c FROM referrals WHERE chat_id=? AND joined_at IS NOT NULL AND joined_at<=?",
            (chat_id, now - 7 * 86400)).fetchone()
        ret7 = self.retention_cohort(chat_id, 7)
        return {"applied": applied, "joined": joined,
                "matured_7d": matured_row["c"] or 0,
                "retained_7d": ret7["retained"], "rate_7d": ret7["rate"]}

    def churn_board(self, chat_id: int, limit: int = 10) -> list:
        return list(self.conn.execute(
            """SELECT inviter_id,
                      COUNT(CASE WHEN status IN ('left','kicked') THEN 1 END) AS churned,
                      COUNT(CASE WHEN status IN ('member','left','kicked') THEN 1 END) AS invited
               FROM referrals WHERE chat_id=?
               GROUP BY inviter_id
               HAVING invited > 0
               ORDER BY churned DESC, invited DESC LIMIT ?""", (chat_id, limit)))

    def daily_growth(self, chat_id: int, days: int = 14) -> list:
        since = int(time.time()) - days * 86400
        rows = self.conn.execute(
            """SELECT DATE(joined_at, 'unixepoch') AS d, COUNT(*) AS c
               FROM referrals WHERE chat_id=? AND joined_at IS NOT NULL AND joined_at>=?
               GROUP BY d ORDER BY d""", (chat_id, since)).fetchall()
        return [(r["d"], r["c"]) for r in rows]

    def count_for(self, inviter_id: int, chat_id: int | None = None) -> int:
        """累计有效邀请人数（去重后真的进来过的）。"""
        sql = ("SELECT COUNT(*) c FROM referrals WHERE inviter_id=? "
               "AND status IN ('member','left','kicked')")
        args = [inviter_id]
        if chat_id is not None:
            sql += " AND chat_id=?"
            args.append(chat_id)
        return self.conn.execute(sql, args).fetchone()["c"]

    def current_for(self, inviter_id: int, chat_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND status='member'"
        args = [inviter_id]
        if chat_id is not None:
            sql += " AND chat_id=?"
            args.append(chat_id)
        return self.conn.execute(sql, args).fetchone()["c"]

    def pending_for(self, inviter_id: int, chat_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM referrals WHERE inviter_id=? AND chat_id=? AND status='pending'",
            (inviter_id, chat_id)).fetchone()["c"]

    def rank_of(self, inviter_id: int, chat_id: int) -> int:
        rows = self.leaderboard(chat_id, limit=100000)
        for i, r in enumerate(rows, 1):
            if r["inviter_id"] == inviter_id:
                return i
        return 0

    def leaderboard(self, chat_id: int, limit: int = 15, offset: int = 0) -> list:
        return list(self.conn.execute(
            """SELECT inviter_id,
                      COUNT(CASE WHEN status IN ('member','left','kicked') THEN 1 END) AS invited,
                      COUNT(CASE WHEN status='member' THEN 1 END) AS present
               FROM referrals WHERE chat_id=?
               GROUP BY inviter_id
               HAVING invited > 0
               ORDER BY invited DESC, MIN(joined_at) ASC
               LIMIT ? OFFSET ?""", (chat_id, limit, offset)))

    def leaderboard_size(self, chat_id: int) -> int:
        return self.conn.execute(
            """SELECT COUNT(*) c FROM (
                   SELECT inviter_id FROM referrals WHERE chat_id=?
                   GROUP BY inviter_id
                   HAVING COUNT(CASE WHEN status IN ('member','left','kicked') THEN 1 END) > 0)""",
            (chat_id,)).fetchone()["c"]

    def invitees_of(self, inviter_id: int, chat_id: int, limit: int = 20, offset: int = 0) -> list:
        return list(self.conn.execute(
            """SELECT * FROM referrals WHERE inviter_id=? AND chat_id=?
               AND status IN ('member','left','kicked')
               ORDER BY joined_at DESC LIMIT ? OFFSET ?""",
            (inviter_id, chat_id, limit, offset)))

    def group_totals(self, chat_id: int) -> dict:
        row = self.conn.execute(
            """SELECT COUNT(CASE WHEN status IN ('member','left','kicked') THEN 1 END) AS invited,
                      COUNT(CASE WHEN status='member' THEN 1 END) AS present,
                      COUNT(CASE WHEN status='pending' THEN 1 END) AS pending,
                      COUNT(DISTINCT inviter_id) AS inviters
               FROM referrals WHERE chat_id=?""", (chat_id,)).fetchone()
        today = int(time.time()) - 86400
        recent = self.conn.execute(
            "SELECT COUNT(*) c FROM referrals WHERE chat_id=? AND joined_at>=?",
            (chat_id, today)).fetchone()["c"]
        return {"invited": row["invited"], "present": row["present"],
                "pending": row["pending"], "inviters": row["inviters"], "last24h": recent}

    def all_referrals(self, chat_id: int) -> list:
        return list(self.conn.execute(
            "SELECT * FROM referrals WHERE chat_id=? ORDER BY joined_at ASC", (chat_id,)))

    def event(self, kind: str, chat_id=None, user_id=None, detail=None) -> None:
        self.conn.execute("INSERT INTO events(ts,kind,chat_id,user_id,detail) VALUES(?,?,?,?,?)",
                          (int(time.time()), kind, chat_id, user_id, detail))
        self.conn.commit()

    def get_offset(self) -> int:
        return int(self.get_kv("update_offset", "0") or 0)

    def set_offset(self, offset: int) -> None:
        self.conn.execute(
            "INSERT INTO kv(key,value) VALUES('update_offset',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
        self.conn.commit()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 二次验证（TOTP，RFC 6238）——纯标准库
# ---------------------------------------------------------------------------
def new_totp_secret(nbytes: int = 20) -> str:
    """生成 base32 密钥（Google Authenticator / Authy 通用）。"""
    return base64.b32encode(os.urandom(nbytes)).decode().rstrip("=")


def totp_code(secret: str, at: float | None = None, digits: int = 6, step: int = 30) -> str:
    pad = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + pad)
    counter = int((time.time() if at is None else at) // step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    val = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(val % (10 ** digits)).zfill(digits)


def totp_verify(secret: str, code: str, window: int = 1) -> bool:
    """允许前后各 window 个时间窗（容忍手机时间误差）。"""
    if not secret or not code:
        return False
    code = str(code).strip().replace(" ", "")
    if not code.isdigit():
        return False
    now = time.time()
    for i in range(-window, window + 1):
        if hmac.compare_digest(totp_code(secret, now + i * 30), code):
            return True
    return False


def totp_uri(secret: str, label: str, issuer: str = "邀请排行榜") -> str:
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(issuer)}:{quote(label)}?secret={secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")


def new_backup_codes(n: int = 8) -> list:
    return ["-".join(uuid.uuid4().hex[:4].upper() for _ in range(2)) for _ in range(n)]


def hash_backup_code(code: str) -> str:
    return hashlib.sha256(("dshb-otp-" + code.strip().upper().replace(" ", "")).encode()).hexdigest()


# ---------------------------------------------------------------------------
# 里程碑
# ---------------------------------------------------------------------------
MILESTONES = (5, 10, 20, 50, 100, 200, 500)


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


# ---------------------------------------------------------------------------
# 密码哈希（只用标准库 PBKDF2-HMAC-SHA256）
# ---------------------------------------------------------------------------
PBKDF2_ITER = 600_000   # OWASP 2023：PBKDF2-HMAC-SHA256 推荐 60 万次迭代


def hash_password(password: str, salt_hex: str | None = None, iterations: int = PBKDF2_ITER):
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return dk.hex(), salt.hex(), iterations


def verify_password(password: str, pw_hash: str, salt_hex: str, iterations: int) -> bool:
    if not pw_hash or not salt_hex:
        return False
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iterations or PBKDF2_ITER))
    except Exception:
        return False
    return hmac.compare_digest(dk.hex(), pw_hash)


def valid_username(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]{3,32}", name or ""))


def valid_email(addr: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,20}", addr or ""))


def display_name(user: dict | None, fallback_id: int | None = None, fallback: str = "") -> str:
    if user:
        name = user.get("first_name") or user.get("username") or ""
        if user.get("last_name"):
            name = f"{name} {user['last_name']}".strip()
        if name:
            return esc(name)
    if fallback:
        return esc(fallback)
    return f"用户 {fallback_id}" if fallback_id else "未知用户"


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def is_member_status(member: dict | None) -> bool:
    if not member:
        return False
    st = member.get("status")
    if st in ("creator", "administrator", "member"):
        return True
    if st == "restricted":
        return bool(member.get("is_member"))
    return False


def is_left_status(member: dict | None) -> bool:
    if not member:
        return False
    return member.get("status") in ("left", "kicked")


def chunk_text(text: str, limit: int = 3800) -> list:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


# ---------------------------------------------------------------------------
# 机器人主体
# ---------------------------------------------------------------------------
class InviteBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tg = Telegram(cfg["bot_token"], cfg.get("api_base", "https://api.telegram.org"))
        self.db = Storage(cfg["db_path"])
        self.me = self.tg.get_me(use_cache=False)
        self.bot_id = int(self.me["id"])
        self.bot_username = self.me.get("username") or ""
        self.owner_ids = [int(x) for x in cfg.get("owner_ids", [])]
        for uid in self.owner_ids:
            self.db.touch_user({"id": uid, "first_name": "owner"}, is_admin=True)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")
        self._running = True
        self._member_cache: dict = {}
        # 私聊对话状态（例如等待管理员输入欢迎语）
        self._awaiting: dict = {}
        self.web = None
        self.web_error = ""

    def submit(self, fn, *args):
        """把耗时任务丢到后台线程池（线程池被关掉后自动重建，避免停服后无法再投递）。"""
        try:
            return self.pool.submit(fn, *args)
        except RuntimeError:
            self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")
            return self.pool.submit(fn, *args)

    def _milestone(self, chat: dict, owner_id: int, count: int, inviter) -> None:
        name = display_name(None, owner_id, inviter["first_name"] if inviter else "")
        medal = {5: "🌟", 10: "🏅", 20: "🥈", 50: "🥇", 100: "👑", 200: "💎", 500: "🚀"}.get(count, "🎉")
        try:
            self.tg.send_message(chat["id"], (
                f"{medal} <b>里程碑达成</b>\n\n"
                f"{mention(owner_id, name)} 在 <b>{esc(chat.get('title'))}</b> "
                f"的邀请人数达到 <b>{count}</b> 人！\n"
                f"用 /top 看最新排行榜。"))
        except TelegramError as e:
            log(f"里程碑群播报失败：{e}", "WARN")
        try:
            self.tg.send_message(owner_id, (
                f"{medal} 恭喜！你在这个群里已经邀请满 <b>{count}</b> 人 🎉\n\n"
                f"链接可以继续分享，战绩随时用 /stats 查看。"))
        except TelegramError:
            pass
        self.db.event("milestone", chat["id"], owner_id, f"count={count}")
        self.audit_soft(owner_id, "milestone", target=str(owner_id), chat_id=chat["id"],
                        detail=f"邀请达到 {count} 人")

    # -- 稳定性：心跳 / 自动备份 / 周报 -------------------------------------
    def tick(self) -> None:
        """主循环里每分钟跑一次：心跳、群人数同步、自动备份、周报。"""
        now = int(time.time())
        self.db.set_kv("bot_heartbeat", str(now))
        self._sync_member_counts()
        # 禁漫下载：按设定的自动清理模式删除过期内容（off 则跳过）
        try:
            import jm_tg
            n = jm_tg.cleanup_expired()
            if n:
                log(f"禁漫自动清理：删除了 {n} 本过期下载")
        except Exception:
            pass
        # 自动同步入群申请：每 5 分钟核对一次"待处理"，
        # Telegram 里已不存在的申请自动归档，仍存在的按规则拒绝 —— 网页和群聊保持一致
        # （独立于备份开关，避免被提前 return 跳过）
        if self.setting("auto_sync_requests", True):
            try:
                last = int(self.db.get_kv("last_sync_req") or 0)
            except (TypeError, ValueError):
                last = 0
            if now - last >= 300:
                self.db.set_kv("last_sync_req", str(now))
                for ch in self.db.active_chats():
                    if self.db.join_requests_stats(ch["chat_id"])["pending"]:
                        self.submit(self.sweep_join_requests, ch["chat_id"], "all", None)
        if not self.setting("auto_backup_enabled", True):
            return
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.db.get_kv("last_backup_day") != today:
            self._do_backup(today)
            self.db.set_kv("last_backup_day", today)
        # 每周一 09:00 后发周报（每群一条）
        lt = time.localtime(now)
        if lt.tm_wday == 0 and lt.tm_hour >= 9 and self.db.get_kv("last_weekly") != today:
            self._do_weekly()
            self.db.set_kv("last_weekly", today)

    def _sync_member_counts(self) -> None:
        """每分钟把每个绑定群的真实人数写入缓存（活跃页"在群 N 人"用）。"""
        try:
            for ch in self.db.active_chats():
                n = self.tg.call("getChatMemberCount", {"chat_id": ch["chat_id"]})
                self.db.set_kv(f"mcount:{ch['chat_id']}", str(int(n)))
        except TelegramError as e:
            log(f"同步群人数失败：{e}", "WARN")

    def _do_backup(self, today: str) -> None:
        try:
            bdir = os.path.join(os.path.dirname(self.cfg["db_path"]), "backups")
            os.makedirs(bdir, exist_ok=True)
            dest = os.path.join(bdir, f"bot-{today}.db")
            if not os.path.exists(dest):
                dst = sqlite3.connect(dest)
                self.db.conn.backup(dst)
                dst.close()
                log(f"数据库已自动备份：{dest}")
                self.audit(None, "auto_backup", target=dest, detail="每日自动备份")
            # 只保留最近 14 份
            files = sorted(f for f in os.listdir(bdir) if f.startswith("bot-") and f.endswith(".db"))
            for old in files[:-14]:
                try:
                    os.remove(os.path.join(bdir, old))
                except OSError:
                    pass
        except Exception:
            log("自动备份失败：\n" + traceback.format_exc(), "ERROR")

    def _do_weekly(self) -> None:
        if not self.setting("weekly_report_enabled", True):
            return
        now = int(time.time())
        since = now - 7 * 86400
        for c in self.db.active_chats():
            gid = c["chat_id"]
            rows = list(self.db.conn.execute(
                """SELECT inviter_id, COUNT(*) AS c FROM referrals
                   WHERE chat_id=? AND joined_at>=? AND status IN ('member','left','kicked')
                   GROUP BY inviter_id ORDER BY c DESC LIMIT 5""", (gid, since)))
            if not rows:
                continue
            lines = [f"📅 <b>{esc(c['title'])}</b> 本周邀请榜", ""]
            medals = ["🥇", "🥈", "🥉"]
            for i, r in enumerate(rows):
                u = self.db.get_user(r["inviter_id"])
                nm = display_name(None, r["inviter_id"], u["first_name"] if u else "")
                lines.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {nm} — <b>{r['c']}</b> 人")
            total = self.db.conn.execute(
                """SELECT COUNT(*) c FROM referrals WHERE chat_id=? AND joined_at>=?
                   AND status IN ('member','left','kicked')""", (gid, since)).fetchone()["c"]
            lines += ["", f"本周共新增 <b>{total}</b> 人。你也想上榜？发 /link 领专属链接。"]
            try:
                self.tg.send_message(gid, "\n".join(lines))
            except TelegramError as e:
                log(f"周报发送失败（{gid}）：{e}", "WARN")
        self.audit(None, "weekly_report", detail="自动发送周报")

    # -- 审计 -------------------------------------------------------------
    def audit(self, actor, action: str, target=None, chat_id=None, detail=None) -> None:
        """记录一条审计日志。actor 可以是 message['from'] 或 None。"""
        try:
            uid = (actor or {}).get("id")
            name = None
            if actor:
                name = actor.get("first_name") or actor.get("username") or None
            self.db.record_audit(uid, name, action, target, chat_id, detail)
        except Exception as e:  # 审计失败绝不能影响主流程
            log(f"写审计日志失败：{e}", "WARN")

    def audit_soft(self, actor_id, action: str, target=None, chat_id=None, detail=None) -> None:
        self.audit({"id": actor_id}, action, target, chat_id, detail)

    # -- 运行时设置（网页可改，DB 优先于 config.json） ----------------------
    def setting(self, key: str, default=None):
        return self.db.get_setting(key, self.cfg.get(key, default))

    def settings_snapshot(self) -> dict:
        keys = ["welcome_enabled", "notify_inviter_on_join", "log_leave_enabled",
                "activity_enabled", "auto_decline_deleted", "auto_bind_enabled",
                "cleanup_notify_admin", "allow_cleanup_kick", "web_public_leaderboard",
                "welcome_on_join_request"]
        return {k: self.setting(k) for k in keys}


    def start_web_panel(self) -> None:
        if not self.cfg.get("web_enabled", True):
            log("网页排行榜已按配置关闭（web_enabled=false）")
            return
        try:
            import webpanel
            self.web = webpanel.WebPanel(self.db, self.cfg, log_fn=log, bot=self)
            self.web.start()
        except Exception as e:
            self.web_error = str(e)
            log(f"网页排行榜启动失败：{e}", "ERROR")

    def web_ready(self) -> bool:
        return bool(self.web and getattr(self.web, "ready", False))

    def personal_web_url(self, uid: int) -> str:
        if not self.web_ready():
            return ""
        target = None
        if self.db.active_chats():
            for c in self.db.active_chats():
                if self.db.get_link_by_owner(uid, c["chat_id"]):
                    target = c["chat_id"]
                    break
            if target is None:
                target = self.db.active_chats()[0]["chat_id"]
        return self.web.personal_url(uid, target)


    # -- 生命周期 ---------------------------------------------------------
    def stop(self, *_a):
        self._running = False
        if self.web:
            try:
                self.web.stop()
            except Exception:
                pass
        log("收到停止信号，准备退出…")

    def run(self):
        log(f"机器人启动：@{self.bot_username} (id={self.bot_id}) v{VERSION}")
        self.db.set_kv("bot_started_at", str(int(time.time())))
        # 记录隐私模式状态（活跃统计准不准就看这个）
        try:
            me = self.tg.get_me()
            if "can_read_all_group_messages" in me:
                self.db.set_kv("bot_privacy_off", str(bool(me["can_read_all_group_messages"])))
                log("隐私模式：" + ("已关闭（能看到全部群消息）" if me["can_read_all_group_messages"]
                                     else "开启中（只能统计命令/@消息，活跃数据会偏少）"))
        except TelegramError:
            pass
        try:
            self.tg.call("deleteWebhook", {"drop_pending_updates": False}, retries=1)
        except TelegramError as e:
            log(f"deleteWebhook 失败（忽略）：{e}", "WARN")
        chats = self.db.active_chats()
        if chats:
            log("已绑定群：" + ", ".join(f"{c['title']}({c['chat_id']})" for c in chats))
        else:
            log("尚未绑定任何群：把机器人拉进群并设为管理员后，在群里发 /bind 即可", "WARN")
        self.start_web_panel()
        offset = self.db.get_offset()
        last_tick = 0.0
        while self._running:
            try:
                updates = self.tg.get_updates(offset, timeout=int(self.cfg.get("poll_timeout", 50)))
            except TelegramError as e:
                if e.is_conflict:
                    log("409 Conflict：可能还有另一个实例在跑，10 秒后重试", "ERROR")
                    time.sleep(10)
                    continue
                log(f"getUpdates 失败：{e}", "ERROR")
                time.sleep(5)
                continue
            except Exception as e:
                log(f"getUpdates 网络异常：{e}", "ERROR")
                time.sleep(5)
                continue
            for upd in updates:
                new_offset = int(upd["update_id"]) + 1
                try:
                    self.handle_update(upd)
                except TelegramError as e:
                    log(f"处理更新 {upd['update_id']} 时 API 报错：{e}", "ERROR")
                except Exception:
                    log("处理更新异常：\n" + traceback.format_exc(), "ERROR")
                offset = new_offset
                self.db.set_offset(offset)
            # 每分钟跑一次后台任务（心跳 / 自动备份 / 周报）
            if time.time() - last_tick > 60:
                last_tick = time.time()
                try:
                    self.tick()
                except Exception:
                    log("定时任务异常：\n" + traceback.format_exc(), "ERROR")
        log("机器人已停止")

    # -- 权限 -------------------------------------------------------------
    def is_admin(self, user_id: int) -> bool:
        return user_id in self.owner_ids or self.db.is_admin(user_id)

    def is_manager_for(self, user_id: int, chat_id: int) -> bool:
        """机器人管理员，或在**这个群里有实权**的 Telegram 管理员。

        缓存超过 6 小时必须实时重查 —— 被降职的管理员不能凭旧缓存继续有权限。
        """
        if self.is_admin(user_id):
            return True
        row = self.db.conn.execute(
            "SELECT checked_at FROM group_admins WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)).fetchone()
        if row and self.db.is_admin_of(chat_id, user_id) and \
                int(time.time()) - int(row["checked_at"]) < 6 * 3600:
            return True
        try:
            m = self.tg.get_chat_member(chat_id, user_id)
        except TelegramError:
            return False
        if not m:
            return False
        st = m.get("status") or "left"
        if st == "creator":
            self.db.set_group_admin(chat_id, user_id, st, "")
            return True
        if st == "administrator":
            RIGHT_KEYS = ("can_invite_users", "can_delete_messages", "can_restrict_members",
                          "can_promote_members", "can_pin_messages", "can_manage_chat",
                          "can_change_info", "can_manage_video_chats", "can_manage_topics")
            rights = ",".join(k for k in RIGHT_KEYS if m.get(k) is True)
            self.db.set_group_admin(chat_id, user_id, st, rights)
            return self.db.is_admin_of(chat_id, user_id)
        return False

    def require_admin(self, msg: dict) -> bool:
        uid = msg["from"]["id"]
        if self.is_admin(uid):
            return True
        return False

    def chat_is_bound(self, chat_id: int) -> bool:
        return any(c["chat_id"] == chat_id for c in self.db.active_chats())

    # -- 成员检查 ---------------------------------------------------------
    def member_status_cached(self, chat_id: int, user_id: int):
        key = (chat_id, user_id)
        hit = self._member_cache.get(key)
        now = time.time()
        if hit and now - hit[0] < 120:
            return hit[1]
        member = self.tg.get_chat_member(chat_id, user_id)
        # 防内存泄漏：超过 2000 条就清掉过期项
        if len(self._member_cache) > 2000:
            self._member_cache = {k: v for k, v in self._member_cache.items() if now - v[0] < 120}
            if len(self._member_cache) > 4000:
                self._member_cache.clear()
        self._member_cache[key] = (now, member)
        return member

    # -- 更新分发 ---------------------------------------------------------
    def handle_update(self, upd: dict) -> None:
        if "message" in upd:
            self.on_message(upd["message"])
        elif "edited_message" in upd:
            pass
        elif "callback_query" in upd:
            self.on_callback(upd["callback_query"])
        elif "chat_member" in upd:
            self.on_chat_member(upd["chat_member"])
        elif "chat_join_request" in upd:
            self.on_join_request(upd["chat_join_request"])
        elif "my_chat_member" in upd:
            self.on_my_chat_member(upd["my_chat_member"])

    # -- 消息 -------------------------------------------------------------
    def on_message(self, msg: dict) -> None:
        chat = msg.get("chat", {})
        user = msg.get("from") or {}
        text = msg.get("text") or msg.get("caption") or ""
        chat_type = chat.get("type")
        if user and not user.get("is_bot"):
            self.db.touch_user(user)
        if chat_type in ("group", "supergroup"):
            if not self.chat_is_bound(chat["id"]):
                # 自动绑定：机器人被拉进新群/设为管理员就自动接入（多群）
                if self.setting("auto_bind_enabled", True):
                    self.db.bind_chat(chat["id"], chat.get("title") or str(chat["id"]))
                    self.audit(None, "auto_bind", target=str(chat["id"]), chat_id=chat["id"],
                               detail=chat.get("title") or "")
                    log(f"自动绑定群：{chat.get('title')} ({chat['id']})")
                else:
                    return
            # 活跃度统计（只记数量和时间，不存内容；机器人消息不计）
            if (not text.startswith("/")) and user and not user.get("is_bot") \
                    and self.setting("activity_enabled", True):
                self.db.bump_activity(chat["id"], user["id"], msg.get("date"))
        if not text.startswith("/"):
            # 管理员正在输入欢迎语模板
            if self._awaiting.get(user.get("id")) == "welcome" and chat_type == "private":
                self._awaiting.pop(user.get("id"), None)
                self.db.set_kv("welcome_text", text[:900])
                self.audit(user, "set_welcome", detail=text[:80])
                self.reply(msg, "✅ 入群欢迎语已更新。")
            return
        args = text.split()
        cmd = args[0].split("@")[0].lower()
        self.dispatch_command(cmd, args[1:], msg)

    def reply(self, msg: dict, text: str, **kw):
        return self.tg.send_message(msg["chat"]["id"], text,
                                    reply_to_message_id=msg.get("message_id"), **kw)

    def dispatch_command(self, cmd: str, args: list, msg: dict) -> None:
        chat = msg["chat"]
        user = msg["from"]
        in_group = chat.get("type") in ("group", "supergroup")

        if cmd in ("/start", "/help"):
            self.cmd_start(msg, args)
        elif cmd in ("/link", "/mylink", "/invite"):
            self.cmd_link(msg)
        elif cmd in ("/stats", "/mystats", "/me"):
            self.cmd_stats(msg)
        elif cmd in ("/top", "/rank", "/leaderboard"):
            self.cmd_top(msg, page=self._int_arg(args, 0, 0))
        elif cmd in ("/list", "/myinvitees", "/invitees"):
            self.cmd_list(msg, page=self._int_arg(args, 0, 0))
        elif cmd == "/bind":
            self.cmd_bind(msg, args)
        elif cmd == "/unbind":
            self.cmd_unbind(msg)
        elif cmd == "/panel":
            self.cmd_panel(msg)
        elif cmd == "/groupinfo":
            self.cmd_groupinfo(msg)
        elif cmd == "/export":
            self.cmd_export(msg)
        elif cmd == "/admins":
            self.cmd_admins(msg)
        elif cmd in ("/addadmin", "/deladmin"):
            self.cmd_admin_toggle(msg, args, add=(cmd == "/addadmin"))
        elif cmd == "/setwelcome":
            self.cmd_setwelcome(msg, args)
        elif cmd == "/broadcast":
            self.cmd_broadcast(msg, args)
        elif cmd == "/logchat":
            self.cmd_logchat(msg)
        elif cmd in ("/reset", "/clear"):
            self.cmd_reset(msg)
        elif cmd in ("/logs", "/audit"):
            self.cmd_logs(msg, page=self._int_arg(args, 0, 0),
                          action=(args[1] if len(args) > 1 else None))
        elif cmd in ("/retention", "/liucun"):
            self.cmd_retention(msg)
        elif cmd in ("/quality", "/zhiliang"):
            self.cmd_quality(msg)
        elif cmd == "/funnel":
            self.cmd_funnel(msg)
        elif cmd in ("/web", "/mypage"):
            self.cmd_web(msg)
        elif cmd == "/site":
            self.cmd_site(msg)
        elif cmd in ("/active", "/activity"):
            self.cmd_active(msg)
        elif cmd in ("/groups", "/grouplist", "/switch", "/sg"):
            self.cmd_groups(msg) if cmd in ("/groups", "/grouplist") else self.cmd_switch(msg)
        elif cmd in ("/declineall", "/rejectall", "/denyall"):
            self.cmd_decline_all(msg, args)
        elif cmd in ("/jm", "/jm2"):
            self.cmd_jm(msg, args)
        elif cmd in ("/manage", "/panel-admin", "/admin"):
            self.cmd_manage(msg, args)
        elif cmd in ("/cleanup", "/clean"):
            self.cmd_cleanup(msg, args)
        elif cmd in ("/settings", "/config"):
            self.cmd_settings(msg)
        elif cmd == "/id":
            self.reply(msg, f"chat_id: <code>{chat['id']}</code>\nyour_id: <code>{user['id']}</code>")
        elif cmd == "/version":
            self.reply(msg, f"xiyuer114514_bot v{VERSION}")
        else:
            if not in_group:
                self.reply(msg, "未知命令，发送 /help 查看用法。")

    @staticmethod
    def _int_arg(args: list, idx: int, default: int) -> int:
        try:
            return int(args[idx])
        except Exception:
            return default

    # -- 禁漫下载（/jm） ----------------------------------------------------
    def cmd_jm(self, msg: dict, args: list) -> None:
        if not self.setting("jm_enabled", True):
            self.reply(msg, "📕 下载功能未开启。")
            return
        chat_id = msg["chat"]["id"]
        try:
            import jm_tg
        except ImportError:
            self.reply(msg, "📕 下载模块未安装（jm_tg.py 缺失）。")
            return
        if not args:
            self.reply(msg, jm_tg.help_text())
            return
        sub = args[0].lower()
        # 中英文别名（对齐 QQ 版习惯：随机 / 搜索 / 下载 / 排行 都能用）
        aliases = {
            "dl": "dl", "dlj": "dl", "下载": "dl", "xiazai": "dl",
            "search": "search", "搜索": "search", "sousuo": "search",
            "top": "top", "排行": "top", "榜单": "top", "榜": "top",
            "random": "random", "随机": "random", "rp": "random",
            "version": "version", "版本": "version",
            "progress": "progress", "进度": "progress",
            "about": "about", "查询": "about", "详情": "about", "info": "about",
            "help": "help", "帮助": "help", "?" : "help",
        }
        sub = aliases.get(sub, sub)
        if sub == "help":
            return self.reply(msg, jm_tg.help_text())
        if sub == "about":
            if len(args) < 2:
                return self.reply(msg, "用法：/jm <漫画码>（或 /jm 查询 <漫画码>）")
            self.reply(msg, "🔍 查询中，请稍候…")
            self.submit(self._jm_query, msg["chat"]["id"], "about", args[1:])
            return
        if sub in ("dl", "dlj"):
            if len(args) < 2:
                return self.reply(msg, "用法：/jm dl <漫画码>")
            code = args[1]
            self.submit(self._jm_download, msg["chat"]["id"], code)
            return
        if sub == "version":
            return self.reply(msg, f"📦 禁漫下载 TG 版 {jm_tg.VERSION}（无加密 · 自动分卷 · 断点续传）")
        if sub == "progress":
            if len(args) < 2:
                return self.reply(msg, "用法：/jm progress <漫画码>")
            try:
                st = jm_tg.task_state(args[1])
                return self.reply(msg, f"📊 JM{args[1]}：{st['state']} · 已获 {st['files']} 张"
                                       + (f"\n错误：{st['err']}" if st.get("err") else ""))
            except ValueError:
                return self.reply(msg, "❌ 漫画码格式不正确")
        if sub in ("autodel", "autodelete", "定时删除", "自动删除"):
            if len(args) < 2:
                cur = jm_tg.get_auto_delete()
                return self.reply(msg,
                                  f"🗑 当前自动清理：<b>{jm_tg.AUTO_MODE_CN.get(cur, cur)}</b>\n"
                                  "可用模式：off / immediate / 30m / 5h / 1d\n"
                                  "例如：/jm autodel 1d（1 天后自动删除）\n"
                                  "immediate = 发送成功后立刻删除本地文件")
            try:
                mode = jm_tg.set_auto_delete(args[1])
            except ValueError as e:
                return self.reply(msg, f"❌ {e}")
            self.audit_soft(msg["from"]["id"], "jm_autodel", detail=mode)
            return self.reply(msg, f"✅ 自动清理已设为：<b>{jm_tg.AUTO_MODE_CN.get(mode, mode)}</b>")
        if sub == "random":
            self.reply(msg, "🎲 正在全站随机挑一本…")
            self.submit(self._jm_random, chat_id)
            return
        # 查询 / 搜索 / 排行：全部放线程池，避免阻塞机器人主循环
        self.reply(msg, "🔍 查询中，请稍候…")
        self.submit(self._jm_query, chat_id, sub, args)

    def _jm_download(self, chat_id: int, code: str) -> None:
        import jm_tg
        try:
            # 1) 简介
            info = jm_tg.about(code)
            self.tg.send_message(chat_id, (
                f"📕 <b>{esc(info['title'])}</b>\n"
                f"👤 {esc(str(info['author']))} · 📄 {info['pages']} 页"))
            # 2) 开始下载
            self.tg.send_message(chat_id, f"⬇️ 开始下载 JM{info['id']} …")

            def on_doc(fn, data, cap):
                try:
                    self.tg.send_document(chat_id, fn, data, caption=cap)
                except TelegramError as e:
                    log(f"jm 发送文件失败：{e}", "ERROR")

            def on_result(r):
                if r["ok"]:
                    miss = (r["expected"] - r["files"]) if (r["expected"] and r["files"] < r["expected"]) else 0
                    txt = f"✅ JM{r['code']} 下载完成：{r['files']} 页，共 {len(r['vols'])} 卷"
                    if miss:
                        txt += f"\n⚠️ 缺 {miss} 页，重发 /jm dl {r['code']} 可自动补全"
                    self.tg.send_message(chat_id, txt)
                else:
                    self.tg.send_message(chat_id, f"❌ 下载失败：{r['error'][:300]}")

            jm_tg.download(code, title=info["title"], on_doc=on_doc, on_result=on_result)
        except Exception as e:
            log(f"jm 下载异常：{e}\n{traceback.format_exc()}", "ERROR")
            try:
                self.tg.send_message(chat_id, f"❌ 下载失败：{str(e)[:200]}")
            except TelegramError:
                pass

    def _jm_random(self, chat_id: int) -> None:
        import jm_tg
        try:
            code, name = jm_tg.random_album()
            self.tg.send_message(chat_id, f"🎲 随机选中：JM{code} · {name}\n⬇️ 自动开始下载…")
            self._jm_download(chat_id, code)
        except Exception as e:
            log(f"jm 随机异常：{e}", "ERROR")
            try:
                self.tg.send_message(chat_id, f"❌ 随机获取失败：{str(e)[:200]}")
            except TelegramError:
                pass

    def _jm_query(self, chat_id: int, sub: str, args: list) -> None:
        import jm_tg
        try:
            if sub in ("about",):
                info = jm_tg.about(args[0])
                tags = "、".join(info["tags"][:10]) or "—"
                self.tg.send_message(chat_id, (
                    f"📕 <b>JM{info['id']}</b>\n"
                    f"📌 标题：{esc(info['title'])}\n"
                    f"👤 作者：{esc(str(info['author']))}\n"
                    f"📄 页数：{info['pages']}\n"
                    f"🏷️ 标签：{esc(tags)}\n"
                    f"💡 下载：/jm dl {info['id']}"))
            elif sub == "search":
                if len(args) < 2:
                    self.tg.send_message(chat_id, "用法：/jm search <关键词> [页码]")
                    return
                page = 1
                parts = args[1:]
                if parts and parts[-1].isdigit() and len(parts) > 1:
                    page = int(parts[-1])
                    parts = parts[:-1]
                kw = " ".join(parts)
                items = jm_tg.search(kw, page)
                if not items:
                    self.tg.send_message(chat_id, f"🔍 未找到「{esc(kw)}」相关漫画")
                    return
                lines = [f"🔍 搜索「{esc(kw)}」第 {page} 页："]
                for it in items[:15]:
                    lines.append(f"  {it['id']} - {esc(str(it['title'])[:40])}")
                lines.append("💡 下载：/jm dl <漫画码>")
                self.tg.send_message(chat_id, "\n".join(lines))
            elif sub == "top":
                cat_map = {"日榜": "day", "周榜": "week", "月榜": "month"}
                cat = args[1] if len(args) > 1 else "日榜"
                kind = cat_map.get(cat, cat if cat in ("day", "week", "month") else "day")
                page = int(args[-1]) if len(args) > 2 and args[-1].isdigit() else 1
                items = jm_tg.top(kind, page)
                if not items:
                    self.tg.send_message(chat_id, "🏆 暂无数据")
                    return
                lines = [f"🏆 {cat}榜 第 {page} 页："]
                for i, it in enumerate(items[:15], 1):
                    lines.append(f"  {i}. {it['id']} - {esc(str(it['title'])[:40])}")
                lines.append("💡 下载：/jm dl <漫画码>")
                self.tg.send_message(chat_id, "\n".join(lines))
            else:
                info = jm_tg.about(args[0])
                self.tg.send_message(chat_id, (
                    f"📕 <b>JM{info['id']}</b>\n"
                    f"📌 标题：{esc(info['title'])}\n"
                    f"👤 作者：{esc(str(info['author']))}\n"
                    f"📄 页数：{info['pages']}\n"
                    f"💡 下载：/jm dl {info['id']}"))
        except Exception as e:
            log(f"jm 查询异常：{e}", "ERROR")
            try:
                self.tg.send_message(chat_id, f"❌ 操作失败：{str(e)[:200]}")
            except TelegramError:
                pass

    # -- 多群：当前命令作用在哪个群 ----------------------------------------
    def cmd_target_chat(self, msg: dict):
        """群里发的命令 → 就作用在本群；私聊 → 用户选中的群 → 第一个群。

        多群下不会串数据：所有统计都按 chat_id 隔离。
        """
        chat = msg.get("chat") or {}
        if chat.get("type") in ("group", "supergroup"):
            return self.db.get_chat(chat["id"]) if self.chat_is_bound(chat["id"]) else None
        sel = self.db.get_kv(f"usel:{msg['from']['id']}")
        if sel and str(sel).lstrip("-").isdigit() and self.chat_is_bound(int(sel)):
            return self.db.get_chat(int(sel))
        return self.db.main_chat()

    def cmd_groups(self, msg: dict) -> None:
        chats = self.db.active_chats()
        if not chats:
            self.reply(msg, "还没有绑定任何群。把机器人设为群管理员后，在群里发 /bind。")
            return
        cur = self.cmd_target_chat(msg)
        lines = [f"📚 <b>已绑定 {len(chats)} 个群</b>", ""]
        for c in chats:
            t = self.db.group_totals(c["chat_id"])
            mark = "👉 " if cur and c["chat_id"] == cur["chat_id"] else "• "
            lines.append(f"{mark}<b>{esc(c['title'])}</b>\n"
                         f"　　<code>{c['chat_id']}</code> · 邀请 {t['invited']} 人 / 在群 {t['present']} 人")
        lines += ["", "私聊里用 <code>/switch</code> 切换当前群；群里发的命令默认作用于本群。"]
        markup = None
        if len(chats) > 1:
            rows = [[{"text": ("✅ " if cur and c["chat_id"] == cur["chat_id"] else "") + c["title"][:28],
                      "callback_data": f"sw:{c['chat_id']}"}] for c in chats[:10]]
            markup = {"inline_keyboard": rows}
        self.tg.send_message(msg["chat"]["id"], "\n".join(lines), reply_markup=markup,
                             reply_to_message_id=msg.get("message_id"))

    def cmd_switch(self, msg: dict) -> None:
        chats = self.db.active_chats()
        if not chats:
            self.reply(msg, "还没有绑定任何群。")
            return
        cur = self.cmd_target_chat(msg)
        rows = [[{"text": ("✅ " if cur and c["chat_id"] == cur["chat_id"] else "") + c["title"][:28],
                  "callback_data": f"sw:{c['chat_id']}"}] for c in chats[:12]]
        self.tg.send_message(msg["chat"]["id"],
                             "🔀 <b>选择要查看的群</b>\n（只影响私聊里的命令；群里发命令始终作用于本群）",
                             reply_markup={"inline_keyboard": rows})

    # -- /start -----------------------------------------------------------
    def cmd_start(self, msg: dict, args: list) -> None:
        user = msg["from"]
        uid = user["id"]
        chat = msg["chat"]
        # 首次使用者自动成为管理员（新机器人常见做法，可被 config.owner_ids 覆盖）
        if self.db.admin_count() == 0 and not self.owner_ids:
            self.db.touch_user(user, is_admin=True)
            log(f"首次启动者 {uid} 成为管理员", "INFO")
        payload = args[0] if args else ""
        if payload.startswith("lg_"):
            return self.confirm_web_login(msg, payload[3:])
        if payload.startswith("bd_"):
            return self.bind_web_account(msg, payload[3:])
        if payload.startswith("link"):
            self.cmd_link(msg)
            return
        if chat.get("type") != "private":
            self.reply(msg, "私聊我才能领取你的专属邀请链接哦 → " + self.bot_link())
            return
        bound = self.cmd_target_chat(msg)
        who = display_name(user)
        lines = [
            f"👋 你好，{who}！我是 <b>群邀请统计机器人</b>。",
            "",
            "我的工作很简单：",
            "1️⃣ 给你一条<b>只属于你的</b>群邀请链接",
            "2️⃣ 有人通过它进群，我就记到你的名下",
            "3️⃣ 随时用 /stats 看战绩，用 /top 看排行榜",
            "",
        ]
        if bound:
            lines.append(f"当前群组：<b>{esc(bound['title'])}</b>")
        else:
            lines.append("⚠️ 还没有绑定群组：把机器人拉进群 → 设为管理员 → 在群里发 <code>/bind</code>")
        lines += ["", "点下面的按钮开始 👇"]
        self.tg.send_message(chat["id"], "\n".join(lines), reply_markup=self.main_menu())

    def bot_link(self, payload: str = "") -> str:
        return f"https://t.me/{self.bot_username}" + (f"?start={payload}" if payload else "")

    # -- 网页账号：Telegram 登录 / 绑定 ------------------------------------
    def get_avatar_bytes(self, uid: int) -> bytes | None:
        """取用户头像 JPEG 字节（网页头像代理用；失败返回 None）。"""
        try:
            photos = self.tg.call("getUserProfilePhotos", {"user_id": uid, "limit": 1})
        except TelegramError:
            return None
        items = (photos or {}).get("photos") or []
        if not items:
            return None
        try:
            smallest = items[0][0]          # 最小尺寸，足够做 34px 头像
            fobj = self.tg.call("getFile", {"file_id": smallest["file_id"]})
            token = self.cfg.get("bot_token") or ""
            url = f"https://api.telegram.org/file/bot{token}/{fobj['file_path']}"
            req = urllib.request.Request(url, headers={"User-Agent": "invite-bot-avatar"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read(256 * 1024)
        except Exception:
            return None

    def refresh_user_admins(self, user: dict) -> None:
        """把这个人在各个群的**身份和权限**刷新到缓存（网页据此判断能看/能管哪些群）。"""
        uid = user.get("id")
        if not uid:
            return
        RIGHT_KEYS = ("can_invite_users", "can_delete_messages", "can_restrict_members",
                      "can_promote_members", "can_pin_messages", "can_manage_chat",
                      "can_change_info", "can_manage_video_chats", "can_manage_topics")
        for c in self.db.active_chats():
            try:
                m = self.tg.get_chat_member(c["chat_id"], uid)
            except TelegramError:
                continue
            if not m:
                continue
            st = m.get("status") or "left"
            if st == "restricted" and not m.get("is_member"):
                st = "left"
            rights = ",".join(k for k in RIGHT_KEYS if m.get(k) is True) if st == "administrator" else ""
            self.db.set_group_admin(c["chat_id"], uid, st, rights)
        self.db.touch_user(user)

    # 兼容旧名
    refresh_user_groups = refresh_user_admins

    # -- 邀请链接申请（普通成员不能自己生成） --------------------------------
    def notify_login(self, tg_user_id: int, ip: str, ua: str = "") -> None:
        """有人登录网页账号 → 私聊本人（不是自己操作就能第一时间发现）。"""
        if not tg_user_id:
            return
        try:
            self.tg.send_message(tg_user_id, (
                "🔔 <b>你的账号刚刚登录了网页后台</b>\n\n"
                f"IP：<code>{esc(ip)}</code>\n"
                f"设备：{esc((ua or '未知')[:80])}\n"
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "<i>如果这不是你本人操作，请立刻在「我的」里改密码，"
                "并让管理员禁用该账号。</i>"), disable_notification=True)
        except TelegramError:
            pass

    def notify_link_request(self, chat_id: int, user_id: int, name: str) -> None:
        base = ""
        try:
            base = (self.personal_web_url(user_id) or "").split("/u/")[0]
        except Exception:
            base = ""
        link = f"{base}/linkreq" if base else "网页后台 → 链接申请"
        for admin in set(self.db.all_admin_ids()) | set(self.owner_ids):
            try:
                self.tg.send_message(admin,
                                     f"🔔 <b>有人申请专属邀请链接</b>\n\n"
                                     f"👤 {esc(name)}（<code>{user_id}</code>）\n"
                                     f"👥 群：<code>{chat_id}</code>\n\n"
                                     f"去处理：{esc(link)}",
                                     disable_notification=True)
            except TelegramError:
                pass

    def approve_link_request(self, req_id: int, admin_id: int | None = None):
        req = self.db.conn.execute("SELECT * FROM link_requests WHERE id=?", (req_id,)).fetchone()
        if not req or req["status"] != "pending":
            return False, "申请不存在或已处理"
        chat_id, uid = req["chat_id"], req["user_id"]
        row = self.db.get_link_by_owner(uid, chat_id)
        if not (row and not row["revoked"]):
            if not self.create_personal_link(chat_id, uid):
                return False, "创建链接失败（多半是机器人缺少「邀请用户」权限）"
            row = self.db.get_link_by_owner(uid, chat_id)
        self.db.decide_link_request(req_id, "approved", admin_id or 0, "管理员批准")
        url = row["invite_link"] if row else ""
        try:
            self.tg.send_message(uid, (
                "✅ <b>管理员已批准你的专属邀请链接</b>\n\n"
                f"<code>{esc(url)}</code>\n\n"
                "把它分享出去，有人通过它进群就会记到你的名下。用 /stats 看战绩。"))
        except TelegramError:
            pass
        self.db.record_audit(admin_id, None, "link_request_approve", target=str(uid),
                             chat_id=chat_id, detail=f"批准申请 #{req_id}")
        log(f"管理员批准了 {uid} 的邀请链接申请 #{req_id}")
        return True, url

    def reject_link_request(self, req_id: int, admin_id: int | None = None, note: str = "") -> bool:
        req = self.db.conn.execute("SELECT * FROM link_requests WHERE id=?", (req_id,)).fetchone()
        if not req or req["status"] != "pending":
            return False
        self.db.decide_link_request(req_id, "rejected", admin_id or 0, note or "管理员驳回")
        try:
            self.tg.send_message(req["user_id"],
                                 f"❌ 你的专属邀请链接申请未通过。{('原因：' + esc(note)) if note else ''}")
        except TelegramError:
            pass
        self.db.record_audit(admin_id, None, "link_request_reject", target=str(req["user_id"]),
                             chat_id=req["chat_id"], detail=f"驳回申请 #{req_id}")
        return True

    def confirm_web_login(self, msg: dict, nonce: str) -> None:
        user = msg["from"]
        ok = self.db.confirm_login_nonce(nonce, user["id"], user.get("first_name") or "",
                                         user.get("username"))
        chat_id = msg["chat"]["id"]
        if not ok:
            self.tg.send_message(chat_id, "⌛️ 这个登录链接已失效或已被使用，请回网页重新发起登录。")
            return
        self.refresh_user_admins(user)
        self.audit(user, "web_login_confirm", target=str(user["id"]),
                   detail="Telegram 确认网页登录")
        log(f"网页登录已由 Telegram 用户 {user['id']} 确认")
        code = ""
        try:
            row = self.db.get_login_nonce(nonce)
            code = (row["code"] if row and "code" in row.keys() else "") or ""
        except Exception:
            code = ""
        if code:
            self.tg.send_message(chat_id, (
                "✅ <b>登录已确认</b>\n\n"
                f"网页确认码：<code>{code}</code>\n\n"
                "回到浏览器：页面会自动进入；如果没反应，就把上面这个 6 位码填进「确认码」框点提交。\n\n"
                "<i>不是你本人操作就忽略——别人拿不到你的 Telegram 就登不进去。</i>"))
        else:
            self.tg.send_message(chat_id, (
                "✅ <b>登录已确认</b>\n\n请回到浏览器，页面会自动进入"
                "（没反应就点页面上的「我已确认」按钮）。\n\n"
                "<i>如果不是你本人操作，请忽略——别人拿不到你的 Telegram 就登不进去。</i>"))

    def bind_web_account(self, msg: dict, nonce: str) -> None:
        user = msg["from"]
        acc_id = self.db.get_kv(f"bindacc:{nonce}")
        chat_id = msg["chat"]["id"]
        if not acc_id or not acc_id.isdigit():
            self.tg.send_message(chat_id, "⌛️ 绑定链接已失效，请回网页重新发起。")
            return
        acc = self.db.get_account(int(acc_id))
        if not acc:
            self.tg.send_message(chat_id, "⌛️ 账号不存在，请回网页重新发起。")
            return
        other = self.db.get_account_by_tg(user["id"])
        if other and other["id"] != acc["id"]:
            self.tg.send_message(chat_id, "⚠️ 这个 Telegram 已经绑定了另一个账号，"
                                          "请先在那边解绑或联系管理员。")
            return
        self.db.link_telegram(acc["id"], user["id"], user.get("first_name"))
        self.db.set_kv(f"bindacc:{nonce}", "")
        self.refresh_user_admins(user)
        self.audit(user, "account_bind_tg", target=str(acc["id"]), detail="绑定 Telegram")
        log(f"账号 #{acc['id']} 已绑定 Telegram {user['id']}")
        self.tg.send_message(chat_id, (
            f"✅ <b>绑定成功</b>\n\n账号 <code>#{acc['id']}</code> 已和你的 Telegram 绑定，"
            f"以后可以在网页上点「用 Telegram 登录」直接进。"))


    def main_menu(self):
        return {"inline_keyboard": [
            [{"text": "🔗 领取我的专属邀请链接", "callback_data": "mylink"}],
            [{"text": "📊 我的战绩", "callback_data": "mystats"},
             {"text": "🏆 排行榜", "callback_data": "top:0"}],
            [{"text": "👥 我邀请的人", "callback_data": "list:0"},
             {"text": "❓ 使用帮助", "callback_data": "help"}],
        ]}

    # -- /link ------------------------------------------------------------
    def cmd_link(self, msg: dict) -> None:
        chat = msg["chat"]
        user = msg["from"]
        uid = user["id"]
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "⚠️ 机器人还没有绑定群组。\n请把机器人拉进群并设为管理员，然后在群里发 <code>/bind</code>。")
            return
        gid = bound["chat_id"]

        # 只给群成员发链接，防止外人蹭链接刷量
        member = self.member_status_cached(gid, uid)
        if not is_member_status(member):
            self.reply(msg, "❌ 你还没有加入群组，先加入后才能领取专属邀请链接。")
            return

        row = self.db.get_link_by_owner(uid, gid)
        if row and not row["revoked"]:
            link = row["invite_link"]
        else:
            link = self.create_personal_link(gid, uid)
            if not link:
                kind, detail = getattr(self, "_last_link_error", ("other", ""))
                if kind == "perm":
                    self.reply(msg, (
                        "❌ 我还不能创建邀请链接：机器人缺少 <b>「邀请用户」</b> 权限。\n\n"
                        "请管理员到群里操作：<b>管理群 → 管理员 → 选中机器人 → 勾选「邀请用户」</b>，"
                        "然后重新发 /link。\n（已经私聊提醒过管理员了）"))
                elif kind == "limit":
                    self.reply(msg, "😖 邀请链接数量达到上限，自动回收闲置链接后仍失败，请稍后再试。")
                else:
                    self.reply(msg, f"😖 创建邀请链接失败：{esc(detail)[:150]}")
                return

        invited = self.db.count_for(uid, gid)
        share_text = (self.db.get_kv("share_text") or self.cfg["share_text"]).format(chat=bound["title"])
        share_url = ("https://t.me/share/url?url=" + urllib.parse.quote(link, safe="")
                     + "&text=" + urllib.parse.quote(share_text, safe=""))
        text = (
            f"🔗 这是你在 <b>{esc(bound['title'])}</b> 的专属邀请链接：\n\n"
            f"<code>{esc(link)}</code>\n\n"
            f"目前你已邀请 <b>{invited}</b> 人。\n"
            f"把链接分享出去，有人进群我就自动记到你的名下 ✅"
        )
        markup = {"inline_keyboard": [
            [{"text": "📤 分享给好友", "url": share_url}],
            [{"text": "📊 我的战绩", "callback_data": "mystats"},
             {"text": "🏆 排行榜", "callback_data": "top:0"}],
        ]}
        if chat["type"] == "private":
            self.tg.send_message(chat["id"], text, reply_markup=markup)
        else:
            self.tg.send_message(uid, text, reply_markup=markup)
            self.reply(msg, f"✅ 已把专属链接私发给你 → {mention(uid, display_name(user))}")

    def create_personal_link(self, chat_id: int, owner_id: int) -> str | None:
        name = f"{self.cfg.get('link_name_prefix', 'ref_')}{owner_id}"
        attempts = 0
        while attempts < 4:
            attempts += 1
            try:
                res = self.tg.call("createChatInviteLink", {
                    "chat_id": chat_id,
                    "name": name[:32],
                }, retries=1)
                link = res.get("invite_link")
                if link:
                    self.db.add_link(link, owner_id, name, chat_id)
                    self.db.event("link_created", chat_id, owner_id, link)
                    log(f"为 {owner_id} 创建邀请链接：{link}")
                    return link
                return None
            except TelegramError as e:
                # 1) 权限不足（最常见）：必须排在"链接"关键词判断前面，
                #    因为 "not enough rights to manage chat invite link" 里也含 "invite link"
                if e.has("not enough rights", "chat_admin_required", "administrator",
                         "can_invite_users", "not enough rights to manage chat invite"):
                    self._last_link_error = ("perm", e.description)
                    log(f"机器人在群 {chat_id} 权限不足，无法创建邀请链接：{e}", "ERROR")
                    self._warn_link_permission(chat_id, e.description)
                    return None
                # 2) 邀请链接数量达到上限 → 回收闲置链接后重试
                if e.has("too many invite links", "invite_link_limit", "invite link limit",
                         "too many links"):
                    log(f"邀请链接数量达到上限，回收闲置链接（第 {attempts} 次）", "WARN")
                    recycled = 0
                    for row in self.db.idle_links(int(self.cfg.get("recycle_batch", 25))):
                        try:
                            self.tg.call("revokeChatInviteLink", {
                                "chat_id": chat_id, "invite_link": row["invite_link"]}, retries=1)
                            self.db.revoke_link_row(row["invite_link"])
                            recycled += 1
                        except TelegramError:
                            pass
                    if recycled == 0:
                        self._last_link_error = ("limit", e.description)
                        log(f"创建链接失败且无可回收链接：{e}", "ERROR")
                        return None
                    self.audit(None, "recycle_links", target=str(chat_id),
                               chat_id=chat_id, detail=f"回收 {recycled} 条闲置链接")
                    continue
                self._last_link_error = ("other", e.description)
                log(f"创建邀请链接失败：{e}", "ERROR")
                return None
        return None

    def _warn_link_permission(self, chat_id: int, detail: str) -> None:
        """权限不足时提醒管理员一次（每小时最多一条，避免刷屏）。"""
        now = int(time.time())
        last = int(self.db.get_kv("link_perm_warn_at", "0") or 0)
        if now - last < 3600:
            return
        self.db.set_kv("link_perm_warn_at", str(now))
        self._notify_admins(
            f"⚠️ <b>机器人缺少「邀请用户」权限</b>，现在没法创建专属邀请链接。\n\n"
            f"请到群里：管理群 → 管理员 → 选中我 → 勾选 <b>邀请用户（Invite Users）</b>。\n"
            f"群里 ID：<code>{chat_id}</code>\n"
            f"错误详情：{esc(detail[:160])}")

    # -- /stats -----------------------------------------------------------
    def cmd_stats(self, msg: dict) -> None:
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。")
            return
        gid = bound["chat_id"]
        uid = msg["from"]["id"]
        self.db.touch_user(msg["from"])
        invited = self.db.count_for(uid, gid)
        present = self.db.current_for(uid, gid)
        pending = self.db.pending_for(uid, gid)
        rank = self.db.rank_of(uid, gid)
        size = self.db.leaderboard_size(gid)
        totals = self.db.group_totals(gid)
        row = self.db.get_link_by_owner(uid, gid)
        text = (
            f"📊 <b>{display_name(msg['from'])}</b> 的战绩\n\n"
            f"🏆 累计邀请：<b>{invited}</b> 人\n"
            f"👥 仍在群：<b>{present}</b> 人\n"
        )
        if pending:
            text += f"⏳ 待审批：<b>{pending}</b> 人\n"
        if rank:
            text += f"📈 排名：第 <b>{rank}</b> / {size} 名\n"
        if row and not row["revoked"]:
            text += f"\n🔗 你的链接：<code>{esc(row['invite_link'])}</code>"
        else:
            text += "\n\n还没领链接？点 /link 领取。"
        text += (f"\n\n—— 群数据 ——\n"
                 f"总邀请 <b>{totals['invited']}</b> 人 ｜ 在群 <b>{totals['present']}</b> 人 ｜ "
                 f"参与人数 <b>{totals['inviters']}</b>\n"
                 f"近 24 小时新增 <b>{totals['last24h']}</b> 人")
        markup = {"inline_keyboard": [
            [{"text": "🔗 我的链接", "callback_data": "mylink"},
             {"text": "🏆 排行榜", "callback_data": "top:0"}],
            [{"text": "👥 我邀请的人", "callback_data": "list:0"}],
        ]}
        self.tg.send_message(msg["chat"]["id"], text, reply_markup=markup,
                             reply_to_message_id=msg.get("message_id"))

    # -- /top -------------------------------------------------------------
    def cmd_top(self, msg: dict, page: int = 0, edit_message_id: int | None = None) -> None:
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。")
            return
        gid = bound["chat_id"]
        per = 10
        page = max(0, page)
        rows = self.db.leaderboard(gid, limit=per, offset=page * per)
        total = self.db.leaderboard_size(gid)
        pages = max(1, (total + per - 1) // per)
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"🏆 <b>{esc(bound['title'])}</b> 邀请排行榜",
                 f"（共 {total} 位参与者 · 第 {page + 1}/{pages} 页）", ""]
        if not rows:
            lines.append("还没有人通过专属链接邀请成功，快来抢第一！")
        for i, r in enumerate(rows):
            n = page * per + i + 1
            badge = medals[n - 1] if n <= 3 else f"{n}."
            u = self.db.get_user(r["inviter_id"])
            name = display_name(None, r["inviter_id"], (u["first_name"] if u else "") or None)
            lines.append(f"{badge} {name} — <b>{r['invited']}</b> 人（在群 {r['present']}）")
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ 上一页", "callback_data": f"top:{page - 1}"})
        nav.append({"text": "🔄 刷新", "callback_data": f"top:{page}"})
        if page + 1 < pages:
            nav.append({"text": "下一页 ➡️", "callback_data": f"top:{page + 1}"})
        markup = {"inline_keyboard": [nav, [{"text": "🔗 领我的链接", "callback_data": "mylink"}]]}
        text = "\n".join(lines)
        if edit_message_id:
            try:
                self.tg.edit_message_text(msg["chat"]["id"], edit_message_id, text, reply_markup=markup)
                return
            except TelegramError:
                pass
        self.tg.send_message(msg["chat"]["id"], text, reply_markup=markup,
                             reply_to_message_id=msg.get("message_id"))

    # -- /list ------------------------------------------------------------
    def cmd_list(self, msg: dict, page: int = 0, edit_message_id: int | None = None) -> None:
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。")
            return
        gid = bound["chat_id"]
        uid = msg["from"]["id"]
        per = 20
        page = max(0, page)
        rows = self.db.invitees_of(uid, gid, limit=per, offset=page * per)
        invited = self.db.count_for(uid, gid)
        pages = max(1, (invited + per - 1) // per)
        lines = [f"👥 你邀请进群的人（共 {invited} 人 · 第 {page + 1}/{pages} 页）", ""]
        if not rows:
            lines.append("还没有人通过你的链接进群。")
        for i, r in enumerate(rows, start=page * per + 1):
            flag = {"member": "🟢", "left": "⚪", "kicked": "🚫"}.get(r["status"], "❔")
            when = time.strftime("%m-%d %H:%M", time.localtime(r["joined_at"] or 0))
            lines.append(f"{i}. {flag} {display_name(None, r['invitee_id'], r['invitee_name'])} · {when}")
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ 上一页", "callback_data": f"list:{page - 1}"})
        nav.append({"text": "🔄 刷新", "callback_data": f"list:{page}"})
        if page + 1 < pages:
            nav.append({"text": "下一页 ➡️", "callback_data": f"list:{page + 1}"})
        markup = {"inline_keyboard": [nav]}
        text = "\n".join(lines)
        if edit_message_id:
            try:
                self.tg.edit_message_text(msg["chat"]["id"], edit_message_id, text, reply_markup=markup)
                return
            except TelegramError:
                pass
        self.tg.send_message(msg["chat"]["id"], text, reply_markup=markup,
                             reply_to_message_id=msg.get("message_id"))

    # -- 审计日志 ---------------------------------------------------------
    ACTION_NAMES = {
        "bind_chat": "绑定群组", "unbind_chat": "解绑群组", "auto_bind": "自动绑定群组",
        "bot_left_chat": "机器人被移出群", "add_admin": "添加管理员", "del_admin": "移除管理员",
        "reset_stats": "清空统计", "set_welcome": "修改欢迎语", "set_logchat": "设置日志会话",
        "post_panel": "发布群面板", "export_csv": "导出CSV", "broadcast": "群发消息",
        "recycle_links": "回收闲置邀请链接", "web_login_ok": "网页登录成功",
        "account_create": "创建账号", "account_admin": "账号管理操作",
        "password_change": "修改密码", "account_bind_tg": "绑定 Telegram",
        "web_login_confirm": "Telegram 确认登录", "email_verified": "邮箱已验证",
        "twofa_enabled": "开启两步验证", "twofa_disabled": "关闭两步验证",
        "twofa_fail": "两步验证失败", "twofa_backup": "重新生成恢复码",
        "web_login_step1": "密码通过（待两步验证）",
        "web_login_fail": "网页登录失败", "link_created": "创建邀请链接",
        "web_start": "网页服务启动",
        "join_request": "收到入群申请", "auto_decline_deleted": "自动拒绝已注销申请",
        "deleted_detected": "发现已注销账号", "cleanup_scan": "扫描已注销账号",
        "cleanup_kick": "移除已注销账号", "setting_change": "修改功能开关",
        "settings_reset": "恢复默认开关",
    }

    def cmd_logs(self, msg: dict, page: int = 0, action: str | None = None) -> None:
        if not self.is_admin(msg["from"]["id"]):
            self.reply(msg, "只有管理员可以查看审计日志。")
            return
        per = 10
        page = max(0, page)
        total = self.db.audit_count(action)
        pages = max(1, (total + per - 1) // per)
        page = min(page, pages - 1)
        rows = self.db.audit_page(limit=per, offset=page * per, action=action)
        lines = [f"📜 <b>审计日志</b>（共 {total} 条 · 第 {page + 1}/{pages} 页）"
                 + (f"\n筛选：<code>{esc(action)}</code>" if action else ""), ""]
        if not rows:
            lines.append("暂无记录。")
        for r in rows:
            ts = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
            who = r["actor_name"] or (f"uid:{r['actor_id']}" if r["actor_id"] else "系统")
            label = self.ACTION_NAMES.get(r["action"], r["action"])
            line = f"• <b>{ts}</b> {esc(who)} — {esc(label)}"
            if r["target"]:
                line += f" → <code>{esc(r['target'])}</code>"
            if r["detail"]:
                line += f"\n   <i>{esc(str(r['detail'])[:120])}</i>"
            lines.append(line)
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ 上一页", "callback_data": f"logs:{page - 1}:{action or ''}"})
        nav.append({"text": "🔄 刷新", "callback_data": f"logs:{page}:{action or ''}"})
        if page + 1 < pages:
            nav.append({"text": "下一页 ➡️", "callback_data": f"logs:{page + 1}:{action or ''}"})
        acts = self.db.audit_actions()[:6]
        act_row = [{"text": self.ACTION_NAMES.get(a, a)[:12], "callback_data": f"logs:0:{a}"} for a in acts]
        keyboard = ([nav] if nav else []) + ([act_row] if act_row else []) + [[
            {"text": "📄 导出全部", "callback_data": "logexport"},
            {"text": "🔄 全部日志", "callback_data": "logs:0:"},
        ]]
        self.tg.send_message(msg["chat"]["id"], "\n".join(chunk_text("\n".join(lines))[0]),
                             reply_markup={"inline_keyboard": keyboard},
                             reply_to_message_id=msg.get("message_id"))

    @staticmethod
    def _csv_safe(v) -> str:
        """防 CSV 公式注入：= + - @ 开头的单元格前面加 '，防止 Excel 当公式执行。"""
        s = "" if v is None else str(v)
        if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + s
        return s

    def export_audit(self, chat_id: int) -> None:
        rows = self.db.audit_page(limit=100000, offset=0)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "actor_id", "actor_name", "action", "action_cn", "target", "chat_id", "detail"])
        for r in rows:
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                        r["actor_id"], self._csv_safe(r["actor_name"]), self._csv_safe(r["action"]),
                        self._csv_safe(self.ACTION_NAMES.get(r["action"], r["action"])),
                        self._csv_safe(r["target"]), r["chat_id"], self._csv_safe(r["detail"])])
        data = ("\ufeff" + buf.getvalue()).encode("utf-8")
        name = f"audit_{time.strftime('%Y%m%d_%H%M')}.csv"
        self.tg.send_document(chat_id, name, data, caption=f"📄 审计日志导出（{len(rows)} 条）")

    # -- 留存 / 质量 ------------------------------------------------------
    def _bar(self, rate: float | None, width: int = 10) -> str:
        if rate is None:
            return "—"
        filled = int(round(rate * width))
        return "█" * filled + "░" * (width - filled)

    def cmd_retention(self, msg: dict) -> None:
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。")
            return
        gid = bound["chat_id"]
        lines = [f"📈 <b>{esc(bound['title'])}</b> 留存分析", ""]
        for d in (1, 3, 7, 14, 30):
            r = self.db.retention_cohort(gid, d)
            pct = f"{r['rate'] * 100:5.1f}%" if r["rate"] is not None else "  —  "
            lines.append(f"第 {d:>2} 天  {self._bar(r['rate'])}  {pct}  ({r['retained']}/{r['matured']} 人)")
        f = self.db.funnel(gid)
        avg = self.db.avg_lifetime_days(gid)
        lines += ["", "🔻 <b>入群漏斗</b>",
                  f"提交入群申请：<b>{f['applied']}</b> 人",
                  f"成功入群：<b>{f['joined']}</b> 人",
                  f"7 日后仍在群：<b>{f['retained_7d']}</b> / {f['matured_7d']} 人（样本已满 7 天）"]
        if avg:
            lines.append(f"平均在群时长：<b>{avg:.1f}</b> 天")
        lines += ["", "<i>口径：加入满 N 天的人里，第 N 天仍有一条在群记录的比例。</i>"]
        self.tg.send_message(msg["chat"]["id"], "\n".join(lines), parse_mode="HTML",
                             reply_to_message_id=msg.get("message_id"))

    def cmd_quality(self, msg: dict) -> None:
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。")
            return
        gid = bound["chat_id"]
        scores = self.db.quality_scores(
            gid, 7, float(self.cfg.get("quality_prior_weight", 5.0)),
            float(self.cfg.get("quality_prior_rate", 0.5)))
        lines = [f"💎 <b>拉新质量榜</b>（7 日留存，样本不足会被拉向 50 分）", ""]
        if not scores:
            lines.append("数据还不够。")
        for i, s in enumerate(scores[:15], 1):
            u = self.db.get_user(s["inviter_id"])
            name = display_name(None, s["inviter_id"], u["first_name"] if u else "")
            rate = f'{s["raw_rate"] * 100:.0f}%' if s["raw_rate"] is not None else "—"
            lines.append(f"{i}. {name} — <b>{s['score']:.1f}</b> 分 "
                         f"（留存 {s['retained']}/{s['matured']} = {rate}，累计 {s['invited']} 人）")
        self.tg.send_message(msg["chat"]["id"], "\n".join(chunk_text("\n".join(lines))[0]),
                             reply_to_message_id=msg.get("message_id"))

    def cmd_funnel(self, msg: dict) -> None:
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。")
            return
        gid = bound["chat_id"]
        f = self.db.funnel(gid)
        churn = self.db.churn_board(gid, 10)
        lines = [f"🔻 <b>入群漏斗</b>（{esc(bound['title'])}）", "",
                 f"提交申请 → <b>{f['applied']}</b> 人",
                 f"成功入群 → <b>{f['joined']}</b> 人",
                 f"7 日留存 → <b>{f['retained_7d']}</b> / {f['matured_7d']} 人", "",
                 "📉 <b>掉人榜</b>"]
        for i, r in enumerate(churn, 1):
            u = self.db.get_user(r["inviter_id"])
            name = display_name(None, r["inviter_id"], u["first_name"] if u else "")
            lines.append(f"{i}. {name} — 流失 <b>{r['churned']}</b> / 累计 {r['invited']}")
        if not churn:
            lines.append("暂无数据。")
        self.tg.send_message(msg["chat"]["id"], "\n".join(chunk_text("\n".join(lines))[0]),
                             reply_to_message_id=msg.get("message_id"))

    # -- 网页 -------------------------------------------------------------
    def cmd_web(self, msg: dict) -> None:
        uid = msg["from"]["id"]
        if not self.web_ready():
            self.reply(msg, "网页排行榜暂时不可用" + (f"：{esc(self.web_error)}" if self.web_error else "（可能未启用或端口被占用）"))
            return
        url = self.personal_web_url(uid)
        text = (f"🌐 <b>我的网页战绩页</b>\n\n<code>{esc(url)}</code>\n\n"
                f"手机浏览器打开就能看到你的邀请链接、二维码和邀请明细，"
                f"这个链接只有你能用（已签名），可以直接转发给朋友看。")
        if msg["chat"].get("type") == "private":
            self.tg.send_message(uid, text)
        else:
            self.tg.send_message(uid, text)
            self.reply(msg, f"✅ 已把网页战绩链接私发给你 → {mention(uid, display_name(msg['from'], uid))}")

    def cmd_site(self, msg: dict) -> None:
        if not self.is_admin(msg["from"]["id"]):
            return
        if not self.web_ready():
            self.reply(msg, "网页排行榜未在运行"
                       + (f"：{esc(self.web_error)}" if self.web_error else "（web_enabled=false 或端口被占用）"))
            return
        pw = self.cfg.get("web_password") or ""
        cfg = self.cfg
        lines = [
            "🌐 <b>网页排行榜状态</b>", "",
            f"站点标题：<b>{esc(self.web.site)}</b>",
            f"地址：<code>{esc(self.web.base_url)}</code>",
            f"监听：<code>{esc(self.web.bind_host)}:{self.web.port}</code>",
            f"访问密码：{'已设置（' + str(len(pw)) + ' 位）' if pw else '⚠️ 未设置，任何人可访问'}",
            f"HTTPS：{'已启用' if (cfg.get('web_tls_cert') and cfg.get('web_tls_key')) else '未启用（HTTP）'}",
            f"域名：<code>{esc(cfg.get('web_base_url') or '未配置，当前用 IP 访问')}</code>",
            "",
            "买域名后改 <code>config.json</code> 里的 <code>web_base_url</code> + "
            "<code>web_tls_cert</code>/<code>web_tls_key</code>，重启即生效，不用改代码。",
        ]
        self.reply(msg, "\n".join(lines))

    # -- 绑定 -------------------------------------------------------------
    def cmd_bind(self, msg: dict, args: list) -> None:
        chat = msg["chat"]
        chat_type = chat.get("type")
        if chat_type not in ("group", "supergroup"):
            if args and args[0].lstrip("-").isdigit() and self.is_admin(msg["from"]["id"]):
                self.db.bind_chat(int(args[0]), f"chat {args[0]}")
                self.audit(msg["from"], "bind_chat", target=str(args[0]), chat_id=int(args[0]),
                           detail="通过私聊命令绑定")
                self.reply(msg, f"✅ 已绑定群 <code>{args[0]}</code>")
            else:
                self.reply(msg, "请在群里发这条命令，或用 <code>/bind -1001234567890</code> 指定群。")
            return
        uid = msg["from"]["id"]
        if not self.is_admin(uid):
            self.reply(msg, "只有机器人管理员可以绑定群组。")
            return
        self.db.bind_chat(chat["id"], chat.get("title") or str(chat["id"]))
        self.db.event("bind", chat["id"], uid)
        self.audit(msg["from"], "bind_chat", target=str(chat["id"]), chat_id=chat["id"],
                   detail=chat.get("title") or "")
        log(f"绑定群 {chat.get('title')} ({chat['id']}) by {uid}")
        self.reply(msg, (
            f"✅ 已绑定本群：<b>{esc(chat.get('title'))}</b>\n"
            f"chat_id: <code>{chat['id']}</code>\n\n"
            "接下来群成员可以：\n"
            "• 在群里发 /link 领专属邀请链接\n"
            "• 在群里发 /top 看排行榜\n"
            "• 私聊我发 /link（推荐，链接会私发）\n\n"
            "用 /panel 可以把「领取链接」面板发到群里。"
        ))

    def cmd_unbind(self, msg: dict) -> None:
        if not self.is_admin(msg["from"]["id"]):
            return
        chat_id = msg["chat"]["id"]
        if msg["chat"].get("type") in ("group", "supergroup"):
            self.db.unbind_chat(chat_id)
            self.audit(msg["from"], "unbind_chat", target=str(chat_id), chat_id=chat_id)
            self.reply(msg, "已解除本群绑定（统计数据保留）。")
        else:
            self.reply(msg, "请在群里使用 /unbind。")

    def cmd_groupinfo(self, msg: dict) -> None:
        lines = ["📌 <b>已绑定群组</b>", ""]
        for c in self.db.active_chats():
            t = self.db.group_totals(c["chat_id"])
            lines.append(f"• <b>{esc(c['title'])}</b>\n  id: <code>{c['chat_id']}</code>\n"
                         f"  邀请 {t['invited']} 人 / 在群 {t['present']} 人 / 参与者 {t['inviters']}")
        if len(lines) == 2:
            lines.append("（无）")
        self.reply(msg, "\n".join(lines))

    # -- 面板 -------------------------------------------------------------
    def cmd_panel(self, msg: dict) -> None:
        chat = msg["chat"]
        if chat.get("type") not in ("group", "supergroup"):
            self.reply(msg, "请在群里使用 /panel。")
            return
        if not self.is_admin(msg["from"]["id"]):
            self.reply(msg, "只有机器人管理员可以发布面板。")
            return
        text = self.db.get_kv("panel_text") or self.cfg["panel_text"]
        markup = {"inline_keyboard": [
            [{"text": "🔗 领取我的专属邀请链接", "url": self.bot_link("link")}],
            [{"text": "🏆 查看排行榜", "url": self.bot_link()}],
        ]}
        self.tg.send_message(chat["id"], text, reply_markup=markup)
        self.audit(msg["from"], "post_panel", target=str(chat["id"]), chat_id=chat["id"])
        self.reply(msg, "✅ 面板已发布。")

    # -- 导出 -------------------------------------------------------------
    def cmd_export(self, msg: dict) -> None:
        if not self.is_admin(msg["from"]["id"]):
            self.reply(msg, "只有管理员可以导出。")
            return
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。")
            return
        gid = bound["chat_id"]
        rows = self.db.all_referrals(gid)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["inviter_id", "inviter_name", "invitee_id", "invitee_name", "status",
                    "joined_at", "left_at", "invite_link"])
        for r in rows:
            inviter = self.db.get_user(r["inviter_id"])
            w.writerow([
                r["inviter_id"], self._csv_safe(inviter["first_name"] if inviter else ""),
                r["invitee_id"], self._csv_safe(r["invitee_name"]),
                r["status"],
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["joined_at"])) if r["joined_at"] else "",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["left_at"])) if r["left_at"] else "",
                self._csv_safe(r["invite_link"]),
            ])
        data = ("\ufeff" + buf.getvalue()).encode("utf-8")
        name = f"invites_{gid}_{time.strftime('%Y%m%d_%H%M')}.csv"
        self.tg.send_document(msg["chat"]["id"], name, data, caption=f"📄 邀请统计导出（{len(rows)} 条）")
        self.audit(msg["from"], "export_csv", target=str(gid), chat_id=gid, detail=f"{len(rows)} 条")

    # -- 管理员管理 -------------------------------------------------------
    def cmd_admins(self, msg: dict) -> None:
        if not self.is_admin(msg["from"]["id"]):
            return
        lines = ["👮 <b>机器人管理员</b>", ""]
        for uid in set(self.db.all_admin_ids()) | set(self.owner_ids):
            u = self.db.get_user(uid)
            lines.append(f"• {display_name(None, uid, u['first_name'] if u else '')} — <code>{uid}</code>")
        lines += ["", "添加：<code>/addadmin 123456789</code> 或 <code>/addadmin @username</code>",
                  "移除：<code>/deladmin 123456789</code>"]
        self.reply(msg, "\n".join(lines))

    def cmd_admin_toggle(self, msg: dict, args: list, add: bool) -> None:
        actor = msg["from"]["id"]
        if not self.is_admin(actor):
            self.reply(msg, "只有管理员可以操作。")
            return
        if not args:
            self.reply(msg, "用法：<code>/addadmin 123456789</code> 或 <code>/addadmin @username</code>")
            return
        target = args[0]
        uid = None
        if target.lstrip("-").isdigit():
            uid = int(target)
        else:
            row = self.db.find_user_by_username(target)
            if row:
                uid = row["user_id"]
        if uid is None:
            self.reply(msg, "找不到这个用户（对方需先给机器人发过消息）。")
            return
        if not add and uid in self.owner_ids:
            self.reply(msg, "config.json 里的 owner 不能被移除。")
            return
        self.db.touch_user({"id": uid, "first_name": "admin"}, is_admin=add)
        self.db.set_admin(uid, add)
        self.audit(msg["from"], "add_admin" if add else "del_admin",
                   target=str(uid), detail=target)
        self.reply(msg, f"{'✅ 已添加' if add else '🗑 已移除'}管理员：<code>{uid}</code>")

    def cmd_setwelcome(self, msg: dict, args: list) -> None:
        if not self.is_admin(msg["from"]["id"]):
            return
        if not args:
            cur = self.db.get_kv("welcome_text") or self.cfg["welcome_text"]
            self._awaiting[msg["from"]["id"]] = "welcome"
            self.reply(msg, "当前欢迎语：\n\n" + esc(cur) +
                       "\n\n可用变量：{invitee} {inviter} {chat} {count}\n"
                       "请直接把新的欢迎语发给我（发 /cancel 取消）。")
            return
        if args[0] == "/cancel":
            self._awaiting.pop(msg["from"]["id"], None)
            self.reply(msg, "已取消。")
            return

    def cmd_logchat(self, msg: dict) -> None:
        if not self.is_admin(msg["from"]["id"]):
            return
        cid = msg["chat"]["id"]
        self.db.set_kv("log_chat_id", cid)
        self.audit(msg["from"], "set_logchat", target=str(cid), chat_id=cid)
        self.reply(msg, f"✅ 日志将发送到本会话 <code>{cid}</code>")

    def cmd_broadcast(self, msg: dict, args: list) -> None:
        if not self.is_admin(msg["from"]["id"]):
            return
        if not args:
            self.reply(msg, "用法：<code>/broadcast 要发送的内容</code>")
            return
        text = msg["text"].split(None, 1)[1] if len(msg["text"].split(None, 1)) > 1 else ""
        if not text:
            self.reply(msg, "内容为空。")
            return
        self.reply(msg, "📣 开始广播…")
        self.audit(msg["from"], "broadcast", detail=text[:100])
        self.submit(self._broadcast_worker, text)

    def _broadcast_worker(self, text: str) -> None:
        ok = fail = 0
        for uid in self.db.all_user_ids():
            try:
                self.tg.send_message(uid, text)
                ok += 1
            except TelegramError:
                fail += 1
            time.sleep(0.05)
        log(f"广播完成：成功 {ok} 失败 {fail}")
        for admin in set(self.db.all_admin_ids()) | set(self.owner_ids):
            try:
                self.tg.send_message(admin, f"📣 广播完成：成功 {ok}，失败 {fail}")
            except TelegramError:
                pass

    def cmd_reset(self, msg: dict) -> None:
        if not self.is_admin(msg["from"]["id"]):
            return
        target = self.cmd_target_chat(msg)
        name = esc(target["title"]) if target else "（未选定群）"
        self.reply(msg, f"⚠️ 这会清空 <b>{name}</b> 的<b>全部</b>邀请统计（不可恢复，其他群不受影响）。\n"
                        f"确认请输入：<code>/reset CONFIRM</code>")

    # -- 账号是否还存在（用于识别"已注销"） --------------------------------
    def account_resolvable(self, chat_id: int, user_id: int):
        """True=账号存在；False=Telegram 明确说查不到（疑似已注销）；None=无法判断。

        只把"明确查不到"当成已注销，其余错误一律返回 None，绝不误伤真人。
        """
        try:
            self.tg.call("getChatMember", {"chat_id": chat_id, "user_id": user_id},
                         retries=0, timeout=20)
            return True
        except TelegramError as e:
            if e.has("user not found", "participant not found", "participant_id_invalid",
                     "user_not_found", "peer_id_invalid", "user is deactivated"):
                return False
            return None

    def _notify_admins(self, text: str) -> None:
        if not self.setting("cleanup_notify_admin", True):
            return
        for admin in set(self.db.all_admin_ids()) | set(self.owner_ids):
            try:
                self.tg.send_message(admin, text, disable_notification=True)
            except TelegramError:
                pass

    # -- 活跃度 -----------------------------------------------------------
    def cmd_active(self, msg: dict) -> None:
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。")
            return
        gid = bound["chat_id"]
        t = self.db.activity_totals(gid)
        rows = self.db.activity_board(gid, limit=15)
        lines = [
            f"📊 <b>{esc(bound['title'])}</b> 活跃数据", "",
            f"日活 <b>{t['dau']}</b> ｜ 周活 <b>{t['wau']}</b> ｜ 月活 <b>{t['mau']}</b>",
            f"发过言的成员 <b>{t['spoken']}</b> 人 ｜ 累计消息 <b>{t['total_messages']}</b> 条",
            f"在群邀请成员 <b>{t['members']}</b> 人，其中从未发言 <b>{t['silent']}</b> 人",
            "",
            "🏅 <b>发言榜 Top15</b>",
        ]
        if not rows:
            lines.append("还没有统计到发言（机器人需为群管理员才能收到全部消息）。")
        for i, r in enumerate(rows, 1):
            u = self.db.get_user(r["user_id"])
            name = display_name(None, r["user_id"], r["first_name"] or (u["first_name"] if u else ""))
            lines.append(f"{i}. {name} — <b>{r['messages']}</b> 条 · 活跃 {r['active_days']} 天")
        lines += ["", "网页版有完整表格（可搜索、看最后活跃时间和邀请人）"]
        self.tg.send_message(msg["chat"]["id"], "\n".join(lines),
                             reply_to_message_id=msg.get("message_id"))

    # -- 清理已注销账号（只标记，踢人要显式确认） --------------------------
    CLEANUP_BATCH = 400

    # -- 一键拒绝所有待处理申请 / 管理面板 --------------------------------
    def cmd_decline_all(self, msg: dict, args: list, quiet: bool = False) -> None:
        """一键拒绝机器人可见的所有待处理入群申请。"""
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。")
            return
        if not self.is_manager_for(msg["from"]["id"], bound["chat_id"]):
            self.reply(msg, "只有管理员（或在这个群里拥有实权的管理员）可以操作。")
            return
        gid = bound["chat_id"]
        st = self.db.join_requests_stats(gid)
        if not st["pending"]:
            self.reply(msg, (
                f"✅ <b>{esc(bound['title'])}</b> 目前没有机器人可见的待处理申请。\n\n"
                "<i>提示：Telegram 客户端里可能还显示着历史积压的申请——"
                "那些从来没推送给机器人（Bot API 没有「列出待处理申请」的接口），"
                "机器人拿不到名单，需要在群管理界面点「全部拒绝」，"
                "或用服务器上的 tools/join_request_cleaner.py 清。</i>"),
                reply_markup=self._manage_kb())
            return
        if not quiet:
            self.reply(msg, f"⏳ 正在逐条拒绝 <b>{st['pending']}</b> 条待处理申请…")
        res = self.sweep_join_requests(gid, "all", msg["from"]["id"])
        self.reply(msg, (
            "🚫 <b>一键拒绝完成</b>\n\n"
            f"👥 群：{esc(bound['title'])}\n"
            f"🔍 检查：<b>{res['checked']}</b> 条\n"
            f"✅ 真正拒绝：<b>{res['declined']}</b> 条\n"
            f"🗂 归档失效：<b>{res['stale']}</b> 条"
            f"（这些在 Telegram 侧早已不存在）\n"
            f"⚠️ 失败：<b>{res['failed']}</b> 条\n\n"
            "<i>说明：只能拒绝机器人收到过的申请。Telegram 里更早的历史积压"
            "从未推送给机器人，接口上也拿不到名单，需要群管理界面「全部拒绝」"
            "或用服务器上的 join_request_cleaner.py。</i>"),
            reply_markup=self._manage_kb())

    def _manage_kb(self) -> dict:
        return {"inline_keyboard": [
            [{"text": "🚫 一键拒绝所有申请", "callback_data": "mgr:declineall"},
             {"text": "🧹 只拒已注销", "callback_data": "mgr:sweepdel"}],
            [{"text": "🔍 扫描已注销成员", "callback_data": "mgr:scan"},
             {"text": "📊 申请概览", "callback_data": "mgr:stats"}],
            [{"text": "🔄 刷新", "callback_data": "mgr:panel"}]]}

    def cmd_manage(self, msg: dict, args: list) -> None:
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。先把我拉进群并设为管理员，然后发 /bind。")
            return
        if not self.is_manager_for(msg["from"]["id"], bound["chat_id"]):
            self.reply(msg, "只有管理员（或在这个群里拥有实权的管理员）可以使用管理面板。")
            return
        gid = bound["chat_id"]
        st = self.db.join_requests_stats(gid)
        d = self.db.deleted_stats(gid)
        self.reply(msg, (
            "🛠 <b>管理面板</b>\n\n"
            f"👥 群：<b>{esc(bound['title'])}</b>\n\n"
            f"📥 入群申请：共 {st['total']} · 待处理 <b>{st['pending']}</b> · "
            f"已通过 {st['approved']} · 已拒绝 {st['declined']}（自动 {st['auto_declined']}）\n"
            f"🗑 已注销标记：{d['total']} 人（已移出 {d['kicked']}）\n"
            f"⚙️ 自动拒绝新申请：{'开 ✅' if self.setting('auto_decline_all_new', False) else '关'}"
            f" · 专属链接自动放行：{'开 ✅' if self.setting('auto_approve_tracked', False) else '关'}\n\n"
            "点下面的按钮即可一键操作："),
            reply_markup=self._manage_kb())

    def _manage_stats_text(self, gid: int) -> str:
        st = self.db.join_requests_stats(gid)
        d = self.db.deleted_stats(gid)
        c = self.db.get_chat(gid)
        return (f"📊 <b>{esc(c['title'] if c else gid)}</b>\n\n"
                f"入群申请：共 {st['total']} · 待处理 <b>{st['pending']}</b> · "
                f"已通过 {st['approved']} · 已拒绝 {st['declined']}"
                f"（自动 {st['auto_declined']}）\n"
                f"已失效归档：{st.get('stale', 0)}\n"
                f"已注销标记：{d['total']} 人（已移出 {d['kicked']}）\n"
                f"自动拒绝新申请：{'开 ✅' if self.setting('auto_decline_all_new', False) else '关'}")

    def _mgr_decline_all(self, gid: int, uid: int) -> None:
        res = self.sweep_join_requests(gid, "all", uid)
        c = self.db.get_chat(gid)
        try:
            self.tg.send_message(uid, (
                "🚫 <b>一键拒绝完成</b>\n\n"
                f"👥 群：{esc(c['title'] if c else gid)}\n"
                f"🔍 检查 <b>{res['checked']}</b> 条 · 真正拒绝 <b>{res['declined']}</b> 条 · "
                f"归档失效 <b>{res['stale']}</b> 条 · 失败 {res['failed']} 条\n\n"
                "<i>Telegram 里更早的历史积压从未推送给机器人，接口上拿不到名单，"
                "需在群管理界面点「全部拒绝」。</i>"), reply_markup=self._manage_kb())
        except TelegramError:
            pass

    def _mgr_sweep_deleted(self, gid: int, uid: int) -> None:
        res = self.sweep_join_requests(gid, "deleted", uid)
        c = self.db.get_chat(gid)
        try:
            self.tg.send_message(uid, (
                "🧹 <b>已注销申请复核完成</b>\n\n"
                f"👥 群：{esc(c['title'] if c else gid)}\n"
                f"🔍 检查 {res['checked']} 条 · 拒绝已注销 <b>{res['declined']}</b> 条 · "
                f"保留（账号正常）{res['kept']} 条 · 归档失效 {res['stale']} 条 · 失败 {res['failed']} 条"),
                reply_markup=self._manage_kb())
        except TelegramError:
            pass

    def cmd_cleanup(self, msg: dict, args: list) -> None:
        if not self.is_admin(msg["from"]["id"]):
            self.reply(msg, "只有管理员可以操作。")
            return
        bound = self.cmd_target_chat(msg)
        if not bound:
            self.reply(msg, "还没有绑定群组。")
            return
        gid = bound["chat_id"]
        if args and args[0].lower() == "kick":
            return self._cleanup_kick(msg, args)
        self.reply(msg, "🔍 正在扫描已注销账号（只标记，<b>不会踢人</b>），扫描期间机器人正常服务…")
        self.submit(self._cleanup_scan, gid, msg["chat"]["id"], msg["from"]["id"])

    def _cleanup_candidates(self, gid: int) -> list:
        """待扫描名单：在群成员 + 邀过人的 + 被邀过的 + 待处理申请 + 所有交互过的用户。"""
        ids = []
        seen = set()
        queries = [
            ("SELECT invitee_id AS uid FROM referrals WHERE chat_id=? AND status='member'", (gid,)),
            ("SELECT inviter_id AS uid FROM referrals WHERE chat_id=?", (gid,)),
            ("SELECT user_id AS uid FROM join_requests WHERE chat_id=? AND decision='pending'", (gid,)),
            # 机器人见过的所有人（私聊过、发过命令、被记录过的）—— 之前漏了这批，导致扫描名单常年为空
            ("SELECT user_id AS uid FROM users", ()),
            ("SELECT user_id AS uid FROM activity WHERE chat_id=?", (gid,)),
            ("SELECT invitee_id AS uid FROM status_history WHERE chat_id=?", (gid,)),        ]
        for sql, params in queries:
            try:
                rows = list(self.db.conn.execute(sql, params))
            except sqlite3.Error as e:
                log(f"扫描候选查询跳过（{e}）", "WARN")
                continue
            for r in rows:
                uid = r["uid"]
                if uid and uid not in seen and uid != self.bot_id:
                    seen.add(uid)
                    ids.append(uid)
        return ids

    def _cleanup_scan(self, gid: int, report_to: int, actor_id: int) -> None:
        try:
            candidates = self._cleanup_candidates(gid)
            batch = candidates[: self.CLEANUP_BATCH]
            found, checked = [], 0
            for uid in batch:
                if self.db.is_marked_deleted(gid, uid):
                    continue
                ok = self.account_resolvable(gid, uid)
                checked += 1
                time.sleep(0.05)  # 对 Telegram 温柔一点
                if ok is False:
                    name = (self.db.get_user(uid) or {})
                    label = (name["first_name"] if name else "") or f"uid {uid}"
                    self.db.mark_deleted(gid, uid, "getChatMember: user not found")
                    self.db.record_audit(actor_id, None, "deleted_detected", target=str(uid),
                                         chat_id=gid, detail=f"{label} · 查不到该账号")
                    found.append((uid, label))
            stats = self.db.deleted_stats(gid)
            # 顺带把"已注销"的待处理申请也拒掉（管理员开了自动拒绝时）
            swept = {"declined": 0}
            if self.setting("auto_decline_deleted", True) and (found or stats["total"]):
                try:
                    swept = self.sweep_join_requests(gid, "deleted", actor_id)
                except Exception:
                    log("顺带复核入群申请失败：\n" + traceback.format_exc(), "ERROR")
            lines = [
                f"🧹 <b>已注销账号扫描完成</b>", "",
                f"本次扫描 <b>{checked}</b> 人（名单共 {len(candidates)} 人）",
                f"新发现疑似已注销 <b>{len(found)}</b> 人，累计 <b>{stats['total']}</b> 人",
                "",
            ]
            if found:
                lines.append("新发现（仅标记，未做任何踢人操作）：")
                for uid, label in found[:15]:
                    lines.append(f"• {esc(label)} — <code>{uid}</code>")
                if len(found) > 15:
                    lines.append(f"… 其余 {len(found) - 15} 人见网页")
            else:
                lines.append("没有发现新的已注销账号 ✅")
            if swept.get("declined"):
                lines.append(f"\n🚫 顺带拒绝了 <b>{swept['declined']}</b> 条已注销账号的入群申请。")
            if len(candidates) > len(batch):
                lines.append(f"\n还有 {len(candidates) - len(batch)} 人未扫描，再次发送 /cleanup 继续。")
            lines += ["", "⚠️ 本次<b>没有踢任何人</b>。如需把已标记的账号移出群，"
                          "要先用 <code>/cleanup kick CONFIRM</code>，且必须在网页设置里打开"
                          "「允许清理时踢人」。"]
            self.tg.send_message(report_to, "\n".join(lines))
            self.db.record_audit(actor_id, None, "cleanup_scan", target=str(gid), chat_id=gid,
                                 detail=f"扫描 {checked} 人，新发现 {len(found)} 个已注销")
        except Exception:
            log("清理扫描异常：\n" + traceback.format_exc(), "ERROR")
            self._safe_send(report_to, "❌ 扫描出错，详情见服务日志。")

    def _cleanup_kick(self, msg: dict, args: list) -> None:
        _target = self.cmd_target_chat(msg)
        gid = _target["chat_id"] if _target else None
        if gid is None:
            self.reply(msg, "没有选定要清理的群。")
            return
        actor = msg["from"]
        if not self.self_setting_allow_kick():
            self.reply(msg, "⛔ 目前<b>禁止</b>清理时踢人。\n"
                            "如需开启，请在网页设置页打开「允许清理时踢人」，"
                            "或让超管在 config.json 里把 <code>allow_cleanup_kick</code> 设为 true。")
            return
        if len(args) < 2 or args[1].upper() != "CONFIRM":
            marked = self.db.deleted_stats(gid)["total"]
            self.reply(msg, f"⚠️ 已标记疑似已注销账号 <b>{marked}</b> 个。\n"
                            f"这一步会把它们从群里移除（可逆）：先封禁再解封，"
                            f"不会影响任何正常成员。\n\n确认请发送：<code>/cleanup kick CONFIRM</code>")
            return
        self.reply(msg, "🧹 开始移除已标记的已注销账号（逐个执行，会写审计日志）…")
        self.submit(self._cleanup_kick_worker, gid, msg["chat"]["id"], actor)

    def self_setting_allow_kick(self) -> bool:
        return bool(self.setting("allow_cleanup_kick", False))

    def _cleanup_kick_worker(self, gid: int, report_to: int, actor: dict) -> None:
        try:
            rows = [r for r in self.db.list_deleted(gid) if not r["kicked"]]
            kicked, skipped = 0, 0
            for r in rows[: self.CLEANUP_BATCH]:
                uid = r["user_id"]
                # 二次保护：管理员 / 群主 / 机器人自己，绝不碰
                if uid == self.bot_id or self.is_admin(uid):
                    skipped += 1
                    continue
                member = self.tg.get_chat_member(gid, uid)
                if member and member.get("status") in ("creator", "administrator"):
                    skipped += 1
                    continue
                try:
                    self.tg.call("banChatMember", {"chat_id": gid, "user_id": uid,
                                                   "revoke_messages": False}, retries=1)
                    self.tg.call("unbanChatMember", {"chat_id": gid, "user_id": uid,
                                                     "only_if_banned": True}, retries=1)
                    self.db.mark_kicked(gid, uid, actor["id"])
                    self.db.record_audit(actor["id"], actor.get("first_name"), "cleanup_kick",
                                         target=str(uid), chat_id=gid,
                                         detail=f"{r['first_name'] or ''} · 已注销账号移出群")
                    kicked += 1
                except TelegramError as e:
                    skipped += 1
                    log(f"移除 {uid} 失败：{e}", "WARN")
                time.sleep(0.4)
            self.tg.send_message(report_to,
                                 f"🧹 清理完成：移除 <b>{kicked}</b> 个已注销账号，"
                                 f"跳过 <b>{skipped}</b> 个（管理员或操作失败）。\n"
                                 f"每一次移除都已写入审计日志。")
            if self._log_chat_id():
                self._safe_send(self._log_chat_id(),
                                f"🧹 {display_name(actor, actor['id'])} 执行了已注销账号清理："
                                f"移除 {kicked} 个，跳过 {skipped} 个")
        except Exception:
            log("清理踢人异常：\n" + traceback.format_exc(), "ERROR")
            self._safe_send(report_to, "❌ 清理出错，详情见服务日志。")

    # -- 设置概览 ---------------------------------------------------------
    def cmd_settings(self, msg: dict) -> None:
        if not self.is_admin(msg["from"]["id"]):
            return
        labels = {
            "welcome_enabled": "入群欢迎语", "notify_inviter_on_join": "邀请人到账私聊通知",
            "log_leave_enabled": "退群通知（防刷屏）", "activity_enabled": "活跃度统计",
            "auto_decline_deleted": "自动拒绝已注销账号申请",
            "auto_bind_enabled": "自动绑定新群", "cleanup_notify_admin": "清理结果通知管理员",
            "allow_cleanup_kick": "允许清理时踢人（默认关）",
            "web_public_leaderboard": "排行榜对所有人公开",
            "welcome_on_join_request": "申请入群时提示",
        }
        snap = self.settings_snapshot()
        lines = ["⚙️ <b>功能开关</b>（改这里请用网页设置页，可留审计日志）", ""]
        for k, v in snap.items():
            on = v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes")
            lines.append(f"{'🟢' if on else '⚪️'} {labels.get(k, k)}")
        web = self.personal_web_url(msg["from"]["id"])
        lines += ["", f"🌐 网页设置/统计入口：<code>{esc(self.web.base_url)}</code>" if self.web_ready()
                     else "🌐 网页服务未运行"]
        self.reply(msg, "\n".join(lines))

    # -- callbacks --------------------------------------------------------
    def on_callback(self, cb: dict) -> None:
        data = cb.get("data") or ""
        msg = cb.get("message") or {}
        user = cb["from"]
        self.db.touch_user(user)
        try:
            if data == "mylink":
                self.tg.answer_callback(cb["id"])
                self.cmd_link({**msg, "from": user, "chat": msg.get("chat", {"id": user["id"]})})
            elif data == "mystats":
                self.tg.answer_callback(cb["id"])
                self.cmd_stats({**msg, "from": user})
            elif data.startswith("top:"):
                page = int(data.split(":", 1)[1] or 0)
                self.tg.answer_callback(cb["id"])
                self.cmd_top({**msg, "from": user}, page=page, edit_message_id=msg.get("message_id"))
            elif data.startswith("list:"):
                page = int(data.split(":", 1)[1] or 0)
                self.tg.answer_callback(cb["id"])
                self.cmd_list({**msg, "from": user}, page=page, edit_message_id=msg.get("message_id"))
            elif data.startswith("logs:"):
                parts = data.split(":", 2)
                page = int(parts[1] or 0) if parts[1].lstrip("-").isdigit() else 0
                action = (parts[2] or None) if len(parts) > 2 else None
                self.tg.answer_callback(cb["id"])
                self.cmd_logs({**msg, "from": user}, page=page, action=action)
            elif data == "logexport":
                self.tg.answer_callback(cb["id"], "正在生成 CSV…")
                if self.is_admin(user["id"]):
                    self.export_audit(msg["chat"]["id"])
            elif data.startswith("sw:"):
                gid_s = data.split(":", 1)[1]
                if gid_s.lstrip("-").isdigit() and self.chat_is_bound(int(gid_s)):
                    self.db.set_kv(f"usel:{user['id']}", str(int(gid_s)))
                    chat = self.db.get_chat(int(gid_s))
                    self.tg.answer_callback(cb["id"], f"已切换到：{chat['title']}" if chat else "已切换")
                    t = self.db.group_totals(int(gid_s))
                    self.tg.send_message(user["id"],
                                         f"✅ 当前群已切换到 <b>{esc(chat['title'])}</b>\n"
                                         f"邀请 {t['invited']} 人 · 在群 {t['present']} 人 · "
                                         f"参与 {t['inviters']} 人\n\n"
                                         f"网页后台也会跟着切到这个群。")
                else:
                    self.tg.answer_callback(cb["id"], "这个群已解绑")
            elif data.startswith("mgr:"):
                action = data.split(":", 1)[1]
                bound = self.db.get_chat(int(self.db.get_kv(f"usel:{user['id']}") or 0)) \
                    or (self.db.main_chat() if msg.get("chat", {}).get("type") == "private"
                        else self.db.get_chat(msg.get("chat", {}).get("id")))
                if not bound:
                    self.tg.answer_callback(cb["id"], "没有可用群", show_alert=True)
                    return
                if not self.is_manager_for(user["id"], bound["chat_id"]):
                    self.tg.answer_callback(cb["id"], "只有这个群的管理员可以操作", show_alert=True)
                    return
                gid = bound["chat_id"]
                if action == "declineall":
                    self.tg.answer_callback(cb["id"], "正在逐条拒绝…")
                    self.submit(self._mgr_decline_all, gid, user["id"])
                elif action == "sweepdel":
                    self.tg.answer_callback(cb["id"], "正在复核已注销申请…")
                    self.submit(self._mgr_sweep_deleted, gid, user["id"])
                elif action == "scan":
                    self.tg.answer_callback(cb["id"], "开始扫描，结果会发到这里")
                    self.submit(self._cleanup_scan, gid, user["id"], user["id"])
                elif action in ("stats", "panel"):
                    self.tg.answer_callback(cb["id"])
                    self.tg.send_message(user["id"], self._manage_stats_text(gid),
                                         reply_markup=self._manage_kb())
            elif data == "help":
                self.tg.answer_callback(cb["id"])
                self.send_help(user["id"])
            elif data == "noop":
                self.tg.answer_callback(cb["id"])
            else:
                self.tg.answer_callback(cb["id"])
        except TelegramError as e:
            log(f"callback 处理异常：{e}", "ERROR")

    def send_help(self, chat_id: int) -> None:
        text = (
            "❓ <b>使用说明</b>\n\n"
            "【普通成员】\n"
            "• /link — 领取你的专属邀请链接\n"
            "• /stats — 查看自己邀请了多少人\n"
            "• /list — 查看你邀请进群的人\n"
            "• /top — 群邀请排行榜\n\n"
            "【多群】\n"
            "• /groups — 查看所有已绑定的群和各自数据\n"
            "• /switch — 切换当前群（私聊里生效；群里发命令始终作用于本群）\n"
            "• 网页后台右上角也有群切换下拉框，切换后所有页面/统计都跟着切\n\n"
            "【群管理员】\n"
            "• /bind — 把当前群绑定给机器人（需机器人为群管理员）\n"
            "• /unbind — 解除绑定\n"
            "• /panel — 在群里发布「领取链接」面板\n"
            "• /groupinfo — 查看绑定群与统计概览\n"
            "• /export — 导出邀请 CSV 统计表\n"
            "• /retention — 留存分析（1/3/7/14/30 天）+ 入群漏斗\n"
            "• /quality — 拉新质量榜（7 日留存，防刷量）\n"
            "• /funnel — 入群漏斗 + 掉人榜\n"
            "• /active — 活跃数据（日活/周活/月活 + 发言榜）\n"
            "• /cleanup — 扫描已注销账号（<b>只标记，不踢人</b>）\n"
            "• /cleanup kick CONFIRM — 把已标记的已注销账号移出群"
            "（需先在网页设置里打开「允许清理时踢人」）\n"
            "• <b>/declineall</b> — 一键拒绝所有待处理入群申请（也可 /rejectall）\n"
            "• <b>/manage</b> — 管理面板：一键拒绝、扫描已注销、申请概览（按钮操作）\n"
            "• /settings — 查看功能开关（在网页设置页修改）\n"
            "• /logs — 审计日志（所有敏感操作留痕），可 /logs 0 筛选\n"
            "• /site — 查看网页排行榜状态（地址/密码/HTTPS）\n"
            "• /setwelcome — 自定义入群欢迎语\n"
            "• /addadmin /deladmin — 增删机器人管理员\n"
            "• /broadcast 内容 — 给所有使用者群发\n"
            "• /reset CONFIRM — 清空统计（危险）\n\n"
            "【禁漫下载】\n"
            "• /jm — 使用帮助（查询/搜索/排行/随机/下载，自动分卷，无需密码）\n\n"
            "【网页版】\n"
            "• /web — 获取你自己的网页战绩页（含邀请链接、二维码、明细）\n"
            "• 排行榜网页：手机浏览器打开站点地址即可（管理员用 /site 查看）\n\n"
            "【工作原理】\n"
            "每个人拿到一条独立邀请链接；有人通过它进群时，Telegram 会推送 "
            "chat_member 更新并附带所用链接，机器人据此把这个人记到链接主人名下。"
            "同一人只计一次，防止刷量。"
        )
        self.tg.send_message(chat_id, text)

    # -- 入群 / 成员变动 --------------------------------------------------
    def on_chat_member(self, upd: dict) -> None:
        chat = upd.get("chat", {})
        if chat.get("type") not in ("group", "supergroup"):
            return
        chat_id = chat["id"]
        if not self.chat_is_bound(chat_id):
            return
        self.db.update_chat_title(chat_id, chat.get("title") or str(chat_id))
        user = upd.get("from") or {}
        old = upd.get("old_chat_member") or {}
        new = upd.get("new_chat_member") or {}
        uid = (user or {}).get("id") or (new.get("user") or {}).get("id")
        if not uid:
            return
        old_joined = is_member_status(old)
        new_joined = is_member_status(new)
        was_left = is_left_status(old) or old.get("status") is None

        # 1) 有人进群
        if new_joined and not old_joined and was_left:
            self.on_user_joined(chat, upd, user, uid)
        # 2) 有人退群/被踢
        elif old_joined and not new_joined:
            status = "kicked" if new.get("status") == "kicked" else "left"
            if self.db.mark_left(uid, chat_id, status):
                self.db.event("left", chat_id, uid, status)
                log(f"成员离开：{uid} ({status})")
                if self._log_chat_id() and self.setting("log_leave_enabled", True):
                    self._safe_send(self._log_chat_id(),
                                    f"⚪ <b>{display_name(user, uid)}</b> 退出了群组（{status}）。")

    def on_user_joined(self, chat: dict, upd: dict, user: dict, uid: int) -> None:
        chat_id = chat["id"]
        if user.get("is_bot"):
            return
        self.db.touch_user(user)
        self.db.decide_join_request(chat_id, uid, "approved", "telegram", "已入群")
        link_obj = upd.get("invite_link") or {}
        owner_id = None
        link_url = ""
        if link_obj:
            creator = (link_obj.get("creator") or {})
            link_url = link_obj.get("invite_link") or ""
            if creator.get("id") == self.bot_id or not creator:
                owner_id = self._resolve_owner(chat_id, link_obj)
        if not owner_id:
            log(f"成员入群但无法归因：{uid} in {chat_id} link={link_obj}")
            self.db.event("join_unattributed", chat_id, uid, json.dumps(link_obj, ensure_ascii=False)[:400])
            return
        if owner_id == uid:
            log(f"忽略自我邀请：{uid}")
            return

        existing = self.db.get_referral(uid, chat_id)
        if existing and existing["status"] in ("member", "left", "kicked"):
            # 已经算过一次，不重复计入
            self.db.mark_joined(uid, chat_id)
            log(f"重复入群，不重复计数：{uid}")
            return
        if not existing:
            self.db.add_pending(uid, chat_id, owner_id, link_url, user.get("first_name") or "")
        self.db.mark_joined(uid, chat_id)
        if link_url:
            self.db.bump_link_uses(link_url)
        self.db.event("join", chat_id, uid, f"inviter={owner_id}")
        count = self.db.count_for(owner_id, chat_id)
        log(f"✅ {uid} 通过 {owner_id} 的链接加入 {chat_id}，其累计 {count} 人")

        inviter = self.db.get_user(owner_id)
        # 里程碑祝贺（5/10/20/50/100/200 人）
        if count in MILESTONES and self.setting("milestone_notify_enabled", True):
            self._milestone(chat, owner_id, count, inviter)
        # 通知邀请人
        if self.setting("notify_inviter_on_join", True):
            self.submit(self._notify_inviter, owner_id, chat, user, uid, count, inviter)
        # 群内欢迎
        if self.setting("welcome_enabled", True):
            self.submit(self._welcome, chat, user, uid, owner_id, count, inviter)
        # 日志频道
        if self._log_chat_id():
            self._safe_send(self._log_chat_id(),
                            f"🟢 <b>{display_name(user, uid)}</b> 加入了 <b>{esc(chat.get('title'))}</b>\n"
                            f"邀请人：{display_name(None, owner_id, inviter['first_name'] if inviter else '')} "
                            f"（累计 {count} 人）")

    def _resolve_owner(self, chat_id: int, link_obj: dict) -> int | None:
        url = link_obj.get("invite_link") or ""
        if url and "…" not in url:
            row = self.db.get_link(url)
            if row:
                return row["owner_id"]
        name = link_obj.get("name") or ""
        if name:
            row = self.db.get_link_by_name(name)
            if row:
                return row["owner_id"]
            m = re.match(r"^.*?_?(\d+)$", name)
            if m:
                return int(m.group(1))
        return None

    def _notify_inviter(self, owner_id: int, chat: dict, user: dict, uid: int, count: int, inviter) -> None:
        try:
            self.tg.send_message(owner_id, (
                f"🎉 有新成员通过你的链接加入了 <b>{esc(chat.get('title'))}</b>！\n\n"
                f"👤 {mention(uid, display_name(user, uid))}\n"
                f"📊 你的累计邀请：<b>{count}</b> 人\n\n"
                f"查看排名 → /top"
            ), disable_notification=True)
        except TelegramError as e:
            log(f"通知邀请人 {owner_id} 失败：{e}", "WARN")

    def _welcome(self, chat: dict, user: dict, uid: int, owner_id: int, count: int, inviter) -> None:
        tpl = self.db.get_kv("welcome_text") or self.cfg["welcome_text"]
        try:
            text = tpl.format(
                invitee=mention(uid, display_name(user, uid)),
                inviter=mention(owner_id, display_name(None, owner_id, inviter["first_name"] if inviter else "")),
                chat=esc(chat.get("title")),
                count=count,
            )
        except Exception:
            text = f"👋 欢迎 {display_name(user, uid)}！"
        try:
            self.tg.send_message(chat["id"], text)
        except TelegramError as e:
            log(f"发送欢迎语失败：{e}", "WARN")

    def _safe_send(self, chat_id, text) -> None:
        try:
            self.tg.send_message(chat_id, text, disable_notification=True)
        except TelegramError as e:
            log(f"发送到 {chat_id} 失败：{e}", "WARN")

    def _log_chat_id(self):
        v = self.db.get_kv("log_chat_id") or self.cfg.get("log_chat_id") or 0
        try:
            return int(v) if int(v) else None
        except Exception:
            return None

    # -- 待处理入群申请：批量复核 ------------------------------------------
    def sweep_join_requests(self, gid: int, mode: str = "deleted", actor_id: int | None = None) -> dict:
        """复核所有待处理申请。

        mode='deleted'：只拒绝"Telegram 查不到该账号"的（安全，不会误伤真人）
        mode='all'    ：全部拒绝（管理员明确要求时用）
        返回 {checked, declined, kept, failed}
        """
        rows = list(self.db.conn.execute(
            "SELECT * FROM join_requests WHERE chat_id=? AND decision='pending'", (gid,)))
        checked = declined = kept = failed = stale = 0
        for r in rows:
            uid = r["user_id"]
            checked += 1
            if mode != "all":
                ok = self.account_resolvable(gid, uid)
                if ok is not False:          # 账号存在 / 无法判断 → 不动
                    kept += 1
                    continue
                self.db.mark_deleted(gid, uid, "sweep: getChatMember user not found")
            try:
                self.tg.call("declineChatJoinRequest", {"chat_id": gid, "user_id": uid}, retries=1)
            except TelegramError as e:
                if "HIDE_REQUESTER_MISSING" in str(e) or "user is deactivated" in str(e):
                    # Telegram 侧这条申请早就没了（被别人处理过/已失效/账号已注销）→ 本地归档
                    self.db.decide_join_request(gid, uid, "stale", "auto",
                                                "Telegram 侧已不存在该申请")
                    stale += 1
                    log(f"申请已失效，本地归档：{uid}")
                    continue
                failed += 1
                log(f"批量拒绝申请失败：{uid} {e}", "WARN")
                continue
            self.db.decide_join_request(gid, uid, "declined", "auto" if mode != "all" else "manual",
                                        "已注销账号" if mode != "all" else "管理员批量拒绝")
            declined += 1
            time.sleep(0.2)
        if declined or stale:
            self.db.record_audit(actor_id, None, "sweep_requests", target=str(gid), chat_id=gid,
                                 detail=f"复核 {checked} 条：拒绝 {declined}，归档失效 {stale}（mode={mode}）")
        log(f"申请复核完成：检查 {checked}，拒绝 {declined}，归档失效 {stale}，保留 {kept}，失败 {failed}")
        return {"checked": checked, "declined": declined, "kept": kept,
                "failed": failed, "stale": stale}

    def decide_join_request_one(self, gid: int, uid: int, action: str, actor_id=None) -> str:
        """网页逐条处理入群申请：action = 'approve' / 'decline'。

        返回 'ok'（已处理）/ 'stale'（Telegram 侧已不存在，本地归档）/ 'error'。
        """
        method = "approveChatJoinRequest" if action == "approve" else "declineChatJoinRequest"
        try:
            self.tg.call(method, {"chat_id": gid, "user_id": uid}, retries=1)
        except TelegramError as e:
            if "HIDE_REQUESTER_MISSING" in str(e) or "user is deactivated" in str(e):
                self.db.decide_join_request(gid, uid, "stale", "manual",
                                            "Telegram 侧已不存在该申请")
                return "stale"
            log(f"逐条处理申请失败：{uid} {e}", "WARN")
            return "error"
        decision = "approved" if action == "approve" else "declined"
        self.db.decide_join_request(gid, uid, decision, "manual",
                                    "管理员网页逐条通过" if action == "approve" else "管理员网页逐条拒绝")
        self.db.record_audit(actor_id, None, "decide_one", target=str(uid), chat_id=gid,
                             detail=f"网页逐条{decision}入群申请")
        log(f"网页逐条处理入群申请：{uid} -> {decision}")
        return "ok"

    # -- 入群申请（群开启了"管理员审批"时） --------------------------------
    def on_join_request(self, req: dict) -> None:
        chat = req.get("chat", {})
        chat_id = chat.get("id")
        if not chat_id or not self.chat_is_bound(chat_id):
            return
        user = req.get("from") or {}
        uid = user.get("id")
        if not uid:
            return
        link_obj = req.get("invite_link") or {}
        link_url = link_obj.get("invite_link") or ""
        owner_id = None
        if link_obj and ((link_obj.get("creator") or {}).get("id") == self.bot_id):
            owner_id = self._resolve_owner(chat_id, link_obj)
        elif link_url:
            # 兜底：Telegram 偶尔不带 creator，但链接本身是我们库里的就能归因
            row = self.db.get_link(link_url)
            if row:
                owner_id = row["owner_id"]
        name = user.get("first_name") or ""
        username = user.get("username")

        # 1) 所有申请都进申请列表（含无法归因的）
        self.db.add_join_request(chat_id, uid, name, username, link_url, owner_id)
        self.db.record_audit(None, None, "join_request", target=str(uid), chat_id=chat_id,
                             detail=f"{name} 申请入群")

        # 1.5) 开关：新申请一律拒绝（逐条、带间隔，避免触发 Telegram 限流）
        if self.setting("auto_decline_all_new", False):
            time.sleep(0.4)
            try:
                self.tg.call("declineChatJoinRequest", {"chat_id": chat_id, "user_id": uid}, retries=1)
                self.db.decide_join_request(chat_id, uid, "declined", "auto", "自动拒绝全部新申请")
                self.db.record_audit(None, None, "auto_decline_all", target=str(uid), chat_id=chat_id,
                                     detail=f"{name} · 已按「自动拒绝全部新申请」处理")
                log(f"自动拒绝入群申请：{uid} ({name})")
            except TelegramError as e:
                if "HIDE_REQUESTER_MISSING" in str(e):
                    self.db.decide_join_request(chat_id, uid, "stale", "auto",
                                                "Telegram 侧已不存在该申请")
                else:
                    log(f"自动拒绝失败：{e}", "WARN")
            return

        # 1.6) 开关：通过机器人专属链接来的申请自动放行（邀请链路不再卡审批）
        if self.setting("auto_approve_tracked", False) and owner_id:
            time.sleep(0.4)
            try:
                self.tg.call("approveChatJoinRequest", {"chat_id": chat_id, "user_id": uid}, retries=1)
                self.db.decide_join_request(chat_id, uid, "approved", "auto", "专属链接自动批准")
                self.db.record_audit(None, None, "auto_approve", target=str(uid), chat_id=chat_id,
                                     detail=f"{name} · 通过 {owner_id} 的专属链接，已自动批准")
                log(f"自动批准入群申请：{uid}（归因给 {owner_id}）")
            except TelegramError as e:
                log(f"自动批准失败：{e}", "WARN")
            return

        # 2) 已注销账号：自动拒绝（可在网页里关掉）
        exists = self.account_resolvable(chat_id, uid)
        if exists is False:
            self.db.mark_deleted(chat_id, uid, "join_request: getChatMember user not found")
            self.db.decide_join_request(chat_id, uid, "declined", "auto", "已注销账号")
            self.db.record_audit(None, None, "auto_decline_deleted", target=str(uid), chat_id=chat_id,
                                 detail=f"{name} · 申请时 Telegram 查不到该账号（疑似已注销）")
            log(f"自动拒绝已注销账号的入群申请：{uid} ({name})")
            if self.setting("auto_decline_deleted", True):
                try:
                    self.tg.call("declineChatJoinRequest", {"chat_id": chat_id, "user_id": uid},
                                 retries=1)
                    self._notify_admins(
                        f"🚫 已自动拒绝一个<b>已注销账号</b>的入群申请。\n"
                        f"👤 {display_name(user, uid)}（<code>{uid}</code>）\n"
                        f"依据：getChatMember 返回 user not found。\n"
                        f"可在网页设置里关闭「自动拒绝已注销账号申请」，也可在申请列表里查看。")
                except TelegramError as e:
                    log(f"拒绝入群申请失败：{e}", "WARN")
            else:
                self.db.decide_join_request(chat_id, uid, "pending", "bot", "已标记已注销，自动拒绝已关闭")
            return
        if exists is None:
            log(f"入群申请账号状态无法判断（不处理）：{uid}")

        # 3) 正常申请：先按旧逻辑记 pending（不计入邀请数）
        if not owner_id or owner_id == uid:
            return
        created = self.db.add_pending(uid, chat_id, owner_id, link_url, name)
        if created:
            self.db.event("join_request", chat_id, uid, f"inviter={owner_id}")
            log(f"入群申请：{uid} 经 {owner_id} 的链接申请加入 {chat_id}（等待审批，先不计入）")
            try:
                self.tg.send_message(owner_id, (
                    f"⏳ 有人申请通过你的链接加入 <b>{esc(chat.get('title'))}</b>：\n"
                    f"👤 {mention(uid, display_name(user, uid))}\n"
                    f"管理员批准后即计入你的战绩。"
                ), disable_notification=True)
            except TelegramError:
                pass

    # -- 机器人自身在群里的状态变化 ----------------------------------------
    def on_my_chat_member(self, upd: dict) -> None:
        chat = upd.get("chat", {})
        new = upd.get("new_chat_member") or {}
        status = new.get("status")
        if chat.get("type") not in ("group", "supergroup"):
            return
        if status in ("administrator", "creator"):
            if not self.chat_is_bound(chat["id"]) and self.setting("auto_bind_enabled", True):
                self.db.bind_chat(chat["id"], chat.get("title") or str(chat["id"]))
                self.audit(None, "auto_bind", target=str(chat["id"]), chat_id=chat["id"],
                           detail=(chat.get("title") or "") + " · 机器人被提升为管理员")
                log(f"机器人被提升为管理员，自动绑定群 {chat.get('title')} ({chat['id']})")
            r = new.get("can_invite_users")
            if r is False:
                self._safe_send(chat["id"], "⚠️ 我还缺少「邀请用户」权限，请到群管理里给我勾选，否则无法生成专属邀请链接。")
        elif status in ("left", "kicked"):
            if self.chat_is_bound(chat["id"]):
                self.db.unbind_chat(chat["id"])
                self.audit(None, "bot_left_chat", target=str(chat["id"]), chat_id=chat["id"],
                           detail=chat.get("title") or "")
                log(f"机器人被移出群 {chat.get('title')}，已自动解绑", "WARN")

    # -- 命令清理 ---------------------------------------------------------
    def do_reset(self, actor_id: int, reply_to: int, gid: int) -> None:
        if not gid:
            return
        self.db.conn.execute("DELETE FROM referrals WHERE chat_id=?", (gid,))
        self.db.conn.execute("DELETE FROM links WHERE chat_id=?", (gid,))
        self.db.conn.commit()
        self.db.event("reset", gid, actor_id)
        self.audit_soft(actor_id, "reset_stats", target=str(gid), chat_id=gid, detail="清空邀请统计")
        log(f"管理员 {actor_id} 清空了群 {gid} 的统计数据")
        self.tg.send_message(reply_to, f"✅ 已清空群 <code>{gid}</code> 的邀请统计数据（其他群不受影响）。")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    cfg_path = DEFAULT_CONFIG_PATH
    argv = sys.argv[1:]
    if argv and argv[0] not in ("run",):
        cfg_path = argv[0]
    cfg = load_config(cfg_path)
    if not cfg.get("bot_token"):
        log("缺少 bot_token：请在 config.json 里填写，或设置环境变量 BOT_TOKEN", "ERROR")
        return 2
    bot = InviteBot(cfg)
    signal.signal(signal.SIGINT, bot.stop)
    signal.signal(signal.SIGTERM, bot.stop)
    # /reset CONFIRM 的兜底：拦截命令
    orig = bot.dispatch_command

    def patched(cmd, args, msg):
        if cmd in ("/reset", "/clear") and args and args[0].upper() == "CONFIRM":
            if bot.is_admin(msg["from"]["id"]):
                target = bot.cmd_target_chat(msg)
                if target:
                    bot.do_reset(msg["from"]["id"], msg["chat"]["id"], target["chat_id"])
                else:
                    bot.reply(msg, "没有选定要清空的群。")
            return
        if cmd == "/cancel":
            bot._awaiting.pop(msg["from"]["id"], None)
            bot.reply(msg, "已取消。")
            return
        return orig(cmd, args, msg)

    bot.dispatch_command = patched  # type: ignore
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
