"""Slack channel adapter (Socket Mode).

Bridges Slack channel mentions, slash-command sessions, and DMs to
turnstone workstreams.  The :class:`TurnstoneSlackBot` runs over
slack-bolt's Socket Mode (no public URL or signing-secret needed)
and shares the per-user routing, approvals, and SSE-event consumption
patterns established by the Discord adapter.

See ``turnstone/channels/cli.py`` for the gateway entry point and
``turnstone/channels/slack/config.py`` for app + token setup.
"""
