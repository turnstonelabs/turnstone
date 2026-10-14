"""Shared skip for suites that run the real resource-limited PDF worker."""

from __future__ import annotations

import sys

import pytest

# macOS rejects any finite RLIMIT_AS, so the worker fails closed there. Checking the
# platform rather than the worker's limit code keeps a regression in that code
# failing on Linux instead of turning these tests into skips. The marker lives here
# once so the PDF and thumbnail suites cannot drift.
pdf_worker_skip = pytest.mark.skipif(
    sys.platform == "darwin", reason="macOS cannot apply the PDF worker's resource limits"
)
