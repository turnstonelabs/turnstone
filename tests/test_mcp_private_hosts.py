"""Trusted private-host configuration for MCP OAuth discovery."""

from __future__ import annotations

import pytest

from turnstone.core.mcp_private_hosts import (
    MAX_TRUSTED_PRIVATE_HOSTS,
    merge_trusted_private_hosts,
    normalize_trusted_private_host,
    parse_trusted_private_hosts,
)


@pytest.mark.parametrize(
    "raw",
    [
        "https://gitlab.internal.example",
        "gitlab.internal.example/path",
        "*.internal.example",
        "user@gitlab.internal.example",
        "gitlab.internal.example:443",
        "",
    ],
)
def test_normalize_rejects_everything_except_an_exact_host(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_trusted_private_host(raw)


def test_normalize_is_case_insensitive_and_removes_dns_root_dot() -> None:
    assert normalize_trusted_private_host("GitLab.Internal.Example.") == ("gitlab.internal.example")


def test_parse_accepts_comma_and_newline_separated_hosts() -> None:
    assert parse_trusted_private_hosts("one.internal, TWO.internal.\n10.20.30.40") == (
        "one.internal",
        "two.internal",
        "10.20.30.40",
    )


def test_merge_marks_environment_entries_readonly_and_wins_duplicates() -> None:
    merged = merge_trusted_private_hosts(
        environment_value="gitlab.internal.example,env.internal",
        manual_value="manual.internal\ngitlab.internal.example",
    )

    assert merged == [
        {"host": "gitlab.internal.example", "source": "environment", "readonly": True},
        {"host": "env.internal", "source": "environment", "readonly": True},
        {"host": "manual.internal", "source": "manual", "readonly": False},
    ]


def test_merge_caps_the_combined_environment_and_manual_list() -> None:
    environment = ",".join(f"env-{i}.internal" for i in range(MAX_TRUSTED_PRIVATE_HOSTS))
    with pytest.raises(ValueError, match="in total"):
        merge_trusted_private_hosts(
            environment_value=environment,
            manual_value="one-more.internal",
        )
