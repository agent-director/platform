#!/bin/bash
set -o pipefail

NAMESPACE=${1:-"platform"}
RELEASE_NAME=${2:-"platform"}
echo "========================================"
echo " Agent Director - Teardown"
echo "========================================"

echo "Uninstalling Helm release '$RELEASE_NAME' in namespace '$NAMESPACE'..."
helm uninstall postgres-operator -n "$NAMESPACE" || echo "Postgres operator not found or already deleted."
helm uninstall "$RELEASE_NAME" -n "$NAMESPACE" || echo "Helm release not found or already deleted."

echo "Deleting namespace '$NAMESPACE' (this may take a moment)..."
kubectl delete namespace "$NAMESPACE" || echo "Namespace already deleted."

echo "Teardown complete."
