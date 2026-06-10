#!/bin/bash
# =============================================================
# EC2 Bootstrap — Fyers InsideBar Auto Trading System
# Run as: sudo bash bootstrap_ec2.sh
# OS: Amazon Linux 2023
# =============================================================
set -euo pipefail

APP_DIR="/home/ec2-user/fyers_insidebar"
IMAGE_NAME="fyers-insidebar"
CONTAINER_NAME="insidebar-strategy"
LOG_FILE="/var/log/fyers_insidebar.log"

echo "──────────────────────────────────────────"
echo "  Fyers InsideBar EC2 Bootstrap"
echo "──────────────────────────────────────────"

# ── 1. System update & Docker install ──────────────────────
yum update -y
yum install -y docker git

systemctl start docker
systemctl enable docker
usermod -aG docker ec2-user

echo "✅ Docker installed and started."

# ── 2. Copy app files ──────────────────────────────────────
mkdir -p "$APP_DIR"
cp -r ./backend/. "$APP_DIR/"

# ── 3. Build Docker image ──────────────────────────────────
cd "$APP_DIR"
docker build -t "$IMAGE_NAME" .
echo "✅ Docker image built: $IMAGE_NAME"

# ── 4. Cron — run once at 09:15 IST (03:45 UTC) weekdays ──
# Format: MIN HOUR DAY MON DOW
CRON_JOB="45 3 * * 1-5 docker run --rm --name $CONTAINER_NAME $IMAGE_NAME >> $LOG_FILE 2>&1"

# Remove old entry if exists, then add fresh
crontab -l 2>/dev/null | grep -v "$IMAGE_NAME" | crontab -
(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -

echo "✅ Cron job set: weekdays 09:15 IST"

# ── 5. Create log file ─────────────────────────────────────
touch "$LOG_FILE"
chmod 644 "$LOG_FILE"

echo ""
echo "══════════════════════════════════════════"
echo "  Bootstrap complete."
echo "  Strategy will run at 09:15 IST (Mon–Fri)"
echo "  Logs: $LOG_FILE"
echo "══════════════════════════════════════════"
