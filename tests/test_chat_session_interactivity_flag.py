"""ChatSession interactivity flag tests (Phase 9).

Validates that ``ChatSession._is_interactive_for_consent`` is computed
correctly from ``client_type`` on construction.  This is the front of
the Phase 9 plumb-through: the flag flows from here to
``_dispatch_pool_sync`` to the structured-error → pending-consent
write path.
"""

from __future__ import annotations

from tests._session_helpers import make_session
from turnstone.prompts import INTERACTIVE_CONSENT_CLIENT_TYPES, ClientType


def test_web_is_interactive() -> None:
    s = make_session(client_type=ClientType.WEB)
    assert s._is_interactive_for_consent is True


def test_cli_is_interactive() -> None:
    s = make_session(client_type=ClientType.CLI)
    assert s._is_interactive_for_consent is True


def test_chat_is_not_interactive() -> None:
    # Discord / Slack adapters cannot drive a browser redirect from
    # inside the channel — consent prompts must be deferred to the
    # dashboard badge.
    s = make_session(client_type=ClientType.CHAT)
    assert s._is_interactive_for_consent is False


def test_scheduled_is_not_interactive() -> None:
    # The scheduler runs autonomously; the user isn't online to
    # complete the OAuth redirect.
    s = make_session(client_type=ClientType.SCHEDULED)
    assert s._is_interactive_for_consent is False


def test_interactive_set_matches_module_constant() -> None:
    # Pin the module-level frozenset against the flag computation —
    # a future reorganisation that drifts the set vs the per-session
    # logic would silently break the gating.
    for ct in ClientType:
        s = make_session(client_type=ct)
        assert s._is_interactive_for_consent == (ct in INTERACTIVE_CONSENT_CLIENT_TYPES), ct


def test_default_client_type_is_cli_interactive() -> None:
    # Defaults preserved — make_session uses ChatSession's default
    # which is CLI.  Sanity check that the default user experience
    # stays interactive-for-consent.
    s = make_session()
    assert s._client_type == ClientType.CLI
    assert s._is_interactive_for_consent is True


def test_scheduled_env_file_exists() -> None:
    """The SCHEDULED env module must exist; otherwise
    ``compose_system_message`` for a scheduled session would 500."""
    from turnstone.prompts import _load

    text = _load("env/scheduled.md")
    assert "Output Environment" in text
    assert "consent" in text.lower()
