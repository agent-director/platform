#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "         EXTRACTING & SCANNING BASE OS IMAGES             "
echo "=========================================================="

# 1. Find all FROM lines
# 2. Strip 'FROM ' and optional '--platform=...' flags
# 3. Print the next column (the image name)
# 4. Exclude 'scratch' (empty image)
# 5. Sort and get unique list
BASE_IMAGES=$(grep -hE "^FROM " images/*/Dockerfile | sed -E 's/^FROM[[:space:]]+(--platform=[^[:space:]]+[[:space:]]+)?//' | awk '{print $1}' | grep -viE "^scratch$" | sort -u)

if [ -z "$BASE_IMAGES" ]; then
  echo "No base images found."
  exit 0
fi

mkdir -p .local-scans
rm -f .local-scans/base-*.log .local-scans/base-*.status

echo "Found the following unique base images:"
echo "$BASE_IMAGES" | while read -r img; do echo "  - $img"; done
echo ""

FAILURES=0

for img in $BASE_IMAGES; do
  # Create a safe filename for the log
  safe_name=$(echo "$img" | tr ':/@' '___')

  echo "Scanning base image: $img"

  # Scan ONLY OS packages in the base image (exclude library/app packages)
  # This provides the de-duplicated OS vulnerability list.
  set +e
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v trivy-cache:/root/.cache/trivy \
    aquasec/trivy:latest image \
    --scanners vuln \
    --pkg-types os \
    --severity HIGH,CRITICAL \
    --exit-code 1 \
    --ignore-unfixed \
    "$img" > ".local-scans/base-${safe_name}.log" 2>&1

  STATUS=$?
  set -e

  if [ "$STATUS" -eq 0 ]; then
    printf "✅ %-50s PASSED\n" "$img"
  else
    printf "❌ %-50s FAILED (OS Vulns found!)\n" "$img"
    FAILURES=$((FAILURES + 1))

    # Extract the summary table from the Trivy output to show in console
    echo "   --- OS Vulnerability Summary for $img ---"
    awk '/Total: /,0' ".local-scans/base-${safe_name}.log" | sed 's/^/   /'
    echo ""
  fi
done

echo "=========================================================="
if [ "$FAILURES" -gt 0 ]; then
  echo "Base Image Scan Failed: $FAILURES base image(s) contain unpatched OS vulnerabilities."
  exit 1
else
  echo "All base OS images are clean!"
  exit 0
fi
