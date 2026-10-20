#!/usr/bin/env bash
# Hardened SessionStart hook for Graphify code-intelligence bundle hydration.
# Fails open so session startup never blocks when offline or before graph-latest exists.
set -euo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
REPO="${GRAPHIFY_REPO:-agent-director/platform}"
MIN_GRAPHIFY_VERSION="${GRAPHIFY_MIN_VERSION:-0.9.92}"
STAGING="${PROJECT_DIR}/.cache/graphify-brain/staging.$$"

cleanup() {
  rm -rf "$STAGING"
}
trap 'cleanup' EXIT
trap 'cleanup; exit 0' ERR

mkdir -p "$STAGING/extracted"
BUNDLE="$STAGING/graphify-bundle.tar.gz"
EXTRACTED="$STAGING/extracted"

gh release download graph-latest \
  --repo "$REPO" \
  --pattern "graphify-bundle.tar.gz" \
  --output "$BUNDLE" \
  --clobber >/dev/null 2>&1

(
  cd "$PROJECT_DIR"
  mise exec -- uv run --no-project python -I -c \
    'import sys, tarfile; tarfile.open(sys.argv[1], "r:gz").extractall(path=sys.argv[2], filter="data")' \
    "$BUNDLE" "$EXTRACTED"
)

jq -e 'has("nodes") and has("links")' "$EXTRACTED/graph.json" >/dev/null
jq -e 'has("source_sha") and has("graphify_version") and has("generated_at")' "$EXTRACTED/metadata.json" >/dev/null

BUNDLE_VERSION="$(jq -r '.graphify_version' "$EXTRACTED/metadata.json")"
if ! sort -V -C <<< "${MIN_GRAPHIFY_VERSION}"$'\n'"${BUNDLE_VERSION}"; then
  exit 0
fi

if [[ -d "$PROJECT_DIR/graphify-out" ]]; then
  mv "$PROJECT_DIR/graphify-out" "$STAGING/graphify-out.old"
fi
if ! mv "$EXTRACTED" "$PROJECT_DIR/graphify-out"; then
  if [[ -d "$STAGING/graphify-out.old" ]]; then
    mv "$STAGING/graphify-out.old" "$PROJECT_DIR/graphify-out" || true
  fi
  exit 0
fi
