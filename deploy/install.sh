#!/usr/bin/env bash
# deploy/install.sh — 在一台干净的 Debian/Ubuntu VPS 上部署本程序。
# 用法(需要 root):
#   bash install.sh your-domain.com
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
	echo "用法: bash install.sh <域名>" >&2
	exit 1
fi

REPO="https://github.com/VMInsideVM/poly-marketmaker.git"
BASE=/opt/pmm
APP="$BASE/poly-marketmaker"

echo "==> 设置时区为 Asia/Shanghai"
# 每日盈亏台账、周报、监控 watermark 都按本地时间算,留在 UTC 会导致日期错位。
timedatectl set-timezone Asia/Shanghai

echo "==> 安装系统依赖"
apt-get update
apt-get install -y python3 python3-venv python3-pip git curl ufw \
	debian-keyring debian-archive-keyring apt-transport-https

echo "==> 创建服务用户 pmm"
id -u pmm >/dev/null 2>&1 || useradd --system --create-home --home-dir "$BASE" --shell /usr/sbin/nologin pmm
mkdir -p "$BASE"
chown -R pmm:pmm "$BASE"

echo "==> 克隆代码"
# 必须是 git clone(而不是下载 zip):网页上的「更新」按钮靠 git fetch/reset 工作。
if [ ! -d "$APP/.git" ]; then
	sudo -u pmm git clone "$REPO" "$APP"
fi

echo "==> 建虚拟环境、装 Python 依赖"
sudo -u pmm python3 -m venv "$BASE/venv"
sudo -u pmm "$BASE/venv/bin/pip" install --upgrade pip
sudo -u pmm "$BASE/venv/bin/pip" install -r "$APP/requirements.txt"

echo "==> 安装 Caddy"
if ! command -v caddy >/dev/null 2>&1; then
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' |
		gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' |
		tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
	apt-get update
	apt-get install -y caddy
fi

echo "==> 配置 Caddy(域名: $DOMAIN)"
sed "s/YOUR_DOMAIN/$DOMAIN/" "$APP/deploy/Caddyfile" >/etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo "==> 安装 systemd 服务"
cp "$APP/deploy/pmm.service" /etc/systemd/system/pmm.service
systemctl daemon-reload
systemctl enable --now pmm

echo "==> 配置防火墙"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo
echo "完成。打开 https://$DOMAIN 首次设置密码。"
echo "查看服务日志: journalctl -u pmm -f"
