#!/bin/bash
# =============================================================
# FYERS InsideBar FULL PRODUCTION INSTALLER
# Docker + Local Logging
# =============================================================

set -euo pipefail

# -------------------------------------------------------------
# Bootstrap Log
# -------------------------------------------------------------
BOOTSTRAP_LOG="/var/log/fyers_insidebar_bootstrap.log"

mkdir -p /var/log
touch "$BOOTSTRAP_LOG"

exec > >(tee -a "$BOOTSTRAP_LOG")
exec 2>&1

APP_DIR="/home/ec2-user/fyers_insidebar"
BACKEND_DIR="/home/ec2-user/fyers_insidebar/backend"

REPO_URL="https://github.com/avijitmajumder2050/fyers_insidebar.git"

IMAGE_NAME="fyers-insidebar"
CONTAINER_NAME="insidebar-strategy"

echo "================================================"
echo " FYERS InsideBar INSTALLER"
echo " Started: $(date)"
echo "================================================"

# -------------------------------------------------------------
# INSTALL DEPENDENCIES
# -------------------------------------------------------------
yum update -y
yum install -y docker git awscli

systemctl enable docker
systemctl start docker

usermod -aG docker ec2-user || true

echo "✅ Docker ready"

# -------------------------------------------------------------
# CLONE REPO
# -------------------------------------------------------------
rm -rf "$APP_DIR"

git clone "$REPO_URL" "$APP_DIR"

echo "✅ Repo cloned"

# -------------------------------------------------------------
# BUILD IMAGE
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

sleep 10

docker ps

echo ""
echo "================================================"
echo " INSTALLATION COMPLETE"
echo "================================================"
echo "Bootstrap Log:"
echo "  /var/log/fyers_insidebar_bootstrap.log"
echo ""
echo "Container Logs:"
echo "  docker logs -f insidebar-strategy"
echo ""
echo "View Bootstrap Log:"
echo "  tail -f /var/log/fyers_insidebar_bootstrap.log"
echo "================================================"

echo "Completed: $(date)"