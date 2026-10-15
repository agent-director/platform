#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Activation is valid only for this checkout, not an inherited foreign worktree.
if [ "${PLATFORM_MISE_ACTIVE:-0}" != "1" ] || [ "${PLATFORM_ROOT:-}" != "$ROOT" ]; then
  exec mise exec -- "$ROOT/scripts/run-all-local-scans.sh" "$@"
fi
cd "$ROOT"

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [all|component]" >&2
  exit 2
fi
COMPONENTS=()
SELECTION=${1:-all}
if [[ ! "$SELECTION" =~ ^(images/)?[a-z0-9][a-z0-9._-]*$ ]]; then
  echo "Error: Invalid image selection: $SELECTION" >&2
  exit 2
fi
INVENTORY=$(uv run --no-project scripts/images.py --format tsv)
while IFS=$'\t' read -r name dockerfile target extra; do
  if [ -z "$name" ]; then continue; fi
  if [[ ! "$name" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || [[ ! "$dockerfile" =~ ^images/[a-z0-9][a-z0-9._-]*/Dockerfile$ ]] || [ -n "$extra" ]; then
    echo "Error: Invalid image inventory row." >&2
    exit 1
  fi
  component=${dockerfile#images/}
  component=${component%/Dockerfile}
  if [ "$SELECTION" = "all" ] || [ "$SELECTION" = "$name" ] || [ "$SELECTION" = "$component" ] || [ "$SELECTION" = "images/$component" ]; then
    COMPONENTS+=("$name")
  fi
done <<< "$INVENTORY"
if [ "${#COMPONENTS[@]}" -eq 0 ]; then
  echo "Error: No images found to scan" >&2
  exit 1
fi

# Scan actual inputs, not only committed/staged diffs. Clean CI checkouts must
# still scan images. Each invocation keeps its own logs/status files; never
# remove another invocation's output. Serial scans avoid Trivy cache writers.
mkdir -p "$ROOT/.cache/local-scans"
RUN_DIR=$(mktemp -d "$ROOT/.cache/local-scans/run.XXXXXXXX")
SCAN_PID=""
ACTIVE_COMPONENT=""
cleanup() {
  local result=$?
  trap - EXIT
  if [ -n "$SCAN_PID" ]; then
    kill -TERM "$SCAN_PID" 2>/dev/null || true
    wait "$SCAN_PID" 2>/dev/null || true
    if [ -n "$ACTIVE_COMPONENT" ]; then
      printf '%s\n' "$result" > "$RUN_DIR/$ACTIVE_COMPONENT.status"
    fi
  fi
  printf '%s\n' "$result" > "$RUN_DIR/run.status"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Scanning ${#COMPONENTS[@]} image(s) sequentially; logs: $RUN_DIR"
FAILURES=0
for component in "${COMPONENTS[@]}"; do
  ACTIVE_COMPONENT=$component
  "$ROOT/scripts/local-container-scan.sh" "$component" > "$RUN_DIR/$component.log" 2>&1 &
  SCAN_PID=$!
  status=0
  if wait "$SCAN_PID"; then :; else status=$?; fi
  SCAN_PID=""
  printf '%s\n' "$status" > "$RUN_DIR/$component.status"
  ACTIVE_COMPONENT=""
  if [ "$status" -eq 0 ]; then
    echo "$component PASSED"
  else
    echo "$component FAILED (exit $status); see $RUN_DIR/$component.log" >&2
    FAILURES=$((FAILURES + 1))
  fi
done

if [ "$FAILURES" -gt 0 ]; then
  echo "Pipeline failed: $FAILURES image(s) failed their enforcing scans." >&2
  exit 1
fi
echo "All images passed their enforcing scans."
