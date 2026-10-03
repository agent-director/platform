#!/bin/bash
set -e

KIND_CLUSTER_NAME=${1:-e2e-cluster}
NAMESPACE="platform"

echo "=== Building all images concurrently ==="
pids=()
docker buildx build --load -t ghcr.io/agent-director/ateapi:local --target ateapi -f images/agent-substrate/Dockerfile . & pids+=($!)
docker buildx build --load -t ghcr.io/agent-director/atecontroller:local --target atecontroller -f images/agent-substrate/Dockerfile . & pids+=($!)
docker buildx build --load -t frontend:local -f images/frontend/Dockerfile . & pids+=($!)
docker buildx build --load -t unified-api:local -f images/unified-api/Dockerfile . & pids+=($!)
docker buildx build --load -t ghcr.io/agent-director/platform-worker:local -f images/platform-worker/Dockerfile . & pids+=($!)
docker buildx build --load -t ghcr.io/agent-director/mcp-server:local -f images/mcp-server/Dockerfile . & pids+=($!)

# Wait for all builds to finish
fail=0
for pid in "${pids[@]}"; do
    wait $pid || let "fail+=1"
done

if [ "$fail" -gt 0 ]; then
    echo "ERROR: $fail image builds failed."
    exit 1
fi

echo "=== Loading images into Kind cluster ($KIND_CLUSTER_NAME) ==="
# Speedup: Load all images in a single archive/call to avoid redundant layer hashing
kind load docker-image \
    ghcr.io/agent-director/ateapi:local \
    ghcr.io/agent-director/atecontroller:local \
    frontend:local \
    unified-api:local \
    ghcr.io/agent-director/platform-worker:local \
    ghcr.io/agent-director/mcp-server:local \
    --name $KIND_CLUSTER_NAME

echo "=== Deploying Platform and Substrate ==="
export IMAGE_TAG="local"
make deploy HELM_ARGS="--set tailscaleIngress.enabled=false --set sandboxedContainers.enabled=false --set global.image.tag=local"

echo "=== Waiting for all pods to be Ready ==="
# Ensure all pods are running as expected BEFORE attempting any smoke tests
kubectl wait --for=condition=Ready pods --all -n $NAMESPACE --timeout=600s
kubectl wait --for=condition=Ready pods --all -n agent-substrate --timeout=600s

echo "=== Running E2E Smoke Tests ==="
make test-e2e

echo "=== Integration Test Suite Passed Successfully ==="
