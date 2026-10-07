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
docker run --rm -v trivy-cache:/root/.cache/trivy -v "$(pwd):/workspace" -w /workspace aquasec/trivy:latest image -c "" --download-db-only >/dev/null


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

if [ "$CI" == "true" ]; then
  echo "=========================================================="
  echo "      STARTING SEQUENTIAL SCANS FOR ${#COMPONENTS[@]} IMAGES (CI Mode)"
  echo "=========================================================="
else
echo "=========================================================="
echo "      STARTING PARALLEL SCANS FOR ${#COMPONENTS[@]} IMAGES"
echo "=========================================================="
fi

for ctx in "${COMPONENTS[@]}"; do
  img=$(basename "$ctx")
  # Run in background now that DB updates are skipped (no bbolt lock!)
  (
    ./scripts/local-container-scan.sh "$ctx" > ".local-scans/$img.log" 2>&1
    echo $? > ".local-scans/$img.status"
  ) &
  if [ "$CI" == "true" ]; then
    wait
  fi
done

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
    if grep -q "Report Summary" ".local-scans/$img.log" 2>/dev/null; then
      printf "❌ %-30s FAILED (Vulnerabilities Found)\n" "$img"
      echo "   --- Vulnerability Summary for $img ---"
      awk '/Report Summary/,0' ".local-scans/$img.log" | sed 's/^/   /'
    else
      printf "🚨 %-30s ERROR (Build or Execution Crash)\n" "$img"
      echo "   --- Last 15 lines of Error Log for $img ---"
      tail -n 15 ".local-scans/$img.log" | sed 's/^/   /'
    fi
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
