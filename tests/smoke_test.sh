#!/bin/bash
set -euo pipefail

echo "Running API HTTP smoke test..."
NAMESPACE="${NAMESPACE:-platform}"

# Start port forward to unified-api
kubectl port-forward svc/unified-api 8000:8000 -n "$NAMESPACE" &
PF_PID=$!

# Ensure we cleanup
trap 'kill $PF_PID || true' EXIT

# Wait for port to be ready
echo "Waiting for port-forward to establish..."
for i in {1..15}; do
  if curl -s http://localhost:8000/health > /dev/null; then
    echo "API is up!"
    break
  fi
  sleep 1
done

# Perform actual test
echo "Testing /health endpoint..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health)
if [ "$STATUS" -ne 200 ]; then
  echo "Error: API returned HTTP $STATUS"
  exit 1
fi

echo "API HTTP smoke test passed successfully."
