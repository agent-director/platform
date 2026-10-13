#!/bin/bash
# Uses an explicitly selected existing cluster. Creation/deletion belong to the caller.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
MISE="$ROOT/scripts/mise.sh"
run() { "$MISE" exec -- "$@"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    printf '%s\n' 'Usage: scripts/test-integration.sh <existing-kind-cluster-name>' \
        'Environment: NAMESPACE, RELEASE_NAME, SUBSTRATE_NAMESPACE, SUBSTRATE_RELEASE_NAME, PROFILE=kind|full.' \
        'BUILD_IMAGES=false explicitly reuses already-built local artifacts; no cluster is created or destroyed.'
    exit 0
fi
CLUSTER_NAME=${1:-${CLUSTER_NAME:-}}
if [[ $# -gt 0 ]]; then shift; fi
[[ -n "$CLUSTER_NAME" ]] || fail 'Integration requires an explicit existing CLUSTER_NAME.'
[[ "$CLUSTER_NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || fail 'Invalid cluster name.'
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-$CLUSTER_NAME}
[[ "$KUBE_CONTEXT" == "kind-$CLUSTER_NAME" ]] || fail 'CLUSTER_NAME and KUBE_CONTEXT disagree.'
export CLUSTER_NAME KUBE_CONTEXT
export KUBECONFIG="$ROOT/.cache/clusters/$CLUSTER_NAME/kubeconfig"
export NAMESPACE=${NAMESPACE:-platform} RELEASE_NAME=${RELEASE_NAME:-platform}
export SUBSTRATE_NAMESPACE=${SUBSTRATE_NAMESPACE:-agent-substrate} SUBSTRATE_RELEASE_NAME=${SUBSTRATE_RELEASE_NAME:-agent-substrate}
export PROVIDER_NAMESPACE=${PROVIDER_NAMESPACE:-$NAMESPACE} PROVIDER_RELEASE_NAME=${PROVIDER_RELEASE_NAME:-postgres-operator}
export PROFILE=${PROFILE:-kind} IMAGE_TAG=${IMAGE_TAG:-local}
export CADDY_IMAGE=${CADDY_IMAGE:-ghcr.io/agent-director/caddy-tailscale:$IMAGE_TAG}

# Bash monitor mode gives each asynchronous job its own process group. The job
# table is ownership evidence; unrelated processes are never selected by name.
set -m
OWNED_PID=
OWNED_GROUP=
DISCOVERY_FILE=

stop_owned_group() {
    local group="$1" attempt alive=true
    if ! kill -0 -- "-$group" 2>/dev/null; then return 0; fi
    kill -TERM -- "-$group" 2>/dev/null || true
    for ((attempt=0; attempt<50; attempt++)); do
        if ! kill -0 -- "-$group" 2>/dev/null; then alive=false; break; fi
        sleep 0.1
    done
    if [[ "$alive" == true ]]; then
        kill -KILL -- "-$group" 2>/dev/null || true
        for ((attempt=0; attempt<50; attempt++)); do
            if ! kill -0 -- "-$group" 2>/dev/null; then alive=false; break; fi
            sleep 0.1
        done
    fi
    if [[ "$alive" == true ]]; then
        printf 'Owned process group %s did not exit after bounded cancellation.\n' "$group" >&2
        return 1
    fi
    wait "$group" 2>/dev/null || true
}

cleanup() {
    local status=$? group pgid
    trap - EXIT INT TERM
    # Retain the verified group even after its leader leaves Bash's job table.
    # The job table also covers a signal between spawn and PID registration.
    for group in $OWNED_GROUP $(jobs -p); do
        pgid=$(ps -o pgid= -p "$group" 2>/dev/null || true)
        pgid=${pgid//[[:space:]]/}
        if [[ -n "$pgid" && "$pgid" != "$group" ]]; then
            printf 'Refusing to signal non-isolated child group %s.\n' "$group" >&2
            status=1
            continue
        fi
        stop_owned_group "$group" || status=1
    done
    if [[ -n "$DISCOVERY_FILE" ]]; then rm -f "$DISCOVERY_FILE"; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_owned() {
    "$@" &
    OWNED_PID=$!
    OWNED_GROUP=$OWNED_PID
    local pgid status=0
    pgid=$(ps -o pgid= -p "$OWNED_PID" 2>/dev/null || true)
    pgid=${pgid//[[:space:]]/}
    [[ -z "$pgid" || "$pgid" == "$OWNED_GROUP" ]] || fail 'Operational child was not isolated in its own group.'
    wait "$OWNED_PID" || status=$?
    # A crashed/hard-exiting pytest may leave forwards after uv exits. Close the
    # remaining group before discarding ownership, on failure AND success.
    stop_owned_group "$OWNED_GROUP" || status=1
    OWNED_PID=
    OWNED_GROUP=
    return "$status"
}

run_owned "$ROOT/scripts/deploy.sh" check-cluster
mkdir -p "$ROOT/.cache/integration"
DISCOVERY_FILE=$(mktemp "$ROOT/.cache/integration/clusters.XXXXXXXX")
run_owned "$MISE" exec -- kind get clusters > "$DISCOVERY_FILE"
EXISTING=$(< "$DISCOVERY_FILE")
FOUND=false
while IFS= read -r name; do
    if [[ "$name" == "$CLUSTER_NAME" ]]; then FOUND=true; fi
done <<< "$EXISTING"
[[ "$FOUND" == true ]] || fail "Kind cluster $CLUSTER_NAME does not exist; the caller must create it explicitly."

BUILD_ARGS_TEXT=$(run uv run --no-project scripts/toolchain.py build-args --format docker)
BUILD_ARGS=()
while IFS= read -r argument; do
    [[ -z "$argument" ]] || BUILD_ARGS+=("$argument")
done <<< "$BUILD_ARGS_TEXT"
# Capture helper failures before iterating; process substitution would hide them.
IMAGE_DEFINITIONS=$(run uv run --no-project scripts/images.py --format tsv)
[[ -n "$IMAGE_DEFINITIONS" ]] || fail 'No exported images discovered.'
IMAGES_TO_LOAD=()
while IFS=$'\t' read -r name dockerfile target; do
    [[ -n "$name" && -f "$dockerfile" ]] || fail 'Malformed shared image definition.'
    image="ghcr.io/agent-director/$name:$IMAGE_TAG"
    local_image="$name:$IMAGE_TAG"
    IMAGES_TO_LOAD+=("$image" "$local_image")
    if [[ "${BUILD_IMAGES:-true}" == true ]]; then
        target_args=()
        if [[ -n "$target" ]]; then target_args=(--target "$target"); fi
        printf 'Building %s from %s.\n' "$image" "$dockerfile"
        run_owned "$MISE" exec -- docker buildx build --load ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} ${target_args[@]+"${target_args[@]}"} \
            -t "$image" -t "$local_image" -f "$dockerfile" .
    else
        run_owned "$MISE" exec -- docker image inspect "$image" "$local_image" > /dev/null
    fi
done <<< "$IMAGE_DEFINITIONS"
run_owned "$MISE" exec -- kind load docker-image "${IMAGES_TO_LOAD[@]}" --name "$CLUSTER_NAME"
run_owned "$ROOT/scripts/deploy.sh" deploy

# Each session fixture discovers chart-owned names/ports, binds random 127.0.0.1 ports,
# proves application readiness, and closes only its own PIDs on success/failure/signals.
run_owned "$MISE" exec -- uv run --locked pytest tests/integration/ -n 0 "$@"
printf 'Integration suite passed; selected cluster %s is retained.\n' "$CLUSTER_NAME"
