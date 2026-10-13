#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Direct invocations get the same tool/build-input selection as Make and CI.
# Activation is valid only for this checkout, not an inherited foreign worktree.
if [ "${PLATFORM_MISE_ACTIVE:-0}" != "1" ] || [ "${PLATFORM_ROOT:-}" != "$ROOT" ]; then
  exec "$ROOT/scripts/mise.sh" exec -- "$ROOT/scripts/local-container-scan.sh" "$@"
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
# Capture first: a failing inventory helper must not be hidden by a read loop.
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
  local kind=$1 resource=$2 error_log="$RUN_DIR/inspect-$2.log" owner
  if [ "$kind" = "image" ]; then
    if owner=$(docker image inspect --format '{{ index .Config.Labels "org.agent-director.local-scan" }}' "$resource" 2> "$error_log"); then
      printf '%s\n' "$owner"
      return 0
    fi
  else
    if owner=$(docker inspect --format '{{ index .Config.Labels "org.agent-director.local-scan" }}' "$resource" 2> "$error_log"); then
      printf '%s\n' "$owner"
      return 0
    fi
  fi
  if grep -Eq '[Nn]o such (object|container|image):' "$error_log"; then
    return 0  # Confirmed absent; --rm scanners normally reach this state.
  fi
  echo "Cannot verify $kind cleanup ownership for $resource; see $error_log" >&2
  return 1
}

remove_owned_containers() {
  local owner container step result=0
  for step in app os-warning os; do
    container="trivy-$RUN_ID-$step"
    if ! owner=$(inspect_owner container "$container"); then
      result=1
      continue
    fi
    if [ "$owner" = "$RUN_DIR" ]; then
      if ! docker rm -f "$container" >/dev/null 2>&1; then
        echo "Failed to remove owned scan container: $container" >&2
        result=1
      fi
    fi
  done
  return "$result"
}

cleanup() {
  local result=$? owner
  trap - EXIT
  if [ -n "$COMMAND_PID" ]; then
    kill -TERM "$COMMAND_PID" 2>/dev/null || true
    # Stop an existing scanner container so its attached client can exit.
    if ! remove_owned_containers; then result=1; fi
    wait "$COMMAND_PID" 2>/dev/null || true
  fi
  # Re-inspect only after the creating command has stopped: it may have loaded
  # the image or created its container during its TERM handler/shutdown.
  if ! remove_owned_containers; then result=1; fi
  if ! owner=$(inspect_owner image "$IMAGE_NAME"); then
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

TRIVY_IMAGE=$(uv run --no-project scripts/toolchain.py image trivy)
BUILD_INPUTS=$(uv run --no-project scripts/toolchain.py build-args --format docker)
BUILD_ARGS=()
while IFS= read -r argument; do
  if [ -n "$argument" ]; then BUILD_ARGS+=("$argument"); fi
done <<< "$BUILD_INPUTS"
if [ -z "$BUILD_INPUTS" ]; then
  echo "Error: No root toolchain build inputs returned." >&2
  exit 1
fi

if [ -n "$TARGET" ]; then BUILD_ARGS+=(--target "$TARGET"); fi
echo "Building and scanning $IMAGE_COMPONENT ($DOCKERFILE, target=${TARGET:-default}); artifacts: $RUN_DIR"
docker buildx build -f "$DOCKERFILE" --load \
  --label "org.agent-director.local-scan=$RUN_DIR" \
  -t "$IMAGE_NAME" "${BUILD_ARGS[@]}" . &
COMMAND_PID=$!
BUILD_EXIT=0
if wait "$COMMAND_PID"; then :; else BUILD_EXIT=$?; fi
COMMAND_PID=""
if [ "$BUILD_EXIT" -ne 0 ]; then exit "$BUILD_EXIT"; fi

run_scan() {
  local step=$1
  shift
  local container="trivy-$RUN_ID-$step"
  # The IaC trivy.yaml MUST NOT supply image thresholds or disable DB updates.
  # This explicit image policy matches the CI app/OS split and scans secrets.
  docker run --rm --name "$container" \
    --label "org.agent-director.local-scan=$RUN_DIR" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$ROOT/.cache/trivy:/root/.cache/trivy" \
    "$TRIVY_IMAGE" image --config "" --cache-dir /root/.cache/trivy \
    --scanners vuln,secret "$@" "$IMAGE_NAME" &
  COMMAND_PID=$!
  local result=0
  if wait "$COMMAND_PID"; then :; else result=$?; fi
  COMMAND_PID=""
  return "$result"
}

# Conditionals deliberately capture failures even under set -e. Every pass runs
# and EXIT always removes only images/containers bearing this invocation's label.
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
