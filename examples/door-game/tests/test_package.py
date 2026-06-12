"""Smoke test for the packaging skeleton."""

from __future__ import annotations

import understone


def test_version_present() -> None:
    assert understone.__version__ == "0.1.0"
