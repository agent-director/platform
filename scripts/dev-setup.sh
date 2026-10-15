#!/usr/bin/env bash
# Reproducible dependency setup, not cluster provisioning or secret collection.
set -euo pipefail

ROOT="$(cd -L -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -L)"
cd -L -- "$ROOT"

INSTALL_HOOKS=true
if [[ "${CI:-false}" == true ]]; then INSTALL_HOOKS=false; fi
for argument in "$@"; do
    case "$argument" in
        --no-hooks) INSTALL_HOOKS=false ;;
        *) >&2 builtin printf 'Usage: scripts/dev-setup.sh [--no-hooks]\n'; exit 2 ;;
    esac
done

builtin printf 'Installing the locked root toolchain and project dependencies.\n'
mise install --locked
mise exec -- uv sync --locked
mise exec -- pnpm install --frozen-lockfile
mise exec -- helm repo add temporal https://go.temporal.io/helm-charts --force-update
mise exec -- helm dependency build charts/platform --skip-refresh

if [[ "$INSTALL_HOOKS" == true ]]; then
    mise exec -- prek install
fi

if [[ ! -f .env ]]; then
    builtin printf 'Optional deployment credentials: copy .env.example to .env and edit it privately.\n'
fi
builtin printf '%s\n' 'Setup complete. Create an owned local kind environment with:' \
    '  make cluster-up' \
    'Then build, deploy, and test against that cluster with:' \
    '  make test-integration' \
    'Remove only that owned local cluster with:' \
    '  make teardown'
