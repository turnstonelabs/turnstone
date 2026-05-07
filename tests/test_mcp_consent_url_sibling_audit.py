"""Structural gate against the Phase 7b sibling-bug pattern.

Phase 7b's bug-1 was a single ``f"MCP X error: {e}"`` site dropping a
structured-error JSON. Phase 8 introduces the ``consent_url`` field on
the same JSON envelope: every ``_structured_error(...)`` invocation
that emits ``mcp_consent_required`` or ``mcp_insufficient_scope`` MUST
also pass a ``consent_url=`` kwarg, otherwise the dashboard renderer
can't surface a re-consent button.

This test is purely structural — it scans the source of
:mod:`turnstone.core.mcp_client` and asserts every consent-required /
insufficient-scope ``_structured_error`` call carries
``consent_url=``. It catches future regressions where a new exec path
adds a fourth call site and forgets the kwarg.
"""

from __future__ import annotations

import re
from pathlib import Path

import turnstone.core.mcp_client as _mcp_client_module

_USER_ACTIONABLE_CODES = ("mcp_consent_required", "mcp_insufficient_scope")


def _read_source() -> str:
    path = Path(_mcp_client_module.__file__)
    return path.read_text(encoding="utf-8")


def _find_structured_error_blocks(source: str) -> list[tuple[int, str]]:
    """Return ``(line_no, block)`` pairs for every ``_structured_error(...)``.

    Each block is the call's argument list expanded across however many
    lines the formatter chose. Uses a paren-counting walk so multi-line
    kwargs and nested expressions are captured correctly.
    """
    blocks: list[tuple[int, str]] = []
    needle = "_structured_error("
    idx = 0
    while True:
        loc = source.find(needle, idx)
        if loc < 0:
            break
        # Skip the function definition itself.
        if source[loc - 4 : loc] == "def ":
            idx = loc + len(needle)
            continue
        line_no = source.count("\n", 0, loc) + 1
        depth = 1
        end = loc + len(needle)
        while end < len(source) and depth > 0:
            ch = source[end]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            end += 1
        blocks.append((line_no, source[loc:end]))
        idx = end
    return blocks


def test_every_user_actionable_structured_error_passes_consent_url() -> None:
    source = _read_source()
    blocks = _find_structured_error_blocks(source)
    user_actionable_blocks = [
        (ln, blk)
        for ln, blk in blocks
        if any(f'code="{code}"' in blk for code in _USER_ACTIONABLE_CODES)
    ]

    # Sanity check: ensure we actually scanned the file the audit cares
    # about (a stale path or import would otherwise silently pass with
    # zero matches).
    assert user_actionable_blocks, (
        "No mcp_consent_required / mcp_insufficient_scope _structured_error "
        "call sites found — has the audit been pointed at the wrong file?"
    )

    missing: list[tuple[int, str]] = []
    for ln, blk in user_actionable_blocks:
        if "consent_url=" not in blk:
            # Strip whitespace and truncate so the failure message is
            # readable in CI.
            collapsed = re.sub(r"\s+", " ", blk).strip()
            missing.append((ln, collapsed[:200]))

    assert not missing, (
        "Sibling-bug regression: the following consent-required / "
        "insufficient-scope _structured_error sites are missing the "
        "consent_url= kwarg.\n" + "\n".join(f"  line {ln}: {snippet}" for ln, snippet in missing)
    )


def test_audit_finds_all_known_user_actionable_sites() -> None:
    """Lock the count so accidental deletions are caught.

    There are 13 user-actionable ``_structured_error`` call sites today
    (4 each in the tool / resource / prompt token-classify branches +
    3 in the post-retry-failed branches + 1 in ``_handle_auth_403``'s
    insufficient-scope branch). If a new exec path is added the count
    can rise; if a branch is removed the count can fall — both are
    fine, but require an intentional bump of this number to confirm
    the change went through review.
    """
    source = _read_source()
    blocks = _find_structured_error_blocks(source)
    user_actionable_count = sum(
        1 for _, blk in blocks if any(f'code="{code}"' in blk for code in _USER_ACTIONABLE_CODES)
    )
    assert user_actionable_count == 13, (
        f"Expected 13 user-actionable _structured_error sites, got "
        f"{user_actionable_count}. If this is intentional, bump the "
        f"expected count and document why in the commit message."
    )
