#!/usr/bin/env bash
# Keep the existing entry point; Python propagates Azure failures without the
# command-substitution errexit traps in the former shell helpers.
set -euo pipefail
exec python3 "$(dirname "$0")/entra_setup.py" "$@"
