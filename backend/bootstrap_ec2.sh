#!/bin/bash
# =============================================================
# FYERS InsideBar FULL PRODUCTION INSTALLER (FIXED)
# Docker + systemd + persistent trading bot + S3 logs
# =============================================================

set -euo pipefail

APP_DIR="/home/ec2-user/fyers_insidebar"
BACKEND_DIR="/home/ec2-user/fyers_insidebar/backend"

REPO_URL="https://github.com/avijitmajumder2050/fyers_insidebar.git"

IMAGE_NAME="fyers-insidebar"
CONTAINER_NAME="insidebar-strategy"

LOG_FILE="/var/log/fyers_insidebar.log"
S3_BUCKET="s3://dhan-trading-data/trading-bot/logs"

echo "================================================"
echo "  FYERS InsideBar INSTALLER (PRODUCTION FIXED)"
echo "================================================"

# -------------------------------------------------------------
# 1. INSTALL DEPENDENCIES
# -------------------------------------------------------------
yum update -y
yum install -y docker git awscli

systemctl enable docker
systemctl start docker

usermod -aG docker ec2-user || true

echo "✅ Docker ready"

# -------------------------------------------------------------
# 2. CLONE REPO
# -------------------------------------------------------------
rm -rf "$APP_DIR"
git clone "$REPO_URL" "$APP_DIR"

echo "✅ Repo cloned"

# -------------------------------------------------------------
# 3. BUILD DOCKER IMAGE (BACKEND ONLY)
# -------------------------------------------------------------
cd "$BACKEND_DIR"

echo "Building Docker image..."

docker build --no-cache -t "$IMAGE_NAME" .

echo "✅ Docker image built"

# -------------------------------------------------------------
# 4. RUN SCRIPT (FIXED)
# -------------------------------------------------------------
cat > /home/ec2-user/run_strategy.sh << 'EOF'
#!/bin/bash
set -euo pipefail

IMAGE_NAME="fyers-insidebar"
CONTAINER_NAME="insidebar-strategy"

echo "Stopping old container if exists..."
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

echo "Starting new container..."

docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  -v /var/log/fyers_insidebar.log:/app/logs/app.log \
  "$IMAGE_NAME"

echo "Container started successfully"
EOF

chmod +x /home/ec2-user/run_strategy.sh

# -------------------------------------------------------------
# 5. LOG SYNC SERVICE (S3 BACKUP)
# -------------------------------------------------------------
cat > /home/ec2-user/log_sync.sh << 'EOF'
#!/bin/bash
set -euo pipefail

LOG_FILE="/var/log/fyers_insidebar.log"
S3_BUCKET="s3://dhan-trading-data/trading-bot/logs"

while true
do
  if [ -f "$LOG_FILE" ]; then
    TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")

    aws s3 cp "$LOG_FILE" \
      "$S3_BUCKET/fyers_insidebar_$TIMESTAMP.log" \
      --only-show-errors
  fi

  sleep 300
done
EOF

chmod +x /home/ec2-user/log_sync.sh

# -------------------------------------------------------------
# 6. SYSTEMD SERVICE (FIXED - LONG RUNNING)
# -------------------------------------------------------------
cat > /etc/systemd/system/fyers-strategy.service << 'EOF'
[Unit]
Description=FYERS InsideBar Trading Strategy
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user

ExecStart=/bin/bash /home/ec2-user/run_strategy.sh

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# -------------------------------------------------------------
# 7. LOG SYNC SYSTEMD SERVICE
# -------------------------------------------------------------
cat > /etc/systemd/system/fyers-log-sync.service << 'EOF'
[Unit]
Description=FYERS Log Sync to S3
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user

ExecStart=/bin/bash /home/ec2-user/log_sync.sh

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# -------------------------------------------------------------
# 8. ENABLE SERVICES
# -------------------------------------------------------------
systemctl daemon-reload

systemctl enable fyers-strategy.service
systemctl enable fyers-log-sync.service

systemctl restart fyers-strategy.service
systemctl restart fyers-log-sync.service

echo "================================================"
echo "  INSTALLATION COMPLETE (PRODUCTION READY)"
echo "================================================"
echo "✔ Docker running in background (-d enabled)"
echo "✔ Restart policy: unless-stopped"
echo "✔ systemd: persistent service"
echo "✔ S3 log sync active"
echo "✔ Auto recovery enabled"
echo "================================================"