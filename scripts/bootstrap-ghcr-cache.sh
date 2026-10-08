#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "📦 Bootstrapping GHCR Packages for BuildKit Caching"
echo "=========================================================="

if ! command -v gh &> /dev/null; then
    echo "Error: GitHub CLI (gh) is required but not installed."
    exit 1
fi

# 1. Determine owner and type (User vs Org)
OWNER=$(gh repo view --json owner -q .owner.login)
REPO=$(gh repo view --json name -q .name)
OWNER_TYPE=$(gh api "users/$OWNER" -q .type)

echo "Detected Owner: $OWNER ($OWNER_TYPE)"
echo "Detected Repo:  $REPO"
echo ""

# 2. Build dummy image
echo "Building 0-byte dummy image..."
echo "FROM scratch" > Dockerfile.empty
docker build -t dummy-cache -f Dockerfile.empty . >/dev/null
rm Dockerfile.empty

echo ""
echo "Pushing cache packages and generating permission URLs..."
echo "----------------------------------------------------------"

# 3. Find all components and push
for df in images/*/Dockerfile; do
  if [ -f "$df" ]; then
    IMG=$(basename "$(dirname "$df")")

    # GHCR requires lowercase owner
    OWNER_LOWER=$(echo "$OWNER" | tr '[:upper:]' '[:lower:]')
    TAG="ghcr.io/${OWNER_LOWER}/${IMG}:buildcache"

    echo "Pushing -> $TAG"
    docker tag dummy-cache "$TAG"
    docker push "$TAG" >/dev/null

    # Format the exact URL based on account type
    if [ "$OWNER_TYPE" == "Organization" ]; then
      URL="https://github.com/orgs/${OWNER}/packages/container/${IMG}/settings"
    else
      URL="https://github.com/users/${OWNER}/packages/container/${IMG}/settings"
    fi

    echo "🔗 Action Required: Click here and grant the '$REPO' repository 'Write' access:"
    echo "   $URL"
    echo "----------------------------------------------------------"
  fi
done

echo "✅ GHCR packages seeded successfully."
echo ""
echo "Once you have clicked the links above and granted Write access to the repository,"
echo "run the following block to permanently turn global registry caching back on:"
echo ""
echo "sed -i '' 's/type=gha,scope=\\${{ inputs.image }}/type=registry,ref=\\${{ inputs.image }}:buildcache/g' .github/workflows/_build-scan-push.yml"
echo "sed -i '' 's/type=gha,mode=max,scope=\\${{ inputs.image }}/type=registry,ref=\\${{ inputs.image }}:buildcache,mode=max/g' .github/workflows/_build-scan-push.yml"
echo "git commit -am \"perf: re-enable global registry caching\" && git push"
echo "=========================================================="
