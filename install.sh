#!/usr/bin/env bash
# =============================================================
# TG 邀请统计机器人 —— 一键安装脚本
# 只需要粘贴 BotFather 给你的机器人 Token，其余全自动。
#
# 用法：
#   bash install.sh
#   # 或直接带 Token：
#   bash install.sh "123456:ABC..."
#
# 环境要求：Ubuntu/Debian（root 或 sudo），能访问外网。
# 核心功能纯 Python 标准库，无需 pip；禁漫下载功能可选装依赖。
# =============================================================
set -e

APP_DIR="/opt/tg-invite-bot"
SERVICE="tg-invite-bot"

echo "=============================================="
echo " TG 邀请统计机器人 一键安装"
echo "=============================================="

# ---------- 1. Token ----------
TOKEN="${1:-}"
if [ -z "$TOKEN" ]; then
  read -r -p "请粘贴机器人 Token（BotFather 里复制）: " TOKEN
fi
if [ -z "$TOKEN" ]; then
  echo "❌ 没有提供 Token，退出。"
  exit 1
fi
echo "✅ 已收到 Token"

# ---------- 2. 基础环境 ----------
if command -v python3 >/dev/null 2>&1; then
  echo "✅ python3: $(python3 -V 2>&1)"
else
  echo "正在安装 python3…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y >/dev/null && apt-get install -y python3 >/dev/null
fi

# ---------- 3. 代码 ----------
if [ -d "$APP_DIR/.git" ]; then
  echo "⏭ 检测到已有安装，执行 git pull…"
  (cd "$APP_DIR" && git pull --ff-only)
else
  echo "正在下载项目…"
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 https://github.com/qwwqeras1145-bot/tg-invite-bot.git "$APP_DIR"
  else
    echo "❌ 需要 git。请先安装：apt-get install -y git"
    exit 1
  fi
fi
cd "$APP_DIR"

# ---------- 4. 生成配置 ----------
python3 - "$TOKEN" <<'PYEOF'
import hashlib, json, os, secrets, string, sys

token = sys.argv[1]
cfg_path = "/opt/tg-invite-bot/config.json"
cfg = json.load(open("/opt/tg-invite-bot/config.example.json", encoding="utf-8"))

cfg["bot_token"] = token
cfg["db_path"] = "/opt/tg-invite-bot/data/bot.db"

# 应急口令：随机高强口令，只存加盐哈希（PBKDF2-HMAC-SHA256 60 万次）
alphabet = string.ascii_letters + string.digits + "!@#%&*-_"
pwd = "".join(secrets.choice(alphabet) for _ in range(24))
salt = secrets.token_hex(16)
dkey = hashlib.pbkdf2_hmac("sha256", pwd.encode(), bytes.fromhex(salt), 600_000)
cfg["web_password_hash"] = dkey.hex()
cfg["web_password_salt"] = salt
cfg["web_password_iter"] = 600_000
cfg["web_emergency_path"] = "/bbufg" + secrets.token_hex(2)

json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
os.chmod(cfg_path, 0o600)

print("=" * 56)
print("【重要】请保存下面的应急入口信息（只显示这一次）")
print(f"  应急口令：{pwd}")
print(f"  应急地址：http://<你的域名或IP>:{cfg['web_port']}{cfg['web_emergency_path']}")
print("=" * 56)
open("/opt/tg-invite-bot/.emergency", "w").write(f"{pwd}\n{cfg['web_emergency_path']}\n")
os.chmod("/opt/tg-invite-bot/.emergency", 0o600)
PYEOF

# ---------- 5. 可选：禁漫下载依赖 ----------
echo ""
echo "可选安装禁漫下载依赖（jmcomic）？"
read -r -p "不需要就直接回车跳过 [y/N]: " WANT_JM
if [ "${WANT_JM,,}" = "y" ]; then
  (command -v pip3 >/dev/null 2>&1 || (apt-get install -y python3-pip >/dev/null 2>&1)) \
    && pip3 install --quiet --break-system-packages jmcomic \
    && echo "✅ jmcomic 已安装（/jm 下载可用）" \
    || echo "⚠️ jmcomic 安装失败：/jm 功能暂不可用（其余功能不受影响）"
fi

# ---------- 6. systemd 服务 ----------
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=TG Invite Bot
After=network.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/invite_bot.py run
Restart=always
RestartSec=3
StandardOutput=append:/var/log/tg-invite-bot.log
StandardError=append:/var/log/tg-invite-bot.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"

# ---------- 7. 结果 ----------
sleep 3
if systemctl is-active --quiet "$SERVICE"; then
  echo ""
  echo "✅ 安装完成，机器人已启动！"
  echo "  服务状态：systemctl status $SERVICE"
  echo "  日志：tail -f /var/log/tg-invite-bot.log"
  echo "  应急口令：cat $APP_DIR/.emergency"
  echo ""
  echo "接下来在 Telegram 里做 3 步："
  echo "  1. 把机器人拉进群，设为管理员，勾选「邀请用户」"
  echo "  2. 群里发 /bind"
  echo "  3. 在 @BotFather 发 /setprivacy 选 Disable（关隐私模式才能统计群消息）"
else
  echo "❌ 服务启动失败，看日志：journalctl -u $SERVICE -n 50"
  exit 1
fi
