#!/usr/bin/env bash
set -e

# Support running a single image scan directly
if [ -n "$1" ] && [ "$1" != "all" ]; then
  IMAGE_PATH="images/$1"
  if [ ! -d "$IMAGE_PATH" ] || [ ! -f "$IMAGE_PATH/Dockerfile" ]; then
    echo "Error: Image $1 not found or missing Dockerfile at $IMAGE_PATH/Dockerfile"
    exit 1
  fi
  ./scripts/local-container-scan.sh "$IMAGE_PATH"
  exit $?
fi

echo "=========================================================="
echo "      UPDATING TRIVY DATABASE CACHE (SEQUENTIAL)          "
echo "=========================================================="
# Prevent bbolt write locks by pulling the DB sequentially once
docker run --rm -v trivy-cache:/root/.cache/trivy aquasec/trivy:latest image --download-db-only >/dev/null

echo ""
set +e
./scripts/scan-base-images.sh
BASE_SCAN_EXIT=$?
set -e


if [ $BASE_SCAN_EXIT -ne 0 ]; then
  echo ""
  echo "⚠️ WARNING: Base OS images have vulnerabilities. These must be fixed upstream or base images swapped."
  echo "Continuing to scan application-level dependencies..."
  echo ""
fi

# Strict discovery: Match CI exactly by finding all actual Dockerfiles
COMPONENTS=()
for df in images/*/Dockerfile; do
  if [ -f "$df" ]; then
    COMPONENTS+=("$(dirname "$df")")
  fi
done

if [ ${#COMPONENTS[@]} -eq 0 ]; then
  echo "No images found to scan."
  exit 0
fi

mkdir -p .local-scans
rm -f .local-scans/*.log .local-scans/*.status

echo "=========================================================="
echo "      STARTING PARALLEL SCANS FOR ${#COMPONENTS[@]} IMAGES"
echo "=========================================================="

for ctx in "${COMPONENTS[@]}"; do
  img=$(basename "$ctx")
  # Run the scan in the background, redirecting output to isolate logs
  (
    ./scripts/local-container-scan.sh "$ctx" > ".local-scans/$img.log" 2>&1
    echo $? > ".local-scans/$img.status"
  ) &
done

# Wait for ALL background processes to finish (does not fail fast)
wait

echo ""
echo "=========================================================="
echo "                 SCAN RESULTS SUMMARY                     "
echo "=========================================================="
FAILURES=0

for ctx in "${COMPONENTS[@]}"; do
  img=$(basename "$ctx")
  # Default to 1 (fail) if the status file somehow wasn't written
  STATUS=$(cat ".local-scans/$img.status" 2>/dev/null || echo "1")

  if [ "$STATUS" -eq 0 ]; then
    printf "✅ %-30s PASSED\n" "$img"
  else
    printf "❌ %-30s FAILED\n" "$img"
    echo "   --- App Vulnerability Summary for $img ---"
    # Print from 'Report Summary' or 'Total:' to EOF, skipping noise
    awk '/Report Summary/,0' ".local-scans/$img.log" | sed 's/^/   /'
    echo ""
    FAILURES=$((FAILURES + 1))
  fi
done
echo "=========================================================="

if [ "$FAILURES" -gt 0 ]; then
  echo "Pipeline failed: $FAILURES image(s) have vulnerabilities or secrets."
  exit 1
fi

echo "All images passed successfully."
exit 0
