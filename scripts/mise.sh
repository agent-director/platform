#!/usr/bin/env bash
# Thin, checksum-verified bootstrap for the standard pinned mise CLI.
set -euo pipefail

# pwd -L retains the caller's symlink spelling; do not canonicalize the checkout.
ROOT="$(cd -L -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -L)"
export PLATFORM_ROOT="$ROOT"
export PLATFORM_CACHE_DIR="$ROOT/.cache"

# Set cache paths before even the first mise process (including GUI Git hooks).
export UV_CACHE_DIR="$PLATFORM_CACHE_DIR/uv"
export UV_PROJECT_ENVIRONMENT="$ROOT/.venv"
export UV_PYTHON_DOWNLOADS=never
export PREK_HOME="$PLATFORM_CACHE_DIR/prek"
export RUFF_CACHE_DIR="$PLATFORM_CACHE_DIR/ruff"
export PYTHONPYCACHEPREFIX="$PLATFORM_CACHE_DIR/pycache"
export TMPDIR="$PLATFORM_CACHE_DIR/tmp"
export TMP="$TMPDIR" TEMP="$TMPDIR"
export HELM_CACHE_HOME="$PLATFORM_CACHE_DIR/helm/cache"
export HELM_CONFIG_HOME="$PLATFORM_CACHE_DIR/helm/config"
export HELM_DATA_HOME="$PLATFORM_CACHE_DIR/helm/data"
export TRIVY_CACHE_DIR="$PLATFORM_CACHE_DIR/trivy"
export COVERAGE_FILE="$PLATFORM_CACHE_DIR/coverage/.coverage"
export pnpm_config_store_dir="$PLATFORM_CACHE_DIR/pnpm/store"
export pnpm_config_cache_dir="$PLATFORM_CACHE_DIR/pnpm/cache"
export pnpm_config_state_dir="$PLATFORM_CACHE_DIR/pnpm/state"
export XDG_CACHE_HOME="$PLATFORM_CACHE_DIR/xdg"
export XDG_RUNTIME_DIR="$PLATFORM_CACHE_DIR/xdg-runtime"
export MISE_CACHE_DIR="$PLATFORM_CACHE_DIR/mise/cache"
export MISE_STATE_DIR="$PLATFORM_CACHE_DIR/mise/state"
export MISE_DOWNLOADS_DIR="$PLATFORM_CACHE_DIR/mise/downloads"
export MISE_TMP_DIR="$PLATFORM_CACHE_DIR/mise/tmp"
export MISE_CONFIG_DIR="$PLATFORM_CACHE_DIR/mise/config"
export MISE_SYSTEM_CONFIG_DIR="$PLATFORM_CACHE_DIR/mise/system"

mkdir -p -- "$UV_CACHE_DIR" "$PREK_HOME" "$RUFF_CACHE_DIR" "$PYTHONPYCACHEPREFIX" \
    "$TMPDIR" "$HELM_CACHE_HOME" "$HELM_CONFIG_HOME" "$HELM_DATA_HOME" \
    "$TRIVY_CACHE_DIR" "$PLATFORM_CACHE_DIR/coverage" "$PLATFORM_CACHE_DIR/pytest" "$pnpm_config_store_dir" \
    "$pnpm_config_cache_dir" "$pnpm_config_state_dir" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR" \
    "$MISE_CACHE_DIR" "$MISE_STATE_DIR" "$MISE_DOWNLOADS_DIR" "$MISE_TMP_DIR" \
    "$MISE_CONFIG_DIR" "$MISE_SYSTEM_CONFIG_DIR" "$PLATFORM_CACHE_DIR/mise/bootstrap"
# pnpm 12 uses XDG_RUNTIME_DIR for store coordination and otherwise writes /tmp.
chmod 700 "$XDG_RUNTIME_DIR"

# Root selection does not modify global config or install shell integration.
# Installed tools remain in normal mise data storage, outside disposable .cache.
export MISE_GLOBAL_CONFIG_FILE="$PLATFORM_CACHE_DIR/mise/config/global.toml"
export MISE_SYSTEM_CONFIG_FILE="$PLATFORM_CACHE_DIR/mise/config/system.toml"
export MISE_CEILING_PATHS="$(dirname -- "$ROOT")"
export MISE_OVERRIDE_CONFIG_FILENAMES="$ROOT/mise.toml"
export MISE_OVERRIDE_TOOL_VERSIONS_FILENAMES=""
export MISE_TRUSTED_CONFIG_PATHS="$ROOT/mise.toml"
unset MISE_ENV MISE_TOOL_VERSIONS MISE_NO_CONFIG MISE_NO_ENV MISE_IGNORED_CONFIG_PATHS MISE_PYTHON_UV_VENV_AUTO
# An empty proxy setting is not a proxy, but some native clients parse it as one.
for PROXY_VARIABLE in http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY; do
    if [[ -z "${!PROXY_VARIABLE-}" ]]; then
        unset "$PROXY_VARIABLE"
    fi
done
# Native MISE_<TOOL>_VERSION selectors override even a locked root config.
# Derive selector names from the declared tools; never inherit a foreign shell's
# selected runtime into this checkout.
SECTION=""
while IFS= read -r LINE || [[ -n "$LINE" ]]; do
    [[ "$LINE" == \[*\] ]] && SECTION="$LINE"
    if [[ "$SECTION" == '[tools]' && "$LINE" =~ ^([a-z][a-z0-9_-]*)[[:space:]]*= ]]; then
        TOOL="$(builtin printf '%s' "${BASH_REMATCH[1]}" | tr '[:lower:]-' '[:upper:]_')"
        unset "MISE_${TOOL}_VERSION"
    fi
done < "$ROOT/mise.toml"

fail() {
    >&2 builtin printf 'mise launcher: %s\n' "$*"
    exit 1
}

# These are intentionally absent files, not trusted configuration in cache.
# Reject planted/restored config before mise can evaluate it as global/system data.
for CONFIG in "$MISE_GLOBAL_CONFIG_FILE" "$MISE_SYSTEM_CONFIG_FILE" \
    "$MISE_CONFIG_DIR"/miserc*.toml "$MISE_SYSTEM_CONFIG_DIR"/miserc*.toml; do
    [[ ! -e "$CONFIG" && ! -L "$CONFIG" ]] || fail "unapproved cached mise configuration: $CONFIG"
done

# Installed storage is persistent state, never an artifact cache. Inspect link
# metadata for cache targets, but never canonicalize or rewrite caller paths.
check_storage() {
    local directory="$1" label="$2" depth="${3:-0}" ancestor target spelling
    [[ "$directory" == /* ]] || fail "$label must be an absolute persistent storage path"
    [[ "$depth" -lt 40 ]] || fail "$label has cyclic or excessive storage aliases"
    spelling="$(builtin printf '%s' "$directory/" | tr '[:upper:]' '[:lower:]')"
    case "$spelling" in
        */.cache/*|*/caches/*) fail "$label must not put installed tools in disposable cache" ;;
    esac
    ancestor="$directory"
    while [[ "$ancestor" != / ]]; do
        if [[ -L "$ancestor" ]]; then
            target="$(readlink "$ancestor")"
            [[ "$target" == /* ]] || target="$(dirname -- "$ancestor")/$target"
            check_storage "$target" "$label" "$((depth + 1))"
        fi
        ancestor="$(dirname -- "$ancestor")"
    done
}
DATA_STORAGE="${MISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/mise}"
INSTALL_STORAGE="${MISE_INSTALLS_DIR:-$DATA_STORAGE/installs}"
check_storage "$DATA_STORAGE" MISE_DATA_DIR
check_storage "$INSTALL_STORAGE" MISE_INSTALLS_DIR
check_storage "${MISE_INSTALL_STORE_DIR:-$INSTALL_STORAGE}" MISE_INSTALL_STORE_DIR
check_storage "${MISE_SYSTEM_INSTALLS_DIR:-${MISE_SYSTEM_DATA_DIR:-/usr/local/share/mise}/installs}" MISE_SYSTEM_INSTALLS_DIR
SHARED_STORAGE="${MISE_SHARED_INSTALL_DIRS:-}"
while [[ -n "$SHARED_STORAGE" ]]; do
    check_storage "${SHARED_STORAGE%%:*}" MISE_SHARED_INSTALL_DIRS
    [[ "$SHARED_STORAGE" == *:* ]] || break
    SHARED_STORAGE="${SHARED_STORAGE#*:}"
done

# Reject native overrides before loading mise, rather than silently honoring a
# subdirectory's tool selection. The full tree is checked by toolchain.py check.
check_directory() {
    local directory="$1" file
    for file in "$directory"/mise*.toml "$directory"/.mise*.toml \
        "$directory"/.miserc* "$directory"/.mise/config*.toml \
        "$directory"/mise/config*.toml "$directory"/.config/mise*.toml \
        "$directory"/.config/mise/config*.toml \
        "$directory"/.mise/conf.d/*.toml "$directory"/mise/conf.d/*.toml \
        "$directory"/.config/mise/conf.d/*.toml \
        "$directory"/.mise/conf.d/*/mise*.toml "$directory"/mise/conf.d/*/mise*.toml \
        "$directory"/.config/mise/conf.d/*/mise*.toml; do
        [[ -e "$file" || -L "$file" ]] || continue
        [[ "$file" == "$ROOT/mise.toml" ]] || fail "unapproved toolchain override: $file"
    done
    for file in .python-version .python-versions .node-version .nvmrc .tool-versions uv.toml pnpm-workspace.yaml; do
        [[ "$directory/$file" == "$ROOT/.python-version" || "$directory/$file" == "$ROOT/pnpm-workspace.yaml" ]] && continue
        [[ ! -e "$directory/$file" && ! -L "$directory/$file" ]] || fail "unapproved toolchain override: $directory/$file"
    done
}
check_directory "$ROOT"
DIRECTORY="$PWD"
case "$DIRECTORY/" in
    "$ROOT/"*)
        while [[ "$DIRECTORY" != "$ROOT" ]]; do
            check_directory "$DIRECTORY"
            DIRECTORY="$(dirname -- "$DIRECTORY")"
        done
        ;;
    *) fail "run this launcher from the project root or a project subdirectory" ;;
esac

# Only read exact quoted scalar bootstrap fields from the root [_] table.
# No external runtime is needed before mise installs the project tools.
manifest_scalar() {
    local key="$1" line section="" value="" count=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == \[*\] ]]; then section="$line"; fi
        if [[ "$section" == '[_]' && "$line" =~ ^$key[[:space:]]*=[[:space:]]*\"([^\"]+)\"[[:space:]]*$ ]]; then
            value="${BASH_REMATCH[1]}"
            count=$((count + 1))
        fi
    done < "$ROOT/mise.toml"
    [[ "$count" == 1 ]] || fail "expected exactly one quoted [_].$key in mise.toml"
    builtin printf '%s' "$value"
}

VERSION="$(manifest_scalar mise_version)"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid mise bootstrap version"
case "$(uname -s)" in
    Darwin) OS=macos ;;
    Linux) OS=linux ;;
    *) fail "supported bootstrap systems are macOS and Linux" ;;
esac
case "$(uname -m)" in
    arm64|aarch64) ARCH=arm64 ;;
    x86_64|amd64) ARCH=x64 ;;
    *) fail "supported bootstrap architectures are arm64 and x64" ;;
esac
HASH="$(manifest_scalar "mise_sha256_${OS}_${ARCH}")"
[[ "$HASH" =~ ^[a-f0-9]{64}$ ]] || fail "invalid bootstrap SHA-256"
BINARY_DIR="$PLATFORM_CACHE_DIR/mise/bootstrap/$VERSION/$OS-$ARCH"
BINARY="$BINARY_DIR/mise"

checksum() {
    local output
    if command -v sha256sum >/dev/null 2>&1; then
        output="$(sha256sum -- "$1")"
    elif command -v shasum >/dev/null 2>&1; then
        output="$(shasum -a 256 -- "$1")"
    else
        fail "sha256sum or shasum is required to verify mise"
    fi
    builtin printf '%s' "${output%% *}"
}

if [[ ! -f "$BINARY" ]]; then
    command -v curl >/dev/null 2>&1 || fail "curl is required to bootstrap mise"
    DOWNLOAD="$(mktemp -d "$PLATFORM_CACHE_DIR/mise/bootstrap/download.XXXXXXXX")"
    trap 'rm -rf -- "$DOWNLOAD"' EXIT
    # Download into a new empty directory; no archive extraction or remote script execution.
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        "https://github.com/jdx/mise/releases/download/v$VERSION/mise-v$VERSION-$OS-$ARCH" \
        --output "$DOWNLOAD/mise"
    [[ "$(checksum "$DOWNLOAD/mise")" == "$HASH" ]] || fail "downloaded mise checksum mismatch"
    mkdir -p -- "$BINARY_DIR"
    chmod +x "$DOWNLOAD/mise"
    mv -- "$DOWNLOAD/mise" "$BINARY"
    rm -rf -- "$DOWNLOAD"
    trap - EXIT
fi
# Verify restored bootstrap bytes too, before executing them.
[[ "$(checksum "$BINARY")" == "$HASH" ]] || fail "cached mise checksum mismatch; remove $BINARY_DIR and retry"
exec "$BINARY" "$@"
