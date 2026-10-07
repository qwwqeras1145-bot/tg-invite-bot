# TG 邀请统计机器人（tg-invite-bot）

一个**纯 Python 标准库**实现的 Telegram 群邀请统计机器人 + 网页管理后台。

给群里每个人一条专属邀请链接，谁拉了多少人、留存多久、谁在活跃，全部自动统计；网页后台可看排行榜、活跃数据、质量分、入群申请审批、账号权限管理。支持多群、深色/浅色主题、自定义 LOGO/背景、TOTP 两步验证，还内置了禁漫下载模块（可选依赖）。

> 核心运行零第三方依赖（只用标准库）；只有「禁漫下载」功能可选安装 `jmcomic`。

---

## 目录

- [功能特性](#功能特性)
- [架构](#架构)
- [快速开始（一键安装）](#快速开始一键安装)
- [手动部署](#手动部署)
- [机器人指令表](#机器人指令表)
- [禁漫下载（/jm）](#禁漫下载jm)
- [网页后台页面说明](#网页后台页面说明)
- [权限体系](#权限体系)
- [配置项说明](#配置项说明)
- [安全设计](#安全设计)
- [常见问题 FAQ](#常见问题-faq)
- [更新日志](#更新日志)
- [测试与开发](#测试与开发)
- [License](#license)

---

## 功能特性

### 🤖 机器人

| 能力 | 说明 |
|---|---|
| 专属邀请链接 | 每人一条（Telegram `invite_link` 字段自动归因），链接坏了可一键回收重发 |
| 邀请统计 | 邀请人数、留存、退群追踪；同一人只计一次防刷 |
| 入群申请管理 | 自动拒绝已注销账号 / 自动拒绝全部 / 专属链接自动放行（均可开关） |
| 多群支持 | `/bind` 绑定、`/groups` 列出、`/switch` 切换，每个群独立统计 |
| 活跃统计 | 每人发言条数、活跃天数（不记录消息内容） |
| 留存 / 质量分 | 贝叶斯平滑质量分、留存分析、掉人榜 |
| 里程碑 | 邀请满 5/10/20/50/100 人群里祝贺 + 私聊通知 |
| 已注销账号清理 | 用 `getChatMember` 扫描，只标记不踢人（踢人需显式 CONFIRM） |
| 自动备份 | 每天备份 SQLite 到 `data/backups`，保留 14 份 |
| 每周播报 | 每周一自动发上周邀请榜 |
| 审计日志 | 所有敏感操作留痕，可指定日志会话 |
| 群发 | `/broadcast` 给所有使用者广播 |

### 🖥️ 网页后台

| 页面 | 说明 |
|---|---|
| 总览 | 核心指标卡 + 邀请榜 |
| 邀请排行榜 | 公开/私密可切换，支持签名 URL（HMAC）防篡改 |
| 活跃数据 | 每人发言条数、活跃天数（成员格内直接显示徽章，手机也看得见） |
| 留存分析 / 质量分 / 掉人榜 | 数据洞察三件套 |
| 入群申请 | 待处理列表**逐条通过/拒绝** + 批量操作 + 自动同步 |
| 链接申请 | 成员领链接的申请列表 |
| 已注销账号 | 扫描结果 + 一键清理 |
| 审计日志 | 全部操作留痕，可筛选 |
| 功能设置 | 全部功能开关 + LOGO/背景图自定义 |
| 下载管理 | 禁漫下载的本地文件管理 + 自动清理设置 |

### 🔒 安全

- 密码只存 **PBKDF2-HMAC-SHA256 加盐哈希**（60 万次迭代），配置里无明文
- 应急口令入口：**随机隐藏路径**（如 `/bbufgf3x`），登录页上没有任何入口，且不受"允许口令登录"开关锁死（救命通道永远可用）
- 每人独立账号：Telegram 一键登录 / 用户名密码 / 邮箱密码 + **TOTP 两步验证**（RFC 6238）
- CSRF 令牌（只绑会话不绑群）、登录限速（IP + 账号双维度，10 次/15 分钟）、会话 12 小时过期
- SNI + Host 双层白名单、兜底端口只绑 127.0.0.1、安全响应头（CSP/HSTS/X-Frame-Options）
- CSV 导出防公式注入、表单体 8MB 上限防滥用

### 📕 禁漫下载（可选）

- `/jm dl 漫画码` 下载：多章节分目录、断点续传、缺页自动补全（最多 15 轮、并发 8→4→2→1 自动降级）
- 自动分卷（每卷 ≤45MB，不撞 Telegram 50MB 上限）
- BOM 容错（图源偶发 BOM 头导致 jmcomic 解析失败，已打补丁）
- 自动清理：发送成功后立即删除 / 30 分钟 / 5 小时 / 1 天 / 关闭

---

## 架构

```
invite_bot.py       机器人核心（长轮询、归因、申请处理、统计、/jm 指令）
webpanel.py         网页后台（纯标准库 ThreadingHTTPServer + 自签 TLS/明文 HTTP）
jm_tg.py            禁漫下载模块（import jmcomic 可选，缺依赖自动降级提示）
qrgen.py            纯标准库二维码生成（网页战绩页用）
setup_bot_profile.py 注册机器人头像/简介/命令菜单（跑一次即可）
install.sh          一键安装脚本
config.example.json 配置模板（所有键都有注释）
tests_flow.py       机器人逻辑测试（归因/去重/退群/防刷/入群申请/清理/多群）
tests_web.py        网页测试（鉴权/分页/CSRF/2FA/品牌外观/下载管理）
```

技术要点：

- **零框架**：机器人 = `getUpdates` 长轮询 + `sqlite3`；网页 = `ThreadingHTTPServer` 手写路由/会话/CSP
- **归因原理**：Telegram 的 `chat_member` 更新里带 `invite_link` 字段，与库里的链接对账即可归因，无需猜
- **隐私模式**：机器人只能在 `can_read_all_group_messages=True` 时收到普通群消息（统计活跃需要）；脚本安装后按提示去 @BotFather 关掉
- **Telegram API 限制**：Bot API 没有"列出待处理入群申请"的接口，机器人只能记录它上任之后的新申请；积压的历史申请需在 Telegram 客户端清理

---

## 快速开始（一键安装）

环境要求：Ubuntu/Debian 20.04+（root 或 sudo）、可访问外网、`git`。

```bash
# 1. 在 @BotFather 创建机器人（/newbot），复制 Token
# 2. 在服务器上执行：
bash <(curl -fsSL https://raw.githubusercontent.com/qwwqeras1145-bot/tg-invite-bot/main/install.sh)
#    或
git clone https://github.com/qwwqeras1145-bot/tg-invite-bot.git /opt/tg-invite-bot
cd /opt/tg-invite-bot && bash install.sh "123456:你的机器人Token"
```

脚本自动完成：装 python3 → 生成配置（含随机应急口令 + 隐藏入口地址，**只显示一次，务必保存**）→ 可选装 jmcomic → 建 systemd 服务 → 启动 → 打印后续步骤。

装完后在 Telegram 里做 4 步：

1. 把机器人拉进群
2. 设为管理员，勾选「**邀请用户**」（必要）
3. 群里发 `/bind`（或机器人会自动绑定）
4. 在 @BotFather 发 `/setprivacy` → 选 **Disable**（不关就统计不到普通消息；机器人每小时自检一次并提示）

---

## 手动部署

```bash
# 1. 拉代码
git clone https://github.com/qwwqeras1145-bot/tg-invite-bot.git /opt/tg-invite-bot
cd /opt/tg-invite-bot

# 2. 配置
cp config.example.json config.json
#    编辑 config.json：填 bot_token、owner_ids（你的 Telegram 数字 ID）
#    生成应急口令哈希（推荐）：
python3 - <<'PYEOF'
import hashlib, json, secrets, string
cfg = json.load(open("config.json", encoding="utf-8"))
import random
alphabet = string.ascii_letters + string.digits + "!@#%&*-_"
pwd = "".join(secrets.choice(alphabet) for _ in range(24))
salt = secrets.token_hex(16)
cfg["web_password_hash"] = hashlib.pbkdf2_hmac(
    "sha256", pwd.encode(), bytes.fromhex(salt), 600_000).hex()
cfg["web_password_salt"] = salt
cfg["web_password_iter"] = 600_000
cfg["web_emergency_path"] = "/bbufg" + secrets.token_hex(2)
json.dump(cfg, open("config.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("应急口令：", pwd)
print("应急地址： http://<你的IP或域名>:8080" + cfg["web_emergency_path"])
PYEOF
chmod 600 config.json

# 3. 启动（systemd 或前台）
sudo cp -n /dev/null /etc/systemd/system/tg-invite-bot.service
#   （把 install.sh 里的 systemd 模板抄进去，WorkingDirectory 指到本目录）
#   或直接前台跑：
python3 invite_bot.py run

# 4. 注册机器人资料（可选但推荐）
python3 setup_bot_profile.py
```

> 没有域名时，网页默认跑在 `http://服务器IP:8080`。强烈建议配置 `web_allowed_hosts` 白名单防 IP 扫描；要 HTTPS 可以把 `web_tls_cert`/`web_tls_key` 指到证书文件，或用任意隧道工具转发。

---

## 机器人指令表

### 私聊（任何人）

| 指令 | 说明 |
|---|---|
| `/start` | 开始使用 / 主菜单 |
| `/link` | 领取我的专属邀请链接 |
| `/mylink` | 查看我的链接 |
| `/stats` | 我的邀请战绩 |
| `/list` | 我邀请进群的人 |
| `/top` | 群邀请排行榜 |
| `/web` | 我的网页战绩页（含二维码） |
| `/switch` | 切换当前群 |
| `/jm` | 禁漫下载帮助 |
| `/help` | 使用帮助 |

### 群聊（任何人）

| 指令 | 说明 |
|---|---|
| `/link` | 领取专属邀请链接 |
| `/top` | 群邀请排行榜 |
| `/stats` | 我的邀请战绩 |
| `/active` | 群活跃数据 |
| `/jm` | 禁漫下载帮助 |

### 管理员

| 指令 | 说明 |
|---|---|
| `/manage` | 管理面板（网页后台入口） |
| `/declineall` | 一键拒绝所有待处理申请 |
| `/groups` | 查看所有已绑定群 |
| `/bind` | 绑定本群 |
| `/panel` | 发布领取链接面板 |
| `/cleanup` | 扫描已注销账号 |
| `/logs` | 审计日志 |
| `/settings` | 功能开关 |

### 超管（owner_ids）

`/addadmin`、`/deladmin`、`/broadcast 内容`、`/reset CONFIRM`、`/setwelcome`、`/site`

---

## 禁漫下载（/jm）

中英文命令都支持（`/jm dl 515320` = `/jm 下载 515320`）。

| 指令 | 说明 |
|---|---|
| `/jm <漫画码>` | 查询信息（标题/作者/页数/标签） |
| `/jm dl <漫画码>` | 下载并发 8，缺页自动补全，自动分卷发送 |
| `/jm search <关键词> [页码]` | 搜索 |
| `/jm top [日榜/周榜/月榜] [页码]` | 排行榜 |
| `/jm random` | 全站随机一本并下载 |
| `/jm progress <漫画码>` | 查看下载进度 |
| `/jm autodel <模式>` | 设置自动清理：`off` / `immediate` / `30m` / `5h` / `1d` |
| `/jm version` | 版本信息 |

设计要点：

- **多章节本子不丢章**：按 `专辑/章节` 分目录，避免章节间图片同名互相顶掉
- **缺页自动补全**：每轮拿到多少算多少，缺页自动降并发重试，最多 15 轮；连续 3 轮零增长才收手
- **分卷 ≤45MB**：Telegram Bot API 单文件上限 50MB，大本子自动切成多卷逐个发送
- **断点续传**：重发命令只补缺页；<10KB 的半截文件自动清理
- **BOM 容错**：图源偶尔在 JSON 开头带 BOM 头导致解析失败，已在 HTTP 层打补丁
- 依赖安装：`pip install jmcomic`（装不上时 /jm 会提示，其余功能不受影响）

---

## 网页后台页面说明

| 页面 | 路径 | 内容 |
|---|---|---|
| 总览 | `/` | 成员/邀请/活跃核心指标 + 邀请榜 |
| 邀请排行榜 | `/leaderboard` | 带名次金银铜徽章 |
| 活跃数据 | `/activity` | 发言条数 + 活跃天数（名字下直接显示徽章） |
| 留存分析 | `/retention` | 拉进来的人留了多久 |
| 质量分 | `/quality` | 贝叶斯平滑的邀请质量分 |
| 掉人榜 | `/churn` | 谁拉的人退群最多 |
| 入群申请 | `/requests` | 逐条通过/拒绝 + 批量操作 + 自动同步 |
| 链接申请 | `/linkreq` | 领链接申请列表 |
| 已注销账号 | `/deleted` | 扫描结果 + 清理（可踢人，需 CONFIRM） |
| 审计日志 | `/logs` | 全部操作留痕 |
| 功能设置 | `/settings` | 所有开关 + LOGO/背景图自定义 |
| 下载管理 | `/jmdl` | 下载清单、占用、单本删除、全部清空、自动清理模式 |

外观自定义：设置页「更换 LOGO（裁剪）」内置了**微信/QQ 式裁剪器**（拖动 + 缩放 + 圆形预览），「选择背景图」上传后全站生效，均可一键恢复默认。

---

## 权限体系

| 角色 | 来源 | 能做什么 |
|---|---|---|
| 平台所有者 | `owner_ids` / 应急口令 | 一切，含账号管理、群发、清空统计 |
| 平台管理员 | `/addadmin` 添加 | 大部分管理操作 |
| 群管理员（有实权） | 群内管理员且持有指定权限 | 本群的申请审批、清理 |
| 普通成员 | 绑定账号 | 看自己的数据、领链接 |

网页登录方式：Telegram 一键登录（群成员）、用户名+密码、邮箱+密码、应急口令（隐藏入口）。每人可开 TOTP 两步验证。

---

## 配置项说明

`config.example.json` 全部键：

| 键 | 默认 | 说明 |
|---|---|---|
| `bot_token` | — | 机器人 Token（必填） |
| `owner_ids` | `[]` | 超管 Telegram 数字 ID 列表 |
| `web_port` | `8080` | 网页端口 |
| `web_bind_host` | `0.0.0.0` | 网页监听地址（只本机访问改 `127.0.0.1`） |
| `web_public_host` | `""` | 公网 IP（排障信息用） |
| `web_fallback_port` | `0` | 兜底端口（0=关；只绑本机，供隧道用） |
| `web_allowed_hosts` | `[]` | Host 白名单（填你的域名/IP，防 IP 扫描） |
| `web_site_title` | `"邀请统计后台"` | 站点名称 |
| `web_base_url` | `""` | 对外访问地址（签名/跳转用） |
| `web_use_tunnel_url` | `false` | 是否使用隧道日志里的地址 |
| `web_public_leaderboard` | `false` | 排行榜是否公开（不登录可看） |
| `web_password_hash` / `_salt` / `_iter` | — | 应急口令的加盐哈希（install.sh 自动生成） |
| `web_emergency_path` | `""` | 应急入口隐藏路径（如 `/bbufgf3x`） |
| `web_allow_password_login` | `true` | 允许口令登录总开关（不影响应急入口） |
| `web_tls_cert` / `web_tls_key` | — | HTTPS 证书路径（留空=HTTP） |
| `reg_mode` | `"invite"` | 网页注册模式：`invite`/`open`/`closed` |
| `group_admin_min_rights` | `"can_invite_users"` | 群管理员需持有的最小权限 |
| `jm_enabled` | `true` | 是否启用 /jm 下载 |
| `smtp_host/port/user/pass/from/tls` | — | 邮箱验证码（不配则邮箱登录不可用） |
| `db_path` | `data/bot.db` | SQLite 路径 |
| `log_chat_id` | `0` | 日志会话 ID |

其余功能开关（欢迎语、活跃统计、自动拒绝、里程碑、备份、周报等）默认值见 `config.example.json`，**运行后可在网页「功能设置」页直接改**，无需改文件重启。

---

## 安全设计

1. **口令**：只存 PBKDF2-HMAC-SHA256 加盐哈希（60 万次迭代，OWASP 推荐），无明文
2. **应急入口**：隐藏随机路径，登录页零入口；不受"允许口令登录"开关锁死，避免管理员自锁
3. **登录限速**：IP + 账号双维度（10 次/15 分钟），失败写审计
4. **会话**：HMAC 签名 Cookie，12 小时过期；改密码/禁用账号立即踢下线
5. **CSRF**：所有 POST 校验令牌，令牌只绑会话不绑群（切群不会误报）
6. **TOTP**：RFC 6238 标准实现，通过官方测试向量
7. **网络**：SNI + Host 双层白名单；兜底端口只绑本机
8. **数据**：CSV 导出防公式注入；上传表单体 8MB 上限；图片上传只收 PNG/JPEG/WebP 且服务端二次校验大小
9. **审计**：登录成败、审批、清理、配置变更全部留痕

---

## 常见问题 FAQ

**Q：网页显示有 11 条待处理，但 Telegram 群里没有？**
A：Bot API 没有"列出待处理申请"的接口，机器人收不到"别人在客户端处理了"的通知。开启「自动同步申请状态」（默认开）后，每 5 分钟自动核对一次：还在的就按规则处理，已经不存在的自动归档。

**Q：为什么只有一半页数？**
A：多章节本子已修（分章节目录）；如果显示"缺 N 页"，重发 `/jm dl <码>` 自动补全。

**Q：排行榜链接打开要密码？**
A：`web_public_leaderboard` 默认 false。打开后排行榜可匿名看，其余页面仍要登录。

**Q：隐私模式是什么？**
A：@BotFather 的 `/setprivacy`。Disabled 时机器人能收到群里的普通消息（统计活跃需要）；Enabled 时只能收到指令。机器人每小时自检并在需要时提示。

**Q：忘记应急口令了？**
A：应急口令只在安装时显示一次。忘记的话用 `owner_ids` 里的超管身份登录网页（Telegram 一键登录），在设置页重设；或者直接改 `config.json` 的哈希三件套后重启。

**Q：能统计机器人上任之前的申请吗？**
A：不能。Telegram 不会把历史申请推给机器人。老申请在客户端清一次即可。

---

## 更新日志

每个版本的变更都记录在 [CHANGELOG.md](CHANGELOG.md)（版本号、日期、新增/修复/安全分类）。

当前版本：**v1.5.0**

## 测试与开发

```bash
python3 tests_flow.py     # 机器人逻辑（归因/去重/退群/防刷/入群申请/清理/多群）
python3 tests_web.py      # 网页（鉴权/分页/CSRF/2FA/品牌外观/下载管理）
```

测试全部离线（不连 Telegram），用假数据打桩。改完代码跑一遍再上线。

## License

MIT
