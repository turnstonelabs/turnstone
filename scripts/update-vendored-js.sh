#!/usr/bin/env bash
# Update a vendored JavaScript library in turnstone/shared_static/.
#
# Usage:
#   scripts/update-vendored-js.sh katex 0.16.39
#   scripts/update-vendored-js.sh hljs 11.12.0
#   scripts/update-vendored-js.sh mermaid 11.14.0
#   scripts/update-vendored-js.sh hls 1.6.15
#
# This script:
#   1. Downloads the new version from CDN
#   2. Creates the new versioned directory
#   3. Updates all version references in source files
#   4. Removes the old versioned directory

set -euo pipefail

STATIC_DIR="turnstone/shared_static"
CDN="https://cdn.jsdelivr.net/npm"

usage() {
    echo "Usage: $0 <katex|hljs|mermaid|hls> <version>"
    echo "Example: $0 katex 0.16.39"
    exit 1
}

[[ $# -eq 2 ]] || usage

LIB="$1"
VERSION="$2"

# Detect current version from the filesystem (not pyproject.toml, which
# Renovate may have already updated).  Falls back to pyproject.toml if
# no directory is found.
detect_old_version() {
    local pattern="$1"
    # Look for existing directory: e.g. turnstone/shared_static/katex-0.16.38
    local dir
    dir=$(find "${STATIC_DIR}" -maxdepth 1 -type d -name "${pattern}-*" | head -1)
    if [[ -n "$dir" ]]; then
        basename "$dir" | sed "s/${pattern}-//"
        return
    fi
    # Fallback to pyproject.toml
    grep -oE "${pattern}-[0-9.]+" pyproject.toml | head -1 | sed "s/${pattern}-//"
}

# Update version references across all source files
update_refs() {
    local old_pattern="$1"  # e.g. katex-0.16.38
    local new_pattern="$2"  # e.g. katex-0.16.39

    # Find all files with version references (excludes vendored JS and worktrees)
    local files
    files=$(grep -rl --include='*.toml' --include='*.html' --include='*.js' --include='*.md' \
        -F "$old_pattern" . \
        --exclude-dir='.claude' --exclude-dir='node_modules' --exclude-dir='shared_static' \
        2>/dev/null || true)
    for f in $files; do
        sed -i "s|${old_pattern}|${new_pattern}|g" "$f"
        echo "  Updated $f"
    done
}

check_same_version() {
    if [[ "$1" == "$2" ]]; then
        echo "ERROR: Old version ($1) == new version ($2). Nothing to update."
        echo "If the old directory was already removed, re-download with:"
        echo "  rm -rf ${STATIC_DIR}/${3}-${1} && $0 $3 $2"
        exit 1
    fi
}

case "$LIB" in
    katex)
        OLD_VERSION=$(detect_old_version "katex")
        check_same_version "$OLD_VERSION" "$VERSION" "katex"
        OLD_DIR="${STATIC_DIR}/katex-${OLD_VERSION}"
        NEW_DIR="${STATIC_DIR}/katex-${VERSION}"

        echo "Updating KaTeX ${OLD_VERSION} -> ${VERSION}"
        mkdir -p "${NEW_DIR}/fonts"

        echo "  Downloading katex.min.js..."
        curl -sSfL "${CDN}/katex@${VERSION}/dist/katex.min.js" -o "${NEW_DIR}/katex.min.js"
        echo "  Downloading katex.min.css..."
        curl -sSfL "${CDN}/katex@${VERSION}/dist/katex.min.css" -o "${NEW_DIR}/katex.min.css"

        echo "  Downloading fonts..."
        # Extract font filenames from the CSS
        font_files=$(curl -sSfL "${CDN}/katex@${VERSION}/dist/katex.min.css" \
            | grep -oE 'fonts/[^")]+' | sort -u)
        for font in $font_files; do
            if ! curl -sSfL "${CDN}/katex@${VERSION}/dist/${font}" -o "${NEW_DIR}/${font}" 2>/dev/null; then
                echo "  WARNING: Failed to download font: ${font}"
            fi
        done

        # Copy LICENSE from old dir if present
        if [[ -f "${OLD_DIR}/LICENSE" ]]; then
            cp "${OLD_DIR}/LICENSE" "${NEW_DIR}/LICENSE"
        fi

        update_refs "katex-${OLD_VERSION}" "katex-${VERSION}"
        rm -rf "${OLD_DIR}"
        echo "Done. Old directory removed: ${OLD_DIR}"
        ;;

    hljs)
        OLD_VERSION=$(detect_old_version "hljs")
        check_same_version "$OLD_VERSION" "$VERSION" "hljs"
        OLD_DIR="${STATIC_DIR}/hljs-${OLD_VERSION}"
        NEW_DIR="${STATIC_DIR}/hljs-${VERSION}"

        echo "Updating Highlight.js ${OLD_VERSION} -> ${VERSION}"
        mkdir -p "${NEW_DIR}"

        echo "  Downloading highlight.min.js..."
        curl -sSfL "${CDN}/@highlightjs/cdn-assets@${VERSION}/highlight.min.js" -o "${NEW_DIR}/highlight.min.js"

        if [[ -f "${OLD_DIR}/LICENSE" ]]; then
            cp "${OLD_DIR}/LICENSE" "${NEW_DIR}/LICENSE"
        fi

        update_refs "hljs-${OLD_VERSION}" "hljs-${VERSION}"
        rm -rf "${OLD_DIR}"
        echo "Done. Old directory removed: ${OLD_DIR}"
        ;;

    mermaid)
        OLD_VERSION=$(detect_old_version "mermaid")
        check_same_version "$OLD_VERSION" "$VERSION" "mermaid"
        OLD_DIR="${STATIC_DIR}/mermaid-${OLD_VERSION}"
        NEW_DIR="${STATIC_DIR}/mermaid-${VERSION}"

        echo "Updating Mermaid ${OLD_VERSION} -> ${VERSION}"
        mkdir -p "${NEW_DIR}"

        echo "  Downloading mermaid.min.js..."
        curl -sSfL "${CDN}/mermaid@${VERSION}/dist/mermaid.min.js" -o "${NEW_DIR}/mermaid.min.js"

        if [[ -f "${OLD_DIR}/LICENSE" ]]; then
            cp "${OLD_DIR}/LICENSE" "${NEW_DIR}/LICENSE"
        fi

        update_refs "mermaid-${OLD_VERSION}" "mermaid-${VERSION}"
        rm -rf "${OLD_DIR}"
        echo "Done. Old directory removed: ${OLD_DIR}"
        ;;

    hls)
        OLD_VERSION=$(detect_old_version "hls")
        check_same_version "$OLD_VERSION" "$VERSION" "hls"
        OLD_DIR="${STATIC_DIR}/hls-${OLD_VERSION}"
        NEW_DIR="${STATIC_DIR}/hls-${VERSION}"

        echo "Updating hls.js ${OLD_VERSION} -> ${VERSION}"
        mkdir -p "${NEW_DIR}"

        echo "  Downloading hls.min.js..."
        curl -sSfL "${CDN}/hls.js@${VERSION}/dist/hls.min.js" -o "${NEW_DIR}/hls.min.js"

        echo "  Downloading LICENSE..."
        if ! curl -sSfL "${CDN}/hls.js@${VERSION}/LICENSE" -o "${NEW_DIR}/LICENSE" 2>/dev/null; then
            if [[ -f "${OLD_DIR}/LICENSE" ]]; then
                cp "${OLD_DIR}/LICENSE" "${NEW_DIR}/LICENSE"
            else
                echo "  WARNING: Could not obtain LICENSE for hls.js ${VERSION}"
            fi
        fi

        update_refs "hls-${OLD_VERSION}" "hls-${VERSION}"
        rm -rf "${OLD_DIR}"
        echo "Done. Old directory removed: ${OLD_DIR}"
        ;;

    *)
        echo "Unknown library: ${LIB}"
        usage
        ;;
esac

echo ""
echo "NOTE: If you added a NEW library (not just updating a version), also update"
echo "  the _ASSET_RE regex in turnstone/core/web_helpers.py — its negative lookahead"
echo "  skips vendored directories to avoid double-versioning static asset URLs."
echo ""
echo "Verify the update:"
echo "  git diff --stat"
echo "  python -m turnstone.server  # test locally"
