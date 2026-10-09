#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网页后台（管理面板）—— 纯标准库 HTTP 服务，零第三方依赖、零外部 CDN。

设计参照主流后台管理系统规范（本次改造前做过检索调研）：
  * 左侧分组侧边栏（240px）+ 顶部粘性标题栏 + 内容区，移动端自动变抽屉式
  * 卡片化数据展示、数字右对齐的表格、状态徽章、进度条、空状态
  * 8px 间距栅格、统一圆角/阴影/描边、深色/浅色双主题（localStorage 记忆）
  * 图标全部内联 SVG，不请求任何外部资源（隧道环境/内网都不受影响）

安全：
  * 全站访问密码 + HMAC 签名会话 Cookie（HttpOnly / SameSite=Lax）+ 登录限速
  * 个人页用 HMAC 签名链接访问，可安全转发；页面不出现服务器 IP
  * 内置 HTTPS：配 web_tls_cert / web_tls_key 即启用；配 web_base_url 即用你的域名
"""
from __future__ import annotations

import hashlib
import hmac
import html
import io
import csv
import json
import os
import re
import secrets
import ssl
import threading
import time
import traceback
import urllib.parse
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 复用主程序里的密码哈希与校验（invite_bot 不在模块级导入 webpanel，无循环依赖）
try:  # pragma: no cover - 独立测试时可降级
    from invite_bot import (hash_password, verify_password, valid_username, valid_email,
                            totp_verify, new_totp_secret, totp_uri, new_backup_codes,
                            hash_backup_code, PBKDF2_ITER)
except Exception:  # pragma: no cover
    import hashlib as _hl
    import hmac as _hm
    import os as _os
    import re as _re

    def hash_password(password, salt_hex=None, iterations=200_000):
        salt = bytes.fromhex(salt_hex) if salt_hex else _os.urandom(16)
        return _hl.pbkdf2_hmac("sha256", password.encode(), salt, iterations).hex(), salt.hex(), iterations

    def verify_password(password, pw_hash, salt_hex, iterations):
        try:
            dk = _hl.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        except Exception:
            return False
        return _hm.compare_digest(dk.hex(), pw_hash or "")

    def valid_username(n):
        return bool(_re.fullmatch(r"[A-Za-z0-9_.\-]{3,32}", n or ""))

    def valid_email(a):
        return bool(_re.fullmatch(r"[^@\s]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,20}", a or ""))

    def totp_verify(secret, code, window=1):
        return False

    def new_totp_secret(nbytes=20):
        import base64 as _b64
        return _b64.b32encode(_os.urandom(nbytes)).decode().rstrip("=")

    def totp_uri(secret, label, issuer="邀请排行榜"):
        return f"otpauth://totp/{issuer}:{label}?secret={secret}"

    def new_backup_codes(n=8):
        import uuid as _uuid
        return ["-".join(_uuid.uuid4().hex[:4].upper() for _ in range(2)) for _ in range(n)]

    def hash_backup_code(code):
        return _hl.sha256(("dshb-otp-" + code.strip().upper()).encode()).hexdigest()

    PBKDF2_ITER = 600_000

VERSION = "1.6.0"
SESSION_COOKIE = "dshb_sess"
TWOFA_COOKIE = "dshb_2fa"
SESSION_TTL = 12 * 3600          # 会话 12 小时过期（原来 7 天，缩短以降低被盗用风险）
LOGIN_WINDOW = 300
LOGIN_MAX_TRIES = 10


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


# 审计动作的中文名（与 invite_bot.ACTION_NAMES 保持一致）
ACTION_CN = {
    "bind_chat": "绑定群组", "unbind_chat": "解绑群组", "auto_bind": "自动绑定群组",
    "bot_left_chat": "机器人被移出群", "add_admin": "添加管理员", "del_admin": "移除管理员",
    "reset_stats": "清空统计", "set_welcome": "修改欢迎语", "set_logchat": "设置日志会话",
    "post_panel": "发布群面板", "export_csv": "导出 CSV", "broadcast": "群发消息",
    "recycle_links": "回收闲置链接", "web_login_ok": "网页登录成功", "web_login_fail": "网页登录失败",
    "link_created": "创建邀请链接", "join_request": "收到入群申请",
    "auto_decline_deleted": "自动拒绝已注销申请", "deleted_detected": "发现已注销账号",
    "cleanup_scan": "扫描已注销账号", "cleanup_kick": "移除已注销账号",
    "setting_change": "修改功能开关",
}

EVIDENCE_CN = {
    "join_request: getChatMember user not found": "提交申请时 Telegram 查不到该账号",
    "getChatMember: user not found": "群成员扫描时 Telegram 查不到该账号",
}


# ---------------------------------------------------------------------------
# 图标（内联 SVG，stroke 风格；不依赖任何图标字体/CDN）
# ---------------------------------------------------------------------------
_ICON_PATHS = {
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
            '<rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "trophy": '<path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0V4z"/>'
              '<path d="M17 5h2.5a1.5 1.5 0 0 1 0 5H17M7 5H4.5a1.5 1.5 0 0 0 0 5H7"/>',
    "pulse": '<path d="M3 12h4l2.5-7 4 14L16 12h5"/>',
    "trend-up": '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
    "trend-down": '<path d="M3 7l6 6 4-4 8 8"/><path d="M15 17h6v-6"/>',
    "star": '<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17.8 6.6 19.9l1-6.1L3.2 9.5l6.1-.9L12 3z"/>',
    "inbox": '<path d="M4 13h4l1.5 3h5L16 13h4"/>'
             '<path d="M6.5 4h11l2.5 9v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-5l2.5-9z"/>',
    "user-x": '<path d="M16 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><circle cx="9.5" cy="7" r="4"/>'
              '<path d="M17 8l5 5m0-5l-5 5"/>',
    "file": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5z"/>'
            '<path d="M14 3v5h5M9 13h6M9 17h4"/>',
    "sliders": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/>'
               '<path d="M1 14h6M9 8h6M17 16h6"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    "menu": '<path d="M3 6h18M3 12h18M3 18h18"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
            'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.4 8.3-8 9-4.6-.7-8-4-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9.5" cy="7" r="4"/>'
             '<path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/>'
            '<path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>',
    "alert": '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
    "upload": '<path d="M12 16V4m0 0L7 9m5-5l5 5"/><path d="M4 20h16"/>',
    "image": '<rect x="3" y="5" width="18" height="14" rx="2.5"/>'
             '<circle cx="9" cy="10" r="1.6"/><path d="M21 15.5l-5-4.5-6.5 5"/>',
    "check": '<path d="M20 6L9 17l-5-5"/>',
    "broom": '<path d="M19 3l-6 6M14 8l2 2-5.5 5.5L4 20l1-6.5L10.5 8z"/><path d="M9 14l3 3"/>',
    "bolt": '<path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z"/>',
}


def icon(name: str, size: int = 18, cls: str = "") -> str:
    body = _ICON_PATHS.get(name, _ICON_PATHS["grid"])
    return (f'<svg class="ic {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{body}</svg>')


# ---------------------------------------------------------------------------
# 设计令牌 + 组件样式
# ---------------------------------------------------------------------------
CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --bg:#0b0e13; --bg-soft:#0f131a; --panel:#141922; --panel-2:#1a212c; --panel-3:#212a37;
  --line:#242e3c; --line-soft:#1c242f;
  --fg:#e9edf5; --fg-dim:#98a4b8; --fg-mute:#6c7889;
  --acc:#4f8cff; --acc-soft:rgba(79,140,255,.14); --acc-fg:#fff;
  --ok:#3fcf8e; --ok-soft:rgba(63,207,142,.14);
  --warn:#f5b544; --warn-soft:rgba(245,181,68,.14);
  --bad:#f4695d; --bad-soft:rgba(244,105,93,.14);
  --gold:#ffd35c; --silver:#ccd6e6; --bronze:#e2a06a;
  --sidebar:#0e131b; --sidebar-fg:#aeb9cb; --sidebar-active:#1b2430;
  --shadow:0 1px 2px rgba(0,0,0,.35),0 8px 24px -12px rgba(0,0,0,.5);
  --r:14px; --r-sm:10px; --r-xs:8px;
  --sbw:248px; --topbar:64px;
}
html[data-theme="light"]{
  --bg:#f4f6fa; --bg-soft:#eef1f7; --panel:#fff; --panel-2:#f7f9fc; --panel-3:#eef2f8;
  --line:#e2e7ef; --line-soft:#edf0f6;
  --fg:#151b26; --fg-dim:#5a6678; --fg-mute:#8a94a6;
  --sidebar:#141b26; --sidebar-fg:#a9b4c6; --sidebar-active:#22303f;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 8px 24px -14px rgba(16,24,40,.18);
}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",
  "Microsoft YaHei",Roboto,Helvetica,Arial,sans-serif;}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
.ic{flex:0 0 auto;vertical-align:-3px}
h1,h2,h3{margin:0;font-weight:650;letter-spacing:.2px}
h2{font-size:15px;margin:26px 0 12px;color:var(--fg)}
h3{font-size:13.5px;color:var(--fg-dim);font-weight:600}

/* ---------- 框架 ---------- */
#navtoggle{display:none}
.app{display:flex;min-height:100vh}
.sidebar{width:var(--sbw);flex:0 0 var(--sbw);background:var(--sidebar);border-right:1px solid var(--line-soft);
  display:flex;flex-direction:column;position:fixed;inset:0 auto 0 0;z-index:40;transition:transform .22s ease}
.brand{display:flex;gap:11px;align-items:center;padding:18px 18px 16px;border-bottom:1px solid rgba(255,255,255,.05)}
.brand .logo{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;color:#fff;
  background:linear-gradient(135deg,var(--acc),#7b5cff);box-shadow:0 6px 16px -6px var(--acc);font-weight:800}
.brand b{display:block;color:#fff;font-size:14.5px;line-height:1.25;max-width:150px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.brand small{color:var(--sidebar-fg);font-size:11.5px;letter-spacing:.4px}
.nav{flex:1;overflow-y:auto;padding:12px 10px 20px}
.nav .group{margin-bottom:6px}
.nav .glabel{display:block;padding:12px 10px 6px;font-size:10.5px;letter-spacing:1.2px;color:#5d6a7d;
  text-transform:uppercase;font-weight:700}
.nav a{display:flex;align-items:center;gap:10px;padding:9px 11px;border-radius:var(--r-sm);color:var(--sidebar-fg);
  font-size:13.5px;font-weight:500;text-decoration:none;position:relative;transition:background .15s,color .15s}
.nav a:hover{background:rgba(255,255,255,.055);color:#fff;text-decoration:none}
.nav a.on{background:var(--sidebar-active);color:#fff}
.nav a.on::before{content:"";position:absolute;left:-10px;top:50%;transform:translateY(-50%);
  width:3px;height:20px;border-radius:0 3px 3px 0;background:var(--acc)}
.nav a .cnt{margin-left:auto;font-size:11px;color:var(--fg-mute);background:rgba(255,255,255,.07);
  padding:1px 7px;border-radius:99px}
.sidefoot{padding:12px 16px;border-top:1px solid rgba(255,255,255,.05);color:#5d6a7d;font-size:11.5px}

.main{flex:1;margin-left:var(--sbw);min-width:0;display:flex;flex-direction:column}
.topbar{height:var(--topbar);position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:14px;
  padding:0 26px;background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line-soft)}
.titles{flex:1;min-width:0}
.titles h1{font-size:18px;line-height:1.25}
.titles p{margin:1px 0 0;color:var(--fg-mute);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.actions{display:flex;align-items:center;gap:9px}
.content{padding:24px 26px 44px;max-width:1240px;width:100%}
.foot{padding:16px 26px 28px;color:var(--fg-mute);font-size:12px}

/* ---------- 组件 ---------- */
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow)}
.card.pad{padding:18px}
.pill{display:inline-flex;align-items:center;gap:6px;background:var(--panel-2);border:1px solid var(--line);
  color:var(--fg-dim);border-radius:99px;padding:4px 11px;font-size:12.5px;white-space:nowrap}
select.gs{background:var(--panel-2);border:1px solid var(--line);color:var(--fg);border-radius:99px;
  padding:6px 12px;font-size:13px;font-family:inherit;max-width:190px;cursor:pointer;outline:none}
select.gs:hover{border-color:var(--acc)}
.badge{display:inline-flex;align-items:center;gap:5px;border-radius:99px;padding:2px 9px;font-size:11.5px;
  font-weight:600;border:1px solid transparent}
.b-ok{background:var(--ok-soft);color:var(--ok);border-color:rgba(63,207,142,.3)}
.b-bad{background:var(--bad-soft);color:var(--bad);border-color:rgba(244,105,93,.3)}
.b-warn{background:var(--warn-soft);color:var(--warn);border-color:rgba(245,181,68,.3)}
.b-acc{background:var(--acc-soft);color:var(--acc);border-color:rgba(79,140,255,.3)}
.b-mute{background:var(--panel-3);color:var(--fg-dim);border-color:var(--line)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid var(--line);
  background:var(--panel-2);color:var(--fg);border-radius:var(--r-sm);padding:8px 14px;font-size:13.5px;
  font-weight:550;cursor:pointer;font-family:inherit;text-decoration:none;transition:background .15s,border-color .15s}
.btn:hover{background:var(--panel-3);text-decoration:none}
.btn-primary{background:var(--acc);border-color:var(--acc);color:var(--acc-fg)}
.btn-primary:hover{background:#3f7df0}
.btn-danger{background:var(--bad-soft);border-color:rgba(244,105,93,.4);color:var(--bad)}
.btn-danger:hover{background:rgba(244,105,93,.22)}
.btn-sm{padding:5px 11px;font-size:12.5px;border-radius:var(--r-xs)}
.btn-ghost{background:transparent}
.iconbtn{width:36px;height:36px;display:inline-grid;place-items:center;border-radius:var(--r-sm);
  border:1px solid var(--line);background:var(--panel-2);color:var(--fg-dim);cursor:pointer}
.iconbtn:hover{color:var(--fg);background:var(--panel-3)}
input[type=text],input[type=password],select{background:var(--panel-2);border:1px solid var(--line);color:var(--fg);
  border-radius:var(--r-sm);padding:9px 12px;font-size:14px;font-family:inherit;width:100%;outline:none}
input:focus,select:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-soft)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.muted{color:var(--fg-dim);font-size:13px}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace}

/* 统计卡 */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(184px,1fr));gap:14px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px 17px;
  box-shadow:var(--shadow);position:relative;overflow:hidden}
.stat .top{display:flex;align-items:center;justify-content:space-between;gap:8px}
.stat .k{color:var(--fg-dim);font-size:12.5px;font-weight:550}
.stat .v{font-size:28px;font-weight:700;letter-spacing:-.5px;margin-top:6px;font-variant-numeric:tabular-nums}
.stat .v small{font-size:13px;color:var(--fg-mute);font-weight:500;margin-left:3px}
.stat .sub{color:var(--fg-mute);font-size:12px;margin-top:2px}
.stat .ico{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;
  background:var(--acc-soft);color:var(--acc)}
.stat.ok .ico{background:var(--ok-soft);color:var(--ok)}
.stat.warn .ico{background:var(--warn-soft);color:var(--warn)}
.stat.bad .ico{background:var(--bad-soft);color:var(--bad)}

/* 表格 */
.tablewrap{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow-x:auto;
  -webkit-overflow-scrolling:touch;box-shadow:var(--shadow)}
table{width:100%;border-collapse:collapse}
th,td{padding:11px 16px;text-align:left;font-size:13.5px;border-bottom:1px solid var(--line-soft)}
th{background:var(--panel-2);color:var(--fg-dim);font-size:11.5px;letter-spacing:.6px;text-transform:uppercase;
  font-weight:700;position:sticky;top:0}
tbody tr:hover{background:var(--panel-2)}
tbody tr:last-child td{border-bottom:none}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.c,th.c{text-align:center}
.rank{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;font-weight:700;font-size:12.5px;
  background:var(--panel-3);color:var(--fg-dim)}
.rank.r1{background:rgba(255,211,92,.16);color:var(--gold)}
.rank.r2{background:rgba(204,214,230,.16);color:var(--silver)}
.rank.r3{background:rgba(226,160,106,.16);color:var(--bronze)}
.who{display:flex;align-items:center;gap:9px;min-width:0}
.who .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.av{width:28px;height:28px;border-radius:9px;display:grid;place-items:center;font-size:12px;font-weight:700;
  color:#fff;background:linear-gradient(135deg,#3f7df0,#7b5cff);flex:0 0 auto}
.avwrap{position:relative;width:28px;height:28px;flex:0 0 auto}
.avwrap .av{position:absolute;inset:0;border-radius:9px}
.avimg{position:relative;z-index:1;width:28px;height:28px;border-radius:9px;object-fit:cover;display:block;
  background:var(--panel-3)}
.bar{height:6px;border-radius:99px;background:var(--panel-3);overflow:hidden;min-width:64px}
.bar>i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--acc),#7b5cff)}
.empty{padding:44px 20px;text-align:center;color:var(--fg-mute)}
.empty .ic{color:var(--panel-3);margin-bottom:8px}

/* 分页 / 筛选 */
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:18px 0 12px}
.pager{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:14px}
.pager a,.pager span{border:1px solid var(--line);background:var(--panel-2);border-radius:var(--r-xs);
  padding:6px 12px;font-size:12.5px;color:var(--fg-dim)}
.pager a.on{background:var(--acc);border-color:var(--acc);color:#fff}
.pager a:hover{text-decoration:none;border-color:var(--acc)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px}
.tabs a{padding:6px 12px;border-radius:99px;font-size:12.5px;color:var(--fg-dim);border:1px solid var(--line);
  background:var(--panel-2)}
.tabs a.on{background:var(--acc);border-color:var(--acc);color:#fff}
.tabs a:hover{text-decoration:none;border-color:var(--acc)}

/* 开关 */
.switch{display:flex;align-items:center;gap:14px;padding:14px 16px;border:1px solid var(--line);
  border-radius:var(--r);background:var(--panel);margin-bottom:10px;cursor:pointer;transition:border-color .15s}
.switch:hover{border-color:var(--acc)}
.switch input{display:none}
.switch .track{width:44px;height:25px;border-radius:99px;background:var(--panel-3);border:1px solid var(--line);
  position:relative;flex:0 0 auto;transition:background .18s,border-color .18s}
.switch .track::after{content:"";position:absolute;top:2px;left:2px;width:19px;height:19px;border-radius:50%;
  background:var(--fg-mute);transition:transform .18s,background .18s}
.switch input:checked + .track{background:var(--acc);border-color:var(--acc)}
.switch input:checked + .track::after{transform:translateX(19px);background:#fff}
.switch .txt b{display:block;font-size:13.5px;font-weight:600}
.switch .txt span{color:var(--fg-mute);font-size:12.5px}
.switch.hot{border-color:rgba(244,105,93,.35)}

/* 提示条 */
.alert{display:flex;gap:11px;padding:14px 16px;border-radius:var(--r);border:1px solid var(--line);
  background:var(--panel-2);font-size:13px;align-items:flex-start}
.alert.warn{border-color:rgba(245,181,68,.35);background:var(--warn-soft);color:var(--warn)}
.alert.ok{border-color:rgba(63,207,142,.35);background:var(--ok-soft);color:var(--ok)}
.alert.bad{border-color:rgba(244,105,93,.35);background:var(--bad-soft);color:var(--bad)}
.alert .ic{margin-top:2px}
.alert b{color:inherit}

/* 图表 */
.chart{display:flex;align-items:flex-end;gap:4px;height:130px;padding:8px 4px 0}
.chart .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:5px;min-width:0}
.chart .col i{display:block;width:100%;border-radius:6px 6px 3px 3px;
  background:linear-gradient(180deg,var(--acc),#7b5cff);min-height:4px}
.chart .col b{font-size:11px;color:var(--fg-dim);font-weight:600}
.chart .col span{font-size:10px;color:var(--fg-mute);white-space:nowrap}
.split{display:grid;grid-template-columns:1.6fr 1fr;gap:16px}
.list{display:flex;flex-direction:column}
.list .item{display:flex;gap:11px;align-items:center;padding:11px 0;border-bottom:1px solid var(--line-soft);font-size:13px}
.list .item:last-child{border-bottom:none}
.list .item .t{color:var(--fg-mute);font-size:11.5px;margin-left:auto;white-space:nowrap}

/* 登录页 */
.login-wrap{min-height:100vh;display:grid;place-items:center;padding:24px}
.login-card{width:100%;max-width:392px;background:var(--panel);border:1px solid var(--line);border-radius:18px;
  padding:30px;box-shadow:var(--shadow)}
.login-card .logo{width:46px;height:46px;border-radius:14px;display:grid;place-items:center;color:#fff;
  background:linear-gradient(135deg,var(--acc),#7b5cff);font-weight:800;font-size:20px;margin-bottom:16px}
.login-card h1{font-size:19px;margin-bottom:4px}
.qr{background:#fff;padding:10px;border-radius:12px;display:inline-block;line-height:0}
.linkbox{background:var(--panel-2);border:1px dashed var(--line);border-radius:var(--r-sm);padding:12px 14px;
  word-break:break-all;font-family:ui-monospace,Consolas,monospace;font-size:13px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.only-mobile{display:none}
.scrim{display:none}

@media (max-width:1024px){
  .split{grid-template-columns:1fr}
}
@media (max-width:860px){
  :root{--topbar:58px}
  .sidebar{transform:translateX(-100%);box-shadow:0 0 60px rgba(0,0,0,.5)}
  #navtoggle:checked ~ .app .sidebar{transform:translateX(0)}
  .main{margin-left:0}
  .content{padding:18px 15px 40px}
  .topbar{padding:0 15px}
  .only-mobile{display:inline-grid}
  #navtoggle:checked ~ .app .scrim{display:block;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:35}
  .split{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
  /* 顶部栏在窄屏只保留标题和主题按钮，避免横向溢出 */
  .actions .pill,.actions .btn{display:none}
  body{overflow-x:hidden}
  .content{overflow-x:hidden}
  /* 小屏隔一个显示一次日期，避免标签挤在一起 */
  .chart .col:nth-child(even) span{visibility:hidden}
  th,td{padding:10px 12px}
  .foot{padding:14px 15px 24px}
  /* 活跃榜：窄屏隐藏重复列（发言条数/活跃天数/最后活跃已在成员格内以徽章显示） */
  table.act td:nth-child(3),table.act th:nth-child(3),
  table.act td:nth-child(4),table.act th:nth-child(4),
  table.act td:nth-child(5),table.act th:nth-child(5){display:none}
}

/* ================= UI 2.0 ================= */
:root{--r:16px;--r-sm:12px;--r-xs:9px}
.btn{transition:background .16s,border-color .16s,transform .12s,box-shadow .18s}
.btn:hover{transform:translateY(-1px);box-shadow:0 6px 16px -8px rgba(0,0,0,.45)}
.btn:active{transform:translateY(0) scale(.98)}
.btn-primary{background:linear-gradient(135deg,#5b9bff,#3f7df0);box-shadow:0 4px 14px -6px rgba(79,140,255,.55)}
.btn-primary:hover{background:linear-gradient(135deg,#6aa6ff,#3f7df0);box-shadow:0 6px 18px -6px rgba(79,140,255,.6)}
.btn-danger:hover{box-shadow:0 6px 16px -8px rgba(244,105,93,.4)}
.nav a{transition:background .16s,color .16s,transform .12s}
.nav a:hover{transform:translateX(3px)}
.nav a.on{background:linear-gradient(135deg,rgba(79,140,255,.24),rgba(123,92,255,.14));color:#fff}
.nav a.on .ic{color:#7ea8ff}
.nav a .ic{transition:color .16s}
.stat{transition:transform .18s,box-shadow .18s,border-color .18s}
.stat:hover{transform:translateY(-3px);box-shadow:0 14px 30px -14px rgba(0,0,0,.55)}
.stat .v{font-size:26px}
.card{transition:border-color .18s,box-shadow .18s}
.card:hover{border-color:color-mix(in srgb,var(--acc) 28%,var(--line))}
tbody tr{transition:background .12s}
tbody tr:hover{background:var(--panel-2)}
tbody tr:nth-child(even){background:color-mix(in srgb,var(--panel-2) 45%,transparent)}
tbody tr:nth-child(even):hover{background:var(--panel-2)}
.iconbtn{transition:background .15s,color .15s,transform .12s}
.iconbtn:hover{transform:translateY(-1px)}
.iconbtn:active{transform:scale(.94)}
input[type=text],input[type=password],select{transition:border-color .15s,box-shadow .15s}
input:focus,select:focus{box-shadow:0 0 0 3.5px var(--acc-soft)}
.pill{transition:border-color .15s,color .15s}
.tabs a{transition:background .15s,color .15s,transform .12s}
.tabs a:hover{transform:translateY(-1px)}
a.btn:hover{text-decoration:none}
/* 登录页装饰 */
.login-wrap{background:
  radial-gradient(600px 400px at 12% -8%, rgba(79,140,255,.16), transparent 60%),
  radial-gradient(700px 500px at 108% 112%, rgba(123,92,255,.14), transparent 60%),
  var(--bg)}
.login-card{border-radius:20px;backdrop-filter:blur(6px);
  background:color-mix(in srgb,var(--panel) 88%,transparent)}
.login-card .logo{box-shadow:0 8px 20px -8px rgba(123,92,255,.6)}
.login-card h1{font-size:20px}
/* 滚动条 */
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--panel-3);border-radius:99px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:var(--fg-mute)}
*{scrollbar-width:thin;scrollbar-color:var(--panel-3) transparent}
/* 弱化动画偏好 */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{transition:none!important;animation:none!important}
}
/* 外观自定义：LOGO / 背景图 */
.logoimg{width:34px;height:34px;border-radius:10px;object-fit:cover;
  box-shadow:0 6px 16px -6px var(--acc)}
.login-card .logoimg{width:46px;height:46px;border-radius:14px;margin-bottom:16px;display:block}
body.hasbg{background-image:url('/branding/bg');background-size:cover;
  background-position:center;background-attachment:fixed}
body.hasbg::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
  background:color-mix(in srgb,var(--bg) 76%,transparent)}
body.hasbg .app,body.hasbg .login-wrap{position:relative;z-index:1}
/* 头像裁剪器（QQ/微信式） */
.cropmask{position:fixed;inset:0;background:rgba(4,8,14,.78);z-index:120;display:none;
  align-items:center;justify-content:center;padding:20px}
.cropmask.on{display:flex}
.cropbox{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:18px;
  width:min(660px,94vw);box-shadow:0 26px 70px -22px rgba(0,0,0,.7)}
.cropbox h3{margin:0 0 12px;color:var(--fg);font-size:15px}
.cropstage{position:relative;height:290px;border-radius:12px;overflow:hidden;background:#05070b;
  cursor:grab;touch-action:none}
.cropstage:active{cursor:grabbing}
.cropstage img{position:absolute;left:0;top:0;transform-origin:0 0;user-select:none;
  pointer-events:none;max-width:none}
.cropring{position:absolute;inset:0;margin:auto;width:200px;height:200px;border-radius:50%;
  border:2px solid rgba(255,255,255,.92);
  box-shadow:0 0 0 9999px rgba(4,8,14,.58);pointer-events:none}
.croprow{display:flex;gap:14px;align-items:center;margin-top:14px}
.croprow input[type=range]{flex:1;accent-color:var(--acc)}
.croppreview{width:56px;height:56px;border-radius:50%;flex:0 0 auto;overflow:hidden;
  border:2px solid var(--line);background:#05070b;background-repeat:no-repeat}
.cropbtns{display:flex;gap:10px;margin-top:14px;justify-content:flex-end}
"""

THEME_BOOT = """<script>(function(){try{
var q=new URLSearchParams(location.search).get('theme');
if(q==='dark'||q==='light'){document.documentElement.setAttribute('data-theme',q);return;}
var t=localStorage.getItem('dshb-theme');
if(t){document.documentElement.setAttribute('data-theme',t);}
else if(window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches){
  document.documentElement.setAttribute('data-theme','light');}}catch(e){}})();</script>"""

TOGGLE_JS = """<script>function dshToggleTheme(){
  var h=document.documentElement, n=h.getAttribute('data-theme')==='light'?'dark':'light';
  h.setAttribute('data-theme',n); try{localStorage.setItem('dshb-theme',n);}catch(e){}
  document.getElementById('themebtn').innerHTML = (n==='light') ? '%(moon)s' : '%(sun)s';}</script>"""


class WebPanel(threading.Thread):
    # 侧边栏分组导航
    NAV = [
        ("总览", [("/", "overview", "grid", "概览"),
                 ("/leaderboard", "rank", "trophy", "邀请排行榜")]),
        ("数据分析", [("/activity", "act", "pulse", "活跃数据"),
                  ("/retention", "ret", "trend-up", "留存分析"),
                  ("/quality", "qua", "star", "质量分"),
                  ("/churn", "chu", "trend-down", "掉人榜")]),
        ("成员管理", [("/requests", "req", "inbox", "入群申请"),
                  ("/linkreq", "lreq", "link", "链接申请")]),
        ("系统", [("/deleted", "del", "user-x", "已注销账号"),
                ("/logs", "log", "file", "审计日志"),
                ("/settings", "set", "sliders", "功能设置"),
                ("/jmdl", "jmdl", "broom", "下载管理")]),
    ]

    # 网页可开关的功能（key, 名称, 说明, 是否高危）
    TOGGLES = [
        ("welcome_enabled", "入群欢迎语", "新人进群时在群里发欢迎语", False),
        ("notify_inviter_on_join", "邀请人到账通知", "有人通过链接进群时私聊通知邀请人", False),
        ("log_leave_enabled", "退群通知（防刷屏）", "成员退群时往日志会话发通知；嫌刷屏就关掉", False),
        ("activity_enabled", "活跃度统计", "统计发言条数/活跃天数，不记录任何消息内容", False),
        ("auto_decline_deleted", "自动拒绝已注销账号申请", "申请入群时若 Telegram 查不到该账号，自动拒绝", False),
        ("auto_decline_all_new", "自动拒绝全部新申请",
         "⚠️ 所有新入群申请一律拒绝（逐条处理、每条带间隔，不会触发限流）。"
         "只对新申请生效：Telegram 不会把历史积压的申请推给机器人", False),
        ("auto_approve_tracked", "专属链接申请自动放行",
         "通过机器人专属邀请链接来的申请自动批准，邀请链路不再卡人工审批", False),
        ("auto_sync_requests", "自动同步申请状态",
         "每 5 分钟核对一次待处理申请：群聊里已不存在的自动归档，仍存在的按规则拒绝，"
         "让网页和 Telegram 保持一致（无需手动点「批量复核」）", False),
        ("auto_bind_enabled", "自动绑定新群", "机器人被设为管理员且尚未绑定任何群时自动绑定", False),
        ("cleanup_notify_admin", "清理结果通知管理员", "扫描/移除的结果私聊通知所有管理员", False),
        ("allow_cleanup_kick", "允许清理时踢人", "关闭时任何清理都不会把人移出群；开启后才响应 CONFIRM", True),
        ("web_public_leaderboard", "排行榜对外公开", "关闭后网页所有页面都需要密码", False),
        ("welcome_on_join_request", "申请入群时提示", "有人提交入群申请时在群里提示（默认关）", False),
        ("web_allow_password_login", "允许管理员应急口令登录",
         "关掉后只能走 Telegram / 独立账号登录，安全性最高；万一进不去可改服务器 config.json", False),
        ("milestone_notify_enabled", "邀请里程碑祝贺", "有人邀请满 5/10/20/50/100 人时，群里祝贺 + 私聊通知", False),
        ("auto_backup_enabled", "每日自动备份数据库", "每天自动备份到 data/backups，保留最近 14 份", False),
        ("weekly_report_enabled", "每周自动播报榜单", "每周一自动把上周邀请榜发到群里", False),
    ]

    def __init__(self, db, cfg: dict, log_fn=print, bot=None):
        super().__init__(name="webpanel", daemon=True)
        self.db = db
        self.cfg = cfg
        self.bot = bot
        self.log = log_fn
        self._server: ThreadingHTTPServer | None = None
        self._login_tries: dict[str, list] = {}
        self._sni_seen: dict = {}
        self._tunnel_cache: tuple | None = None
        secret = db.get_kv("web_secret")
        if not secret:
            secret = hashlib.sha256(os.urandom(32)).hexdigest()[:48]
            db.set_kv("web_secret", secret)
        self.secret = secret
        self.port = int(cfg.get("web_port", 8080))
        self.bind_host = cfg.get("web_bind_host", "0.0.0.0")
        self.password = (cfg.get("web_password") or "").strip()
        # 应急口令：只存加盐哈希（OWASP：单向慢哈希，绝不存明文/可逆密文）。
        # 老部署的明文 web_password 自动迁移为哈希，明文不再用于比对。
        self.pw_hash = (cfg.get("web_password_hash") or "").strip()
        self.pw_salt = (cfg.get("web_password_salt") or "").strip()
        try:
            self.pw_iter = int(cfg.get("web_password_iter") or PBKDF2_ITER)
        except (TypeError, ValueError):
            self.pw_iter = PBKDF2_ITER
        if self.password and not self.pw_hash:
            self.pw_hash, self.pw_salt, self.pw_iter = hash_password(self.password)
            cfg["web_password_hash"], cfg["web_password_salt"], cfg["web_password_iter"] = \
                self.pw_hash, self.pw_salt, self.pw_iter
            self.log("⚠️ 检测到明文 web_password，已自动转为加盐哈希（建议从 config.json 删除明文）")
        self.emergency_path = (cfg.get("web_emergency_path") or "").strip() or None
        self.site = cfg.get("web_site_title") or "邀请统计后台"
        self.public_board = bool(cfg.get("web_public_leaderboard", False))
        self.ready = False
        # 每个请求线程各自的"当前选中群"，避免多线程互相串
        self._tl = threading.local()

    # -- 多群选择 ----------------------------------------------------------
    GROUP_COOKIE = "dshb_group"

    def _groups(self) -> list:
        try:
            return self.db.active_chats()
        except Exception:
            return []

    def _cookie_gid(self, cookie_header: str | None):
        if not cookie_header:
            return None
        c = SimpleCookie()
        try:
            c.load(cookie_header)
        except Exception:
            return None
        m = c.get(self.GROUP_COOKIE)
        if not m:
            return None
        try:
            return int(m.value)
        except (TypeError, ValueError):
            return None

    def _pick_group(self, qs, cookie_header: str | None = None):
        """当前群 = ?g= > Cookie > 第一个群；**只会在这个用户有权限的群里选**。"""
        chats = self._groups()
        u = self.current_user()
        if u is not None and u["role"] not in ("owner", "admin"):
            allow = set(u.get("groups") or [])
            chats = [c for c in chats if c["chat_id"] in allow]
        ids = {c["chat_id"] for c in chats}
        want = (qs.get("g") or [""])[0] if qs else ""
        gid = int(want) if want.lstrip("-").isdigit() else None
        if gid not in ids:
            gid = self._cookie_gid(cookie_header)
        if gid not in ids:
            gid = chats[0]["chat_id"] if chats else None
        return gid

    def accessible_groups(self) -> list:
        """当前账号能访问的群列表（用于切换下拉框）。"""
        chats = self._groups()
        u = self.current_user()
        if u is None or u["role"] in ("owner", "admin"):
            return chats
        allow = set(u.get("groups") or [])
        return [c for c in chats if c["chat_id"] in allow]

    def _gid(self):
        return getattr(self._tl, "gid", None)

    def _q(self, extra: str = "") -> str:
        """把当前群拼进查询串，供导航/分页链接复用。"""
        gid = self._gid()
        parts = [f"g={gid}"] if gid is not None else []
        if extra:
            parts.append(extra)
        return ("?" + "&".join(parts)) if parts else ""

    def _chat(self):
        """当前选中的群（不存在时返回 None）。"""
        gid = self._gid()
        for c in self._groups():
            if c["chat_id"] == gid:
                return c
        return None

    # -- 地址 --------------------------------------------------------------
    def _tunnel_url(self) -> str:
        path = self.cfg.get("web_tunnel_log") or "/var/log/invite-bot-tunnel.log"
        try:
            st = os.stat(path)
        except OSError:
            return ""
        now = time.time()
        if self._tunnel_cache and self._tunnel_cache[0] == st.st_mtime and now - self._tunnel_cache[2] < 30:
            return self._tunnel_cache[1]
        url = ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    for m in re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", line):
                        url = m
        except OSError:
            url = ""
        self._tunnel_cache = (st.st_mtime, url, now)
        return url

    @property
    def base_url(self) -> str:
        explicit = (self.cfg.get("web_base_url") or "").strip()
        if explicit:
            return explicit.rstrip("/")
        if self.cfg.get("web_use_tunnel_url", True):
            tun = self._tunnel_url()
            if tun:
                return tun
        scheme = "https" if (self.cfg.get("web_tls_cert") and self.cfg.get("web_tls_key")) else "http"
        host = (self.cfg.get("web_public_host") or self.bind_host).strip()
        suffix = "" if self.port in (80, 443) else f":{self.port}"
        return f"{scheme}://{host}{suffix}"

    # -- 个人页签名 --------------------------------------------------------
    def sign(self, uid: int) -> str:
        return hmac.new(self.secret.encode(), f"u{uid}".encode(), hashlib.sha256).hexdigest()[:32]

    def personal_token(self, uid: int) -> str:
        return f"{uid}-{self.sign(uid)}"

    def personal_url(self, uid: int, gid: int | None = None) -> str:
        suffix = f"?g={gid}" if gid is not None else ""
        return f"{self.base_url}/u/{self.personal_token(uid)}{suffix}"

    def parse_personal_token(self, token: str):
        try:
            uid_s, sig = token.rsplit("-", 1)
            uid = int(uid_s)
        except Exception:
            return None
        if hmac.compare_digest(sig, self.sign(uid)):
            return uid
        return None

    # -- 会话 --------------------------------------------------------------
    def _session_value(self) -> str:
        exp = int(time.time()) + SESSION_TTL
        mac = hmac.new(self.secret.encode(), f"sess{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{exp}.{mac}"

    def _session_ok(self, cookie_header: str | None) -> bool:
        if not (self.pw_hash or self.password):
            return True
        if not cookie_header:
            return False
        c = SimpleCookie()
        try:
            c.load(cookie_header)
        except Exception:
            return False
        morsel = c.get(SESSION_COOKIE)
        if not morsel:
            return False
        try:
            exp_s, mac = morsel.value.split(".", 1)
            exp = int(exp_s)
        except Exception:
            return False
        if exp < time.time():
            return False
        want = hmac.new(self.secret.encode(), f"sess{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(mac, want)

    def _rate_limited(self, ip: str) -> bool:
        now = time.time()
        tries = [t for t in self._login_tries.get(ip, []) if now - t < LOGIN_WINDOW]
        self._login_tries[ip] = tries
        return len(tries) >= LOGIN_MAX_TRIES

    def _note_try(self, ip: str) -> None:
        self._login_tries.setdefault(ip, []).append(time.time())

    # -- 服务器 ------------------------------------------------------------
    def _allowed_hosts(self) -> set:
        """允许访问的域名白名单（防止有人直接拿 IP 或别的域名来访问）。"""
        hosts = {"localhost", "127.0.0.1", "::1"}
        for key in ("web_allowed_hosts",):
            for h in (self.cfg.get(key) or []):
                if h:
                    hosts.add(str(h).strip().lower())
        base = (self.cfg.get("web_base_url") or "").strip()
        if base:
            from urllib.parse import urlparse
            host = (urlparse(base).hostname or "").lower()
            if host:
                hosts.add(host)
        ph = (self.cfg.get("web_public_host") or "").strip().lower()
        if ph and ph not in ("0.0.0.0", "127.0.0.1"):
            hosts.add(ph)
        return hosts

    def _sni_callback(self, sock, server_name, ctx):
        """识别 SNI 但**不在这里抛异常**。

        历史上这里 raise ssl.SSLError 来拒绝陌生 SNI，结果 Python 的 ssl 模块会把每次
        拒绝都打成 "Exception ignored in: ..." 的 traceback（几小时就刷几百条，看着像崩了）。
        真正的拦截在 HTTP 层：Host 头不在白名单一律返回 421，效果一样、日志干净。
        这里只做节流记录，方便排查谁在扫。
        """
        name = (server_name or "").strip().lower()
        if name and name in self.allowed_hosts:
            return
        try:
            now = time.time()
            key = name or "(空)"
            last = self._sni_seen.get(key, 0)
            if now - last > 600:                     # 同一来源 10 分钟最多记一次
                self._sni_seen[key] = now
                self.log(f"收到非白名单 SNI：{name or '(空/直接拿 IP 连)'}（HTTP 层将返回 421）")
        except Exception:
            pass

    def run(self) -> None:
        handler = _make_handler(self)
        try:
            self._server = ThreadingHTTPServer((self.bind_host, self.port), handler)
        except OSError as e:
            self.log(f"网页后台启动失败（端口 {self.port}）：{e}", "ERROR")
            return
        self._server.daemon_threads = True
        self.ready = True
        self.allowed_hosts = self._allowed_hosts()
        self._sni_seen: dict = {}
        cert, key = self.cfg.get("web_tls_cert"), self.cfg.get("web_tls_key")
        if cert and key and os.path.exists(cert) and os.path.exists(key):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            # 只接受白名单 SNI：拿 IP 扫描的人连握手都过不去，拿不到任何内容
            ctx.sni_callback = self._sni_callback
            self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
            self.log(f"网页后台已启动（HTTPS）：{self.base_url}，SNI 白名单：{sorted(self.allowed_hosts)}")
        else:
            self.log(f"网页后台已启动：{self.base_url}"
                     + ("（未设密码，任何人可访问）" if not (self.pw_hash or self.password)
                        else "（已启用访问密码，口令以加盐哈希存储）"))
        self._start_fallback(handler)
        try:
            self._server.serve_forever(poll_interval=0.5)
        except Exception as e:  # pragma: no cover
            self.log(f"网页后台异常退出：{e}", "ERROR")

    def _start_fallback(self, handler) -> None:
        """可选的兜底端口，默认**只监听 127.0.0.1**。

        安全原则：服务器 IP 绝不对外暴露。所以兜底端口不绑 0.0.0.0，
        只在本机回环上提供，需要时用 SSH 端口转发访问：
            ssh -L 18080:127.0.0.1:18080 root@<通过隧道或跳板>
        公网出口只保留 https（由域名/隧道承载，不暴露源站）。
        """
        try:
            fb = int(self.cfg.get("web_fallback_port") or 0)
        except (TypeError, ValueError):
            fb = 0
        if not fb or fb == self.port:
            return
        host = (self.cfg.get("web_fallback_host") or "127.0.0.1").strip()
        try:
            srv = ThreadingHTTPServer((host, fb), handler)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                             daemon=True, name="webfallback").start()
            self._fallback = srv
            self.log(f"兜底端口已开启（仅本机 {host}:{fb}，公网不可达）")
        except OSError as e:
            self.log(f"兜底端口 {fb} 启动失败：{e}", "WARN")

    def stop(self) -> None:
        for srv in (self._server, getattr(self, "_fallback", None)):
            if srv:
                try:
                    srv.shutdown()
                    srv.server_close()
                except Exception:
                    pass

    # -- 公用 --------------------------------------------------------------
    def _who(self, uid: int) -> str:
        u = self.db.get_user(uid)
        if u and u["first_name"]:
            return u["first_name"]
        return f"用户 {uid}"

    def _setting(self, key: str, default=None):
        v = self.db.get_setting(key)
        return self.cfg.get(key, default) if v is None else v

    def _started_at(self) -> int:
        """服务启动时间（用于底部"已稳定运行"）。"""
        try:
            v = int(self.db.get_kv("bot_started_at") or 0)
        except (TypeError, ValueError):
            v = 0
        return v or int(time.time())

    def _uptime_script(self) -> str:
        t0 = self._started_at()
        return ("<script>(function(){var t0=%d;"
                "function f(s){var d=Math.floor(s/86400),h=Math.floor(s%%86400/3600),"
                "m=Math.floor(s%%3600/60),x=s%%60;"
                "return (d>0?d+' 天 ':'')+h+' 小时 '+m+' 分 '+x+' 秒';}"
                "function t(){var e=document.getElementById('uptime');if(e){"
                "e.textContent='已稳定运行 '+f(Math.max(0,Math.floor(Date.now()/1000)-t0));}}"
                "t();setInterval(t,1000);})();</script>") % t0

    def _shell(self, active: str, title: str, subtitle: str, body: str,
               actions: str = "", script: str = "", headright: str = "") -> bytes:
        chat = self._chat()
        u = self.current_user() or {"role": "member", "groups": [], "name": "-", "uid": None}
        role = u["role"]
        has_groups = bool(u.get("groups"))
        if role == "member" and not has_groups:
            nav_def = [("我的", [("/me", "me", "user", "我的战绩")])]
        else:
            nav_def = []
            for gname, items in self.NAV:
                if gname == "系统" and role not in ("owner", "admin"):
                    continue
                if gname == "成员管理":
                    # 平台管理员，或"有实权的群管理员"都能看到审批菜单
                    if role not in ("owner", "admin") and not u.get("admin_groups"):
                        continue
                nav_def.append((gname, items))
            nav_def.insert(0, ("我的", [("/me", "me", "user", "个人中心")]))
            if role in ("owner", "admin"):
                nav_def.append(("账号", [("/admin/accounts", "acc", "users", "账号管理")]))
        nav_items = []
        counts = self._nav_counts()
        q = self._q()
        for group, items in nav_def:
            links = []
            for href, key, icon_name, label in items:
                cls = " class=\"on\"" if key == active else ""
                badge = ""
                if key in counts and counts[key]:
                    badge = f'<span class="cnt">{counts[key]}</span>'
                links.append(f'<a href="{href}{q}"{cls}>{icon(icon_name, 17)}<span>{label}</span>{badge}</a>')
            nav_items.append(f'<div class="group"><span class="glabel">{group}</span>{"".join(links)}</div>')
        groups = self.accessible_groups()
        if len(groups) > 1:
            opts = "".join(
                f'<option value="{c["chat_id"]}"'
                + (" selected" if chat and c["chat_id"] == chat["chat_id"] else "")
                + f'>{esc(c["title"])}</option>' for c in groups)
            switcher = ('<select class="gs" onchange="location.href=location.pathname+'
                        "'?g='+this.value\" title=\"切换群聊\">" + opts + "</select>")
        elif chat:
            switcher = f'<span class="pill">{icon("users", 15)}{esc(chat["title"])}</span>'
        else:
            switcher = ""
        role_cn = {"owner": "超管", "admin": "管理员", "member": "成员"}.get(role, role)
        if role == "member" and u.get("admin_groups"):
            role_cn = "群管理员"
        user_chip = f'<span class="pill" title="{esc(role_cn)}">{icon("user", 15)}{esc(u["name"])}</span>'
        foot = f'{VERSION} · {len(self.db.active_chats())} 个群 · {esc(chat["title"]) if chat else "未绑定群组"}'
        return f"""<!doctype html>
<html lang="zh-CN" data-theme="dark"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{esc(title)} · {esc(self.site)}</title>
{THEME_BOOT}<style>{CSS}</style></head><body{(' class="hasbg"' if self._has_bg() else "")}>
<input type="checkbox" id="navtoggle">
<div class="app">
  <aside class="sidebar">
    <div class="brand">{self._brand_logo_html()}
      <div><b>{esc(self.site)}</b><small>INVITE ANALYTICS</small></div></div>
    <nav class="nav">{"".join(nav_items)}</nav>
    <div class="sidefoot">{foot}</div>
  </aside>
  <label class="scrim" for="navtoggle"></label>
  <div class="main">
    <header class="topbar">
      <label class="iconbtn only-mobile" for="navtoggle">{icon("menu", 18)}</label>
      <div class="titles"><h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div>
      <div class="actions">
        {switcher}
        {user_chip}
        {actions}
        <button class="iconbtn" id="themebtn" onclick="dshToggleTheme()" title="切换主题">{icon("sun", 18)}</button>
        {f'<a class="iconbtn" href="/logout" title="退出">{icon("logout", 18)}</a>' if self.current_user() else ''}
      </div>
    </header>
    <main class="content">{body}</main>
    <footer class="foot">{esc(self.site)} · 数据实时同步自 Telegram · 页面不显示服务器地址<br>
      <span id="uptime" class="mono">已稳定运行 —</span> · 服务版本 {VERSION}</footer>
  </div>
</div>
{TOGGLE_JS % {"sun": icon("sun", 18), "moon": icon("moon", 18)}}{self._uptime_script()}{script}</body></html>""".encode("utf-8")

    def _series(self, gid: int, days: int = 14) -> list:
        """把稀疏的每日新增补成连续序列（没数据的日期补 0），图表才不会有断档。"""
        raw = dict(self.db.daily_growth(gid, days))
        today = int(time.time())
        out = []
        for i in range(days - 1, -1, -1):
            key = time.strftime("%Y-%m-%d", time.gmtime(today - i * 86400))
            out.append((key[5:], raw.get(key, 0)))
        return out

    def _bar_chart(self, gid: int, days: int = 14, title: str = "近 14 天增长") -> str:
        series = self._series(gid, days)
        mx = max([c for _, c in series] or [1]) or 1
        cols = "".join(f'<div class="col" title="{d}：{c} 人"><b>{c or ""}</b>'
                       f'<i style="height:{max(3, int(96 * c / mx))}px;'
                       f'{"opacity:.35" if not c else ""}"></i><span>{d}</span></div>'
                       for d, c in series)
        total = sum(c for _, c in series)
        return (f'<div class="card pad"><div class="row" style="justify-content:space-between">'
                f'<h2 style="margin:0">{esc(title)}</h2>'
                f'<span class="pill">共 {total} 人</span></div>'
                f'<div class="chart">{cols}</div></div>')

    def _nav_counts(self) -> dict:
        chat = self._chat()
        if not chat:
            return {}
        gid = chat["chat_id"]
        try:
            return {"req": self.db.join_requests_stats(gid)["pending"],
                    "lreq": self.db.link_requests_count(),
                    "del": self.db.deleted_stats(gid)["total"]}
        except Exception:
            return {}

    def _login_shell(self, title: str, body: str) -> bytes:
        return f"""<!doctype html>
<html lang="zh-CN" data-theme="dark"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{esc(title)} · {esc(self.site)}</title>
{THEME_BOOT}<style>{CSS}</style></head>
<body{(' class="hasbg"' if self._has_bg() else "")}><div class="login-wrap"><div>
<div class="login-card">
  {self._brand_logo_html()}
  <h1>{esc(self.site)}</h1>
  <p class="muted" style="margin:4px 0 20px">Telegram 群邀请与成员数据后台</p>
  {body}
</div>
<p class="muted" style="text-align:center;margin-top:16px;font-size:12px">
  {icon("shield", 13)} 需要访问密码 · 登录尝试会被记录</p>
<p class="muted mono" style="text-align:center;font-size:11.5px">
  <span id="uptime">已稳定运行 —</span> · v{VERSION}</p>
</div></div>{self._uptime_script()}</body></html>""".encode("utf-8")

    def _stat(self, label: str, value, unit: str = "", icon_name: str = "grid",
              tone: str = "", sub: str = "") -> str:
        return (f'<div class="stat {tone}"><div class="top"><span class="k">{esc(label)}</span>'
                f'<span class="ico">{icon(icon_name, 19)}</span></div>'
                f'<div class="v">{esc(value)}<small>{esc(unit)}</small></div>'
                f'<div class="sub">{esc(sub)}</div></div>')

    def _avatar(self, name: str) -> str:
        return f'<span class="av">{esc((name or "?")[:1].upper())}</span>'

    # -- 头像代理（真照片，磁盘缓存） --------------------------------------
    def _avatar_cache_path(self, uid: int) -> str:
        dbp = self.cfg.get("db_path") or getattr(self.db, "path", None) or "bot.db"
        base = os.path.join(os.path.dirname(dbp), "avatars")
        return os.path.join(base, f"{uid}.jpg")

    # -- 外观自定义（LOGO / 背景图） --------------------------------------
    def _branding_dir(self) -> str:
        dbp = self.cfg.get("db_path") or getattr(self.db, "path", None) or "bot.db"
        return os.path.join(os.path.dirname(dbp), "branding")

    def _branding_file(self, kind: str):
        """返回 (path, ext) —— kind: 'logo' / 'bg'"""
        d = self._branding_dir()
        if not os.path.isdir(d):
            return None, None
        for f in os.listdir(d):
            if f.startswith(kind + ".") and f.split(".", 1)[1] in ("png", "jpg", "jpeg", "webp"):
                return os.path.join(d, f), f.split(".", 1)[1]
        return None, None

    def _has_logo(self) -> bool:
        p, _ = self._branding_file("logo")
        return bool(p)

    def _has_bg(self) -> bool:
        p, _ = self._branding_file("bg")
        return bool(p)

    def _brand_v(self, kind: str) -> str:
        try:
            p, _ = self._branding_file(kind)
            return str(int(os.path.getmtime(p))) if p else "0"
        except OSError:
            return "0"

    def _brand_logo_html(self) -> str:
        if self._has_logo():
            return (f'<img class="logoimg" src="/branding/logo?v={self._brand_v("logo")}" '
                    f'alt="logo">')
        return f'<div class="logo">{esc(self.site)[:1] or "邀"}</div>'

    def _branding_route(self, h, kind: str) -> None:
        p, ext = self._branding_file(kind)
        if not p:
            return self._send(h, 404, b"", "image/png")
        ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "webp": "image/webp"}.get(ext, "image/png")
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            return self._send(h, 404, b"", ctype)
        return self._send(h, 200, data, ctype,
                          extra=[("Cache-Control", "public, max-age=86400")])

    def _save_branding(self, kind: str, dataurl: str) -> bool:
        import base64
        try:
            head, b64 = dataurl.split(",", 1)
            ext = head.split("/")[1].split(";")[0].lower()
            if ext not in ("png", "jpg", "jpeg", "webp"):
                return False
            raw = base64.b64decode(b64)
            if len(raw) > 2 * 1024 * 1024:
                return False
            d = self._branding_dir()
            os.makedirs(d, exist_ok=True)
            for f in os.listdir(d):
                if f.startswith(kind + "."):
                    try:
                        os.remove(os.path.join(d, f))
                    except OSError:
                        pass
            with open(os.path.join(d, f"{kind}.{ext}"), "wb") as f:
                f.write(raw)
            return True
        except Exception:
            return False

    def _clear_branding(self) -> None:
        d = self._branding_dir()
        if os.path.isdir(d):
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass

    def _fetch_avatar(self, uid: int) -> bytes | None:
        """从 Bot API 取用户头像（失败返回 None → 前端退回字母头像）。"""
        if not self.bot:
            return None
        getter = getattr(self.bot, "get_avatar_bytes", None)
        if getter:
            try:
                return getter(uid)
            except Exception:
                return None
        try:
            photos = self.bot.tg.call("getUserProfilePhotos", {"user_id": uid, "limit": 1})
        except Exception:
            return None
        items = (photos or {}).get("photos") or []
        if not items:
            return None
        try:
            smallest = items[0][0]          # 最小尺寸，够做 34px 头像
            fobj = self.bot.tg.call("getFile", {"file_id": smallest["file_id"]})
            token = self.cfg.get("bot_token") or ""
            url = f"https://api.telegram.org/file/bot{token}/{fobj['file_path']}"
            req = urllib.request.Request(url, headers={"User-Agent": "invite-bot-avatar"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read(256 * 1024)
        except Exception:
            return None

    def _avatar_img_route(self, h, uid_s: str) -> None:
        """GET /avatar/<uid>：有缓存返回缓存；没有就现取并缓存。"""
        try:
            uid = int(uid_s)
        except ValueError:
            return self._send(h, 404, b"", "image/jpeg")
        path = self._avatar_cache_path(uid)
        try:
            if os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400:
                with open(path, "rb") as f:
                    return self._send(h, 200, f.read(256 * 1024), "image/jpeg")
        except OSError:
            pass
        data = self._fetch_avatar(uid)
        if not data:
            return self._send(h, 404, b"", "image/jpeg")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            pass
        return self._send(h, 200, data, "image/jpeg")

    def _avatar_img(self, uid, name: str) -> str:
        """真头像 + 字母头像兜底：img 加载失败(onerror)就露出字母。"""
        return (f'<span class="avwrap">{self._avatar(name)}'
                f'<img class="avimg" src="/avatar/{int(uid)}" loading="lazy" alt="" '
                f'onerror="this.style.display=\'none\'"></span>')

    def _pager(self, base: str, page: int, pages: int, extra_qs: str = "") -> str:
        if pages <= 1:
            return f'<div class="pager"><span>共 1 页</span></div>'
        gid = self._gid()
        gpart = f"g={gid}" if gid is not None else ""
        joined = "&".join(x for x in (gpart, extra_qs) if x)
        q = (joined + "&") if joined else ""
        out = ['<div class="pager">', f'<span>第 {page + 1} / {pages} 页</span>']
        if page > 0:
            out.append(f'<a href="{base}?{q}page={page - 1}">← 上一页</a>')
        lo = max(0, page - 2)
        hi = min(pages, lo + 5)
        for p in range(lo, hi):
            cls = ' class="on"' if p == page else ""
            out.append(f'<a href="{base}?{q}page={p}"{cls}>{p + 1}</a>')
        if page + 1 < pages:
            out.append(f'<a href="{base}?{q}page={page + 1}">下一页 →</a>')
        out.append("</div>")
        return "".join(out)

    def _not_bound(self, h) -> None:
        """还没绑定群组时的引导页（注意：必须自己把响应发出去）。"""
        self._html(h, 200, self._shell(
            "overview", "还没有绑定群组", "完成绑定后这里才会显示数据",
            f'<div class="card pad"><div class="empty">{icon("alert", 40)}'
            f'<h2 style="margin:10px 0 6px">还没有绑定任何群组</h2>'
            f'<p class="muted">把机器人拉进群 → 设为管理员（勾选「邀请用户」）→ 在群里发送 '
            f'<code class="mono">/bind</code></p></div></div>'))

    # -- 路由 --------------------------------------------------------------
    def handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urllib.parse.urlparse(h.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query or "")
        ip = h.client_address[0] if h.client_address else "-"
        cookie = h.headers.get("Cookie")
        self._tl.user = None
        self._tl.gid = None
        self._tl.set_cookie = False
        # POST 请求体先读一次（CSRF 校验与各页面共用，避免重复读取）
        self._tl.form = self._read_body(h) if method == "POST" else {}

        try:
            # Host 白名单放最前面：拿 IP 或陌生域名来访问的一律拒绝（防扫描）
            host = (h.headers.get("Host") or "").split(":")[0].strip().lower()
            if getattr(self, "allowed_hosts", None) and host and host not in self.allowed_hosts:
                return self._send(h, 421, b"misdirected request",
                                  "text/plain; charset=utf-8")
            if path == "/healthz":
                try:
                    hb = int(self.db.get_kv("bot_heartbeat") or 0)
                except (TypeError, ValueError):
                    hb = 0
                age = int(time.time()) - hb if hb else -1
                txt = f"ok heartbeat={'n/a' if age < 0 else str(age) + 's'} db=ok"
                return self._send(h, 200, (txt + "\n").encode(), "text/plain; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(h, 204, b"", "image/x-icon")

            # 个人页（签名链接，免密码）
            if path.startswith("/u/"):
                rest = path[3:]
                if rest.endswith("/qr.svg"):
                    return self._qr(h, self.parse_personal_token(rest[: -len("/qr.svg")]), qs)
                uid = self.parse_personal_token(rest)
                if uid is None:
                    return self._error(h, 403, "链接无效或已被篡改", "请回到 Telegram 重新获取你的专属链接。")
                return self._personal(h, uid, qs)

            # 头像代理（页面里 <img src="/avatar/<uid>">；磁盘缓存 7 天）
            if path.startswith("/avatar/"):
                return self._avatar_img_route(h, path[len("/avatar/"):])

            # 外观资源（自定义 LOGO / 背景图）
            if path in ("/branding/logo", "/branding/bg"):
                return self._branding_route(h, path.split("/")[-1])

            # ---- 公开路由（不需要登录）----
            if path == "/login":
                return self._login(h, method, qs)
            if path == "/login/start":
                return self._login_start(h)
            if path == "/login/poll":
                return self._login_poll(h, qs)
            if path == "/login/code":
                if method != "POST":
                    return self._redirect(h, "/login")
                return self._login_code(h)
            if self.emergency_path and path == self.emergency_path:
                return self._emergency_page(h, method)
            if path == "/login/2fa":
                return self._login_2fa(h, method, qs)
            if path == "/verify":
                return self._verify_email(h, qs)
            if path == "/register":
                return self._register(h, method, qs)
            if path == "/logout":
                return self._send(h, 302, b"", "text/html; charset=utf-8",
                                  extra=[("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0"),
                                         ("Location", "/login")])

            # ---- 身份 ----
            user = self._resolve_user(cookie)
            self._tl.user = user
            self._tl.gid = self._pick_group(qs, cookie)      # 已按权限过滤
            try:
                want = int((qs.get("g") or [""])[0])
                self._tl.set_cookie = (want == self._tl.gid and self._tl.gid is not None)
            except (TypeError, ValueError):
                self._tl.set_cookie = False
            if not user:
                if self.public_board and path in ("/", "/leaderboard"):
                    return self._leaderboard(h, qs, public=True)
                return self._login_page(h, error=None, next_path=path)

            # 登录后的 POST 一律校验 CSRF（防"借你的浏览器偷偷操作"）
            if method == "POST" and not self._csrf_ok(getattr(self._tl, "form", {}) or {}):
                self.log(f"CSRF 校验失败：{path} from {ip}", "WARN")
                return self._error(h, 403, "安全校验失败（CSRF）",
                                   "请刷新页面后重新操作。如果你是用脚本调用，记得带上 csrf 令牌。")

            # 普通成员：首页就是「我的」；有群的话首页给他看数据概览
            if path == "/":
                if user["role"] == "member" and not user["groups"]:
                    return self._me(h, qs)
                return self._overview(h, qs)
            if path == "/me":
                return self._me(h, qs)
            if path == "/me/2fa":
                return self._twofa_page(h)
            if path in ("/me/2fa/on", "/me/2fa/off", "/me/2fa/newbackup"):
                return self._post_twofa(h, path)
            if path in ("/me/pw", "/me/pw-init", "/me/tg", "/me/linkreq",
                        "/me/username", "/me/email"):
                return self._post_me(h, path)

            # ---- 需要群权限的页面 ----
            group_pages = ("/leaderboard", "/activity", "/quality", "/retention", "/churn")
            if path in group_pages:
                if not self._groups():
                    return self._not_bound(h)
                if not self._gid() or not self.user_can(self._gid()):
                    return self._error(h, 403, "没有这个群的权限",
                                       "你只能查看自己所在的群；如需权限请联系管理员。")
                if path == "/leaderboard":
                    return self._leaderboard(h, qs)
                if path == "/activity":
                    return self._activity(h, qs)
                if path == "/quality":
                    return self._quality(h, qs)
                if path == "/retention":
                    return self._retention(h, qs)
                if path == "/churn":
                    return self._churn(h, qs)
                return self._requests(h, qs)

            # ---- 管理类（平台管理员 或 有实权的群管理员）----
            if path in ("/requests", "/linkreq"):
                if not self._groups():
                    return self._not_bound(h)
                if not self.require_manager():
                    return self._error(h, 403, "需要管理权限",
                                       "只有平台管理员，或群里拥有实权的管理员才能审批。")
                if path == "/requests":
                    if self._gid() not in self.managed_groups():
                        return self._error(h, 403, "这个群你没有管理权限", "只能审批自己管理的群。")
                    return (self._post_requests(h) if method == "POST" else self._requests(h, qs))
                return (self._post_linkreq(h) if method == "POST" else self._linkreq(h, qs))

            # ---- 仅平台管理员 ----
            if path in ("/deleted", "/settings", "/panel", "/logs", "/admin/accounts", "/jmdl"):
                if not self.require_admin():
                    return self._error(h, 403, "需要管理员权限",
                                       "普通成员只能查看数据；审批和设置类操作仅管理员可用。")
                if path == "/deleted":
                    return (self._post_deleted(h) if method == "POST" else self._deleted(h, qs))
                if path == "/settings":
                    return (self._post_settings(h) if method == "POST" else self._settings(h, qs))
                if path == "/panel":
                    return (self._post_panel(h) if method == "POST" else self._overview(h, qs))
                if path == "/logs":
                    return self._logs(h, qs)
                if path == "/jmdl":
                    return (self._post_jmdl(h) if method == "POST" else self._jm_page(h, qs))
                if path == "/requests":
                    return self._requests(h, qs)
                if path == "/linkreq":
                    return (self._post_linkreq(h) if method == "POST" else self._linkreq(h, qs))
                return (self._post_accounts(h) if method == "POST" else self._admin_accounts(h, qs))

            return self._error(h, 404, "页面不存在", "请从左侧菜单选择要查看的页面。")
        except Exception as e:  # pragma: no cover
            self.log(f"网页请求处理异常 {path}：{e}\n{traceback.format_exc()}", "ERROR")
            return self._error(h, 500, "服务器内部错误", str(e)[:200])

    # -- 响应 --------------------------------------------------------------
    def _send(self, h, code: int, body: bytes, ctype: str, extra=None) -> None:
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        # 安全响应头
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("X-Frame-Options", "DENY")
        h.send_header("Referrer-Policy", "no-referrer")
        h.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        h.send_header("Cross-Origin-Opener-Policy", "same-origin")
        h.send_header("Content-Security-Policy",
                      "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                      "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
                      "base-uri 'none'; form-action 'self'")
        if code in (200, 201):
            h.send_header("Cache-Control", "no-store")
        else:
            h.send_header("Cache-Control", "no-store")
        if self.cfg.get("web_tls_cert"):
            h.send_header("Strict-Transport-Security", "max-age=31536000")
        # 记住用户选的群
        if getattr(self._tl, "set_cookie", False) and self._gid() is not None:
            h.send_header("Set-Cookie",
                          f"{self.GROUP_COOKIE}={self._gid()}; Path=/; Max-Age=2592000; "
                          f"SameSite=Lax")
        for k, v in (extra or []):
            h.send_header(k, self._hdr(v))
        h.end_headers()
        if body:
            h.wfile.write(body)

    @staticmethod
    def _hdr(v) -> str:
        """响应头只允许 latin-1：中文等字符统一转成百分号编码，避免 UnicodeEncodeError 把请求打成 500。"""
        s = str(v)
        try:
            s.encode("latin-1")
            return s
        except UnicodeEncodeError:
            return urllib.parse.quote(s, safe="/:?&=%#[]@!$'()*+,;~.-_")

    def _redirect(self, h, location: str, extra=None) -> None:
        self._send(h, 302, b"", "text/html; charset=utf-8",
                   extra=[("Location", self._hdr(location))] + list(extra or []))

    def _html(self, h, code: int, data: bytes, extra=None) -> None:
        self._send(h, code, data, "text/html; charset=utf-8", extra)

    def _error(self, h, code: int, title: str, detail: str = "") -> None:
        body = (f'<div class="card pad"><div class="empty">{icon("alert", 40)}'
                f'<h2 style="margin:10px 0 6px">{esc(title)}</h2>'
                f'<p class="muted">{esc(detail)}</p>'
                f'<p><a class="btn btn-primary" href="/">{icon("grid", 16)} 回到概览</a></p></div></div>')
        self._html(h, code, self._shell("none", title, "出错了", body))

    # -- 账号 / 会话 -------------------------------------------------------
    def _sign_session(self, payload: str) -> str:
        mac = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{payload}.{mac}"

    def _verify_session(self, value: str):
        try:
            payload, mac = value.rsplit(".", 1)
        except ValueError:
            return None
        want = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, want):
            return None
        return payload

    def _cookie_value(self, payload: str, ttl: int = SESSION_TTL) -> str:
        exp = int(time.time()) + ttl
        return f"{payload}.{exp}"

    def account_cookie(self, acc) -> str:
        """账号会话：u<id>.<pw_ver>.<exp>.<签名>（改密码/禁用后自动失效）"""
        return self._sign_session(f"u{acc['id']}.{acc['pw_ver']}.{int(time.time()) + SESSION_TTL}")

    def password_cookie(self) -> str:
        """应急口令会话（owner 级）：p.<exp>.<签名>"""
        return self._sign_session(f"p.{int(time.time()) + SESSION_TTL}")

    # -- 两步验证（TOTP） --------------------------------------------------
    def twofa_cookie(self, acc_id: int) -> str:
        """密码正确但还没过二次验证时的临时凭证（10 分钟）。"""
        return self._sign_session(f"2fa.{acc_id}.{int(time.time()) + 600}")

    def _pending_2fa(self, cookie_header: str | None) -> int | None:
        """解析临时凭证，返回待验证的账号 id（过期/伪造返回 None）。"""
        if not cookie_header:
            return None
        try:
            jar = SimpleCookie()
            jar.load(cookie_header)
            raw = jar[TWOFA_COOKIE].value if TWOFA_COOKIE in jar else ""
            payload = self._verify_session(raw) if raw else None
            if not payload:
                return None
            parts = payload.split(".")
            if parts[0] != "2fa" or int(parts[2]) < time.time():
                return None
            return int(parts[1])
        except Exception:
            return None

    def _login_2fa(self, h, method: str, qs) -> None:
        """二次验证页：输 6 位动态码，或用一次性恢复码。"""
        cookie = h.headers.get("Cookie")
        acc_id = self._pending_2fa(cookie)
        if not acc_id:
            return self._redirect(h, "/login")
        acc = self.db.get_account(acc_id)
        if not acc or acc["status"] != "active":
            return self._redirect(h, "/login")

        if method == "GET":
            body = (f'<h2 style="margin:0 0 6px">两步验证</h2>'
                    f'<p class="muted" style="margin:0 0 14px">请输入 {esc(acc["display_name"] or "你的账号")} '
                    f'的动态验证码（6 位数字），或一次性恢复码。</p>'
                    f'<form method="post" action="/login/2fa">'
                    f'<div style="margin:6px 0 14px"><input type="text" name="code" autofocus '
                    f'inputmode="numeric" autocomplete="one-time-code" placeholder="6 位动态码 / 恢复码"></div>'
                    f'<button class="btn btn-primary" type="submit" style="width:100%">验证并登录</button></form>'
                    f'<p class="muted" style="font-size:12px;margin-top:14px">'
                    f'换手机或丢了验证器？用保存的恢复码登录，然后去「我的」重新绑定。</p>')
            return self._html(h, 200, self._login_shell("两步验证", body))

        form = self._read_form(h)
        code = (form.get("code") or [""])[0].strip()
        ip = h.client_address[0] if h.client_address else "-"
        if self.db.count_attempts("2fa", ip, 900) >= 10:
            return self._html(h, 200, self._login_shell(
                "两步验证", '<div class="alert bad"><span>尝试次数过多，请 15 分钟后再试</span></div>'))
        row = self.db.get_totp(acc_id)
        good = totp_verify(row["totp_secret"], code) if row and row["totp_enabled"] else False
        used_backup = False
        if not good and code:
            good = self.db.consume_backup_code(acc_id, code)
            used_backup = good
        if not good:
            self.db.note_attempt("2fa", ip)
            self.db.record_audit(acc["tg_user_id"], acc["display_name"], "twofa_fail",
                                 target=str(acc_id), detail=f"来源 {ip}")
            return self._html(h, 200, self._login_shell(
                "两步验证", '<div class="alert bad"><span>验证码不正确</span></div>'
                            '<p><a class="btn" href="/login/2fa">再试一次</a></p>'))
        self.db.touch_login(acc_id, ip)
        self.db.record_audit(acc["tg_user_id"], acc["display_name"], "web_login_ok",
                             target=ip, detail=("恢复码登录" if used_backup else "两步验证通过"))
        self.log(f"两步验证通过：账号 #{acc_id}"
                 + ("（使用了恢复码）" if used_backup else ""))
        return self._send(h, 302, b"", "text/html; charset=utf-8",
                          extra=[("Set-Cookie",
                                  f"{SESSION_COOKIE}={self.account_cookie(acc)}; Path=/; "
                                  f"Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax"),
                                 ("Set-Cookie",
                                  f"{TWOFA_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"),
                                 ("Location", "/me" + ("?ok=2fa_backup" if used_backup else ""))])

    def _twofa_page(self, h) -> None:
        u = self.current_user()
        acc = self.db.get_account(u["acc_id"]) if u and u.get("acc_id") else None
        if not acc:
            return self._error(h, 403, "需要独立账号", "应急口令登录无法设置两步验证。")
        row = self.db.get_totp(acc["id"])
        if row and row["totp_enabled"]:
            n = 0
            try:
                n = len(json.loads(row["totp_backup"] or "[]"))
            except Exception:
                n = 0
            body = (f'<div class="card pad"><h2 style="margin:0 0 12px">🔐 两步验证：已开启</h2>'
                    f'<p class="muted">每次用用户名/密码登录时，会额外要求输入动态验证码。</p>'
                    f'<p class="muted">剩余恢复码：<b>{n}</b> 个</p>'
                    f'<form method="post" action="/me/2fa/off" class="row" style="margin-top:14px">'
                    f'{self._ginput()}'
                    f'<div style="max-width:200px;flex:1"><input type="password" name="password" '
                    f'placeholder="输入密码确认"></div>'
                    f'<button class="btn btn-danger" type="submit">关闭两步验证</button></form>'
                    f'<form method="post" action="/me/2fa/newbackup" style="margin-top:10px">'
                    f'{self._ginput()}'
                    f'<button class="btn" type="submit">重新生成恢复码</button></form></div>')
            return self._html(h, 200, self._shell("me", "两步验证", "已开启", body))

        # 未开启：生成待确认的密钥
        pending = self.db.get_kv(f"totp_pending:{acc['id']}") or ""
        if not pending:
            pending = new_totp_secret()
            self.db.set_kv(f"totp_pending:{acc['id']}", pending)
        uri = totp_uri(pending, acc["username"] or acc["email"] or f"acc{acc['id']}")
        qr = ""
        try:
            import qrgen
            qr = f'<div class="qr" style="margin:14px 0">{qrgen.svg(uri, scale=3, border=2)}</div>'
        except Exception:
            qr = ""
        body = (f'<div class="card pad"><h2 style="margin:0 0 12px">🔐 开启两步验证</h2>'
                f'<p class="muted">1）用 <b>Google Authenticator</b> / <b>微软验证器</b> / '
                f'<b>Authy</b> 扫下面的二维码（或手输密钥）<br>'
                f'2）把 App 里显示的 6 位数字填进来完成绑定</p>'
                f'{qr}'
                f'<div class="linkbox mono" style="word-break:break-all">密钥：{esc(pending)}</div>'
                f'<form method="post" action="/me/2fa/on" class="row" style="margin-top:14px">'
                f'{self._ginput()}'
                f'<div style="max-width:180px"><input type="text" name="code" inputmode="numeric" '
                f'placeholder="6 位动态码"></div>'
                f'<button class="btn btn-primary" type="submit">确认开启</button></form></div>')
        self._html(h, 200, self._shell("me", "两步验证", "未开启", body))

    def _post_twofa(self, h, path: str) -> None:
        u = self.current_user()
        acc = self.db.get_account(u["acc_id"]) if u and u.get("acc_id") else None
        if not acc:
            return self._error(h, 403, "需要独立账号", "")
        form = self._read_form(h)
        if path == "/me/2fa/on":
            secret = self.db.get_kv(f"totp_pending:{acc['id']}") or ""
            code = (form.get("code") or [""])[0].strip()
            if not secret or not totp_verify(secret, code):
                return self._html(h, 200, self._login_shell(
                    "开启失败", '<div class="alert bad"><span>验证码不对，请确认手机时间准确后重试</span></div>'
                                '<p><a class="btn" href="/me/2fa">返回</a></p>'))
            codes = new_backup_codes(8)
            self.db.set_totp(acc["id"], secret, True, json.dumps([hash_backup_code(c) for c in codes]))
            self.db.set_kv(f"totp_pending:{acc['id']}", "")
            self.db.record_audit(acc["tg_user_id"], acc["display_name"], "twofa_enabled",
                                 target=str(acc["id"]))
            self.log(f"账号 #{acc['id']} 已开启两步验证")
            listing = "".join(f'<div class="mono" style="padding:4px 0">{esc(c)}</div>' for c in codes)
            return self._html(h, 200, self._login_shell(
                "两步验证已开启", '<div class="alert ok"><span>✅ 已开启</span></div>'
                                  '<p class="muted">下面这些<b>恢复码</b>只显示这一次，请抄下来保存：</p>'
                                  f'<div class="linkbox">{listing}</div>'
                                  '<p><a class="btn btn-primary" href="/me">回到「我的」</a></p>'))
        if path == "/me/2fa/off":
            if not verify_password((form.get("password") or [""])[0],
                                   acc["pw_hash"], acc["pw_salt"], acc["pw_iter"]):
                return self._redirect(h, "/me/2fa")
            self.db.set_totp(acc["id"], None, False, None)
            self.db.record_audit(acc["tg_user_id"], acc["display_name"], "twofa_disabled",
                                 target=str(acc["id"]))
            return self._redirect(h, "/me?ok=2fa_off")
        if path == "/me/2fa/newbackup":
            codes = new_backup_codes(8)
            self.db.set_totp(acc["id"], self.db.get_totp(acc["id"])["totp_secret"], True,
                             json.dumps([hash_backup_code(c) for c in codes]))
            self.db.record_audit(acc["tg_user_id"], acc["display_name"], "twofa_backup",
                                 target=str(acc["id"]))
            listing = "".join(f'<div class="mono" style="padding:4px 0">{esc(c)}</div>' for c in codes)
            return self._html(h, 200, self._login_shell(
                "新的恢复码", '<p class="muted">旧恢复码已全部作废，请保存新的：</p>'
                              f'<div class="linkbox">{listing}</div>'
                              '<p><a class="btn btn-primary" href="/me">回到「我的」</a></p>'))
        return self._error(h, 404, "未知操作", "")

    # -- CSRF --------------------------------------------------------------
    def _csrf_token(self) -> str:
        """每个会话一个 CSRF 令牌：只绑会话，不绑群（切群/旧页面不会错配）。"""
        return hmac.new(self.secret.encode(),
                        f"csrf:{self._csrf_seed()}".encode(),
                        hashlib.sha256).hexdigest()[:32]

    def _csrf_seed(self) -> str:
        u = self.current_user()
        if not u:
            return "-"
        return f"{u['acc_id']}-{u['role']}"

    def csrf_for_cookie(self, cookie_header: str) -> str:
        """给测试/脚本用：算出某个会话对应的 CSRF 令牌。"""
        old_user, old_gid = getattr(self._tl, "user", None), getattr(self._tl, "gid", None)
        try:
            self._tl.user = self._resolve_user(cookie_header)
            self._tl.gid = self._pick_group({}, cookie_header)
            return self._csrf_token()
        finally:
            self._tl.user, self._tl.gid = old_user, old_gid

    def _csrf_ok(self, form: dict) -> bool:
        tok = (form.get("csrf") or [""])[0]
        return bool(tok) and hmac.compare_digest(tok, self._csrf_token())

    def _read_body(self, h) -> dict:
        try:
            length = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        # 上限 8MB：LOGO/背景图走 base64 表单字段（2MB 图片 → 约 2.7MB 文本）。
        # 旧代码 64KB 上限会把大图截断成"只上传一半"。
        # 循环读到满，防止底层短读/分批到达导致的截断。
        raw = b""
        if 0 < length <= 8 * 1024 * 1024:
            try:
                h.connection.settimeout(20)
                while len(raw) < length:
                    chunk = h.rfile.read(min(65536, length - len(raw)))
                    if not chunk:
                        break
                    raw += chunk
            except OSError:
                pass
        return urllib.parse.parse_qs(raw.decode("utf-8", "replace") if raw else "")

    def _resolve_user(self, cookie_header: str | None):
        """返回当前登录身份；未登录返回 None。

        user = {kind, acc_id, uid, name, role, is_owner, groups:[chat_id...]}
        """
        if not cookie_header:
            return None
        c = SimpleCookie()
        try:
            c.load(cookie_header)
        except Exception:
            return None
        m = c.get(SESSION_COOKIE)
        if not m:
            return None
        payload = self._verify_session(m.value)
        if not payload:
            return None
        parts = payload.split(".")
        now = int(time.time())
        # 应急口令（只在签发时校验是否允许，已登录的会话不因后面关开关被踢下线）
        if parts[0] == "p":
            if not (self.pw_hash or self.password):
                return None
            try:
                if int(parts[1]) < now:
                    return None
            except (IndexError, ValueError):
                return None
            return {"kind": "password", "acc_id": None, "uid": None, "name": "应急入口",
                    "role": "owner", "is_owner": True,
                    "groups": [g["chat_id"] for g in self._groups()]}
        # 账号会话
        if parts[0].startswith("u"):
            try:
                acc_id = int(parts[0][1:])
                pw_ver = int(parts[1])
                exp = int(parts[2])
            except (IndexError, ValueError):
                return None
            if exp < now:
                return None
            acc = self.db.get_account(acc_id)
            if not acc or acc["status"] != "active" or int(acc["pw_ver"]) != pw_ver:
                return None
            role = acc["role"] if acc["role"] in ("owner", "admin") else "member"
            is_owner = role == "owner" or (acc["tg_user_id"] and
                                           acc["tg_user_id"] in self.owner_tg_ids)
            if is_owner:
                role = "owner"
            if role in ("owner", "admin"):
                groups = [g["chat_id"] for g in self._groups()]
                admin_groups = list(groups)
            else:
                # 缓存过期/缺失时，从 Telegram 实时刷新一次（限流：同一用户 5 分钟一次）
                self._refresh_group_cache(acc)
                # 看数据：只要"和机器人同在的群"；能管理：必须是**有实权**的群管理
                groups = [g for g in self.db.member_groups_of(acc["tg_user_id"] or 0)
                          if any(c["chat_id"] == g for c in self._groups())]
                admin_groups = [g for g in self.db.admin_groups_of(acc["tg_user_id"] or 0)
                                if any(c["chat_id"] == g for c in self._groups())]
            return {"kind": "account", "acc_id": acc_id, "uid": acc["tg_user_id"],
                    "name": acc["display_name"] or acc["username"] or acc["email"] or f"#{acc_id}",
                    "role": role, "is_owner": is_owner, "groups": groups,
                    "admin_groups": admin_groups, "acc": acc}
        return None

    def _refresh_group_cache(self, acc) -> None:
        """群身份缓存过期时，从 Telegram 实时补一次（否则新晋升的管理员要等下一次登录才生效）。"""
        try:
            uid = acc["tg_user_id"]
        except (KeyError, IndexError, TypeError):
            return
        if not uid or not self.bot:
            return
        refresher = getattr(self.bot, "refresh_user_admins", None)
        if not refresher:
            return
        try:
            last = int(self.db.get_kv(f"garefresh:{uid}") or 0)
        except (TypeError, ValueError):
            last = 0
        if int(time.time()) - last < 300:
            return
        cutoff = int(time.time()) - 6 * 3600
        fresh = self.db.conn.execute(
            "SELECT 1 FROM group_admins WHERE user_id=? AND checked_at>=?", (uid, cutoff)).fetchone()
        if fresh:
            return
        self.db.set_kv(f"garefresh:{uid}", str(int(time.time())))
        try:
            refresher({"id": uid})
        except Exception:
            pass

    @property
    def owner_tg_ids(self) -> set:
        return {int(x) for x in (self.cfg.get("owner_ids") or [])}

    def _allow_password_login(self) -> bool:
        v = self.db.get_setting("web_allow_password_login")
        if v is None:
            return bool(self.cfg.get("web_allow_password_login", True))
        return str(v).lower() in ("true", "1", "yes")

    def current_user(self):
        return getattr(self._tl, "user", None)

    def user_can(self, chat_id: int) -> bool:
        u = self.current_user()
        if not u:
            return False
        if u["role"] in ("owner", "admin"):
            return True
        return chat_id in u["groups"]

    def require_admin(self) -> bool:
        """平台级管理员（超管/被授予 admin 的账号）：能看设置、审计、账号管理。"""
        u = self.current_user()
        return bool(u and u["role"] in ("owner", "admin"))

    def require_manager(self) -> bool:
        """有管理能力：平台管理员，或者**在群里拥有实权**的群管理员。"""
        u = self.current_user()
        if not u:
            return False
        if u["role"] in ("owner", "admin"):
            return True
        return bool(u.get("admin_groups"))

    def managed_groups(self) -> list:
        """当前用户能管理的群（平台管理员 = 全部）。"""
        u = self.current_user()
        if not u:
            return []
        if u["role"] in ("owner", "admin"):
            return [g["chat_id"] for g in self._groups()]
        return list(u.get("admin_groups") or [])

    def is_group_admin(self, chat_id: int) -> bool:
        """平台管理员，或者在这个群里是**有实权**的 Telegram 管理员。"""
        return chat_id in self.managed_groups()

    # -- 邮箱（可选，配了 SMTP 才启用） -------------------------------------
    def smtp_ready(self) -> bool:
        return bool((self.cfg.get("smtp_host") or "").strip() and
                    (self.cfg.get("smtp_from") or "").strip())

    def send_mail(self, to: str, subject: str, body: str) -> bool:
        if not self.smtp_ready():
            return False
        import smtplib
        from email.mime.text import MIMEText
        from email.header import Header
        cfg = self.cfg
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = cfg.get("smtp_from")
        msg["To"] = to
        try:
            port = int(cfg.get("smtp_port") or 465)
            if port == 465:
                srv = smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=20)
            else:
                srv = smtplib.SMTP(cfg["smtp_host"], port, timeout=20)
                if cfg.get("smtp_tls", True):
                    srv.starttls()
            user = cfg.get("smtp_user")
            if user:
                srv.login(user, cfg.get("smtp_pass") or "")
            srv.sendmail(cfg["smtp_from"], [to], msg.as_string())
            srv.quit()
            self.log(f"验证邮件已发送至 {to}")
            return True
        except Exception as e:
            self.log(f"发送邮件失败（{to}）：{e}", "ERROR")
            return False

    def _emergency_page(self, h, method: str) -> None:
        """隐藏的应急口令入口：只有知道秘密地址的人能到这里，登录页上没有任何入口。

        不受「允许密码登录」总开关限制——应急入口是救命通道，
        被开关锁死会让所有人（包括管理员自己）进不来。
        """
        if not (self.pw_hash or self.password):
            return self._send(h, 404, b"", "text/plain; charset=utf-8")
        if method == "GET":
            body = ('<h2 style="margin:0 0 6px">应急入口</h2>'
                    '<p class="muted" style="margin:0 0 14px">此入口已隐藏，仅在需要时使用。</p>'
                    '<form method="post" action="">'
                    '<div style="margin:6px 0 14px"><input type="password" name="password" autofocus '
                    'placeholder="应急口令"></div>'
                    '<button class="btn btn-primary" type="submit" style="width:100%">进入</button></form>')
            return self._html(h, 200, self._login_shell("应急入口", body))
        form = self._read_form(h)
        pwd = (form.get("password") or [""])[0]
        ip = h.client_address[0] if h.client_address else "-"
        if self.db.count_attempts("emergency", ip, 900) >= 10:
            return self._html(h, 200, self._login_shell(
                "应急入口", '<div class="alert bad"><span>尝试次数过多，请稍后再试。</span></div>'))
        # 应急口令只与加盐哈希比对（OWASP：单向慢哈希，配置里不存明文）
        if self.pw_hash and verify_password(pwd, self.pw_hash, self.pw_salt, self.pw_iter):
            self.db.record_audit(None, "emergency", "web_login_ok", target=ip,
                                 detail="使用管理员应急口令登录（隐藏入口）")
            self.log(f"⚠️ 应急口令登录成功（隐藏入口）：{ip}", "WARN")
            return self._send(h, 302, b"", "text/html; charset=utf-8",
                              extra=[("Set-Cookie",
                                      f"{SESSION_COOKIE}={self.password_cookie()}; Path=/; "
                                      f"Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax"),
                                     ("Location", "/")])
        self.db.note_attempt("emergency", ip)
        self.db.record_audit(None, "emergency", "web_login_fail", target=ip, detail="应急口令错误")
        self.log(f"应急口令登录失败（隐藏入口）：{ip}", "WARN")
        return self._html(h, 200, self._login_shell(
            "应急入口", '<div class="alert bad"><span>应急口令不正确。</span></div>'))

    def _verify_email(self, h, qs) -> None:
        token = (qs.get("t") or [""])[0]
        acc_id = self.db.consume_email_token(token, "verify") if token else None
        if not acc_id:
            return self._html(h, 200, self._login_shell(
                "验证失败", '<div class="alert bad"><span>链接无效或已过期</span></div>'
                            '<p><a class="btn" href="/login">去登录</a></p>'))
        self.db.set_email_verified(acc_id)
        self.db.record_audit(None, "web", "email_verified", target=str(acc_id))
        return self._html(h, 200, self._login_shell(
            "验证成功", '<div class="alert ok"><span>邮箱已验证 ✅</span></div>'
                        '<p><a class="btn btn-primary" href="/login">去登录</a></p>'))

    def _login_page(self, h, error: str | None, next_path: str = "/") -> None:
        tabs = ("<div class='tabs'><a href='/login' class='on'>Telegram 登录</a>"
                "<a href='/login?m=pw'>用户名 / 密码</a>"
                + ("<a href='/login?m=em'>邮箱 / 密码</a>" if True else "")
                + "<a href='/register'>注册</a></div>")
        err = (f'<div class="alert bad" style="margin-bottom:16px">{icon("alert", 16)}'
               f'<span>{esc(error)}</span></div>') if error else ""
        body = (f'{err}{tabs}'
                f'<p class="muted" style="margin:0 0 14px">用你的 Telegram 账号登录，'
                f'只影响你自己能看到的内容；账号之间互不可见。</p>'
                f'<form method="post" action="/login/start">'
                f'<input type="hidden" name="next" value="{esc(next_path)}">'
                f'<button class="btn btn-primary" type="submit" style="width:100%">'
                f'{icon("bolt", 16)} 用 Telegram 登录</button></form>')
        self._html(h, 200, self._login_shell("登录", body))

    def _login(self, h, method: str, qs) -> None:
        """GET 显示登录页；POST 处理 用户名/邮箱 + 密码 登录。"""
        mode = (qs.get("m") or ["tg"])[0]
        err_map = {
            "code": "确认码不正确或已过期。请在 Telegram 机器人回复里找最新的 6 位码（有效期 15 分钟）。",
            "closed": "站点已关闭新账号注册，请联系管理员。",
            "disabled": "这个账号已被管理员禁用。",
            "expired": "登录凭证已过期，请重新发起登录。",
            "wait": "还没有收到 Telegram 的确认。请先在 Telegram 里给机器人发送 /start，然后重试。",
            "create": "账号创建失败，请稍后重试或联系管理员。",
            "toomany": "尝试次数过多，请 15 分钟后再试。",
        }
        err_key = (qs.get("err") or [""])[0]
        page_err = err_map.get(err_key)
        if method == "GET":
            if mode == "pw":
                return self._password_form(h, "用户名 / 密码", "username", "/login?m=pw")
            if mode == "em":
                return self._password_form(h, "邮箱 / 密码", "email", "/login?m=em")
            return self._login_page(h, page_err)
        form = self._read_form(h)
        ident = (form.get("ident") or [""])[0].strip()
        pwd = (form.get("password") or [""])[0]
        nxt = (form.get("next") or ["/"])[0]
        if not nxt.startswith("/"):
            nxt = "/"
        ip = h.client_address[0] if h.client_address else "-"
        # 限速：IP 与账号分别计数
        if self.db.count_attempts("login_ip", ip, 900) >= 20 or \
                self.db.count_attempts("login_user", ident.lower(), 900) >= 8:
            self.db.note_attempt("login_fail", ip)
            return self._login_page(h, "尝试次数过多，请 15 分钟后再试。")
        acc = self.db.get_account_by_username(ident) or self.db.get_account_by_email(ident)
        if acc and acc["status"] == "active" and \
                verify_password(pwd, acc["pw_hash"], acc["pw_salt"], acc["pw_iter"]):
            self.db.note_attempt("login_ok", ip)
            # 开了两步验证 → 先发临时凭证，去 /login/2fa 输动态码
            trow = self.db.get_totp(acc["id"])
            if trow and trow["totp_enabled"]:
                self.db.record_audit(acc["tg_user_id"], acc["display_name"], "web_login_step1",
                                     target=ip, detail=f"账号 #{acc['id']} 密码通过，等待两步验证")
                self.log(f"密码通过，等待两步验证：{ident} from {ip}")
                return self._send(h, 302, b"", "text/html; charset=utf-8",
                                  extra=[("Set-Cookie",
                                          f"{TWOFA_COOKIE}={self.twofa_cookie(acc['id'])}; Path=/; "
                                          f"Max-Age=600; HttpOnly; SameSite=Lax"),
                                         ("Location", "/login/2fa")])
            self.db.note_attempt("login_ok", ip)
            self.db.touch_login(acc["id"], ip)
            self.db.record_audit(acc["tg_user_id"], acc["display_name"] or ident,
                                 "web_login_ok", target=ip, detail=f"账号 #{acc['id']} 密码登录")
            self._alert_login(acc["tg_user_id"], ip, h.headers.get("User-Agent") or "")
            self.log(f"账号登录成功：{ident} from {ip}")
            return self._send(h, 302, b"", "text/html; charset=utf-8",
                              extra=[("Set-Cookie",
                                      f"{SESSION_COOKIE}={self.account_cookie(acc)}; Path=/; "
                                      f"Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax"),
                                     ("Location", nxt)])
        self.db.note_attempt("login_ip", ip)
        self.db.note_attempt("login_user", ident.lower())
        self.db.record_audit(None, "web", "web_login_fail", target=ip, detail=f"账号 {ident}")
        self.log(f"账号登录失败：{ident} from {ip}", "WARN")
        return self._login_page(h, "账号或密码不正确。")

    def _alert_login(self, tg_user_id, ip: str, ua: str) -> None:
        """登录成功后私聊提醒本人（不是自己操作就能第一时间发现）。"""
        if not tg_user_id or not self.bot:
            return
        try:
            self.bot.submit(self.bot.notify_login, int(tg_user_id), ip, ua)
        except Exception:
            pass

    def _password_form(self, h, title: str, placeholder: str, action: str) -> None:
        err = ""
        body = (f'<div class="tabs"><a href="/login">Telegram 登录</a>'
                f'<a href="/login?m=pw"{" class=\'on\'" if "pw" in action else ""}>用户名 / 密码</a>'
                f'<a href="/login?m=em"{" class=\'on\'" if "em" in action else ""}>邮箱 / 密码</a>'
                f'<a href="/register">注册</a></div>'
                f'{err}<form method="post" action="/login">'
                f'<label class="muted" style="font-size:12.5px">{"用户名或邮箱" if False else title}</label>'
                f'<div style="margin:6px 0 12px"><input type="text" name="ident" '
                f'placeholder="{esc(placeholder)}" autofocus autocomplete="username"></div>'
                f'<label class="muted" style="font-size:12.5px">密码</label>'
                f'<div style="margin:6px 0 16px"><input type="password" name="password" '
                f'autocomplete="current-password"></div>'
                f'<button class="btn btn-primary" type="submit" style="width:100%">登录</button>'
                f'</form>')
        self._html(h, 200, self._login_shell(title, body))

    def _login_start(self, h) -> None:
        """生成一次性登录凭证，让用户去 Telegram 里点一下确认。"""
        form = self._read_form(h)
        nxt = (form.get("next") or ["/"])[0]
        if not nxt.startswith("/"):
            nxt = "/"
        nonce = uuid.uuid4().hex[:24]
        # 6 位确认码：跨设备兜底（手机上确认，电脑上输码），不依赖 fetch/Set-Cookie
        code = f"{secrets.randbelow(1000000):06d}"
        try:
            self.db.create_login_nonce(nonce, code=code)
        except TypeError:                      # 老版本 Storage 兼容
            self.db.create_login_nonce(nonce)
            code = ""
        uname = ""
        try:
            uname = self.bot.bot_username if self.bot else ""
        except Exception:
            uname = ""
        uname = uname or (self.cfg.get("bot_username") or "")
        deep = f"https://t.me/{uname}?start=lg_{nonce}"
        qr = ""
        try:
            import qrgen
            qr = (f'<div class="qr" style="margin:14px auto;display:block;width:max-content">'
                  f'{qrgen.svg(deep, scale=4, border=2)}</div>')
        except Exception:
            qr = ""
        body = (f'<h2 style="margin:0 0 6px">用 Telegram 确认登录</h2>'
                f'<p class="muted" style="margin:0 0 14px">1）点下面的按钮打开 Telegram<br>'
                f'2）给机器人发送 <code>/start</code><br>3）这里会自动进入；'
                f'没反应就用下面两种方式之一</p>'
                f'<a class="btn btn-primary" href="{esc(deep)}" target="_blank" rel="noopener" '
                f'style="width:100%;justify-content:center">{icon("bolt", 16)} 打开 Telegram 确认</a>'
                f'{qr}'
                f'<p class="muted mono" style="font-size:11px;word-break:break-all">{esc(deep)}</p>'
                f'<p id="st" class="muted">⏳ 等待你在 Telegram 里确认…</p>'
                f'<div style="margin:10px 0 6px">'
                f'<button class="btn" type="button" style="width:100%;justify-content:center" '
                f'onclick="location.href=\'/login/poll?go=1&n={nonce}\'">'
                f'我已经在 Telegram 确认了 · 立即进入</button></div>'
                f'<div class="card pad" style="margin-top:12px">'
                f'<p class="muted" style="margin:0 0 8px">手机上确认的？把 Telegram 机器人回复里的 '
                f'<b>6 位确认码</b>填到这里：</p>'
                f'<form method="post" action="/login/code" class="row">'
                f'<input type="hidden" name="next" value="{esc(nxt)}">'
                f'<div style="max-width:160px;flex:1"><input type="text" name="code" inputmode="numeric" '
                f'maxlength="6" placeholder="6 位确认码" autocomplete="one-time-code"></div>'
                f'<button class="btn btn-primary" type="submit">提交</button></form></div>'
                f'<script>var n="{nonce}";var tm=setInterval(function(){{'
                f'fetch("/login/poll?n="+n,{{cache:"no-store",credentials:"same-origin"}})'
                f'.then(function(r){{return r.json()}}).then(function(d){{'
                f'if(d.ok){{clearInterval(tm);'
                f'document.getElementById("st").textContent="✅ 登录成功，正在跳转…";'
                f'location.href="{esc(nxt)}";}}else if(d.expired){{clearInterval(tm);'
                f'document.getElementById("st").textContent="⌛️ 已过期，请重新发起登录";}}}})'
                f'.catch(function(){{}});}},2000);</script>')
        self._html(h, 200, self._login_shell("Telegram 登录", body))

    def _grant_session_for_tg(self, h, uid: int, first_name: str, go: bool, nxt: str = "/"):
        """根据 Telegram uid 找到/创建账号并签发网页会话。

        go=True  ：302 跳转（顶层导航，Set-Cookie 一定生效，跨设备兜底路径）
        go=False ：返回 JSON（页面轮询路径）
        """
        acc = self.db.get_account_by_tg(uid)
        if not acc:
            if not self._registration_open():
                return (self._redirect(h, "/login?err=closed") if go else
                        self._json(h, {"ok": False, "expired": False, "error": "closed"}))
            try:
                acc_id = self.db.create_account(
                    tg_user_id=uid, display_name=first_name,
                    role="owner" if self.db.account_count() == 0 else "member")
                acc = self.db.get_account(acc_id)
                self.db.record_audit(uid, first_name, "account_create",
                                     target=str(acc_id), detail="Telegram 首次登录自动建号")
            except ValueError:
                acc = self.db.get_account_by_tg(uid)
            if not acc:
                return (self._redirect(h, "/login?err=create") if go else
                        self._json(h, {"ok": False, "expired": False, "error": "create_failed"}))
        if acc["status"] != "active":
            return (self._redirect(h, "/login?err=disabled") if go else
                    self._json(h, {"ok": False, "expired": False, "error": "disabled"}))
        ip = h.client_address[0] if h.client_address else "-"
        self.db.touch_login(acc["id"], ip)
        self.db.record_audit(uid, acc["display_name"], "web_login_ok", target="telegram",
                             detail=f"账号 #{acc['id']} Telegram 登录")
        self.log(f"Telegram 登录成功：uid={uid} 账号 #{acc['id']}")
        self._alert_login(uid, ip, h.headers.get("User-Agent") or "")
        cookie = (f"{SESSION_COOKIE}={self.account_cookie(acc)}; Path=/; "
                  f"Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax")
        if go:
            return self._send(h, 302, b"", "text/html; charset=utf-8",
                              extra=[("Set-Cookie", cookie), ("Location", nxt)])
        return self._send(h, 200, b'{"ok":true}', "application/json; charset=utf-8",
                          extra=[("Set-Cookie", cookie)])

    def _login_code(self, h) -> None:
        """确认码登录：顶层表单提交，Set-Cookie 必定生效（跨设备兜底）。"""
        form = self._read_form(h)
        code = (form.get("code") or [""])[0].strip()
        nxt = (form.get("next") or ["/"])[0]
        if not nxt.startswith("/"):
            nxt = "/"
        ip = h.client_address[0] if h.client_address else "-"
        if self.db.count_attempts("login_code", ip, 900) >= 20:
            return self._redirect(h, "/login?err=toomany")
        row = self.db.get_login_nonce_by_code(code)
        if not row or not row["used_at"] or not row["user_id"]:
            self.db.note_attempt("login_code", ip)
            self.db.record_audit(None, "web", "web_login_fail", target=ip,
                                 detail=f"确认码错误 {code[:6]}")
            self.log(f"确认码登录失败（{code[:6]}）from {ip}", "WARN")
            return self._redirect(h, "/login?err=code")
        return self._grant_session_for_tg(h, int(row["user_id"]), row["first_name"] or "",
                                          go=True, nxt=nxt)

    def _login_poll(self, h, qs) -> None:
        nonce = (qs.get("n") or [""])[0]
        go = (qs.get("go") or [""])[0] in ("1", "true", "yes")
        row = self.db.get_login_nonce(nonce) if nonce else None
        if not row:
            return (self._redirect(h, "/login?err=expired") if go else
                    self._json(h, {"ok": False, "expired": True}))
        if not row["used_at"]:
            return (self._redirect(h, "/login?err=wait") if go else
                    self._json(h, {"ok": False, "expired": False}))
        return self._grant_session_for_tg(h, int(row["user_id"]), row["first_name"] or "",
                                          go=go)

    def _json(self, h, obj: dict) -> None:
        self._send(h, 200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _registration_open(self) -> bool:
        mode = self.db.get_setting("reg_mode", self.cfg.get("reg_mode", "invite"))
        return str(mode) != "closed"

    def _register(self, h, method: str, qs) -> None:
        mode = str(self.db.get_setting("reg_mode", self.cfg.get("reg_mode", "invite")))
        if mode == "closed" and self.db.account_count() > 0:
            return self._html(h, 200, self._login_shell(
                "注册已关闭", '<p class="muted">管理员关闭了注册。请联系管理员开号。</p>'))
        if method == "GET":
            need_code = mode == "invite" and self.db.account_count() > 0
            body = (f'<h2 style="margin:0 0 6px">注册账号</h2>'
                    f'<p class="muted" style="margin:0 0 14px">用户名或邮箱二选一即可；'
                    f'也可以直接 <a href="/login">用 Telegram 登录</a>自动建号。</p>'
                    f'<form method="post" action="/register">'
                    f'<label class="muted" style="font-size:12.5px">用户名（3-32 位字母数字 _ . -）</label>'
                    f'<div style="margin:6px 0 12px"><input type="text" name="username"></div>'
                    f'<label class="muted" style="font-size:12.5px">邮箱（可选）</label>'
                    f'<div style="margin:6px 0 12px"><input type="text" name="email"></div>'
                    f'<label class="muted" style="font-size:12.5px">密码（至少 8 位）</label>'
                    f'<div style="margin:6px 0 12px"><input type="password" name="password" '
                    f'autocomplete="new-password"></div>'
                    + (f'<label class="muted" style="font-size:12.5px">邀请码</label>'
                       f'<div style="margin:6px 0 12px"><input type="text" name="code"></div>'
                       if need_code else "")
                    + f'<button class="btn btn-primary" type="submit" style="width:100%">注册</button>'
                      f'</form><p class="muted" style="font-size:12px">注册后如需要群数据权限，'
                      f'请把账号绑定 Telegram（在「我的」页面），或让管理员给你开权限。</p>')
            return self._html(h, 200, self._login_shell("注册", body))
        form = self._read_form(h)
        username = (form.get("username") or [""])[0].strip()
        email = (form.get("email") or [""])[0].strip() or None
        password = (form.get("password") or [""])[0]
        code = (form.get("code") or [""])[0].strip()
        if username and not valid_username(username):
            return self._login_shell_error(h, "用户名格式不对（3-32 位字母数字 _ . -）")
        if email and not valid_email(email):
            return self._login_shell_error(h, "邮箱格式不对")
        if len(password) < 8:
            return self._login_shell_error(h, "密码至少 8 位")
        if not username and not email:
            return self._login_shell_error(h, "用户名和邮箱至少填一个")
        if mode == "invite" and self.db.account_count() > 0:
            row = self.db.conn.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()
            if not row or row["used_at"]:
                return self._login_shell_error(h, "邀请码无效或已被使用")
        try:
            role = "owner" if self.db.account_count() == 0 else "member"
            acc_id = self.db.create_account(username=username or None, email=email,
                                            password=password, role=role)
        except ValueError as e:
            return self._login_shell_error(h, str(e))
        if mode == "invite" and code:
            self.db.use_invite_code(code, acc_id)
        self.db.record_audit(None, "web", "account_create", target=str(acc_id),
                             detail=f"注册 {username or email}（{mode}）")
        if email and self.smtp_ready():
            token = self.db.create_email_token(acc_id, "verify")
            link = f"{self.base_url}/verify?t={token}"
            self.send_mail(email, "验证你的邮箱", f"点击链接完成验证：\n{link}\n\n如果不是你操作，请忽略。")
        acc = self.db.get_account(acc_id)
        self.log(f"新注册账号 #{acc_id}：{username or email}")
        return self._send(h, 302, b"", "text/html; charset=utf-8",
                          extra=[("Set-Cookie",
                                  f"{SESSION_COOKIE}={self.account_cookie(acc)}; Path=/; "
                                  f"Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax"),
                                 ("Location", "/me")])

    def _login_shell_error(self, h, msg: str) -> None:
        return self._html(h, 200, self._login_shell(
            "出错了", f'<div class="alert bad">{icon("alert", 16)}<span>{esc(msg)}</span></div>'
                      f'<p><a class="btn" href="/register">返回注册</a></p>'))

    # -- 链接申请（仅管理员审批） ------------------------------------------
    def _linkreq(self, h, qs) -> None:
        managed = self.managed_groups()
        rows = [r for r in self.db.pending_link_requests(limit=200)
                if self.require_admin() or r["chat_id"] in managed]
        ok = (qs.get("ok") or [""])[0]
        head = (f'<div class="alert ok" style="margin-bottom:14px">{icon("check", 16)}'
                f'<span>{esc(ok)}</span></div>') if ok else ""
        trs = []
        for r in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["requested_at"]))
            who = r["user_name"] or f'用户 {r["user_id"]}'
            trs.append(
                f'<tr><td><div class="who">{self._avatar_img(r["user_id"], who)}<span class="nm">{esc(who)}</span></div>'
                f'<div class="muted mono" style="font-size:11.5px">{r["user_id"]}</div></td>'
                f'<td>{esc(r["chat_title"] or r["chat_id"])}</td><td class="n">{when}</td>'
                f'<td class="n"><form method="post" action="/linkreq" class="row" style="gap:6px">'
                f'<input type="hidden" name="id" value="{r["id"]}">'
                f'<button class="btn btn-sm btn-primary" name="action" value="approve">批准</button>'
                f'<button class="btn btn-sm" name="action" value="reject">驳回</button>'
                f'</form></td></tr>')
        table = ('<div class="tablewrap"><table><thead><tr><th>申请人</th><th>群</th>'
                 '<th class="n">申请时间</th><th class="n">操作</th></tr></thead><tbody>'
                 + ("".join(trs) or f'<tr><td colspan="4"><div class="empty">{icon("link", 34)}'
                                    f'<p>没有待处理的链接申请</p></div></td></tr>')
                 + "</tbody></table></div>")
        tip = ('<div class="alert" style="margin-top:16px">' + icon("shield", 16) +
               '<span>普通成员没有「生成邀请链接」的入口，只能在这里提交申请；'
               '批准后机器人会自动创建他的专属链接并私聊发给他。</span></div>')
        self._html(h, 200, self._shell("lreq", "链接申请", f"待处理 {len(rows)} 条",
                                       head + table + tip))

    def _post_linkreq(self, h) -> None:
        if not self.require_admin():
            return self._error(h, 403, "需要管理员权限", "")
        form = self._read_form(h)
        rid = (form.get("id") or [""])[0]
        action = (form.get("action") or [""])[0]
        if not rid.isdigit():
            return self._send(h, 302, b"", "text/html; charset=utf-8",
                              extra=[("Location", "/linkreq?ok=参数不对")])
        actor = self.current_user().get("uid") or 0
        if self.bot:
            if action == "approve":
                ok, msg = self.bot.approve_link_request(int(rid), actor)
                text = "已批准并已把链接发给对方" if ok else f"失败：{msg}"
            else:
                ok = self.bot.reject_link_request(int(rid), actor)
                text = "已驳回" if ok else "失败：申请不存在或已处理"
        else:
            # 没接机器人时至少把状态落库（避免申请卡死）
            status = "approved" if action == "approve" else "rejected"
            ok = self.db.decide_link_request(int(rid), status, actor, "管理员处理")
            text = "已处理（未接机器人，未自动建链）" if ok else "失败：申请不存在或已处理"
        return self._send(h, 302, b"", "text/html; charset=utf-8",
                          extra=[("Location", f"/linkreq?ok={urllib.parse.quote(text)}")])

    # -- 我的（个人中心） --------------------------------------------------
    def _me(self, h, qs) -> None:
        u = self.current_user()
        acc = self.db.get_account(u["acc_id"]) if u and u["acc_id"] else None
        saved = (qs.get("ok") or [""])[0]
        notes = {"pw": "密码已更新，其他设备上的登录已失效",
                 "tg": "Telegram 绑定已更新", "tg_off": "已解绑 Telegram",
                 "mail": "验证邮件已发送" if self.smtp_ready() else "未配置邮件服务，无法发送验证邮件"}
        head = (f'<div class="alert ok" style="margin-bottom:14px">{icon("check", 16)}'
                f'<span>{esc(notes.get(saved, "已保存"))}</span></div>') if saved else ""

        if acc:
            tg = (f'已绑定 Telegram（ID <code>{acc["tg_user_id"]}</code>）'
                  if acc["tg_user_id"] else "未绑定 —— 绑定后可用 Telegram 一键登录")
            role_cn = {"owner": "👑 超级管理员", "admin": "🛡 管理员",
                       "member": "成员"}.get(acc["role"], acc["role"])
            info = (f'<div class="card pad"><h2 style="margin:0 0 12px">账号</h2><div class="list">'
                    f'<div class="item">用户名 <span class="t">{esc(acc["username"] or "—")}</span></div>'
                    f'<div class="item">邮箱 <span class="t">{esc(acc["email"] or "—")}'
                    f'{" ✅已验证" if acc["email_verified"] else (" ⚠️未验证" if acc["email"] else "")}</span></div>'
                    f'<div class="item">角色 <span class="t">{esc(role_cn)}'
                    f'{" · 拥有全部权限" if acc["role"] == "owner" else ""}</span></div>'
                    f'<div class="item">Telegram <span class="t">{tg}</span></div>'
                    f'<div class="item">上次登录 <span class="t">'
                    f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(acc["last_login"])) if acc["last_login"] else "—"}'
                    f' · {esc(acc["last_ip"] or "")}</span></div></div></div>')
            pw_form = (f'<div class="card pad" style="margin-top:16px"><h2 style="margin:0 0 12px">'
                       f'{"改密码" if acc["pw_hash"] else "设置密码"}</h2>'
                       + (f'<form method="post" action="/me/pw" class="row">{self._ginput()}'
                          f'<div style="flex:1;min-width:160px"><input type="password" name="old" '
                          f'placeholder="当前密码"></div>'
                          f'<div style="flex:1;min-width:160px"><input type="password" name="new" '
                          f'placeholder="新密码（至少 8 位）"></div>'
                          f'<button class="btn btn-primary" type="submit">保存</button></form>'
                          if acc["pw_hash"] else
                          f'<form method="post" action="/me/pw-init" class="row">{self._ginput()}'
                          f'<div style="flex:1;min-width:180px"><input type="password" name="new" '
                          f'placeholder="新密码（至少 8 位）"></div>'
                          f'<button class="btn btn-primary" type="submit">设置密码</button></form>'
                          f'<p class="muted" style="margin:10px 0 0">你还没设过密码，直接设一个即可'
                          f'——以后不依赖 Telegram 也能用「用户名 + 密码」登录。</p>')
                       + (f'<p class="muted" style="margin:10px 0 0">改完密码后，其他设备会被强制退出。</p>'
                          if acc["pw_hash"] else "")
                       + '</div>')
            login_card = (
                f'<div class="card pad" style="margin-top:16px"><h2 style="margin:0 0 12px">'
                f'🔑 登录方式 / 账号绑定</h2>'
                f'<p class="muted" style="margin:0 0 14px">给这个账号加上「用户名」和「邮箱」，'
                f'之后不依赖 Telegram 也能登录（邮箱用于找回密码）。</p>'
                f'<form method="post" action="/me/username" class="row">{self._ginput()}'
                f'<div style="flex:1;min-width:190px"><input type="text" name="username" '
                f'placeholder="用户名（3-20 位字母/数字/下划线）" value="{esc(acc["username"] or "")}" '
                f'autocomplete="username"></div>'
                f'<button class="btn btn-primary" type="submit">保存用户名</button></form>'
                f'<form method="post" action="/me/email" class="row" style="margin-top:10px">{self._ginput()}'
                f'<div style="flex:1;min-width:190px"><input type="text" name="email" '
                f'placeholder="绑定邮箱（留空则解绑）" value="{esc(acc["email"] or "")}" '
                f'autocomplete="email"></div>'
                f'<button class="btn btn-primary" type="submit">保存邮箱</button></form>'
                f'<p class="muted" style="margin:10px 0 0">'
                f'{"邮箱服务未配置，绑定后无法收验证邮件（不影响登录，只影响找回密码）。" if not self.smtp_ready() else "邮箱用于找回密码与安全提醒。"}'
                f'</p></div>')
            tg_form = (f'<div class="card pad" style="margin-top:16px"><h2 style="margin:0 0 12px">Telegram</h2>'
                       + (f'<form method="post" action="/me/tg" class="row">{self._ginput()}'
                          f'<button class="btn" type="submit">{icon("bolt", 15)} 重新绑定 / 解绑</button>'
                          f'</form>' if acc["tg_user_id"] else
                          f'<form method="post" action="/me/tg" class="row">{self._ginput()}'
                          f'<button class="btn btn-primary" type="submit">{icon("bolt", 15)} '
                          f'去 Telegram 绑定</button></form>')
                       + f'<p class="muted" style="margin:10px 0 0">绑定后可以用 Telegram 一键登录，'
                         f'并且如果你的账号是群管理员，会自动获得对应群的查看权限。</p></div>')
        else:
            info = (f'<div class="card pad muted">当前是应急口令登录（无独立账号）。'
                    f'建议 <a href="/register">注册一个账号</a> 并绑定 Telegram。</div>')
            pw_form = tg_form = login_card = ""

        stats = self._me_stats()
        twofa = ""
        if acc:
            trow = self.db.get_totp(acc["id"])
            on = bool(trow and trow["totp_enabled"])
            n_bk = 0
            if on:
                try:
                    n_bk = len(json.loads(trow["totp_backup"] or "[]"))
                except Exception:
                    n_bk = 0
            state = (f'<span class="badge b-ok">已开启</span>　剩余恢复码 {n_bk} 个'
                     if on else '<span class="badge b-warn">未开启</span>')
            twofa = (f'<div class="card pad" style="margin-top:16px"><h2 style="margin:0 0 10px">'
                     f'🔐 两步验证</h2><p style="margin:0 0 12px">{state}</p>'
                     f'<p class="muted" style="margin:0 0 12px">开启后，用「用户名/密码」登录还要输手机上的 '
                     f'6 位动态码 —— 就算密码泄露也进不来。'
                     f'（用 Telegram 登录不受影响）</p>'
                     f'<a class="btn {"btn-danger" if on else "btn-primary"}" href="/me/2fa">'
                     f'{"管理两步验证" if on else "立即开启"}</a></div>')
        body = head + info + stats + login_card + pw_form + tg_form + twofa
        self._html(h, 200, self._shell("me", "我的", f"{u['name']} · {u['role']}", body))

    def _me_stats(self) -> str:
        u = self.current_user()
        uid = u.get("uid")
        if not uid:
            return ""
        allow = set(u.get("groups") or [])
        blocks = []
        for c in self._groups():
            if c["chat_id"] not in allow:
                continue
            gid = c["chat_id"]
            invited = self.db.count_for(uid, gid)
            present = self.db.current_for(uid, gid)
            rank = self.db.rank_of(uid, gid)
            link_row = self.db.get_link_by_owner(uid, gid)
            link = link_row["invite_link"] if (link_row and not link_row["revoked"]) else None
            if link:
                action_html = f'<div class="linkbox">{esc(link)}</div>'
            else:
                req = self.db.my_link_request(gid, uid)
                if req and req["status"] == "pending":
                    action_html = ('<div class="alert warn">' + icon("alert", 16) +
                                   '<span>你的邀请链接申请已提交，等待管理员批准。</span></div>')
                elif req and req["status"] == "rejected":
                    action_html = (f'<div class="alert bad">{icon("alert", 16)}'
                                   f'<span>上次申请被驳回{("：" + esc(req["note"])) if req["note"] else ""}；'
                                   f'可以重新申请。</span></div>'
                                   f'<form method="post" action="/me/linkreq" style="margin-top:10px">{self._ginput()}'
                                   f'<button class="btn btn-primary" type="submit">重新申请专属链接</button>'
                                   f'</form>')
                else:
                    action_html = ('<p class="muted" style="margin:0 0 10px">'
                                   '普通成员不能自己生成邀请链接，需要管理员批准。</p>'
                                   f'<form method="post" action="/me/linkreq">{self._ginput()}'
                                   '<button class="btn btn-primary" type="submit">'
                                   + icon("link", 16) + ' 申请专属邀请链接</button></form>')
                qr = ""
            qr = ""
            if link:
                try:
                    import qrgen
                    qr = f'<div class="qr" style="margin-top:10px">{qrgen.svg(link, scale=3, border=2)}</div>'
                except Exception:
                    qr = ""
            blocks.append(
                f'<div class="card pad" style="margin-top:16px">'
                f'<h2 style="margin:0 0 12px">{esc(c["title"])}</h2>'
                f'<div class="stats">'
                f'{self._stat("累计邀请", invited, "人", "link")}'
                f'{self._stat("仍在群", present, "人", "users", tone="ok")}'
                f'{self._stat("排名", rank or "—", f"/ {self.db.leaderboard_size(gid)}", "trophy", tone="warn")}'
                f'</div><div style="margin-top:14px">{action_html}{qr}</div></div>')
        return "".join(blocks)

    def access_groups_for_personal(self, uid: int) -> list:
        """这个 Telegram 用户在哪些群有邀请记录（个人中心展示用）。"""
        out = []
        for c in self._groups():
            if self.db.count_for(uid, c["chat_id"]) or self.db.get_link_by_owner(uid, c["chat_id"]):
                out.append(c)
        return out

    def _post_me(self, h, path: str) -> None:
        u = self.current_user()
        acc = self.db.get_account(u["acc_id"]) if u and u["acc_id"] else None
        if not acc:
            return self._error(h, 403, "应急口令登录无法修改资料", "请先注册独立账号。")
        form = self._read_form(h)
        if path == "/me/pw-init":
            # 用 Telegram 登录进来的账号还没设过密码 → 免旧密码直接设置
            if acc["pw_hash"]:
                return self._redirect(h, "/me?ok=" + urllib.parse.quote(
                    "已有密码，请用「改密码」修改"))
            new = (form.get("new") or [""])[0]
            if len(new) < 8:
                return self._redirect(h, "/me?ok=" + urllib.parse.quote("新密码至少 8 位"))
            self.db.set_password(acc["id"], new)
            self.db.record_audit(acc["tg_user_id"], acc["display_name"], "password_set",
                                 target=str(acc["id"]), detail="Telegram 登录后自助设置密码")
            self.log(f"账号 #{acc['id']} 自助设置密码")
            return self._redirect(h, "/me?ok=" + urllib.parse.quote(
                "密码已设置，现在可以用「用户名 / 密码」登录了"))
        if path == "/me/username":
            uname = (form.get("username") or [""])[0].strip()
            if uname and not re.match(r"^[A-Za-z0-9_]{3,20}$", uname):
                return self._redirect(h, "/me?ok=" + urllib.parse.quote(
                    "用户名只能是 3-20 位字母、数字或下划线"))
            try:
                self.db.set_username(acc["id"], uname or None)
            except ValueError as e:
                return self._redirect(h, "/me?ok=" + urllib.parse.quote(str(e)))
            self.db.record_audit(acc["tg_user_id"], acc["display_name"], "username_set",
                                 target=str(acc["id"]), detail=f"用户名={uname or '（清空）'}")
            return self._redirect(h, "/me?ok=" + urllib.parse.quote(
                f"用户名已保存：{uname}" if uname else "已解除用户名绑定"))
        if path == "/me/email":
            email = (form.get("email") or [""])[0].strip()
            if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                return self._redirect(h, "/me?ok=" + urllib.parse.quote("邮箱格式不正确"))
            try:
                self.db.set_email(acc["id"], email or None, verified=0)
            except ValueError as e:
                return self._redirect(h, "/me?ok=" + urllib.parse.quote(str(e)))
            self.db.record_audit(acc["tg_user_id"], acc["display_name"], "email_set",
                                 target=str(acc["id"]), detail=f"邮箱={email or '（解绑）'}")
            if not email:
                return self._redirect(h, "/me?ok=" + urllib.parse.quote("已解除邮箱绑定"))
            # 配了邮件服务就发验证邮件（与注册流程同一套令牌）
            if self.smtp_ready():
                try:
                    token = self.db.create_email_token(acc["id"], "verify")
                    link = f"{self.base_url}/verify?t={token}"
                    self.send_mail(email, "验证你的邮箱",
                                   f"点击链接完成验证：\n{link}\n\n如果不是你操作，请忽略。")
                    return self._redirect(h, "/me?ok=" + urllib.parse.quote(
                        "邮箱已保存，验证邮件已发送"))
                except Exception as e:
                    self.log(f"验证邮件发送失败：{e}", "WARN")
                    return self._redirect(h, "/me?ok=" + urllib.parse.quote(
                        "邮箱已保存（验证邮件发送失败，可稍后重试）"))
            return self._redirect(h, "/me?ok=" + urllib.parse.quote(
                "邮箱已保存（未配置邮件服务，暂不发送验证邮件）"))
        if path == "/me/pw":
            old = (form.get("old") or [""])[0]
            new = (form.get("new") or [""])[0]
            if not verify_password(old, acc["pw_hash"], acc["pw_salt"], acc["pw_iter"]):
                return self._html(h, 200, self._login_shell(
                    "改密码", '<div class="alert bad"><span>当前密码不正确</span></div>'
                              '<p><a class="btn" href="/me">返回</a></p>'))
            if len(new) < 8:
                return self._html(h, 200, self._login_shell(
                    "改密码", '<div class="alert bad"><span>新密码至少 8 位</span></div>'
                              '<p><a class="btn" href="/me">返回</a></p>'))
            self.db.set_password(acc["id"], new)
            self.db.record_audit(acc["tg_user_id"], acc["display_name"], "password_change",
                                 target=str(acc["id"]))
            return self._send(h, 302, b"", "text/html; charset=utf-8",
                              extra=[("Location", "/login?ok=pw")])
        if path == "/me/linkreq":
            gid = self._gid()
            if not gid:
                return self._send(h, 302, b"", "text/html; charset=utf-8",
                                  extra=[("Location", "/me?ok=没有可申请的群")])
            if not acc["tg_user_id"]:
                return self._send(h, 302, b"", "text/html; charset=utf-8",
                                  extra=[("Location", "/me?ok=请先绑定 Telegram 再申请")])
            name = acc["display_name"] or acc["username"] or f'#{acc["id"]}'
            created = self.db.request_link(gid, acc["tg_user_id"], name)
            self.db.record_audit(acc["tg_user_id"], name, "link_request",
                                 target=str(acc["tg_user_id"]), chat_id=gid,
                                 detail="申请专属邀请链接")
            if created and self.bot:
                try:
                    self.bot.submit(self.bot.notify_link_request, gid, acc["tg_user_id"], name)
                except Exception:
                    pass
            return self._send(h, 302, b"", "text/html; charset=utf-8",
                              extra=[("Location", "/me?ok=" + urllib.parse.quote(
                                  "申请已提交，等管理员批准" if created else "你已经有一条待处理申请了"))])
        if path == "/me/tg":
            if acc["tg_user_id"]:
                self.db.unlink_telegram(acc["id"])
                self.db.set_group_admin(0, acc["tg_user_id"], "unlinked")
                return self._send(h, 302, b"", "text/html; charset=utf-8",
                                  extra=[("Location", "/me?ok=tg_off")])
            nonce = uuid.uuid4().hex[:24]
            self.db.create_login_nonce(nonce)
            self.db.set_kv(f"bindacc:{nonce}", str(acc["id"]))
            uname = (self.bot.bot_username if self.bot else "") or self.cfg.get("bot_username") or ""
            return self._send(h, 302, b"", "text/html; charset=utf-8",
                              extra=[("Location", f"https://t.me/{uname}?start=bd_{nonce}")])
        return self._error(h, 404, "未知操作", "")

    # -- 账号管理（超管） --------------------------------------------------
    def _admin_accounts(self, h, qs) -> None:
        q = (qs.get("q") or [""])[0].strip()
        accs = self.db.list_accounts(limit=200, q=q or None)
        ok = (qs.get("ok") or [""])[0]
        head = (f'<div class="alert ok" style="margin-bottom:14px">{icon("check", 16)}'
                f'<span>{esc(ok)}</span></div>') if ok else ""
        search = ('<form method="get" action="/admin/accounts" class="toolbar">'
                  f'<div style="max-width:280px;flex:1"><input type="text" name="q" value="{esc(q)}" '
                  'placeholder="搜索用户名 / 邮箱 / Telegram ID"></div>'
                  '<button class="btn btn-primary" type="submit">搜索</button>'
                  + ('<a class="btn btn-ghost" href="/admin/accounts">清除</a>' if q else "")
                  + "</form>")
        trs = []
        for a in accs:
            role = {"owner": "👑 超级管理员", "admin": "🛡 管理员",
                    "member": "成员"}.get(a["role"], a["role"])
            tg = f'<code>{a["tg_user_id"]}</code>' if a["tg_user_id"] else "—"
            state = ('<span class="badge b-ok">正常</span>' if a["status"] == "active"
                     else '<span class="badge b-bad">已禁用</span>')
            who = a["display_name"] or a["username"] or a["email"] or f'#{a["id"]}'
            trs.append(f'<tr><td class="c">{a["id"]}</td>'
                       f'<td><div class="who">{self._avatar(who)}<span class="nm">{esc(who)}</span></div>'
                       f'<div class="muted" style="font-size:11.5px">'
                       f'{esc(a["username"] or "")} {esc(a["email"] or "")}</div></td>'
                       f'<td>{esc(role)}</td><td>{tg}</td>'
                       f'<td class="n">{a["admin_groups"] or 0}</td><td>{state}</td>'
                       f'<td class="n">'
                       f'{time.strftime("%m-%d %H:%M", time.localtime(a["last_login"])) if a["last_login"] else "—"}'
                       f'</td><td class="n">'
                       f'<form method="post" action="/admin/accounts" class="row" style="gap:6px">{self._ginput()}'
                       f'<input type="hidden" name="acc" value="{a["id"]}">'
                       f'<select name="action" style="width:auto"><option value="">操作…</option>'
                       f'<option value="role:admin">设为管理员</option>'
                       f'<option value="role:member">设为成员</option>'
                       f'<option value="role:owner">设为超级管理员</option>'
                       f'<option value="disable">禁用</option>'
                       f'<option value="enable">启用</option></select>'
                       f'<button class="btn btn-sm" type="submit">执行</button></form>'
                       f'</td></tr>')
        table = ('<div class="tablewrap"><table><thead><tr><th class="c">ID</th><th>账号</th>'
                 '<th>角色</th><th>Telegram</th><th class="n">管理群</th><th>状态</th>'
                 '<th class="n">上次登录</th><th>操作</th></tr></thead><tbody>'
                 + ("".join(trs) or '<tr><td colspan="8"><div class="empty">还没有账号</div></td></tr>')
                 + "</tbody></table></div>")
        codes = self.db.invite_codes(30)
        now = int(time.time())
        ctrs = []
        for c in codes:
            keys = c.keys()
            exp = c["expires_at"] if "expires_at" in keys else None
            revoked = c["revoked"] if "revoked" in keys else 0
            if c["used_at"]:
                state, tone = "✅ 已使用", "b-ok"
            elif revoked:
                state, tone = "🚫 已作废", "b-bad"
            elif exp and now > int(exp):
                state, tone = "⌛️ 已过期", "b-mute"
            else:
                state, tone = "⏳ 可用", "b-warn"
            exp_txt = (time.strftime("%Y-%m-%d %H:%M", time.localtime(int(exp))) if exp else "永久")
            act = ("<span class='muted'>—</span>" if (c["used_at"] or revoked or (exp and now > int(exp)))
                   else f'<form method="post" action="/admin/accounts" style="margin:0">{self._ginput()}'
                        f'<input type="hidden" name="action" value="revoke">'
                        f'<input type="hidden" name="code" value="{c["code"]}">'
                        f'<button class="btn btn-sm btn-danger" type="submit">作废</button></form>')
            ctrs.append(f'<tr><td class="mono"><code>{c["code"]}</code></td>'
                        f'<td><span class="badge {tone}">{state}</span></td>'
                        f'<td class="n">{exp_txt}</td>'
                        f'<td class="muted">{esc(c["note"] or "")}</td><td class="n">{act}</td></tr>')
        code_card = ('<div class="card pad" style="margin-top:16px"><h2 style="margin:0 0 6px">邀请码</h2>'
                     '<p class="muted" style="margin:0 0 12px">默认 7 天有效、一次性使用；'
                     '作废后立即失效。</p>'
                     f'<form method="post" action="/admin/accounts" class="row">{self._ginput()}'
                     '<input type="hidden" name="action" value="newcode">'
                     '<div style="max-width:200px;flex:1"><input type="text" name="note" '
                     'placeholder="备注（可选）"></div>'
                     '<div style="max-width:140px"><select name="ttl">'
                     '<option value="7">7 天有效</option><option value="1">1 天</option>'
                     '<option value="30">30 天</option><option value="0">永久有效</option>'
                     '</select></div>'
                     '<button class="btn btn-primary" type="submit">生成邀请码</button></form>'
                     '<div class="tablewrap" style="margin-top:12px"><table><thead><tr><th>邀请码</th>'
                     '<th>状态</th><th class="n">到期</th><th>备注</th><th class="n">操作</th>'
                     '</tr></thead><tbody>'
                     + ("".join(ctrs) or '<tr><td colspan="5" class="muted">暂无</td></tr>')
                     + "</tbody></table></div></div>")
        legend = ('<div class="card pad" style="margin-top:16px"><h2 style="margin:0 0 12px">'
                  '角色与权限对照</h2><div class="tablewrap"><table><thead><tr><th>角色</th>'
                  '<th>能看到</th><th>能操作</th></tr></thead><tbody>'
                  '<tr><td><b>👑 超级管理员</b></td><td>全部群、全部页面</td>'
                  '<td><b>所有权限</b>：审批申请、发链接、账号管理、功能设置、审计日志、'
                  '批量清理（含踢人开关）</td></tr>'
                  '<tr><td>🛡 管理员</td><td>全部群、全部页面</td>'
                  '<td>同超管，但<b>不能</b>改动超管账号</td></tr>'
                  '<tr><td>🛡 群管理员<br><span class="muted">（群里<b>有实权</b>的管理）</span></td>'
                  '<td>只限他管理的群</td><td>审批该群的入群申请 / 链接申请</td></tr>'
                  '<tr><td>成员</td><td>自己和机器人同在的群的<b>数据</b></td>'
                  '<td>看战绩、申请专属链接（需管理员批准）</td></tr>'
                  '</tbody></table></div>'
                  '<p class="muted" style="margin:12px 0 0">要给别人超管：在下面那一行的下拉框里选'
                  '「设为 owner」再点执行。</p></div>')
        cap_note = ('<div class="alert" style="margin:12px 0 0">' + icon("alert", 16) +
                    '<span>列表最多显示 200 个账号；更多请用上方搜索框按用户名/邮箱/Telegram ID 定位。</span></div>') \
            if len(accs) >= 200 else ""
        self._html(h, 200, self._shell("acc", "账号管理",
                                       f"共 {self.db.account_count()} 个账号"
                                       + (f"（筛选：{q}）" if q else ""),
                                       head + search + table + legend + cap_note + code_card))

    def _post_accounts(self, h) -> None:
        if not self.require_admin():
            return self._error(h, 403, "需要管理员权限", "")
        form = self._read_form(h)
        action = (form.get("action") or [""])[0]
        if action == "newcode":
            try:
                ttl = int((form.get("ttl") or ["7"])[0])
            except ValueError:
                ttl = 7
            code = self.db.create_invite_code((form.get("note") or [""])[0], None, ttl_days=ttl)
            self.db.record_audit(None, "web", "invite_code_new", target=code,
                                 detail=f"有效期 {ttl} 天")
            return self._redirect(h, f"/admin/accounts?ok=新邀请码 {code}")
        if action == "revoke":
            code = (form.get("code") or [""])[0]
            ok = self.db.revoke_invite_code(code)
            self.db.record_audit(None, "web", "invite_code_revoke", target=code,
                                 detail="作废邀请码" if ok else "作废失败")
            return self._redirect(h, f"/admin/accounts?ok={'已作废 ' + code if ok else '作废失败'}")
        acc_id = (form.get("acc") or [""])[0]
        if not acc_id.isdigit():
            return self._send(h, 302, b"", "text/html; charset=utf-8",
                              extra=[("Location", "/admin/accounts")])
        acc = self.db.get_account(int(acc_id))
        me = self.current_user()
        if not acc:
            return self._send(h, 302, b"", "text/html; charset=utf-8",
                              extra=[("Location", "/admin/accounts?ok=账号不存在")])
        if acc["role"] == "owner" and not me["is_owner"]:
            return self._send(h, 302, b"", "text/html; charset=utf-8",
                              extra=[("Location", "/admin/accounts?ok=不能操作 owner")])
        if action.startswith("role:"):
            self.db.set_account_role(acc["id"], action.split(":", 1)[1])
            msg = f"已调整 #{acc['id']} 角色"
        elif action == "disable":
            self.db.set_account_status(acc["id"], "disabled")
            msg = f"已禁用 #{acc['id']}"
        elif action == "enable":
            self.db.set_account_status(acc["id"], "active")
            msg = f"已启用 #{acc['id']}"
        else:
            msg = "未识别操作"
        self.db.record_audit(None, "web", "account_admin", target=str(acc["id"]), detail=f"{action} · {msg}")
        return self._send(h, 302, b"", "text/html; charset=utf-8",
                          extra=[("Location", f"/admin/accounts?ok={urllib.parse.quote(msg)}")])

    # -- 概览（后台首页） --------------------------------------------------
    def _overview(self, h, qs) -> None:
        chat = self._chat()
        if not chat:
            return self._not_bound(h)
        gid = chat["chat_id"]
        g = self.db.group_totals(gid)
        a = self.db.activity_totals(gid)
        d = self.db.deleted_stats(gid)
        j = self.db.join_requests_stats(gid)
        r7 = self.db.retention_cohort(gid, 7)
        rate7 = f'{r7["rate"] * 100:.0f}%' if r7["rate"] is not None else "—"

        stats = (f'<div class="stats">'
                 f'{self._stat("累计邀请", g["invited"], "人", "link", sub=f"近 24 小时 +{g['last24h']}")}'
                 f'{self._stat("仍在群", g["present"], "人", "users", tone="ok", sub=f"参与人数 {g['inviters']}")}'
                 f'{self._stat("日活跃", a["dau"], "人", "pulse", sub=f"周活 {a['wau']} · 月活 {a['mau']}")}'
                 f'{self._stat("7 日留存", rate7, "", "trend-up", tone="warn", sub=f"{r7['retained']}/{r7['matured']} 人样本")}'
                 f'{self._stat("待处理申请", j["pending"], "条", "inbox", sub=f"累计 {j['total']} · 自动拒 {j['auto_declined']}")}'
                 f'{self._stat("疑似已注销", d["total"], "人", "user-x", tone="bad", sub=f"已移出 {d['kicked']}")}'
                 f'</div>')

        # 近 14 天增长
        chart = self._bar_chart(gid, 14)

        # Top5 + 最近操作
        top = self.db.leaderboard(gid, limit=5)
        trs = []
        for i, row in enumerate(top, 1):
            name = self._who(row["inviter_id"])
            trs.append(f'<tr><td class="c"><span class="rank r{i}">{i}</span></td>'
                       f'<td><div class="who">{self._avatar(name)}<span class="nm">{esc(name)}</span></div></td>'
                       f'<td class="n"><b>{row["invited"]}</b></td><td class="n">{row["present"]}</td></tr>')
        top_card = ('<div class="card pad"><div class="row" style="justify-content:space-between">'
                    '<h2 style="margin:0">邀请榜 Top5</h2>'
                    '<a class="btn btn-sm btn-ghost" href="/leaderboard">查看全部</a></div>'
                    '<div style="margin:10px -18px -18px">'
                    '<table><thead><tr><th class="c">#</th><th>成员</th><th class="n">累计邀请</th>'
                    '<th class="n">在群</th></tr></thead><tbody>'
                    + ("".join(trs) or '<tr><td colspan="4"><div class="empty">还没有数据</div></td></tr>')
                    + '</tbody></table></div></div>')

        logs = self.db.audit_page(limit=6)
        items = []
        for r in logs:
            ts = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
            who = r["actor_name"] or (f"uid:{r['actor_id']}" if r["actor_id"] else "系统")
            label = ACTION_CN.get(r["action"], r["action"])
            items.append(f'<div class="item">{icon("bolt", 15)}<span>{esc(label)}</span>'
                         f'<span class="t">{esc(who)} · {ts}</span></div>')
        audit_card = ('<div class="card pad"><div class="row" style="justify-content:space-between">'
                      '<h2 style="margin:0">最近操作</h2>'
                      '<a class="btn btn-sm btn-ghost" href="/logs">审计日志</a></div>'
                      f'<div class="list">{"".join(items) or "<div class=empty>暂无记录</div>"}</div></div>')

        if self.require_admin():
            quick_inner = (
                f'<form method="post" action="/panel" style="margin:0">{self._ginput()}'
                '<button class="btn" type="submit">' + icon("bolt", 16) + ' 发送群面板</button></form>'
                f'<a class="btn" href="/deleted{self._q()}">' + icon("broom", 16) + ' 扫描已注销</a>'
                f'<a class="btn" href="/logs?export=csv&g={self._gid()}">' + icon("file", 16) + ' 导出审计日志</a>'
                f'<a class="btn" href="/settings{self._q()}">' + icon("sliders", 16) + ' 功能开关</a>')
            quick_note = '「发送群面板」会在群里发一条带按钮的领取链接面板（需机器人是群管理员）。'
        else:
            quick_inner = (f'<a class="btn" href="/me{self._q()}">' + icon("user", 16) + ' 我的战绩</a>'
                           f'<a class="btn" href="/activity{self._q()}">' + icon("pulse", 16) + ' 活跃数据</a>'
                           f'<a class="btn" href="/leaderboard{self._q()}">' + icon("trophy", 16) + ' 排行榜</a>')
            quick_note = '普通成员只能查看数据；生成邀请链接、审批申请等操作仅管理员可用。'
        quick = ('<div class="card pad"><h2 style="margin:0 0 12px">快捷操作</h2><div class="row">'
                 + quick_inner +
                 f'</div><p class="muted" style="margin:12px 0 0">{quick_note}</p></div>')

        body = stats + '<div style="height:18px"></div>' + \
            f'<div class="split">{chart}{audit_card}</div><div style="height:18px"></div>' + \
            f'<div class="split">{top_card}{quick}</div>'
        self._html(h, 200, self._shell(
            "overview", "概览", f"{chat['title']} · 数据实时同步",
            body, actions=((f'<form method="post" action="/panel" style="margin:0">{self._ginput()}'
                            f'<button class="btn btn-sm" type="submit">{icon("bolt", 15)} 群面板</button>'
                            '</form>') if self.require_admin() else '')))

    # -- 排行榜 ------------------------------------------------------------
    def _leaderboard(self, h, qs, public: bool = False) -> None:
        chat = self._chat()
        if not chat:
            return self._not_bound(h)
        gid = chat["chat_id"]
        q = (qs.get("q") or [""])[0].strip()
        try:
            page = max(0, int((qs.get("page") or ["0"])[0]))
        except ValueError:
            page = 0
        per = 20
        rows = self.db.leaderboard(gid, limit=2000, offset=0)
        names = {r["inviter_id"]: self._who(r["inviter_id"]) for r in rows}
        if q:
            rows = [r for r in rows if q.lower() in names[r["inviter_id"]].lower()]
        total = len(rows)
        pages = max(1, (total + per - 1) // per)
        page = min(page, pages - 1)
        page_rows = rows[page * per:(page + 1) * per]
        g = self.db.group_totals(gid)

        stats = (f'<div class="stats">'
                 f'{self._stat("累计邀请", g["invited"], "人", "link")}'
                 f'{self._stat("仍在群", g["present"], "人", "users", tone="ok")}'
                 f'{self._stat("参与人数", g["inviters"], "人", "trophy", tone="warn")}'
                 f'{self._stat("近 24 小时", g["last24h"], "人", "pulse")}</div>')

        max_inv = max([r["invited"] for r in page_rows] or [1])
        trs = []
        for i, r in enumerate(page_rows):
            n = page * per + i + 1
            name = names[r["inviter_id"]]
            pct = int(100 * r["invited"] / max_inv) if max_inv else 0
            trs.append(f'<tr><td class="c"><span class="rank {"r%d" % n if n <= 3 else ""}">{n}</span></td>'
                       f'<td><div class="who">{self._avatar(name)}<span class="nm">{esc(name)}</span></div></td>'
                       f'<td class="n"><b>{r["invited"]}</b></td><td class="n">{r["present"]}</td>'
                       f'<td style="width:170px"><div class="bar"><i style="width:{pct}%"></i></div></td></tr>')
        table = ('<div class="tablewrap"><table><thead><tr><th class="c">名次</th><th>成员</th>'
                 '<th class="n">累计邀请</th><th class="n">仍在群</th><th>相对占比</th></tr></thead><tbody>'
                 + ("".join(trs) or f'<tr><td colspan="5"><div class="empty">{icon("trophy", 34)}'
                                    f'<p>还没有人通过专属链接邀请成功</p></div></td></tr>')
                 + "</tbody></table></div>")

        search = (f'<form method="get" action="/leaderboard" class="toolbar">{self._ginput()}'
                  f'<div style="max-width:260px;flex:1">{icon("search", 15)} '
                  f'<input type="text" name="q" value="{esc(q)}" placeholder="搜索昵称" '
                  f'style="display:inline-block;width:calc(100% - 26px)"></div>'
                  f'<button class="btn btn-primary" type="submit">搜索</button>'
                  + (f'<a class="btn btn-ghost" href="/leaderboard{self._q()}">清除</a>' if q else "")
                  + "</form>")
        pager = self._pager("/leaderboard", page, pages, f"q={urllib.parse.quote(q)}")
        if public:
            body = (f'<div class="card pad" style="margin-bottom:16px">'
                    f'<h2 style="margin:0 0 4px">{esc(chat["title"])} · 邀请排行榜</h2>'
                    f'<p class="muted" style="margin:0">公开浏览 · 不显示任何私人数据</p></div>'
                    + table + pager)
            page_html = (f'<!doctype html><html lang="zh-CN" data-theme="dark"><head><meta charset="utf-8">'
                         f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                         f'<meta name="robots" content="noindex,nofollow"><title>邀请排行榜</title>'
                         f'{THEME_BOOT}<style>{CSS}</style></head><body>'
                         f'<div class="app"><div class="main" style="margin-left:0">'
                         f'<header class="topbar"><div class="titles"><h1>邀请排行榜</h1>'
                         f'<p>{esc(self.site)}</p></div><div class="actions">'
                         f'<button class="iconbtn" id="themebtn" onclick="dshToggleTheme()">{icon("sun", 18)}'
                         f'</button></div></header>'
                         f'<main class="content">{body}</main></div></div>'
                         f'{TOGGLE_JS % {"sun": icon("sun", 18), "moon": icon("moon", 18)}}'
                         f'</body></html>').encode("utf-8")
            return self._html(h, 200, page_html)
        self._html(h, 200, self._shell("rank", "邀请排行榜", "谁拉的人最多",
                                       stats + search + table + pager))

    # -- 活跃数据 ----------------------------------------------------------
    def _activity(self, h, qs) -> None:
        chat = self._chat()
        if not chat:
            return self._not_bound(h)
        gid = chat["chat_id"]
        q = (qs.get("q") or [""])[0].strip()
        try:
            page = max(0, int((qs.get("page") or ["0"])[0]))
        except ValueError:
            page = 0
        t = self.db.activity_totals(gid)
        stats = (f'<div class="stats">'
                 f'{self._stat("日活跃", t["dau"], "人", "pulse")}'
                 f'{self._stat("周活跃", t["wau"], "人", "trend-up")}'
                 f'{self._stat("月活跃", t["mau"], "人", "users", tone="ok")}'
                 f'{self._stat("累计消息", t["total_messages"], "条", "bolt", tone="warn")}'
                 f'{self._stat("发过言", t["spoken"], "人", "star")}'
                 f'{self._stat("从未发言", t["silent"], "人", "user-x", tone="bad", sub=f"在群 {t['members']} 人")}'
                 f'</div>')
        per = 30
        rows = self.db.activity_board(gid, limit=5000, offset=0, q=q or None)
        total = len(rows)
        pages = max(1, (total + per - 1) // per)
        page = min(page, pages - 1)
        page_rows = rows[page * per:(page + 1) * per]
        trs = []
        for i, r in enumerate(page_rows):
            n = page * per + i + 1
            u = self.db.get_user(r["user_id"])
            name = r["first_name"] or (u["first_name"] if u else "") or f"用户 {r['user_id']}"
            last = time.strftime("%m-%d %H:%M", time.localtime(r["last_ts"])) if r["last_ts"] else "—"
            st = {"member": '<span class="badge b-ok">在群</span>',
                  "left": '<span class="badge b-mute">已退群</span>',
                  "kicked": '<span class="badge b-bad">被移出</span>'}.get(r["member_status"],
                                                                         '<span class="badge b-mute">—</span>')
            inviter = self._who(r["inviter_id"]) if r["inviter_id"] else "—"
            dtag = ' <span class="badge b-bad">已注销</span>' if r["is_deleted"] else ""
            # 发言条数 + 活跃天数直接做成名字旁的徽章：屏幕再窄也看得到，不依赖表格列
            stat_tags = (f'<div class="row" style="gap:6px;margin-top:4px;flex-wrap:wrap">'
                         f'<span class="pill">💬 {r["messages"]} 条</span>'
                         f'<span class="pill">📅 活跃 {r["active_days"]} 天</span>'
                         f'<span class="pill">🕐 {last}</span></div>')
            trs.append(f'<tr><td class="c">{n}</td>'
                       f'<td><div class="who">{self._avatar_img(r["user_id"], name)}'
                       f'<div style="min-width:0"><div class="nm">{esc(name)}</div>{stat_tags}</div>{dtag}'
                       f'</div></td><td class="n">'
                       f'<span class="pill">💬 {r["messages"]} 条</span></td>'
                       f'<td class="n">{r["active_days"]}</td><td class="n">{last}</td>'
                       f'<td>{st}</td><td class="muted">{esc(inviter)}</td></tr>')
        table = ('<div class="tablewrap"><table class="act"><thead><tr><th class="c">#</th><th>成员</th>'
                 '<th class="n">💬 发言条数</th><th class="n">活跃天数</th><th class="n">最后活跃</th>'
                 '<th>在群状态</th><th>邀请人</th></tr></thead><tbody>'
                 + ("".join(trs) or f'<tr><td colspan="7"><div class="empty">{icon("pulse", 34)}'
                                    f'<p>还没有统计到发言</p></div></td></tr>')
                 + "</tbody></table></div>")
        search = (f'<form method="get" action="/activity" class="toolbar">{self._ginput()}'
                  f'<div style="max-width:260px;flex:1"><input type="text" name="q" value="{esc(q)}" '
                  f'placeholder="搜索昵称"></div><button class="btn btn-primary" type="submit">搜索</button>'
                  + (f'<a class="btn btn-ghost" href="/activity{self._q()}">清除</a>' if q else "") + "</form>")
        scope = ('<div class="alert" style="margin-bottom:14px">' + icon("users", 16) +
                 '<span><b>这张表只列出「机器人统计到发言的人」</b>（当前 '
                 f'<b>{t["spoken"]}</b> 人，共 {pages} 页）。群里另外 <b>{t["silent"]}</b> 个'
                 '没发过言的成员<b>不会出现在这里</b> —— Bot API 没有"列出全部群成员"的接口，'
                 '机器人拿不到他们的名单，只能等他们开口说话后逐个进入榜单。'
                 '想要全量名单可用服务器的 MTProto 工具（member_scanner.py）。</span></div>')
        tip = ('<div class="alert" style="margin-top:16px">' + icon("shield", 16) +
               '<span>只统计条数与时间，<b>不保存任何消息内容</b>。机器人需为群管理员才能收到全部消息，'
               '否则只能统计到命令。</span></div>')
        priv = (self.db.get_kv("bot_privacy_off") or "")
        priv_warn = ""
        if priv in ("False", "false", "0"):
            priv_warn = ('<div class="alert warn" style="margin-bottom:14px">' + icon("alert", 16) +
                         '<span><b>⚠️ 机器人隐私模式还开着</b> —— 现在只能统计到命令、回复和 @它的消息，'
                         '所以每个人的「消息数」会明显偏少。要准确数据：到 <b>@BotFather</b> 发 '
                         '<code>/setprivacy</code> 选 <b>Disable</b>，然后重启机器人服务即可'
                         '（不用重新加群）。</span></div>')
        self._html(h, 200, self._shell("act", "活跃数据", "谁在说话、谁很久没来了",
                                       priv_warn + stats + search + scope + table
                                       + self._pager("/activity", page, pages,
                                                     f"q={urllib.parse.quote(q)}") + tip))

    # -- 质量分 ------------------------------------------------------------
    def _quality(self, h, qs) -> None:
        chat = self._chat()
        if not chat:
            return self._not_bound(h)
        gid = chat["chat_id"]
        scores = self.db.quality_scores(gid, 7,
                                        float(self._setting("quality_prior_weight", 5.0)),
                                        float(self._setting("quality_prior_rate", 0.5)))
        avg = sum(s["score"] for s in scores) / len(scores) if scores else 0
        top = scores[0] if scores else None
        stats = (f'<div class="stats">'
                 f'{self._stat("参与评分", len(scores), "人", "users")}'
                 f'{self._stat("平均质量分", f"{avg:.1f}", "", "star", tone="warn")}'
                 f'{self._stat("最高分", f"{top['score']:.1f}" if top else "—", "", "trophy", tone="ok", sub=self._who(top["inviter_id"]) if top else "")}'
                 f'</div>')
        trs = []
        for i, s in enumerate(scores[:60], 1):
            name = self._who(s["inviter_id"])
            rate = f'{s["raw_rate"] * 100:.0f}%' if s["raw_rate"] is not None else "—"
            tone = "b-ok" if s["score"] >= 70 else ("b-warn" if s["score"] >= 45 else "b-bad")
            trs.append(f'<tr><td class="c"><span class="rank {"r%d" % i if i <= 3 else ""}">{i}</span></td>'
                       f'<td><div class="who">{self._avatar(name)}<span class="nm">{esc(name)}</span></div></td>'
                       f'<td class="n"><span class="badge {tone}">{s["score"]:.1f}</span></td>'
                       f'<td class="n">{s["retained"]}/{s["matured"]} <span class="muted">({rate})</span></td>'
                       f'<td class="n">{s["invited"]}</td><td class="n">{s["churned"]}</td></tr>')
        table = ('<div class="tablewrap"><table><thead><tr><th class="c">#</th><th>成员</th><th class="n">质量分</th>'
                 '<th class="n">7 日留存</th><th class="n">累计邀请</th><th class="n">已流失</th>'
                 '</tr></thead><tbody>' + ("".join(trs) or f'<tr><td colspan="6"><div class="empty">'
                                                          f'{icon("star", 34)}'
                                   f'<p>还没有人通过专属链接邀请成功</p>'
                                   f'<p class="muted" style="font-size:12.5px">'
                                   '质量分 = 邀请来的人 7 天后的留存率：需要先有人用 /link 领专属链接'
                                   '并真的把人拉进群，有邀请记录后这里才会长出数据。</p></div></td></tr>')
                 + "</tbody></table></div>")
        tip = ('<div class="alert" style="margin-top:16px">' + icon("shield", 16) +
               '<span>质量分 = 贝叶斯平滑后的 7 日留存率（0-100）。样本不足的人会被拉向 50 分，'
               '避免「只拉 1 人还没跑」就霸榜。</span></div>')
        self._html(h, 200, self._shell("qua", "质量分", "拉得多，不如拉得稳", stats + table + tip))

    # -- 留存 --------------------------------------------------------------
    def _retention(self, h, qs) -> None:
        chat = self._chat()
        if not chat:
            return self._not_bound(h)
        gid = chat["chat_id"]
        r7 = self.db.retention_cohort(gid, 7)
        f = self.db.funnel(gid)
        avg = self.db.avg_lifetime_days(gid)
        stats = (f'<div class="stats">'
                 f'{self._stat("7 日留存率", f'{r7["rate"] * 100:.1f}%' if r7["rate"] is not None else "—",
                               "", "trend-up", tone="ok", sub=f'{r7["retained"]}/{r7["matured"]} 人样本')}'
                 f'{self._stat("成功入群", f["joined"], "人", "users", sub=f"申请 {f['applied']} 人")}'
                 f'{self._stat("平均在群", f"{avg:.1f}" if avg else "—", "天", "pulse", tone="warn")}'
                 f'{self._stat("7 日后仍在", f["retained_7d"], "人", "shield", sub=f"样本 {f['matured_7d']} 人")}'
                 f'</div>')
        rows = []
        for d in (1, 3, 7, 14, 30):
            r = self.db.retention_cohort(gid, d)
            pct = r["rate"] * 100 if r["rate"] is not None else 0
            rate = f'{pct:.1f}%' if r["rate"] is not None else "—"
            rows.append(f'<tr><td>第 {d} 天</td><td class="n">{r["retained"]} / {r["matured"]}</td>'
                        f'<td class="n"><b>{rate}</b></td>'
                        f'<td style="width:220px"><div class="bar"><i style="width:{pct:.0f}%"></i></div></td></tr>')
        table = ('<div class="tablewrap"><table><thead><tr><th>留存节点</th><th class="n">仍在群 / 样本</th>'
                 '<th class="n">留存率</th><th>可视化</th></tr></thead><tbody>'
                 + "".join(rows) + "</tbody></table></div>")
        growth = self._series(gid, 14)
        chart = self._bar_chart(gid, 14, "近 14 天新增")
        funnel = (f'<div class="card pad"><h2 style="margin:0 0 12px">入群漏斗</h2>'
                  f'<div class="list">'
                  f'<div class="item">{icon("inbox", 15)} 提交申请 <b style="margin-left:auto">{f["applied"]}</b></div>'
                  f'<div class="item">{icon("users", 15)} 成功入群 <b style="margin-left:auto">{f["joined"]}</b></div>'
                  f'<div class="item">{icon("shield", 15)} 7 日后仍在 <b style="margin-left:auto">{f["retained_7d"]}</b></div>'
                  f'</div></div>')
        tip = ('<div class="alert" style="margin-top:16px">' + icon("alert", 16) +
               '<span>留存判定：现在仍在群，或首次退群时间晚于「加入时间 + N 天」，都算第 N 天留存；'
               '样本只统计加入已满 N 天的人。</span></div>')
        self._html(h, 200, self._shell("ret", "留存分析", "拉来的人留住了吗",
                                       stats + '<div style="height:16px"></div>' + table +
                                       '<div style="height:16px"></div><div class="split">'
                                       + chart + funnel + '</div>' + tip))

    # -- 掉人榜 ------------------------------------------------------------
    def _churn(self, h, qs) -> None:
        chat = self._chat()
        if not chat:
            return self._not_bound(h)
        gid = chat["chat_id"]
        rows = self.db.churn_board(gid, 60)
        total_churn = sum(r["churned"] for r in rows)
        stats = (f'<div class="stats">'
                 f'{self._stat("累计流失", total_churn, "人", "trend-down", tone="bad")}'
                 f'{self._stat("上榜成员", len(rows), "人", "users")}</div>')
        trs = []
        for i, r in enumerate(rows, 1):
            name = self._who(r["inviter_id"])
            rate = f'{100 * r["churned"] / r["invited"]:.0f}%' if r["invited"] else "—"
            pct = int(100 * r["churned"] / r["invited"]) if r["invited"] else 0
            trs.append(f'<tr><td class="c">{i}</td>'
                       f'<td><div class="who">{self._avatar(name)}<span class="nm">{esc(name)}</span></div></td>'
                       f'<td class="n"><b style="color:var(--bad)">{r["churned"]}</b></td>'
                       f'<td class="n">{r["invited"]}</td><td class="n">{rate}</td>'
                       f'<td style="width:170px"><div class="bar"><i style="width:{pct}%;'
                       f'background:linear-gradient(90deg,var(--bad),#c2413a)"></i></div></td></tr>')
        table = ('<div class="tablewrap"><table><thead><tr><th class="c">#</th><th>成员</th>'
                 '<th class="n">带来的人已退群</th><th class="n">累计邀请</th><th class="n">流失率</th>'
                 '<th>占比</th></tr></thead><tbody>'
                 + ("".join(trs) or f'<tr><td colspan="6"><div class="empty">{icon("trend-down", 34)}'
                                    f'<p>还没有数据</p></div></td></tr>') + "</tbody></table></div>")
        self._html(h, 200, self._shell("chu", "掉人榜", "谁拉来的人跑得最多", stats + table))

    # -- 入群申请 ----------------------------------------------------------
    def _requests(self, h, qs) -> None:
        chat = self._chat()
        if not chat:
            return self._not_bound(h)
        gid = chat["chat_id"]
        st = self.db.join_requests_stats(gid)
        tab = (qs.get("d") or [""])[0]
        # 默认「待处理」（和 Telegram 客户端一致）；「全部记录」用 d=all 显式指定
        # （不能用 d= 空值：parse_qs 默认丢弃空值，会退化成"没带参数"）
        if tab not in ("pending", "approved", "declined", "stale", "all"):
            tab = "pending"
        dec = tab if tab in ("pending", "approved", "declined", "stale") else None
        try:
            page = max(0, int((qs.get("page") or ["0"])[0]))
        except ValueError:
            page = 0
        per = 30
        total = self.db.join_requests_count(gid, dec or None)
        pages = max(1, (total + per - 1) // per)
        page = min(page, pages - 1)
        rows = self.db.join_requests_page(gid, limit=per, offset=page * per, decision=dec or None)

        stats = (f'<div class="stats">'
                 f'{self._stat("总申请", st["total"], "条", "inbox")}'
                 f'{self._stat("待处理", st["pending"], "条", "alert", tone="warn")}'
                 f'{self._stat("已通过", st["approved"], "条", "check", tone="ok")}'
                 f'{self._stat("已拒绝", st["declined"], "条", "user-x", tone="bad", sub=f"自动拒绝 {st['auto_declined']} 条")}'
                 f'{self._stat("已失效", st.get("stale", 0), "条", "file", tone="mute", sub="在 Telegram 侧已不存在")}'
                 f'</div>')

        labels = {"pending": '<span class="badge b-warn">待处理</span>',
                  "approved": '<span class="badge b-ok">已通过</span>',
                  "declined": '<span class="badge b-bad">已拒绝</span>',
                  "stale": '<span class="badge b-mute">已失效</span>'}
        trs = []
        for r in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["requested_at"]))
            mark = self.db.is_marked_deleted(gid, r["user_id"])
            name = r["user_name"] or f"用户 {r['user_id']}"
            inviter = self._who(r["inviter_id"]) if r["inviter_id"] else "—"
            # 待处理页：每条申请都能单独「通过 / 拒绝」（对应 Telegram 客户端里的单条操作）
            acts_cell = ""
            if tab == "pending":
                acts_cell = (
                    f'<td class="n"><div class="row" style="gap:6px;flex-wrap:nowrap">'
                    f'<form method="post" action="/requests" style="margin:0">{self._ginput()}'
                    f'<input type="hidden" name="action" value="approve_one">'
                    f'<input type="hidden" name="user_id" value="{r["user_id"]}">'
                    f'<button class="btn btn-sm btn-primary" type="submit">通过</button></form>'
                    f'<form method="post" action="/requests" style="margin:0">{self._ginput()}'
                    f'<input type="hidden" name="action" value="decline_one">'
                    f'<input type="hidden" name="user_id" value="{r["user_id"]}">'
                    f'<button class="btn btn-sm btn-danger" type="submit">拒绝</button></form>'
                    f'</div></td>')
            trs.append(
                f'<tr><td><div class="who">{self._avatar_img(r["user_id"], name)}<div style="min-width:0">'
                f'<div class="nm">{esc(name)}' + (' <span class="badge b-bad">已注销</span>' if mark else "")
                + f'</div><div class="muted" style="font-size:11.5px">'
                + (f'@{esc(r["username"])} · ' if r["username"] else "") + f'<code>{r["user_id"]}</code>'
                f'</div></div></div></td>'
                f'<td class="n">{when}</td><td>{labels.get(r["decision"], r["decision"])}</td>'
                f'<td class="n">{esc(r["decided_by"] or "—")}</td><td class="muted">{esc(r["reason"] or "")}</td>'
                f'<td class="muted">{esc(inviter)}</td>{acts_cell}</tr>')
        empty_html = (
            f'<div class="empty">{icon("check", 40)}<h2 style="margin:8px 0 4px">当前没有待处理申请</h2>'
            f'<p class="muted">和 Telegram 客户端一致。更早的申请都在「全部记录」里（历史留档，可审计）。</p></div>'
            if tab == "pending" and st["pending"] == 0 else
            f'<tr><td colspan="{7 if tab == "pending" else 6}"><div class="empty">{icon("inbox", 34)}'
            f'<p>还没有入群申请</p></div></td></tr>')
        head = ('<div class="tablewrap"><table><thead><tr><th>申请者</th><th class="n">申请时间</th>'
                '<th>状态</th><th class="n">处理方</th><th>原因</th><th>邀请人</th>'
                + ('<th class="n">操作</th>' if tab == "pending" else "")
                + '</tr></thead><tbody>')
        table = (head + ("".join(trs) or empty_html) + "</tbody></table></div>")
        tabs = ['<div class="tabs">']
        for key, label in [("pending", "待处理"), ("all", "全部记录"), ("approved", "已通过"),
                           ("declined", "已拒绝"), ("stale", "已失效")]:
            cls = ' class="on"' if tab == key else ""
            tabs.append(f'<a href="/requests?d={key}&g={self._gid()}"{cls}>{label}</a>')
        tabs.append("</div>")
        tip = ('<div class="alert" style="margin-top:16px">' + icon("shield", 16) +
               '<span>所有申请都会进这张表（含无法归因的）。带「已注销」标记的，是机器人用 '
               '<code>getChatMember</code> 查不到该账号后自动拒绝的；依据与时间都记在审计日志里。<br>'
               '⚠️ 自动拒绝只在<b>开关打开</b>时生效；开关关着时进来的申请会一直挂在"待处理"里，'
               '点下面的按钮可以批量补拒。</span></div>'
               '<div class="alert warn" style="margin-top:12px">' + icon("alert", 16) +
               '<span><b>为什么这里只有几条，Telegram 群里却有上千条？</b><br>'
               'Telegram <b>不会</b>把历史入群申请推给机器人（Bot API 也没有"列出待处理申请"的接口），'
               '机器人只能记录<b>它当上管理员之后</b>新来的申请。<br>'
               '要清掉积压的历史申请，只能在 Telegram 客户端里操作：群 → 管理 → 入群申请 → '
               '「全部拒绝」；或者到群设置里关掉"批准新成员"。之后新申请机器人会按规则实时处理。'
               '</span></div>')
        acts = (f'<form method="post" action="/requests" class="row" style="margin:14px 0 0">'
                f'{self._ginput()}'
                f'<input type="hidden" name="action" value="sweep_deleted">'
                f'<button class="btn btn-primary" type="submit">{icon("broom", 15)} '
                f'批量拒绝「已注销」的申请</button>'
                f'</form>'
                f'<form method="post" action="/requests" class="row" style="margin:10px 0 0">'
                f'{self._ginput()}'
                f'<input type="hidden" name="action" value="sweep_all">'
                f'<div style="max-width:180px"><input type="text" name="confirm" placeholder="输入 CONFIRM"></div>'
                f'<button class="btn btn-danger" type="submit">全部拒绝（谨慎）</button></form>')
        self._html(h, 200, self._shell("req", "入群申请", "申请列表 + 已注销统计",
                                       stats + "".join(tabs) + table
                                       + self._pager("/requests", page, pages, f"d={tab}") + tip + acts))

    def _post_requests(self, h) -> None:
        """批量复核/拒绝待处理申请。"""
        if not self.require_manager():
            return self._error(h, 403, "需要管理权限", "")
        gid = self._gid()
        if not gid or gid not in self.managed_groups():
            return self._error(h, 403, "这个群你没有管理权限", "")
        form = self._read_form(h)
        action = (form.get("action") or [""])[0]
        # 逐条处理：通过 / 拒绝单条申请
        if action in ("approve_one", "decline_one"):
            try:
                uid = int((form.get("user_id") or [""])[0] or 0)
            except ValueError:
                uid = 0
            if not uid:
                return self._redirect(h, f"/requests?g={gid}")
            if not self.bot:
                return self._error(h, 503, "机器人未就绪", "无法执行操作。")
            res = self.bot.decide_join_request_one(
                gid, uid, "approve" if action == "approve_one" else "decline",
                self.current_user().get("uid") or 0)
            msgs = {"ok": "已通过该申请" if action == "approve_one" else "已拒绝该申请",
                    "stale": "该申请在 Telegram 侧已不存在，已归档",
                    "error": "操作失败：Telegram 返回错误，稍后再试"}
            return self._redirect(h, f"/requests?g={gid}&ok=" +
                                  urllib.parse.quote(msgs.get(res, str(res))))
        if action not in ("sweep_deleted", "sweep_all"):
            return self._redirect(h, f"/requests?g={gid}")
        if action == "sweep_all" and (form.get("confirm") or [""])[0].strip().upper() != "CONFIRM":
            return self._redirect(h, f"/requests?g={gid}&ok=" + urllib.parse.quote(
                "全部拒绝需要输入 CONFIRM"))
        if not self.bot:
            return self._error(h, 503, "机器人未就绪", "无法执行批量操作。")
        mode = "deleted" if action == "sweep_deleted" else "all"
        self.bot.submit(self.bot.sweep_join_requests, gid, mode,
                        self.current_user().get("uid") or 0)
        self.log(f"网页触发申请复核 mode={mode} gid={gid}")
        return self._redirect(h, f"/requests?g={gid}&ok=" + urllib.parse.quote(
            "已开始批量复核，稍后刷新看结果"))

    # -- 已注销 ------------------------------------------------------------
    def _deleted(self, h, qs) -> None:
        chat = self._chat()
        if not chat:
            return self._not_bound(h)
        gid = chat["chat_id"]
        st = self.db.deleted_stats(gid)
        allow_kick = bool(self._setting("allow_cleanup_kick", False))
        try:
            page = max(0, int((qs.get("page") or ["0"])[0]))
        except ValueError:
            page = 0
        per = 30
        pages = max(1, (st["total"] + per - 1) // per)
        page = min(page, pages - 1)
        rows = self.db.deleted_page(gid, limit=per, offset=page * per)
        stats = (f'<div class="stats">'
                 f'{self._stat("疑似已注销", st["total"], "人", "user-x", tone="bad")}'
                 f'{self._stat("已移出群", st["kicked"], "人", "check", tone="ok")}'
                 f'{self._stat("移除开关", "已允许" if allow_kick else "已禁止", "", "shield", tone="warn" if allow_kick else "")}'
                 f'</div>')
        trs = []
        for r in rows:
            u = self.db.get_user(r["user_id"])
            name = r["first_name"] or (u["first_name"] if u else "") or f"用户 {r['user_id']}"
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["detected_at"]))
            flag = ('<span class="badge b-ok">已移出</span>' if r["kicked"]
                    else '<span class="badge b-mute">未处理</span>')
            trs.append(f'<tr><td><div class="who">{self._avatar_img(r["user_id"], name)}'
                       f'<span class="nm">{esc(name)}</span> <code class="mono">{r["user_id"]}</code></div></td>'
                       f'<td class="n">{when}</td><td class="muted">'
                       f'{esc(EVIDENCE_CN.get(r["evidence"] or "", r["evidence"] or ""))}</td>'
                       f'<td>{flag}</td></tr>')
        table = ('<div class="tablewrap"><table><thead><tr><th>账号</th><th class="n">发现时间</th>'
                 '<th>判定依据</th><th>状态</th></tr></thead><tbody>'
                 + ("".join(trs) or f'<tr><td colspan="4"><div class="empty">{icon("user-x", 34)}'
                                    f'<p>还没有发现已注销账号</p></div></td></tr>') + "</tbody></table></div>")
        banner = ('<div class="alert warn" style="margin-bottom:16px">' + icon("shield", 17) +
                  '<div><b>安全约定</b><br>只认 Telegram 明确返回「查不到该账号」的对象，'
                  '绝不因为「不活跃」「没说话」就标记。默认<b>只标记、不踢人</b>；'
                  '移除必须由管理员显式确认，管理员/群主/机器人自己永远不会被碰，每次操作都写审计日志。</div></div>')
        scan = (f'<form method="post" action="/deleted" style="margin:0 0 14px">{self._ginput()}'
                '<input type="hidden" name="action" value="scan">'
                '<button class="btn btn-primary" type="submit">' + icon("search", 16) +
                ' 扫描已注销账号（只标记，不踢人）</button></form>')
        if allow_kick:
            kick = (f'<form method="post" action="/deleted" class="row" style="margin:0 0 16px">{self._ginput()}'
                    '<input type="hidden" name="action" value="kick">'
                    '<div style="max-width:280px;flex:1"><input type="text" name="confirm" '
                    'placeholder="输入 CONFIRM 以确认移除"></div>'
                    '<button class="btn btn-danger" type="submit">' + icon("broom", 16) +
                    ' 移除已标记的账号</button></form>')
        else:
            kick = ('<div class="alert" style="margin-bottom:16px">' + icon("alert", 16) +
                    f'<span>移除功能<b>已禁止</b>。需要时先到 <a href="/settings{self._q()}">功能设置</a> '
                    '打开「允许清理时踢人」。</span></div>')
        note = ('<div class="alert" style="margin-top:16px">' + icon("alert", 16) +
                '<span>扫描对象：在群成员 + 有邀请记录的账号 + 待处理申请；一次最多 400 人，'
                '人多可以多点几次。扫描不发任何群消息。</span></div>')
        self._html(h, 200, self._shell("del", "已注销账号", "只标记，绝不自动踢人",
                                       stats + banner + scan + kick + table + self._pager("/deleted", page, pages) + note))

    # -- 审计日志（网页） --------------------------------------------------
    def _logs(self, h, qs) -> None:
        if (qs.get("export") or [""])[0] == "csv":
            return self._export_audit(h, (qs.get("action") or [None])[0])
        action = (qs.get("action") or [""])[0] or None
        try:
            page = max(0, int((qs.get("page") or ["0"])[0]))
        except ValueError:
            page = 0
        per = 30
        total = self.db.audit_count(action)
        pages = max(1, (total + per - 1) // per)
        page = min(page, pages - 1)
        rows = self.db.audit_page(limit=per, offset=page * per, action=action)
        actions = self.db.audit_actions()[:10]
        stats = (f'<div class="stats">'
                 f'{self._stat("日志总数", self.db.audit_count(), "条", "file")}'
                 f'{self._stat("当前筛选", total, "条", "search", tone="warn", sub=action or "全部")}'
                 f'{self._stat("动作类型", len(self.db.audit_actions()), "种", "sliders", tone="ok")}'
                 f'</div>')
        trs = []
        for r in rows:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            who = r["actor_name"] or (f"uid:{r['actor_id']}" if r["actor_id"] else "系统")
            trs.append(f'<tr><td class="n mono" style="font-size:12px">{ts}</td>'
                       f'<td>{esc(who)}</td>'
                       f'<td><span class="badge b-acc">{esc(ACTION_CN.get(r["action"], r["action"]))}</span>'
                       f'<div class="muted mono" style="font-size:11px">{esc(r["action"])}</div></td>'
                       f'<td class="mono" style="font-size:12px">{esc(r["target"] or "")}</td>'
                       f'<td class="muted">{esc(r["detail"] or "")}</td></tr>')
        table = ('<div class="tablewrap"><table><thead><tr><th class="n">时间</th><th>操作人</th>'
                 '<th>动作</th><th>对象</th><th>详情</th></tr></thead><tbody>'
                 + ("".join(trs) or f'<tr><td colspan="5"><div class="empty">{icon("file", 34)}'
                                    f'<p>暂无日志</p></div></td></tr>') + "</tbody></table></div>")
        tabs = ['<div class="tabs"><a href="/logs' + self._q() + '"' + (' class="on"' if not action else '') + '>全部</a>']
        for a in actions:
            cls = ' class="on"' if a == action else ""
            tabs.append(f'<a href="/logs{self._q("action=" + urllib.parse.quote(a))}"{cls}>'
                        f'{esc(ACTION_CN.get(a, a))}</a>')
        tabs.append("</div>")
        toolbar = (f'<div class="toolbar">'
                   f'<a class="btn" href="/logs?export=csv&g={self._gid()}'
                   + (f'&action={urllib.parse.quote(action)}' if action else '') + '">'
                   + icon("file", 15) + ' 导出 CSV</a>'
                   + '<span class="muted">所有管理员操作都会自动记录在这里</span></div>')
        self._html(h, 200, self._shell("log", "审计日志", "谁在什么时候做了什么",
                                       stats + "".join(tabs) + toolbar + table
                                       + self._pager("/logs", page, pages,
                                                     f"action={urllib.parse.quote(action)}" if action else "")))

    def _export_audit(self, h, action: str | None) -> None:
        rows = self.db.audit_page(limit=100000, offset=0, action=action)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "actor_id", "actor_name", "action", "target", "chat_id", "detail"])
        for r in rows:
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                        r["actor_id"], self._csv_safe(r["actor_name"]), self._csv_safe(r["action"]),
                        self._csv_safe(r["target"]), r["chat_id"], self._csv_safe(r["detail"])])
        data = ("\ufeff" + buf.getvalue()).encode("utf-8")
        self.db.record_audit(None, "web", "export_csv", target="audit", detail=f"{len(rows)} 条")
        self._send(h, 200, data, "text/csv; charset=utf-8",
                   extra=[("Content-Disposition", 'attachment; filename="audit.csv"')])

    @staticmethod
    def _csv_safe(v) -> str:
        """防 CSV 公式注入（Excel 把 = + - @ 开头的单元格当公式执行）。"""
        s = "" if v is None else str(v)
        if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + s
        return s

    # -- 禁漫下载管理（网页） ----------------------------------------------
    def _jm_page(self, h, qs) -> None:
        ok = (qs.get("ok") or [""])[0]
        head = (f'<div class="alert ok" style="margin-bottom:14px">{icon("check", 16)}'
                f'<span>{esc(ok)}</span></div>') if ok else ""
        try:
            import jm_tg
        except ImportError:
            return self._html(h, 200, self._shell("jmdl", "下载管理", "禁漫下载",
                                                   head + '<div class="alert warn">下载模块未安装</div>'))
        items = jm_tg.list_downloaded()
        used = jm_tg.disk_used()

        def _fmt(n):
            if n >= 1024 * 1024 * 1024:
                return f"{n / 1024 / 1024 / 1024:.1f} GB"
            if n >= 1024 * 1024:
                return f"{n / 1024 / 1024:.1f} MB"
            if n >= 1024:
                return f"{n / 1024:.0f} KB"
            return f"{n} B"

        trs = []
        for it in items:
            when = time.strftime("%m-%d %H:%M", time.localtime(it["time"])) if it["time"] else "—"
            status = ('<span class="badge b-ok">完整</span>' if not it["pages"] or it["files"] >= it["pages"]
                      else f'<span class="badge b-warn">缺 {it["pages"] - it["files"]} 页</span>')
            trs.append(f'<tr><td class="mono"><code>JM{it["code"]}</code></td>'
                       f'<td>{esc(str(it["title"])[:42])}</td>'
                       f'<td class="n">{it["files"]}{"/" + str(it["pages"]) if it["pages"] else ""}</td>'
                       f'<td class="n">{_fmt(it["size"])}</td>'
                       f'<td class="n">{it["zips"]} 卷</td><td class="n">{when}</td><td>{status}</td>'
                       f'<td class="n"><form method="post" action="/jmdl" style="margin:0">{self._ginput()}'
                       f'<input type="hidden" name="code" value="{it["code"]}">'
                       f'<button class="btn btn-sm btn-danger" name="action" value="del" type="submit">'
                       f'删除</button></form></td></tr>')
        table = ('<div class="tablewrap"><table><thead><tr><th>漫画</th><th>标题</th>'
                 '<th class="n">页数</th><th class="n">占用</th><th class="n">分卷</th>'
                 '<th class="n">时间</th><th>状态</th><th class="n">操作</th></tr></thead><tbody>'
                 + ("".join(trs) or '<tr><td colspan="8"><div class="empty">'
                                    '<p>还没有下载记录</p></div></td></tr>')
                 + "</tbody></table></div>")
        stats = (f'<div class="stats">'
                 f'{self._stat("已下载", len(items), "本", "file")}'
                 f'{self._stat("磁盘占用", _fmt(used), "", "broom", tone="warn")}'
                 f'</div>')
        clear = ('<form method="post" action="/jmdl" class="row" style="margin:14px 0 0">{self._ginput()}'
                 '<input type="hidden" name="action" value="clearall">'
                 '<div style="max-width:180px"><input type="text" name="confirm" placeholder="输入 CONFIRM"></div>'
                 '<button class="btn btn-danger" type="submit">全部清空（谨慎）</button></form>')
        tip = ('<div class="alert" style="margin-top:16px">' + icon("shield", 16) +
               '<span>删除只清本地文件，不影响机器人功能；删完想再要就重新 /jm dl。</span></div>')
        try:
            cur_mode = jm_tg.get_auto_delete()
        except Exception:
            cur_mode = "off"
        mode_opts = "".join(
            f'<option value="{k}"{" selected" if k == cur_mode else ""}>'
            f'{esc(v)}</option>' for k, v in jm_tg.AUTO_MODE_CN.items())
        autodel = ('<div class="card pad" style="margin-top:16px"><h2 style="margin:0 0 12px">'
                   '🗑 自动清理</h2>'
                   f'<form method="post" action="/jmdl" class="row">{self._ginput()}'
                   '<input type="hidden" name="action" value="autodel">'
                   f'<div style="max-width:230px;flex:1"><select name="mode">{mode_opts}</select></div>'
                   '<button class="btn btn-primary" type="submit">保存</button></form>'
                   '<p class="muted" style="margin:10px 0 0">'
                   '「发送成功后立即删除」= 压缩包发给你之后当场清掉服务器文件；'
                   '其余模式由机器人每分钟检查一次自动执行。'
                   '也可在 Telegram 发 /jm autodel 设置。</p></div>')
        self._html(h, 200, self._shell("jmdl", "下载管理", "已下载的漫画与清理",
                                        head + stats + table + clear + autodel + tip))

    def _post_jmdl(self, h) -> None:
        if not self.require_admin():
            return self._error(h, 403, "需要管理员权限", "")
        form = self._read_form(h)
        action = (form.get("action") or [""])[0]
        try:
            import jm_tg
        except ImportError:
            return self._redirect(h, "/jmdl?ok=下载模块未安装")
        if action == "del":
            code = (form.get("code") or [""])[0]
            try:
                removed = jm_tg.remove_download(code)
                self.db.record_audit(None, "web", "jm_delete", target=str(code),
                                     detail=f"删除 {len(removed)} 项")
                return self._redirect(h, f"/jmdl?ok=" + urllib.parse.quote(f"已删除 JM{code}"))
            except ValueError as e:
                return self._redirect(h, "/jmdl?ok=" + urllib.parse.quote(str(e)))
        if action == "autodel":
            mode = (form.get("mode") or [""])[0].strip().lower()
            try:
                jm_tg.set_auto_delete(mode)
                self.db.record_audit(None, "web", "jm_autodel", detail=mode)
                return self._redirect(h, "/jmdl?ok=" + urllib.parse.quote(
                    f"自动清理已设为：{jm_tg.AUTO_MODE_CN.get(mode, mode)}"))
            except ValueError as e:
                return self._redirect(h, "/jmdl?ok=" + urllib.parse.quote(str(e)))
        if action == "clearall":
            if (form.get("confirm") or [""])[0].strip().upper() != "CONFIRM":
                return self._redirect(h, "/jmdl?ok=" + urllib.parse.quote("全部清空需要输入 CONFIRM"))
            n = 0
            for it in jm_tg.list_downloaded():
                try:
                    jm_tg.remove_download(it["code"])
                    n += 1
                except ValueError:
                    pass
            self.db.record_audit(None, "web", "jm_clear", detail=f"清空 {n} 本下载")
            return self._redirect(h, f"/jmdl?ok=" + urllib.parse.quote(f"已清空 {n} 本"))
        return self._redirect(h, "/jmdl")

    # -- 设置 --------------------------------------------------------------
    def _settings(self, h, qs) -> None:
        saved = (qs.get("ok") or [""])[0]
        chat = self._chat()
        items = []
        for key, label, desc, hot in self.TOGGLES:
            on = bool(self._setting(key, False))
            items.append(
                f'<label class="switch{(" hot" if hot else "")}">'
                f'<input type="checkbox" name="{key}" {"checked" if on else ""}>'
                f'<span class="track"></span>'
                f'<span class="txt"><b>{esc(label)}'
                + (' <span class="badge b-bad">高风险</span>' if hot else "")
                + f'</b><span>{esc(desc)}</span></span></label>')
        form = (f'<form method="post" action="/settings">{self._ginput()}' + "".join(items) +
                '<div class="row" style="margin-top:18px">'
                '<button class="btn btn-primary" type="submit">' + icon("check", 16) + ' 保存开关</button>'
                '<span class="muted">改动立刻生效，并写入审计日志</span></div></form>')
        info = (f'<div class="card pad"><h2 style="margin:0 0 12px">运行信息</h2><div class="list">'
                f'<div class="item">站点名称 <span class="t">{esc(self.site)}</span></div>'
                f'<div class="item">访问地址 <span class="t mono">{esc(self.base_url)}</span></div>'
                f'<div class="item">访问密码 <span class="t">{"已设置（加盐哈希存储）" if (self.pw_hash or self.password) else "⚠️ 未设置"}</span></div>'
                f'<div class="item">自定义 LOGO <span class="t">{"✅ 已上传" if self._has_logo() else "默认字母"}</span></div>'
                f'<div class="item">自定义背景 <span class="t">{"✅ 已上传" if self._has_bg() else "默认"}</span></div>'
                f'<div class="item">HTTPS <span class="t">{"内置证书" if (self.cfg.get("web_tls_cert") and self.cfg.get("web_tls_key")) else "经由隧道"}</span></div>'
                f'<div class="item">已绑定群 <span class="t">{esc(chat["title"]) if chat else "未绑定"}</span></div>'
                f'<div class="item">域名 <span class="t">{esc(self.cfg.get("web_base_url") or "未配置（用隧道地址）")}</span></div>'
                f'</div></div>')
        appearance = (
            '<div class="card pad" style="margin-top:16px"><h2 style="margin:0 0 12px">'
            '🎨 外观（LOGO / 背景图）</h2>'
            '<form method="post" action="/settings" class="row" id="logoform">{self._ginput()}'
            '<input type="hidden" name="logo_data" id="logo_data" value="">'
            '<input type="file" id="logo_file" accept="image/png,image/jpeg,image/webp" '
            'style="display:none" onchange="dshOpenCrop(this.files && this.files[0])">'
            '<button class="btn" type="button" onclick="document.getElementById(\'logo_file\').click()">'
            + icon("upload", 15) + ' 更换 LOGO（裁剪）</button>'
            '<span class="muted" style="font-size:12px">'
            f'{"当前：已自定义" if self._has_logo() else "当前：默认字母"}</span></form>'
            f'<form method="post" action="/settings" class="row" style="margin-top:10px">{self._ginput()}'
            '<input type="hidden" name="bg_data" id="bg_data" value="">'
            '<input type="file" id="bg_file" accept="image/png,image/jpeg,image/webp" '
            'style="display:none" onchange="dshRead(this, \'bg_data\')">'
            '<button class="btn" type="button" onclick="document.getElementById(\'bg_file\').click()">'
            + icon("upload", 15) + ' 选择背景图</button>'
            '<button class="btn btn-primary" type="submit">保存背景</button>'
            '<span class="muted" style="font-size:12px">'
            f'{"当前：已自定义" if self._has_bg() else "当前：默认"}</span></form>'
            f'<form method="post" action="/settings" class="row" style="margin-top:10px">{self._ginput()}'
            '<input type="hidden" name="action" value="clear_branding">'
            '<button class="btn btn-danger" type="submit">恢复默认外观</button></form>'
            '<p class="muted" style="margin:10px 0 0">LOGO 支持像微信/QQ 一样拖动 + 缩放裁剪；'
            '背景图建议 1920×1080，均 ≤10MB。保存后刷新页面（Ctrl+F5）生效。</p></div>'

            # ---- 头像裁剪器（QQ/微信式：拖拽 + 缩放 + 圆形预览）----
            '<div class="cropmask" id="cropmask"><div class="cropbox">'
            '<h3>✂️ 裁剪 LOGO（拖动图片 / 拖动滑块缩放）</h3>'
            '<div class="cropstage" id="cropstage"><img id="cropimg" alt=""><div class="cropring"></div></div>'
            '<div class="croprow">'
            '<input type="range" id="cropzoom" min="1" max="4" step="0.01" value="1">'
            '<div class="croppreview" id="croppreview"></div>'
            '</div>'
            '<div class="cropbtns">'
            '<button class="btn" type="button" onclick="dshCropClose()">取消</button>'
            '<button class="btn btn-primary" type="button" onclick="dshCropCommit()">确定</button>'
            '</div></div></div>'

            '<script>'
            'function dshRead(input,target){var f=input.files&&input.files[0];if(!f)return;'
            'if(f.size>10*1024*1024){alert("图片不能超过 10MB");return;}'
            'var r=new FileReader();r.onload=function(){'
            'document.getElementById(target).value=r.result;};r.readAsDataURL(f);}'
            'var dc={img:new Image(),s:1,x:0,y:0,drag:false,px:0,py:0,cw:200,ch:200};'
            'function dshEl(id){return document.getElementById(id)}'
            'dc.img.onload=function(){dc.s=1;dc.x=0;dc.y=0;dshCropFit();dshCropPaint();};'
            'function dshOpenCrop(f){'
            ' if(!f)return;'
            ' if(f.size>10*1024*1024){alert("图片不能超过 10MB");return;}'
            ' var fr=new FileReader();'
            ' fr.onload=function(){dc.img.src=fr.result;dshEl("cropmask").classList.add("on");};'
            ' fr.readAsDataURL(f);}'
            'function dshCropClose(){dshEl("cropmask").classList.remove("on")}'
            'function dshCropFit(){'
            ' var st=dshEl("cropstage"),iw=dc.img.naturalWidth,ih=dc.img.naturalHeight;'
            ' dc.s=Math.max(dc.cw/iw,dc.ch/ih);'
            ' dc.x=(st.clientWidth-iw*dc.s)/2;dc.y=(st.clientHeight-ih*dc.s)/2;}'
            'function dshCropClamp(){'
            ' var st=dshEl("cropstage"),iw=dc.img.naturalWidth*dc.s,ih=dc.img.naturalHeight*dc.s;'
            ' var cx=(st.clientWidth-dc.cw)/2,cy=(st.clientHeight-dc.ch)/2;'
            ' dc.x=Math.min(cx,Math.max(cx-(iw-dc.cw),dc.x));'
            ' dc.y=Math.min(cy,Math.max(cy-(ih-dc.ch),dc.y));}'
            'function dshCropPaint(){'
            ' dshCropClamp();'
            ' dshEl("cropimg").style.transform="translate("+dc.x+"px,"+dc.y+"px) scale("+dc.s+")";'
            ' dshCropPreview();}'
            'function dshCropPreview(){'
            ' var st=dshEl("cropstage"),cx=(st.clientWidth-dc.cw)/2,cy=(st.clientHeight-dc.ch)/2;'
            ' var pv=dshEl("croppreview");if(!pv)return;'
            ' var iw=dc.img.naturalWidth*dc.s,ih=dc.img.naturalHeight*dc.s;'
            ' var scale=pv.clientWidth/(dc.cw/dc.s);'
            ' pv.style.backgroundImage="url("+dc.img.src+")";'
            ' pv.style.backgroundSize=(iw*scale)+"px "+(ih*scale)+"px";'
            ' pv.style.backgroundPosition=(-(cx-dc.x)*scale)+"px "+(-(cy-dc.y)*scale)+"px";'
            ' pv.style.backgroundRepeat="no-repeat";}'
            'function dshCropCommit(){'
            ' var st=dshEl("cropstage"),cx=(st.clientWidth-dc.cw)/2,cy=(st.clientHeight-dc.ch)/2;'
            ' var cv=document.createElement("canvas");cv.width=512;cv.height=512;'
            ' var g=cv.getContext("2d");'
            ' var sx=(cx-dc.x)/dc.s,sy=(cy-dc.y)/dc.s,sw=dc.cw/dc.s;'
            ' try{g.drawImage(dc.img,sx,sy,sw,sw,0,0,512,512);}catch(e){}'
            ' dshEl("logo_data").value=cv.toDataURL("image/png");'
            ' dshCropClose();'
            ' document.getElementById("logoform").submit();}'
            '(function(){'
            ' var st=dshEl("cropstage"),z=dshEl("cropzoom");'
            ' st.addEventListener("pointerdown",function(e){dc.drag=true;dc.px=e.clientX;dc.py=e.clientY;'
            '  st.setPointerCapture&&st.setPointerCapture(e.pointerId);});'
            ' st.addEventListener("pointermove",function(e){if(!dc.drag)return;'
            '  dc.x+=e.clientX-dc.px;dc.y+=e.clientY-dc.py;dc.px=e.clientX;dc.py=e.clientY;dshCropPaint();});'
            ' ["pointerup","pointercancel"].forEach(function(t){st.addEventListener(t,function(){dc.drag=false;})});'
            ' z.addEventListener("input",function(){dc.s=+z.value;dshCropPaint();});'
            '})();'
            '</script>')
        danger = ('<div class="card pad" style="margin-top:16px"><h2 style="margin:0 0 10px">危险操作</h2>'
                  f'<div class="row"><a class="btn btn-danger" href="/deleted{self._q()}">' + icon("broom", 16) +
                  ' 已注销账号清理</a><a class="btn" href="/logs">' + icon("file", 16) +
                  ' 查看审计日志</a></div>'
                  '<p class="muted" style="margin:10px 0 0">清理默认只标记不踢人；'
                  '开启「允许清理时踢人」后仍需在页面输入 CONFIRM 才会执行。</p></div>')
        ok = ('<div class="alert ok" style="margin-bottom:16px">' + icon("check", 16) +
              '<span>设置已保存并记入审计日志</span></div>') if saved else ""
        mode = str(self.db.get_setting("reg_mode", self.cfg.get("reg_mode", "invite")))
        minr = str(self.db.get_setting("group_admin_min_rights",
                                       self.cfg.get("group_admin_min_rights", "can_invite_users")))
        reg = (f'<div class="card pad" style="margin-top:16px"><h2 style="margin:0 0 12px">注册与权限</h2>'
               f'<form method="post" action="/settings" class="row">{self._ginput()}'
               f'<div style="max-width:230px;flex:1"><select name="reg_mode">'
               f'<option value="invite"{" selected" if mode == "invite" else ""}>需要邀请码（默认）</option>'
               f'<option value="open"{" selected" if mode == "open" else ""}>开放注册</option>'
               f'<option value="closed"{" selected" if mode == "closed" else ""}>关闭注册</option>'
               f'</select></div>'
               f'<button class="btn" type="submit">保存注册模式</button></form>'
               f'<p class="muted" style="margin:8px 0 16px">用 Telegram 登录首次会自动建号，'
               f'不受这里的限制；这里只管"用户名/邮箱注册"。</p>'
               f'<form method="post" action="/settings" class="row">{self._ginput()}'
               f'<div style="max-width:290px;flex:1"><select name="group_admin_min_rights">'
               f'<option value="can_invite_users"{" selected" if minr == "can_invite_users" else ""}>'
               f'群管理员需有「邀请用户」权限</option>'
               f'<option value="any"{" selected" if minr == "any" else ""}>只要是群管理员就行（含空壳）</option>'
               f'<option value="can_invite_users,can_restrict_members,can_delete_messages"'
               f'{" selected" if "," in minr else ""}>要求拥有全部关键权限（最严）</option>'
               f'</select></div>'
               f'<button class="btn" type="submit">保存管理权限判定</button></form>'
               f'<p class="muted" style="margin:8px 0 0">决定"群里什么级别的管理员算有实权"：'
               f'只有达标的群管理员才能在后台审批；没权限的空壳管理员只能看数据。</p></div>')
        tip = ('<div class="alert" style="margin-top:16px">' + icon("alert", 16) +
               '<span>端口、密码、域名、TLS 证书、SMTP 这类配置在服务器 '
               '<code>config.json</code> 里改，改完重启服务；功能开关在这里改即可。</span></div>')
        self._html(h, 200, self._shell("set", "功能设置", "网页上就能开关所有功能",
                                       ok + form + reg + '<div style="height:18px"></div>' + info
                                       + appearance + danger + tip))

    def _read_form(self, h) -> dict:
        form = getattr(self._tl, "form", None)
        if form is None:
            form = self._read_body(h)
            self._tl.form = form
        g = (form.get("g") or [""])[0]
        if g.lstrip("-").isdigit():
            self._tl.gid = int(g)
        return form

    def _ginput(self) -> str:
        """给 GET/POST 表单带上当前群 + CSRF 令牌。"""
        gid = self._gid()
        g = f'<input type="hidden" name="g" value="{gid}">' if gid is not None else ""
        return f'{g}<input type="hidden" name="csrf" value="{self._csrf_token()}">'

    def _post_settings(self, h) -> None:
        form = self._read_form(h)
        changed = []
        # 注册模式（下拉框）
        if "reg_mode" in form:
            rm = (form.get("reg_mode") or [""])[0]
            if rm in ("invite", "open", "closed") and rm != str(self.db.get_setting("reg_mode")):
                self.db.set_setting("reg_mode", rm)
                changed.append(f"注册模式={rm}")
                self.db.record_audit(None, "web", "setting_change", target="reg_mode", detail=rm)
        # 外观：LOGO / 背景图（base64 data URL，≤2MB）
        logo_data = (form.get("logo_data") or [""])[0]
        if logo_data.startswith("data:image/"):
            if self._save_branding("logo", logo_data):
                changed.append("LOGO 已更新")
                self.db.record_audit(None, "web", "setting_change", target="branding", detail="更新 LOGO")
        bg_data = (form.get("bg_data") or [""])[0]
        if bg_data.startswith("data:image/"):
            if self._save_branding("bg", bg_data):
                changed.append("背景图已更新")
                self.db.record_audit(None, "web", "setting_change", target="branding", detail="更新背景图")
        if (form.get("action") or [""])[0] == "clear_branding":
            self._clear_branding()
            changed.append("外观已恢复默认")
            self.db.record_audit(None, "web", "setting_change", target="branding", detail="恢复默认外观")
        if "group_admin_min_rights" in form:
            gr = (form.get("group_admin_min_rights") or [""])[0]
            if gr and gr != str(self.db.get_setting("group_admin_min_rights")):
                self.db.set_setting("group_admin_min_rights", gr)
                changed.append(f"管理权限判定={gr}")
                self.db.record_audit(None, "web", "setting_change",
                                     target="group_admin_min_rights", detail=gr)
        for key, label, _desc, _hot in self.TOGGLES:
            new = key in form
            old = bool(self._setting(key, False))
            if new != old:
                self.db.set_setting(key, new)
                changed.append(f"{label}={'开' if new else '关'}")
                self.db.record_audit(None, "web", "setting_change", target=key,
                                     detail=f"{label} -> {'on' if new else 'off'}")
        self.log(f"网页修改开关：{changed or '无变化'}")
        return self._send(h, 302, b"", "text/html; charset=utf-8",
                          extra=[("Location", "/settings?ok=1")])

    def _post_panel(self, h) -> None:
        """在群里发送领取链接面板（等价于 Telegram 里的 /panel）。"""
        self._read_form(h)
        chat = self._chat()
        if not chat:
            return self._error(h, 400, "还没有绑定群组", "")
        if not self.bot:
            return self._error(h, 503, "机器人未就绪", "无法发送面板。")
        gid = chat["chat_id"]
        try:
            bot = self.bot
            text = bot.db.get_kv("panel_text") or bot.cfg.get("panel_text") or "领取你的专属邀请链接"
            markup = {"inline_keyboard": [
                [{"text": "🔗 领取我的专属邀请链接", "url": bot.bot_link("link")}],
                [{"text": "🏆 查看排行榜", "url": bot.bot_link()}],
            ]}
            bot.tg.send_message(gid, text, reply_markup=markup)
            self.db.record_audit(None, "web", "post_panel", target=str(gid), chat_id=gid,
                                 detail="网页触发发送群面板")
            self.log("网页触发了群面板发布")
        except Exception as e:
            self.log(f"网页发布面板失败：{e}", "ERROR")
            return self._error(h, 500, "发送失败", str(e)[:160])
        return self._send(h, 302, b"", "text/html; charset=utf-8",
                          extra=[("Location", "/?panel=1")])

    def _post_deleted(self, h) -> None:
        form = self._read_form(h)
        action = (form.get("action") or [""])[0]
        chat = self._chat()
        if not chat:
            return self._error(h, 400, "还没有绑定群组", "")
        gid = chat["chat_id"]
        admins = self.db.all_admin_ids()
        report_to = admins[0] if admins else gid
        if action == "scan":
            if not self.bot:
                return self._error(h, 503, "机器人未就绪", "无法触发扫描。")
            self.db.record_audit(None, "web", "cleanup_scan", target=str(gid), chat_id=gid,
                                 detail="网页触发扫描（只标记，不踢人）")
            self.bot.submit(self.bot._cleanup_scan, gid, report_to, 0)
            self.log("网页触发了已注销账号扫描")
        elif action == "kick":
            confirm = (form.get("confirm") or [""])[0].strip().upper()
            if not self._setting("allow_cleanup_kick", False):
                return self._error(h, 403, "移除功能已禁止", "请先在功能设置里打开「允许清理时踢人」。")
            if confirm != "CONFIRM":
                return self._error(h, 400, "确认失败", "确认框里必须输入 CONFIRM。")
            if not self.bot:
                return self._error(h, 503, "机器人未就绪", "无法触发清理。")
            self.db.record_audit(None, "web", "cleanup_kick", target=str(gid), chat_id=gid,
                                 detail="网页触发移除已注销账号")
            self.bot.submit(self.bot._cleanup_kick_worker, gid, report_to,
                            {"id": 0, "first_name": "网页管理员"})
            self.log("网页触发了已注销账号移除")
        return self._send(h, 302, b"", "text/html; charset=utf-8",
                          extra=[("Location", "/deleted")])

    # -- 个人页 ------------------------------------------------------------
    def _personal_chat(self, uid: int, qs) -> None:
        """个人页要选群：优先 ?g=，其次"这个人有链接的群"，最后第一个群。"""
        want = (qs.get("g") or [""])[0] if qs else ""
        if want.lstrip("-").isdigit():
            return
        cur = self._gid()
        try:
            row = self.db.get_link_by_owner(uid, cur) if cur is not None else None
            if row:
                return
            for g in self.db.links_of_owner(uid):
                self._tl.gid = g["chat_id"]
                return
        except Exception:
            return

    def _personal(self, h, uid: int, qs=None) -> None:
        self._personal_chat(uid, qs or {})
        chat = self._chat()
        if not chat:
            return self._error(h, 200, "还没有绑定群组", "请稍后再试。")
        gid = chat["chat_id"]
        invited = self.db.count_for(uid, gid)
        present = self.db.current_for(uid, gid)
        rank = self.db.rank_of(uid, gid)
        size = self.db.leaderboard_size(gid)
        s7 = None
        for s in self.db.quality_scores(gid, 7, float(self._setting("quality_prior_weight", 5.0)),
                                        float(self._setting("quality_prior_rate", 0.5))):
            if s["inviter_id"] == uid:
                s7 = s
                break
        who = self._who(uid)
        stats = (f'<div class="stats">'
                 f'{self._stat("累计邀请", invited, "人", "link")}'
                 f'{self._stat("仍在群", present, "人", "users", tone="ok")}'
                 f'{self._stat("排名", rank or "—", f"/ {size}", "trophy", tone="warn")}'
                 f'{self._stat("质量分", f"{s7['score']:.1f}" if s7 else "—", "", "star")}</div>')

        link_row = self.db.get_link_by_owner(uid, gid)
        link = link_row["invite_link"] if (link_row and not link_row["revoked"]) else None
        script = ""
        if link:
            qr_block = ""
            try:
                import qrgen
                qr_block = f'<div class="qr">{qrgen.svg(link, scale=4, border=2)}</div>'
            except Exception:
                qr_block = ""
            qr_html = f'<p>{qr_block}</p>' if qr_block else '<p class="muted">（二维码模块未启用）</p>'
            link_html = (f'<div class="card pad"><h2 style="margin:0 0 12px">我的专属邀请链接</h2>'
                         f'<div class="linkbox" id="lk">{esc(link)}</div>'
                         f'<div class="row" style="margin-top:12px">'
                         f'<button class="btn btn-primary" onclick="cp()">📋 复制链接</button>'
                         f'<a class="btn" href="{esc(link)}" target="_blank" rel="noopener">在 Telegram 打开</a>'
                         f'</div>{qr_html}</div>')
            script = ("<script>function cp(){var t=document.getElementById('lk').innerText.trim();"
                      "if(navigator.clipboard){navigator.clipboard.writeText(t);}"
                      "var b=event.target;b.textContent='\u2705 \u5df2\u590d\u5236';"
                      "setTimeout(function(){b.textContent='\U0001F4CB \u590d\u5236\u94fe\u63a5'},1500);}</script>")
        else:
            link_html = ('<div class="card pad muted">还没有领取链接，'
                         '回到 Telegram 给机器人发送 /link 即可领取。</div>')

        rows = self.db.invitees_of(uid, gid, limit=25, offset=0)
        trs = []
        for r in rows:
            flag = {"member": '<span class="badge b-ok">在群</span>',
                    "left": '<span class="badge b-mute">已退群</span>',
                    "kicked": '<span class="badge b-bad">被移出</span>'}.get(r["status"], r["status"])
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["joined_at"] or 0))
            nm = r["invitee_name"] or self._who(r["invitee_id"])
            trs.append(f'<tr><td><div class="who">{self._avatar(nm)}<span class="nm">{esc(nm)}</span></div></td>'
                       f'<td class="n">{when}</td><td>{flag}</td></tr>')
        table = ('<div class="tablewrap"><table><thead><tr><th>成员</th><th class="n">加入时间</th>'
                 '<th>状态</th></tr></thead><tbody>'
                 + ("".join(trs) or f'<tr><td colspan="3"><div class="empty">{icon("users", 34)}'
                                    f'<p>还没有人通过你的链接进群</p></div></td></tr>') + "</tbody></table></div>")
        body = (f'<div class="card pad" style="margin-bottom:16px"><div class="row">'
                f'{self._avatar(who)}<div><h2 style="margin:0">{esc(who)} 的战绩</h2>'
                f'<p class="muted" style="margin:0">{esc(chat["title"])}</p></div></div></div>'
                + stats + '<div style="height:16px"></div>' + link_html
                + '<h2>我邀请的人</h2>' + table)
        plain = (f'<!doctype html><html lang="zh-CN" data-theme="dark"><head><meta charset="utf-8">'
                 f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                 f'<meta name="robots" content="noindex,nofollow"><title>{esc(who)} 的战绩</title>'
                 f'{THEME_BOOT}<style>{CSS}</style></head><body>'
                 f'<div class="app"><div class="main" style="margin-left:0">'
                 f'<header class="topbar"><div class="titles"><h1>{esc(who)} 的战绩</h1>'
                 f'<p>{esc(self.site)} · 个人页（签名链接，可安全转发）</p></div>'
                 f'<div class="actions"><button class="iconbtn" id="themebtn" onclick="dshToggleTheme()">'
                 f'{icon("sun", 18)}</button></div></header>'
                 f'<main class="content">{body}</main></div></div>'
                 f'{TOGGLE_JS % {"sun": icon("sun", 18), "moon": icon("moon", 18)}}{script}'
                 f'</body></html>').encode("utf-8")
        self._html(h, 200, plain)

    # -- 二维码 ------------------------------------------------------------
    def _qr(self, h, uid, qs=None) -> None:
        if uid is None:
            return self._error(h, 403, "链接无效", "")
        self._personal_chat(uid, qs or {})
        chat = self._chat()
        if not chat:
            return self._error(h, 404, "未绑定群组", "")
        row = self.db.get_link_by_owner(uid, chat["chat_id"])
        if not row or row["revoked"]:
            return self._error(h, 404, "还没有专属链接", "回到 Telegram 发送 /link 领取。")
        try:
            import qrgen
            data = qrgen.svg(row["invite_link"], scale=6, border=3).encode("utf-8")
        except Exception as e:
            return self._error(h, 503, "二维码不可用", str(e)[:120])
        self._send(h, 200, data, "image/svg+xml; charset=utf-8")


def _make_handler(panel: "WebPanel"):
    class Handler(BaseHTTPRequestHandler):
        server_version = "xiyuer-bot-web"
        sys_version = ""

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            panel.handle(self, "GET")

        def do_POST(self):
            panel.handle(self, "POST")

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError):
                pass

    return Handler
