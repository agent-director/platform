#!/usr/bin/env bash
set -e

if [ -z "$1" ]; then
  echo "Usage: $0 <path-to-docker-context>"
  echo "Example: $0 images/unified-api"
  exit 1
fi

CONTEXT_DIR=$1
if [ ! -f "$CONTEXT_DIR/Dockerfile" ]; then
  echo "Error: No Dockerfile found in $CONTEXT_DIR"
  exit 1
fi

IMAGE_NAME="local-scan-$(basename "$CONTEXT_DIR")"

echo "=========================================================="
echo "🛡️ Starting Optimized Shift-Left Scan for $CONTEXT_DIR"
echo "=========================================================="

echo "[1/4] Building image to local daemon (using BuildKit cache)..."
# Build and load the image into the Docker daemon.
# This is MUCH faster on macOS than extracting rootfs because it avoids
# writing thousands of files across the VirtioFS mount to the host disk.
docker buildx build \
  -f "$CONTEXT_DIR/Dockerfile" \
  --load \
  -t "$IMAGE_NAME" \
  .

echo ""
echo "[2/4] 📦 APP DEPENDENCIES (Fail on HIGH & CRITICAL)..."
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v trivy-cache:/root/.cache/trivy \
  -v "$(pwd):/workspace" -w /workspace \
  aquasec/trivy:latest image \
  --pkg-types library \
  --severity HIGH,CRITICAL \
  --exit-code 1 \
  "$IMAGE_NAME"
APP_EXIT=$?

echo ""
echo "[3/4] ⚠️  OS DEPENDENCIES WARNING (Warn on HIGH)..."
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v trivy-cache:/root/.cache/trivy \
  -v "$(pwd):/workspace" -w /workspace \
  aquasec/trivy:latest image \
  --pkg-types os \
  --severity HIGH \
  --exit-code 0 \
  "$IMAGE_NAME"

echo ""
echo "[4/4] 🛑 OS DEPENDENCIES ENFORCEMENT (Fail on CRITICAL)..."
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v trivy-cache:/root/.cache/trivy \
  -v "$(pwd):/workspace" -w /workspace \
  aquasec/trivy:latest image \
  --pkg-types os \
  --skip-db-update \
  --skip-java-db-update \
  --severity CRITICAL \
  --exit-code 1 \
  "$IMAGE_NAME"
OS_EXIT=$?

if [ $APP_EXIT -ne 0 ] || [ $OS_EXIT -ne 0 ]; then
  TRIVY_EXIT_CODE=1
else
  TRIVY_EXIT_CODE=0
fi

echo ""
echo "[Cleanup] Removing temporary image tag..."
docker rmi "$IMAGE_NAME" >/dev/null 2>&1 || true

if [ $TRIVY_EXIT_CODE -eq 0 ]; then
  echo "✅ Scan completed successfully. No critical/high vulnerabilities found."
else
  echo "❌ Scan failed. Vulnerabilities or secrets detected."
fi

exit $TRIVY_EXIT_CODE
