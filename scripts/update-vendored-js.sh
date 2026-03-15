#!/usr/bin/env bash
# Update a vendored JavaScript library in turnstone/shared_static/.
#
# Usage:
#   scripts/update-vendored-js.sh katex 0.16.39
#   scripts/update-vendored-js.sh hljs 11.12.0
#   scripts/update-vendored-js.sh mermaid 11.14.0
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
    echo "Usage: $0 <katex|hljs|mermaid> <version>"
    echo "Example: $0 katex 0.16.39"
    exit 1
}

[[ $# -eq 2 ]] || usage

LIB="$1"
VERSION="$2"

# Detect current version from pyproject.toml
detect_old_version() {
    local pattern="$1"
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

case "$LIB" in
    katex)
        OLD_VERSION=$(detect_old_version "katex")
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

    *)
        echo "Unknown library: ${LIB}"
        usage
        ;;
esac

echo ""
echo "Verify the update:"
echo "  git diff --stat"
echo "  python -m turnstone.server  # test locally"
