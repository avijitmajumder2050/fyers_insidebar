#!/bin/bash
set -e

LOGSCANNER=/var/log/trading-app-mtf-mstock.log
LOG=/var/log/trading-app-mtf-mstock-bootstrap.log
exec > >(tee -a $LOG) 2>&1

echo "🚀 Bootstrapping Trading app mstock MTF EC2"

REGION="ap-south-1"
SSM_REPO_PARAM="/trading-app-mtf/github_repo"
SSM_MOMENTUM_REPO="/momentum-watchlist/github_repo"
APP_USER="ec2-user"
APP_HOME="/home/ec2-user"
RUN_DOCKER_APP=true
S3_BUCKET="s3://dhan-trading-data"
S3_PREFIX="trading-bot"
# FYERS INSIDEBAR
APP_DIR="/home/ec2-user/fyers_insidebar"
BACKEND_DIR="/home/ec2-user/fyers_insidebar/backend"

SSM_REPO_URL="https://github.com/avijitmajumder2050/fyers_insidebar.git"

IMAGE_NAME="fyers-insidebar"
CONTAINER_NAME="insidebar-strategy"

LOG_FILE="/var/log/fyers_insidebar.log"
S3_BUCKET_FYERS="s3://dhan-trading-data/trading-bot/logs"

# -----------------------------
# System update & deps
# -----------------------------
sudo yum update -y
sudo timedatectl set-timezone Asia/Kolkata
sudo yum install -y git python3.11 python3.11-pip python3.11-devel awscli docker
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $APP_USER
echo "✅ Installed Python 3.11 + Docker"

# -----------------------------
# Attach Elastic IP
# -----------------------------
EIP_ALLOCATION_ID="eipalloc-098e0e7a5bcfe7bfe"

echo "🔐 Fetching IMDSv2 token..."

IMDS_TOKEN=$(curl -s -X PUT \
  "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

INSTANCE_ID=$(curl -s \
  -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-id)

echo "🔗 Attaching Elastic IP to instance: $INSTANCE_ID"

for i in {1..5}; do
  aws ec2 associate-address \
    --instance-id $INSTANCE_ID \
    --allocation-id $EIP_ALLOCATION_ID \
    --allow-reassociation \
    --region $REGION && break
  echo "Retrying EIP attach..."
  sleep 5
done

echo "✅ Elastic IP attached (or already associated)"
sleep 10

/usr/bin/python3.11 --version
python3 --version

# -----------------------------
# Safe python aliases
# -----------------------------
BASHRC="$APP_HOME/.bashrc"
grep -q "alias python=python3.11" "$BASHRC" || echo "alias python=python3.11" >> "$BASHRC"
grep -q "alias pip=pip3.11" "$BASHRC" || echo "alias pip=pip3.11" >> "$BASHRC"

# -----------------------------
# Get repo URL from SSM
# -----------------------------
REPO_URL=$(aws ssm get-parameter \
  --name "$SSM_REPO_PARAM" \
  --region "$REGION" \
  --query "Parameter.Value" \
  --output text)

cd "$APP_HOME"

# -----------------------------
# Clone repo
# -----------------------------
REPO_NAME=$(basename "$REPO_URL" .git)
if [ ! -d "$REPO_NAME" ]; then
  git clone "$REPO_URL"
  chown -R $APP_USER:$APP_USER $APP_HOME/$REPO_NAME
fi

cd "$REPO_NAME"

# -----------------------------
# Python venv
# -----------------------------
if [ ! -d "venv" ]; then
  /usr/bin/python3.11 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
[ -f requirements.txt ] && pip install -r requirements.txt

# -----------------------------
# Runtime dirs
# -----------------------------
mkdir -p logs outputs
chmod -R 755 logs outputs
chown -R $APP_USER:$APP_USER logs outputs

# -----------------------------
# log file setup
# -----------------------------
sudo touch $LOGSCANNER
sudo chown $APP_USER:$APP_USER $LOGSCANNER
sudo chmod 664 $LOGSCANNER

export PYTHONPATH=$PWD

# -----------------------------
# S3 log uploader
# -----------------------------
sudo tee /usr/local/bin/upload-trading-app-mtf-log.sh > /dev/null <<EOF
#!/bin/bash
aws s3 cp $LOGSCANNER \
  $S3_BUCKET/$S3_PREFIX/logs/trading-app-mtf-mstock.log \
  --region $REGION || true
EOF
sudo chmod +x /usr/local/bin/upload-trading-app-mtf-log.sh

# -----------------------------
# systemd uploader
# -----------------------------
sudo tee /etc/systemd/system/trading-app-mtf-log-upload.service > /dev/null <<EOF
[Unit]
Description=Upload trading log

[Service]
Type=oneshot
ExecStart=/usr/local/bin/upload-trading-app-mtf-log.sh
EOF

sudo tee /etc/systemd/system/trading-app-mtf-log-upload.timer > /dev/null <<EOF
[Unit]
Description=Upload log every 5 min

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# -----------------------------
# Trading service
# -----------------------------
sudo tee /etc/systemd/system/trading-app-mtf.service > /dev/null <<EOF
[Unit]
Description=Trading app scanner Service
After=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_HOME/$REPO_NAME
Environment=PYTHONPATH=$APP_HOME/$REPO_NAME
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/bin/sleep 15
ExecStart=$APP_HOME/$REPO_NAME/venv/bin/python app/main.py
Restart=always
RestartSec=10
StandardOutput=append:$LOGSCANNER
StandardError=append:$LOGSCANNER
ExecStopPost=/usr/local/bin/upload-trading-app-mtf-log.sh

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable trading-app-mtf
sudo systemctl enable --now trading-app-mtf-log-upload.timer
sudo systemctl restart trading-app-mtf

echo "✅ Trading Bot scanner started"


# ============================================================
# 🟢 PART 2: MOMENTUM DOCKER APP
# ============================================================

if [ "$RUN_DOCKER_APP" = true ]; then

  echo "=== Setup Momentum Docker App ==="

  cd "$APP_HOME"

  MOMENTUM_REPO=$(aws ssm get-parameter \
    --name "$SSM_MOMENTUM_REPO" \
    --region "$REGION" \
    --query "Parameter.Value" \
    --output text)

  MOMENTUM_NAME=$(basename "$MOMENTUM_REPO" .git)

  [ ! -d "$MOMENTUM_NAME" ] && git clone "$MOMENTUM_REPO"

  cd "$MOMENTUM_NAME"

  echo "🔨 Building Docker image..."
  sudo docker build -t momentum-watchlist:latest .

  # -----------------------------
  # Install docker-compose if missing
  # -----------------------------
  if ! command -v docker-compose &> /dev/null && ! sudo docker compose version &> /dev/null; then
    echo "⚠️ Installing docker-compose..."
    sudo curl -L https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
      -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
  fi

  # -----------------------------
  # Move to docker folder
  # -----------------------------
  cd docker

  # -----------------------------
  # Ensure runtime.env
  # -----------------------------
  if [ ! -f runtime.env ]; then
    echo "⚠️ runtime.env not found → creating default"
    cat <<EOF > runtime.env
PORT=5000
ENV=prod
EOF
  fi

  # -----------------------------
  # Cleanup old container
  # -----------------------------
  echo "🧹 Cleaning old container..."
  sudo docker rm -f momentum-watchlist 2>/dev/null || true

  # -----------------------------
  # Start container
  # -----------------------------
  echo "🚀 Starting container..."

  if sudo docker compose version &> /dev/null; then
    sudo docker compose up -d
  elif command -v docker-compose &> /dev/null; then
    sudo docker-compose up -d
  else
    echo "⚠️ Compose not available → fallback"
    sudo docker run -d \
      --restart unless-stopped \
      -p 5000:5000 \
      --name momentum-watchlist \
      momentum-watchlist:latest
  fi

  sleep 5
  sudo docker ps

  # -----------------------------
  # Health check
  # -----------------------------
  echo "🔍 Health check..."

  for i in {1..6}; do
    STATUS=$(curl -sf http://localhost:5000/health || echo "fail")
    if echo "$STATUS" | grep -q "ok"; then
      echo "✅ Momentum app healthy"
      break
    fi
    echo "Retry $i..."
    sleep 5
  done

  echo "📜 Container logs:"
  sudo docker logs  momentum-api --tail 20 || true

fi
sudo tee /usr/local/bin/momentum-watchdog.sh > /dev/null <<'EOF'
#!/bin/bash

while true; do
  STATUS=$(docker inspect -f '{{.State.Health.Status}}' momentum-api 2>/dev/null || echo "missing")

  echo "$(date) - STATUS: $STATUS"

  if [ "$STATUS" = "unhealthy" ] || [ "$STATUS" = "missing" ]; then
    echo "Restarting momentum-api..."
    docker restart momentum-api
  fi

  sleep 20
done
EOF

sudo chmod +x /usr/local/bin/momentum-watchdog.sh
# ============================================================
# 🟢 WATCHDOG (SYSTEMD AUTO-HEALING - IMPORTANT PART)
# ============================================================

sudo tee /etc/systemd/system/momentum-watchdog.service > /dev/null <<EOF
[Unit]
Description=Momentum Health Watchdog
After=docker.service
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=5

ExecStart=/usr/local/bin/momentum-watchdog.sh

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable momentum-watchdog
sudo systemctl restart momentum-watchdog

echo "✅ Watchdog enabled"

# =============================================================
# FYERS InsideBar FULL PRODUCTION INSTALLER (FIXED LOGGING)
# Docker + systemd + S3 logs (FIXED)
# =============================================================

echo "================================================"
echo "  FYERS InsideBar INSTALLER (FIXED LOG SYSTEM)"
echo "================================================"

# -------------------------------------------------------------
# CLONE REPO
# -------------------------------------------------------------
rm -rf "$APP_DIR"
git clone "$SSM_REPO_URL" "$APP_DIR"

echo "✅ Repo cloned"

# -------------------------------------------------------------
# BUILD DOCKER IMAGE
# -------------------------------------------------------------
cd "$BACKEND_DIR"

echo "Building Docker image..."

docker build --no-cache -t "$IMAGE_NAME" .

echo "✅ Docker image built"

# -------------------------------------------------------------
# RUN CONTAINER
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
# LOG CAPTURE FROM DOCKER
# -------------------------------------------------------------

mkdir -p /var/log
touch "$LOG_FILE"

chown ec2-user:ec2-user "$LOG_FILE"
chmod 664 "$LOG_FILE"

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
# SYSTEMD: LOG CAPTURE SERVICE
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
# S3 LOG SYNC SERVICE
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
# ENABLE SERVICES
# -------------------------------------------------------------
systemctl daemon-reload

systemctl enable fyers-docker-logs.service
systemctl enable fyers-log-sync.service

systemctl restart fyers-docker-logs.service
systemctl restart fyers-log-sync.service

echo "================================================"
echo "  FYERS INSTALLATION COMPLETE"
echo "================================================"

# ============================================================
# FINAL OUTPUT
# ============================================================

PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)

echo ""
echo "======================================"
echo "🚀 SYSTEM READY"
echo "Trading App  : running (systemd)"
echo "Momentum App : http://${PUBLIC_IP}:5000"
echo "======================================"