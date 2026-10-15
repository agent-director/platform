#!/bin/bash
# Local-only destruction: never uninstall a provider/release or delete a namespace.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
run() { mise exec -- "$@"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    printf '%s\n' 'Usage: scripts/teardown.sh [explicit-owned-kind-cluster-name]' \
        'Only clusters successfully created by this worktree lifecycle may be destroyed.'
    exit 0
fi
CLUSTER_NAME=${1:-${CLUSTER_NAME:-platform-dev}}
[[ -n "$CLUSTER_NAME" ]] || fail 'Provide an explicit owned kind cluster name.'
[[ "$CLUSTER_NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#CLUSTER_NAME} -le 63 ]] || fail 'Invalid cluster name.'
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-$CLUSTER_NAME}
export KUBECONFIG="$ROOT/.cache/clusters/$CLUSTER_NAME/kubeconfig"
[[ "$KUBE_CONTEXT" == "kind-$CLUSTER_NAME" ]] || fail 'CLUSTER_NAME and KUBE_CONTEXT disagree.'
MARKER="$ROOT/.cache/kind/$CLUSTER_NAME.json"
[[ -f "$MARKER" && ! -L "$MARKER" ]] || fail "No creation ownership marker for $CLUSTER_NAME; refusing deletion."
run jq -e --arg name "$CLUSTER_NAME" --arg context "$KUBE_CONTEXT" --arg root "$ROOT" --arg kubeconfig "$KUBECONFIG" \
    '.created == true and .name == $name and .context == $context and .root == $root and .kubeconfig == $kubeconfig and (.uid | type == "string" and length > 0) and (.server | type == "string" and length > 0)' \
    "$MARKER" > /dev/null || fail 'Invalid ownership marker; refusing deletion.'

EXISTS=false
CLUSTERS=$(run kind get clusters)
while IFS= read -r name; do
    if [[ "$name" == "$CLUSTER_NAME" ]]; then EXISTS=true; fi
done <<< "$CLUSTERS"
[[ "$EXISTS" == true ]] || fail 'The selected kind backend has no matching cluster; retaining ownership evidence rather than assuming another backend cluster is gone.'
mkdir -p "$ROOT/.cache/clusters/$CLUSTER_NAME"
VERIFY_CONFIG=$(umask 077; mktemp "$ROOT/.cache/clusters/$CLUSTER_NAME/verify.XXXXXXXX")
trap 'rm -f "$VERIFY_CONFIG"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
run kind get kubeconfig --name "$CLUSTER_NAME" > "$VERIFY_CONFIG"
UID_ACTUAL=$(run kubectl --kubeconfig "$VERIFY_CONFIG" --context "$KUBE_CONTEXT" --request-timeout=15s get namespace kube-system -o 'jsonpath={.metadata.uid}')
SERVER_ACTUAL=$(run kubectl --kubeconfig "$VERIFY_CONFIG" --context "$KUBE_CONTEXT" config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
run jq -e --arg uid "$UID_ACTUAL" --arg server "$SERVER_ACTUAL" \
    '.uid == $uid and .server == $server' "$MARKER" > /dev/null || fail 'Current kind backend cluster identity differs from the creation marker; refusing deletion.'
run kind delete cluster --name "$CLUSTER_NAME" --kubeconfig "$KUBECONFIG"
rm -f "$MARKER" "$ROOT/.cache/kind/$CLUSTER_NAME-config.yaml" "$KUBECONFIG"
printf 'Owned kind cluster %s is absent. No Helm releases or namespaces were uninstalled.\n' "$CLUSTER_NAME"
