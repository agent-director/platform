#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [ "${PLATFORM_MISE_ACTIVE:-0}" != "1" ] || [ "${PLATFORM_ROOT:-}" != "$ROOT" ]; then
  exec mise exec -- "$ROOT/scripts/local-container-scan.sh" "$@"
fi
cd "$ROOT"

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <image-name|images/single-export-component>"
  exit 2
fi
SELECTION=${1#"$ROOT/"}
SELECTION=${SELECTION%/}
if [[ ! "$SELECTION" =~ ^(images/)?[a-z0-9][a-z0-9._-]*$ ]]; then
  echo "Error: Expected a repository image name or component path: $1" >&2
  exit 2
fi
INVENTORY=$(uv run --no-project scripts/images.py --format tsv)
MATCHES=0
IMAGE_COMPONENT="" DOCKERFILE="" TARGET=""
while IFS=$'\t' read -r name dockerfile target extra; do
  if [ -z "$name" ]; then continue; fi
  if [[ ! "$name" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || [[ ! "$dockerfile" =~ ^images/[a-z0-9][a-z0-9._-]*/Dockerfile$ ]] || [ -n "$extra" ]; then
    echo "Error: Invalid image inventory row." >&2
    exit 1
  fi
  if [ "$name" = "$SELECTION" ] || [ "${dockerfile%/Dockerfile}" = "$SELECTION" ]; then
    IMAGE_COMPONENT=$name DOCKERFILE=$dockerfile TARGET=$target
    MATCHES=$((MATCHES + 1))
  fi
done <<< "$INVENTORY"
if [ "$MATCHES" -ne 1 ] || [ ! -f "$DOCKERFILE" ]; then
  echo "Error: Selection must identify one image export; use its inventory name (e.g. ateapi or atecontroller)." >&2
  exit 2
fi
if [ -n "$TARGET" ] && [[ ! "$TARGET" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
  echo "Error: Invalid Docker export target: $TARGET" >&2
  exit 1
fi

mkdir -p "$ROOT/.cache/local-scans" "$ROOT/.cache/trivy"
RUN_DIR=$(mktemp -d "$ROOT/.cache/local-scans/$IMAGE_COMPONENT.XXXXXXXX")
RUN_ID=$(basename "$RUN_DIR")
IMAGE_NAME="local-scan:$RUN_ID"
COMMAND_PID=""

inspect_owner() {
  local resource=$1 error_log="$RUN_DIR/inspect-$1.log" owner
  if owner=$(docker image inspect --format '{{ index .Config.Labels "org.agent-director.local-scan" }}' "$resource" 2> "$error_log"); then
    printf '%s\n' "$owner"
    return 0
  fi
  if grep -Eq '[Nn]o such (object|container|image):' "$error_log"; then
    return 0
  fi
  echo "Cannot verify image cleanup ownership for $resource; see $error_log" >&2
  return 1
}

cleanup() {
  local result=$? owner
  trap - EXIT
  if [ -n "$COMMAND_PID" ]; then
    kill -TERM "$COMMAND_PID" 2>/dev/null || true
    wait "$COMMAND_PID" 2>/dev/null || true
  fi
  if ! owner=$(inspect_owner "$IMAGE_NAME"); then
    result=1
  elif [ "$owner" = "$RUN_DIR" ]; then
    if ! docker image rm "$IMAGE_NAME" >/dev/null 2>&1; then
      echo "Failed to remove owned scan image: $IMAGE_NAME" >&2
      result=1
    fi
  fi
  printf '%s\n' "$result" > "$RUN_DIR/status"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
exec > >(tee "$RUN_DIR/scan.log") 2>&1

BUILD_ARGS=()
if [ -n "$TARGET" ]; then BUILD_ARGS+=(--target "$TARGET"); fi
echo "Building and scanning $IMAGE_COMPONENT ($DOCKERFILE, target=${TARGET:-default}); artifacts: $RUN_DIR"
docker buildx build -f "$DOCKERFILE" --load \
  --label "org.agent-director.local-scan=$RUN_DIR" \
  -t "$IMAGE_NAME" ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} . &
COMMAND_PID=$!
BUILD_EXIT=0
if wait "$COMMAND_PID"; then :; else BUILD_EXIT=$?; fi
COMMAND_PID=""
if [ "$BUILD_EXIT" -ne 0 ]; then exit "$BUILD_EXIT"; fi

run_scan() {
  shift
  trivy image --config "$ROOT/trivy.yaml" --cache-dir "$ROOT/.cache/trivy" \
    --scanners vuln,secret "$@" "$IMAGE_NAME" &
  COMMAND_PID=$!
  local result=0
  if wait "$COMMAND_PID"; then :; else result=$?; fi
  COMMAND_PID=""
  return "$result"
}

APP_EXIT=0
OS_WARN_EXIT=0
OS_EXIT=0
if run_scan app --pkg-types library --severity HIGH,CRITICAL --exit-code 1; then :; else APP_EXIT=$?; fi
if run_scan os-warning --pkg-types os --severity HIGH --exit-code 0; then :; else OS_WARN_EXIT=$?; fi
if run_scan os --pkg-types os --severity CRITICAL --exit-code 1; then :; else OS_EXIT=$?; fi

if [ "$APP_EXIT" -ne 0 ] || [ "$OS_WARN_EXIT" -ne 0 ] || [ "$OS_EXIT" -ne 0 ]; then
  echo "Scan failed (app=$APP_EXIT, OS warning execution=$OS_WARN_EXIT, OS=$OS_EXIT)." >&2
  exit 1
fi
echo "Image passed the enforced library HIGH/CRITICAL and OS CRITICAL gates."
