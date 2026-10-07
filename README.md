# TG 邀请统计机器人（tg-invite-bot）

一个纯 Python 标准库实现的 Telegram 群邀请统计机器人 + 网页管理后台。

给群里每个人一条**专属邀请链接**，谁拉了多少人、留存多久、谁在活跃，全部自动统计；网页后台可查看排行榜、活跃数据、质量分、入群申请审批、账号权限管理，支持多群、深色/浅色主题、自定义 LOGO/背景。

## ✨ 功能

**机器人**
- `/link` 领取专属邀请链接，`/stats` 战绩，`/top` 排行榜，`/list` 明细
- 入群自动归因（Telegram `invite_link` 字段），同一人只计一次防刷
- 入群申请自动处理：拒绝已注销账号 / 全部拒绝 / 专属链接自动放行（可开关）
- 多群支持：`/bind`、`/groups`、`/switch`
- 活跃统计（每人发言条数、活跃天数）、留存分析、质量分（贝叶斯平滑）
- 已注销账号扫描（只标记不踢人，踢人需显式 CONFIRM）
- 里程碑祝贺、每周榜单播报、每日自动备份、审计日志
- **禁漫下载**（可选依赖）：`/jm dl 漫画码` 自动分卷发送、断点续传、多章节分目录、BOM 容错（详见 `jm_tg.py`）

**网页后台**（纯标准库 HTTPS 服务器，无任何外部 CDN）
- 总览 / 排行榜 / 活跃数据 / 质量分 / 留存 / 漏斗 / 入群申请 / 链接申请 / 已注销 / 审计日志 / 功能设置 / 下载管理
- 每人独立账号：Telegram 一键登录 / 用户名密码 / 邮箱密码，TOTP 两步验证（RFC 6238）
- 权限分级：超管 / 管理员 / 有实权的群管理员 / 普通成员
- 应急口令入口（随机隐藏路径 + 加盐哈希存储）、CSRF 防护、安全响应头、登录限速
- 自定义 LOGO / 背景图（设置页上传，≤2MB）

## 🚀 一键安装

```bash
bash install.sh
# 或直接带 Token：
bash install.sh "123456:你的机器人Token"
```

脚本会自动：装 python3 → 拉取本仓库 → 生成配置（含随机应急口令）→ 可选安装 jmcomic → 建 systemd 服务 → 启动。

装完后在 Telegram 做 3 步：

1. 把机器人拉进群，设为管理员，勾选「邀请用户」
2. 群里发 `/bind`
3. 在 @BotFather 发 `/setprivacy` 选 **Disable**（不关隐私模式统计不到普通消息）

## ⚙️ 配置

复制 `config.example.json` 为 `config.json` 后修改：

| 字段 | 说明 |
|---|---|
| `bot_token` | 机器人 Token（必填） |
| `owner_ids` | 超管 Telegram ID 列表 |
| `web_port` | 网页端口（默认 8080） |
| `web_password_hash/salt/iter` | 应急口令的加盐哈希（install.sh 自动生成） |
| `web_emergency_path` | 应急入口的隐藏路径 |
| `web_allowed_hosts` | Host 白名单（防 IP 扫描） |
| `web_tls_cert/key` | 自签/Let's Encrypt 证书路径（留空则 HTTP） |

## 🔒 安全要点

- 密码只存 **PBKDF2-HMAC-SHA256 加盐哈希**（60 万次迭代），不存明文
- CSRF 令牌、登录限速（IP + 账号双维度）、会话 12 小时过期、禁用/改密码立刻踢下线
- SNI + Host 双层白名单，兜底端口只绑本机
- 所有敏感操作写审计日志

## 🧪 测试

```bash
python3 tests_flow.py    # 归因/去重/退群/防刷/入群申请/清理/多群
python3 tests_web.py     # 鉴权/分页/CSRF/2FA/品牌外观/下载管理
```

## 📁 目录

```
invite_bot.py    机器人核心（含 /jm 下载指令）
webpanel.py      网页后台（含品牌外观、下载管理）
jm_tg.py         禁漫下载模块（需 jmcomic，可选）
qrgen.py         纯标准库二维码生成
setup_bot_profile.py  注册机器人简介/头像/命令菜单
install.sh       一键安装脚本
config.example.json   配置模板
tests_*.py       测试
```

## 📄 License

MIT
