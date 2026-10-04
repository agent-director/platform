#!/usr/bin/env bash
set -e
export KUBECONFIG=~/.kube/config
./scripts/test-integration.sh platform-dev
