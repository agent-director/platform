#!/usr/bin/env bash
set -e
export KUBECONFIG=~/.kube/config
echo "=== Destroying old cluster ==="
make cluster-down
echo "=== Bringing up clean cluster ==="
make cluster-up
echo "=== Running Integration Suite ==="
./scripts/test-integration.sh platform-dev
