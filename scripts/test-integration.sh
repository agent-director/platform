#!/bin/bash
set -e

KIND_CLUSTER_NAME=${1:-e2e-cluster}
NAMESPACE="platform"

echo "=== Building all images concurrently (Dynamic Discovery) ==="
pids=()
IMAGES_TO_LOAD=()

for dockerfile in $(find images -name Dockerfile); do
    DIR_NAME=$(basename $(dirname $dockerfile))

    # Identify multi-stage exports (ignoring common build stages)
    TARGETS=$(grep -iE "^FROM .* AS " $dockerfile | awk '{print $NF}' | grep -viE "^(builder|deps|runner|base)$" || true)

    if [ -n "$TARGETS" ]; then
        for target in $TARGETS; do
            IMG_GHCR="ghcr.io/agent-director/${target}:local"
            IMG_LOCAL="${target}:local"

            echo "Building target '$target' from $DIR_NAME..."
            docker buildx build --load -t "$IMG_GHCR" -t "$IMG_LOCAL" --target "$target" -f "$dockerfile" . &
            pids+=($!)
            IMAGES_TO_LOAD+=("$IMG_GHCR" "$IMG_LOCAL")
        done
    else
        IMG_GHCR="ghcr.io/agent-director/${DIR_NAME}:local"
        IMG_LOCAL="${DIR_NAME}:local"

        echo "Building $DIR_NAME..."
        docker buildx build --load -t "$IMG_GHCR" -t "$IMG_LOCAL" -f "$dockerfile" . &
        pids+=($!)
        IMAGES_TO_LOAD+=("$IMG_GHCR" "$IMG_LOCAL")
    fi
done

# Wait for all builds to finish
fail=0
for pid in "${pids[@]}"; do
    wait $pid || let "fail+=1"
done

if [ "$fail" -gt 0 ]; then
    echo "ERROR: $fail image builds failed."
    exit 1
fi

echo "=== Loading all compiled artifacts into Kind cluster ($KIND_CLUSTER_NAME) ==="
# Speedup: Load all images in a single API call to avoid redundant layer hashing
kind load docker-image "${IMAGES_TO_LOAD[@]}" --name $KIND_CLUSTER_NAME

echo "=== Deploying Platform and Substrate ==="
export IMAGE_TAG="local"

DEPLOY_HELM_ARGS="--set caddyTailscale.enabled=false --set sandboxedContainers.enabled=false"
if [ "$LIGHTWEIGHT_CI" = "true" ]; then
    echo "=== Running Lightweight Temporal Dev Server ==="
    docker run -d --name temporal-dev --network kind -p 7233:7233 temporalio/admin-tools:latest temporal server start-dev
    DEPLOY_HELM_ARGS="$DEPLOY_HELM_ARGS --set temporal.enabled=false"
fi

make deploy HELM_ARGS="$DEPLOY_HELM_ARGS" HELM_ARGS_SUBSTRATE="--set ateapi.image.pullPolicy=IfNotPresent --set atecontroller.image.pullPolicy=IfNotPresent"
echo "=== Waiting for all pods to be Ready ==="
# Ensure all pods are running as expected BEFORE attempting any smoke tests
kubectl wait --for=condition=Ready pods --all -n $NAMESPACE --timeout=600s
kubectl wait --for=condition=Ready pods --all -n agent-substrate --timeout=600s

echo "=== Running E2E Smoke Tests ==="
make test-e2e

if [ "$LIGHTWEIGHT_CI" = "true" ]; then
    echo "=== Cleaning up Temporal Dev Server ==="
    docker rm -f temporal-dev || true
fi
echo "=== Integration Test Suite Passed Successfully ==="
