#!/bin/bash
# =============================================================
# FYERS InsideBar FULL PRODUCTION INSTALLER (FIXED LOGGING)
# Docker + systemd + S3 logs (FIXED)
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
echo "  FYERS InsideBar INSTALLER (FIXED LOG SYSTEM)"
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
# 3. BUILD DOCKER IMAGE
# -------------------------------------------------------------
cd "$BACKEND_DIR"

echo "Building Docker image..."

docker build --no-cache -t "$IMAGE_NAME" .

echo "✅ Docker image built"

# -------------------------------------------------------------
# 4. RUN CONTAINER (FIXED - NO VOLUME MOUNT)
# -------------------------------------------------------------

docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

docker run -d \
  --name "$CONTAINER_NAME" \
  --restart on-failure:5 \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  "$IMAGE_NAME"

echo "✅ Container started"




# -------------------------------------------------------------
# 5. LOG CAPTURE FROM DOCKER
# -------------------------------------------------------------

mkdir -p /var/log
touch "$LOG_FILE"

chown ec2-user:ec2-user "$LOG_FILE"
chmod 664 "$LOG_FILE"
# -------------------------------------------------------------
cat > /home/ec2-user/docker_log_capture.sh << 'EOF'
#!/bin/bash

CONTAINER_NAME="insidebar-strategy"
LOG_FILE="/var/log/fyers_insidebar.log"

mkdir -p /var/log


while true
do
  docker logs --timestamps "$CONTAINER_NAME" >> "$LOG_FILE" 2>&1
  sleep 5
done
EOF

chmod +x /home/ec2-user/docker_log_capture.sh


# -------------------------------------------------------------
# 7. SYSTEMD: LOG CAPTURE SERVICE
# -------------------------------------------------------------
cat > /etc/systemd/system/fyers-docker-logs.service << 'EOF'
[Unit]
Description=Docker Log Capture Service
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=ec2-user
ExecStart=/bin/bash /home/ec2-user/docker_log_capture.sh
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# -------------------------------------------------------------
# 8. S3 LOG SYNC SERVICE
# -------------------------------------------------------------
cat > /home/ec2-user/log_sync.sh << 'EOF'
#!/bin/bash

LOG_FILE="/var/log/fyers_insidebar.log"
S3_BUCKET="s3://dhan-trading-data/trading-bot/logs"

while true
do
  if [ -f "$LOG_FILE" ]; then
    TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")

    aws s3 cp "$LOG_FILE" \
      "$S3_BUCKET/fyers_insidebar.log"
  fi

  sleep 300
done
EOF

chmod +x /home/ec2-user/log_sync.sh

cat > /etc/systemd/system/fyers-log-sync.service << 'EOF'
[Unit]
Description=S3 Log Sync Service
After=network.target

[Service]
Type=simple
User=ec2-user
ExecStart=/bin/bash /home/ec2-user/log_sync.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# -------------------------------------------------------------
# 9. ENABLE SERVICES
# -------------------------------------------------------------
systemctl daemon-reload


systemctl enable fyers-docker-logs.service
systemctl enable fyers-log-sync.service


systemctl restart fyers-docker-logs.service
systemctl restart fyers-log-sync.service

echo "================================================"
echo "  INSTALLATION COMPLETE (FIXED & PRODUCTION READY)"
echo "================================================"
echo "✔ Docker running in background"
echo "✔ Logs captured from container stdout"
echo "✔ Logs stored in /var/log/fyers_insidebar.log"
echo "✔ S3 sync every 5 minutes"
echo "✔ systemd auto-recovery enabled"
echo "================================================"