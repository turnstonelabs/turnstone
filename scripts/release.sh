#!/usr/bin/env bash
#
# Bump version (package and Helm chart appVersion), regenerate lockfile, commit,
# and tag.
#
# Usage:
#   scripts/release.sh 1.8.1 --push   # stable release from main
#   scripts/release.sh 1.9.0a1 --push # pre-release from dev
#   scripts/release.sh 1.8.2 --push   # prior-line patch from stable/1.8
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

# Enforce the release topology before changing any files. main is the current
# stable line, dev is the next-release line, and stable/X.Y is a maintained
# prior line whose branch name must match the version being released.
BRANCH=$(git symbolic-ref --quiet --short HEAD || true)
if [ -z "$BRANCH" ]; then
    echo "error: releases must be cut from a branch, not detached HEAD" >&2
    exit 1
fi

if echo "$VERSION" | grep -qE '(a|b|rc)[0-9]+$'; then
    if [ "$BRANCH" != "dev" ]; then
        echo "error: pre-releases must be cut from dev (current branch: $BRANCH)" >&2
        exit 1
    fi
else
    case "$BRANCH" in
        main)
            ;;
        stable/*)
            RELEASE_LINE="${VERSION%.*}"
            if [ "$BRANCH" != "stable/$RELEASE_LINE" ]; then
                echo "error: $VERSION belongs on stable/$RELEASE_LINE, not $BRANCH" >&2
                exit 1
            fi
            ;;
        dev)
            echo "error: stable releases must be cut from main or matching stable/X.Y" >&2
            exit 1
            ;;
        *)
            echo "error: releases must be cut from main, dev, or stable/X.Y" >&2
            echo "  current branch: $BRANCH" >&2
            exit 1
            ;;
    esac
fi

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

# The chart's appVersion is its default image tag. A stable release points the
# chart on its own branch at itself. A pre-release points dev's chart at the
# newest stable release instead, so installing the chart from dev, the default
# branch, never selects a pre-release image.
CHART="deploy/helm/turnstone/Chart.yaml"
if ! grep -q '^appVersion: ' "$CHART" 2>/dev/null; then
    echo "error: $CHART has no appVersion line" >&2
    exit 1
fi
if echo "$VERSION" | grep -qE '(a|b|rc)[0-9]+$'; then
    CHART_APP_VERSION=$(git tag -l 'v*' \
        | sed -n 's/^v\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$/\1/p' \
        | sort -V | tail -n 1)
    if [ -z "$CHART_APP_VERSION" ]; then
        echo "error: no stable vX.Y.Z tag for the chart's appVersion; fetch tags first" >&2
        exit 1
    fi
else
    CHART_APP_VERSION="$VERSION"
fi
# Never move the default image backwards: a clone missing the newest stable tag
# would otherwise quietly downgrade dev's chart.
CURRENT_APP_VERSION=$(sed -n 's/^appVersion: "\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$CHART")
if ! printf '%s\n%s\n' "$CURRENT_APP_VERSION" "$CHART_APP_VERSION" | sort -V -C; then
    echo "error: the chart's appVersion would move back from $CURRENT_APP_VERSION to $CHART_APP_VERSION; fetch tags first" >&2
    exit 1
fi
# A changed default image is a changed chart, so its patch version moves too:
# deployments that follow the chart from Git may rebuild it only when the
# chart's version changes.
CHART_VERSION=$(sed -n 's/^version: \([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$/\1/p' "$CHART")
if [ -z "$CHART_VERSION" ]; then
    echo "error: $CHART version is not a plain X.Y.Z" >&2
    exit 1
fi
NEW_CHART_VERSION="$CHART_VERSION"
if [ "$CURRENT_APP_VERSION" != "$CHART_APP_VERSION" ]; then
    NEW_CHART_VERSION="${CHART_VERSION%.*}.$(( ${CHART_VERSION##*.} + 1 ))"
fi

# Detect current version
CURRENT=$(grep -oP '(?<=^version = ")[^"]+' pyproject.toml)
echo "Bumping $CURRENT → $VERSION (chart $NEW_CHART_VERSION, appVersion $CHART_APP_VERSION)"

# Update the version in the package files and the chart
sed -i "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
sed -i "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" turnstone/__init__.py
sed -i "s/^appVersion: .*/appVersion: \"$CHART_APP_VERSION\"/" "$CHART"
sed -i "s/^version: .*/version: $NEW_CHART_VERSION/" "$CHART"

# Regenerate lockfile
echo "Regenerating uv.lock..."
uv lock

# Commit and tag
git add pyproject.toml turnstone/__init__.py uv.lock "$CHART"
git commit -m "chore: bump version to $VERSION"
git tag "$TAG"

echo ""
echo "Created commit and tag $TAG"

if [ "$PUSH" = "--push" ]; then
    echo "Pushing $BRANCH + $TAG to origin..."
    git push --atomic origin "$BRANCH" "$TAG"
else
    echo "Run 'git push --atomic origin $BRANCH $TAG' to publish"
fi
