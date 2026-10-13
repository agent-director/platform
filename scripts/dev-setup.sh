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
"$ROOT/scripts/mise.sh" install --locked
# uv owns the editable root project and .venv; mise only selects its interpreter.
# Every operation re-enters the current checkout's launcher. An inherited active
# marker from a different worktree cannot select that worktree's .venv or caches.
"$ROOT/scripts/mise.sh" exec -- uv sync --locked
"$ROOT/scripts/mise.sh" exec -- uv run --locked scripts/toolchain.py check
"$ROOT/scripts/mise.sh" exec -- pnpm install --frozen-lockfile
# The helper reads repository declarations and preserves committed Chart.lock files.
"$ROOT/scripts/mise.sh" exec -- uv run --locked scripts/helm_dependencies.py build

if [[ "$INSTALL_HOOKS" == true ]]; then
    # Stock Prek install honors default_install_hook_types and existing legacy
    # hooks. The helper only prefixes recognized shims with root mise/cache env.
    "$ROOT/scripts/mise.sh" exec -- uv run --locked scripts/toolchain.py install-hooks
fi

if [[ ! -f .env ]]; then
    builtin printf 'Optional deployment credentials: copy .env.example to .env and edit it privately.\n'
fi
builtin printf '%s\n' 'Setup complete. Create an explicitly owned local kind environment with:' \
    '  make cluster-up CLUSTER_NAME=<unique-name>' \
    'Then build, deploy, and test against that existing cluster with:' \
    '  make test-integration CLUSTER_NAME=<same-name>' \
    'Remove only that owned local cluster with:' \
    '  make teardown CLUSTER_NAME=<same-name>'
