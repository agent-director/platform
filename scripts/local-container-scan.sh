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

echo "[1/2] Building image to local daemon (using BuildKit cache)..."
# Build and load the image into the Docker daemon.
# This is MUCH faster on macOS than extracting rootfs because it avoids
# writing thousands of files across the VirtioFS mount to the host disk.
docker buildx build \
  -f "$CONTEXT_DIR/Dockerfile" \
  --load \
  -t "$IMAGE_NAME" \
  .

echo ""
echo "[2/2] Running Trivy Image scan (with persistent DB cache)..."
# Run Trivy targeting the local daemon image
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v trivy-cache:/root/.cache/trivy \
  aquasec/trivy:latest image \
  --scanners vuln,secret \
  --pkg-types library \
  --severity HIGH,CRITICAL \
  --exit-code 1 \
  --ignore-unfixed \
  "$IMAGE_NAME"
TRIVY_EXIT_CODE=$?

echo ""
echo "[Cleanup] Removing temporary image tag..."
docker rmi "$IMAGE_NAME" >/dev/null 2>&1 || true

if [ $TRIVY_EXIT_CODE -eq 0 ]; then
  echo "✅ Scan completed successfully. No critical/high vulnerabilities found."
else
  echo "❌ Scan failed. Vulnerabilities or secrets detected."
fi

exit $TRIVY_EXIT_CODE
