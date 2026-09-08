#!/usr/bin/env bash
# DigitalOcean bootstrap for the London weather bots.
# Run ONCE as root on a fresh Ubuntu 24.04 droplet, AFTER rsyncing the
# workspace subset into /opt/london (see deploy/README.md).
set -euo pipefail

WORK=/opt/london
USER=london

# 1. system packages
apt-get update
apt-get install -y python3 python3-venv python3-pip ufw rsync

# 2. unprivileged run user
id -u "$USER" >/dev/null 2>&1 || useradd --system --home "$WORK" --shell /usr/sbin/nologin "$USER"

# 3. python deps (isolated venv; only two non-stdlib packages needed)
cd "$WORK"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install duckdb openpyxl
chown -R "$USER":"$USER" "$WORK"
chmod 600 "$WORK/.env" 2>/dev/null || echo "WARN: no .env found"

# 4. systemd units
cp deploy/london-fieldmaker.service  /etc/systemd/system/
cp deploy/london-dawnwatcher.service /etc/systemd/system/
cp deploy/london-bookrecorder.service /etc/systemd/system/
cp deploy/london-bookrecorder.timer  /etc/systemd/system/
systemctl daemon-reload

# 5. enable: maker 24/7, dawn watcher 24/7 (window self-managed), tape @15min
systemctl enable --now london-fieldmaker.service
systemctl enable --now london-dawnwatcher.service
systemctl enable --now london-bookrecorder.timer

# 6. firewall: ssh only (bots are outbound-only, no domain needed)
ufw allow OpenSSH
ufw --force enable

systemctl --no-pager --plain | grep london || true
echo "bootstrap done. check: journalctl -u london-fieldmaker -f"
