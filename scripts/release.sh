#!/usr/bin/env bash
#
# Bump version, regenerate lockfile, commit, and tag.
#
# Usage:
#   scripts/release.sh 1.0.0          # stable release
#   scripts/release.sh 1.1.0a1        # experimental pre-release
#   scripts/release.sh 1.0.1 --push   # bump + push tag to origin
#
set -euo pipefail

VERSION="${1:?Usage: scripts/release.sh VERSION [--push]}"
PUSH="${2:-}"

# Validate PEP 440 version
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(a[0-9]+|b[0-9]+|rc[0-9]+)?$'; then
    echo "error: invalid PEP 440 version: $VERSION" >&2
    echo "  examples: 1.0.0, 1.1.0a1, 1.0.1rc2" >&2
    exit 1
fi

TAG="v${VERSION}"

# Check for clean working tree
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "error: working tree is dirty — commit or stash first" >&2
    exit 1
fi

# Check tag doesn't already exist
if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "error: tag $TAG already exists" >&2
    exit 1
fi

# Detect current version
CURRENT=$(grep -oP '(?<=^version = ")[^"]+' pyproject.toml)
echo "Bumping $CURRENT → $VERSION"

# Update version in both files
sed -i "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
sed -i "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" turnstone/__init__.py

# Regenerate lockfile
echo "Regenerating uv.lock..."
uv lock

# Commit and tag
git add pyproject.toml turnstone/__init__.py uv.lock
git commit -m "chore: bump version to $VERSION"
git tag "$TAG"

echo ""
echo "Created commit and tag $TAG"

if [ "$PUSH" = "--push" ]; then
    BRANCH=$(git rev-parse --abbrev-ref HEAD)
    echo "Pushing $BRANCH + $TAG to origin..."
    git push origin "$BRANCH" "$TAG"
else
    echo "Run 'git push origin <branch> $TAG' to publish"
fi
