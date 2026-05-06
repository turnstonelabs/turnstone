"""Unit tests for ``turnstone.core.mcp_http_parsers``.

The parser replaces the prior hand-rolled scanners that used
``header.lower().find("scope")`` to locate parameter names — that approach
misparsed ``scope`` embedded inside other tokens (``xscope``) or inside
quoted-string values of preceding params. Each adversarial case below
asserts the new tokenizer respects RFC 7235 ``challenge → auth-param``
boundaries; the docstrings document the equivalent input that broke the
naive parser. Negative-test verification: temporarily reverting
``parse_www_authenticate_scope`` to delegate to ``header.lower().find("scope")``
makes ``test_scope_inside_realm_value`` and ``test_scope_inside_xscope`` fail.
"""

from __future__ import annotations

import time

import pytest

from turnstone.core.mcp_http_parsers import (
    parse_www_authenticate_bearer,
    parse_www_authenticate_error,
    parse_www_authenticate_scope,
)


class TestParseScope:
    def test_basic_scope(self) -> None:
        header = 'Bearer error="insufficient_scope", scope="files:read mail:send"'
        assert parse_www_authenticate_scope(header) == ("files:read", "mail:send")

    def test_no_scope_param(self) -> None:
        assert parse_www_authenticate_scope('Bearer error="invalid_token"') == ()

    def test_unterminated_quoted_string_returns_empty(self) -> None:
        assert parse_www_authenticate_scope('Bearer scope="files:read') == ()

    def test_escaped_chars_in_value_drops_invalid_scope_token(self) -> None:
        # RFC 7230 §3.2.6 backslash escapes decode the literal scope to
        # ``files:read "weird"``. RFC 6749 §3.3 ``scope-token`` forbids
        # ``"``, so ``"weird"`` is dropped and only ``files:read``
        # survives the post-split validation.
        header = r'Bearer scope="files:read \"weird\""'
        assert parse_www_authenticate_scope(header) == ("files:read",)

    def test_empty_string(self) -> None:
        assert parse_www_authenticate_scope("") == ()

    def test_unquoted_scope_value(self) -> None:
        # Unquoted single token.
        assert parse_www_authenticate_scope("Bearer scope=files:read") == ("files:read",)

    # --- the four headline misparse cases ---

    def test_scope_inside_xscope(self) -> None:
        """``Bearer xscope="value"`` must NOT be read as ``scope``.

        The naive ``find("scope")`` matched at position 7 inside
        ``xscope`` and returned ``("value",)``.
        """
        assert parse_www_authenticate_scope('Bearer xscope="value"') == ()

    def test_scope_inside_realm_value(self) -> None:
        """``Bearer realm="my scope=fake", scope="real"`` must return ``("real",)``.

        The naive parser found ``scope=`` inside the quoted ``realm``
        value first and returned ``("fake",)``.
        """
        header = 'Bearer realm="my scope=fake", scope="real"'
        assert parse_www_authenticate_scope(header) == ("real",)

    def test_scope_inside_quoted_realm_with_escaped_quotes(self) -> None:
        """``Bearer realm="foo scope=\\"admin:write\\" bar"`` returns ``()``.

        The inner ``scope=`` is wholly inside the quoted-string value of
        ``realm`` — there is no top-level ``scope`` auth-param, so the
        result is empty.
        """
        header = r'Bearer realm="foo scope=\"admin:write\" bar"'
        assert parse_www_authenticate_scope(header) == ()

    def test_scope_token_validation_drops_control_bytes(self) -> None:
        """Tokens containing CR / LF / tab / DEL / quote are dropped.

        RFC 6749 §3.3 restricts ``scope-token`` to visible ASCII
        excluding ``"`` and ``\\``. The splitter applies that
        validation so a malicious AS cannot smuggle CRLF (or the like)
        through a future log / notification path that prints the scope
        list verbatim. ``"a\\rb"`` and ``"\\nc"`` fail validation;
        ``"d"`` survives. The legitimate space separator splits ``d``
        into its own token.
        """
        # Build via concatenation so the assertion stays intelligible.
        header = 'Bearer scope="a\rb \nc d"'
        assert parse_www_authenticate_scope(header) == ("d",)


class TestParseError:
    def test_basic_quoted_error(self) -> None:
        assert (
            parse_www_authenticate_error('Bearer error="insufficient_scope"')
            == "insufficient_scope"
        )

    def test_other_quoted_error_tokens(self) -> None:
        assert parse_www_authenticate_error('Bearer error="invalid_token"') == "invalid_token"
        assert parse_www_authenticate_error('Bearer error="invalid_request"') == "invalid_request"

    def test_no_error_param(self) -> None:
        assert parse_www_authenticate_error("Bearer realm=foo") is None

    def test_error_description_does_not_match_error(self) -> None:
        """``error_description`` is its own auth-param key, not ``error``.

        The tokenizer reads ``_`` as part of the token (RFC 7230 ``tchar``),
        so ``error_description`` becomes one key, ``error`` another.
        """
        assert parse_www_authenticate_error('Bearer error_description="bad"') is None

    def test_unquoted_error(self) -> None:
        # Some ASes don't quote the error token.
        assert (
            parse_www_authenticate_error("Bearer error=insufficient_scope") == "insufficient_scope"
        )

    def test_empty_string(self) -> None:
        assert parse_www_authenticate_error("") is None

    def test_error_inside_realm_value(self) -> None:
        """``Bearer realm="my error=fake", error="real"`` must return ``"real"``.

        Naive parser grabbed ``fake`` from inside the ``realm`` quoted
        value.
        """
        header = 'Bearer realm="my error=fake", error="real"'
        assert parse_www_authenticate_error(header) == "real"


class TestBearerDict:
    def test_returns_lowercased_keys(self) -> None:
        header = 'Bearer Realm="x", Error="y", Scope="a b"'
        params = parse_www_authenticate_bearer(header)
        assert params == {"realm": "x", "error": "y", "scope": "a b"}

    def test_non_bearer_scheme_returns_empty(self) -> None:
        assert parse_www_authenticate_bearer('Basic realm="x"') == {}

    def test_no_scheme(self) -> None:
        assert parse_www_authenticate_bearer('realm="x"') == {}

    def test_bearer_only_no_params(self) -> None:
        assert parse_www_authenticate_bearer("Bearer ") == {}

    def test_bearer_with_no_space_returns_empty(self) -> None:
        # ``BearerToken`` is not a Bearer challenge (no separator).
        assert parse_www_authenticate_bearer("BearerToken") == {}

    def test_first_value_wins_on_duplicate(self) -> None:
        # If a malformed AS sends two ``scope=`` params we keep the first.
        # The earlier ``find()``-based scanner would have returned the
        # last; either choice is legal for malformed input but we need
        # to be consistent.
        header = 'Bearer scope="first", scope="second"'
        assert parse_www_authenticate_bearer(header) == {"scope": "first"}

    def test_trailing_comma(self) -> None:
        header = 'Bearer error="x",'
        assert parse_www_authenticate_bearer(header) == {"error": "x"}

    def test_multiple_commas(self) -> None:
        header = 'Bearer ,, error="x",,, scope="y"'
        assert parse_www_authenticate_bearer(header) == {"error": "x", "scope": "y"}

    def test_embedded_escaped_quote(self) -> None:
        header = r'Bearer realm="he said \"hi\""'
        assert parse_www_authenticate_bearer(header) == {"realm": 'he said "hi"'}

    def test_param_without_value_skipped(self) -> None:
        header = 'Bearer realm, error="x"'
        # ``realm`` without ``=`` is dropped; ``error`` survives.
        assert parse_www_authenticate_bearer(header) == {"error": "x"}

    @pytest.mark.parametrize(
        "header,expected",
        [
            ("", {}),
            ("Bearer", {}),
            ('Bearer realm=""', {"realm": ""}),
            ('Bearer realm="", scope=""', {"realm": "", "scope": ""}),
        ],
    )
    def test_edge_cases(self, header: str, expected: dict[str, str]) -> None:
        assert parse_www_authenticate_bearer(header) == expected


class TestPathologicalInput:
    def test_oversized_pathological_input_rejected_under_50ms(self) -> None:
        """Headers longer than the defensive cap return ``{}`` immediately.

        The cap is set to 4096 bytes — real ASes emit a few hundred bytes
        at most. This guards both ``parse_www_authenticate_bearer``
        callers against pathological input from a misbehaving server.
        The previous ``header.lower().find("scope", i)`` loop was
        O(N**2) — a 100 KB header with no ``=`` took ~330 ms because
        each ``find`` rescanned the entire suffix. The single-pass
        tokenizer (capped at 4 KB) reduces this to a one-shot length
        check that returns ``{}`` in microseconds, so the budget is
        generous regardless of which side of the cap was hit.
        """
        big = "Bearer scope=" + "a" * 10_000
        start = time.perf_counter()
        result = parse_www_authenticate_scope(big)
        elapsed = time.perf_counter() - start
        assert result == ()
        assert elapsed < 0.05, f"oversized-header reject took {elapsed * 1000:.1f}ms"

    def test_within_cap_long_header_under_50ms(self) -> None:
        """A 4 KB header with thousands of ``find`` candidates still parses fast.

        Stays under the cap so the tokenizer actually runs end to end —
        the goal is to prove the inner loop is O(N), not just that the
        cap rejects oversized input.
        """
        # Pack the header right up to the cap with non-matching
        # auth-params, then put the real ``scope`` at the end.
        filler_parts = []
        size = len("Bearer ")
        i = 0
        while size < 3900:
            part = f'xscope{i}="ignore", '
            if size + len(part) > 3900:
                break
            filler_parts.append(part)
            size += len(part)
            i += 1
        header = "Bearer " + "".join(filler_parts) + 'scope="real"'
        assert len(header) <= 4096
        start = time.perf_counter()
        result = parse_www_authenticate_scope(header)
        elapsed = time.perf_counter() - start
        assert result == ("real",)
        assert elapsed < 0.05, f"4kb tokenize took {elapsed * 1000:.1f}ms"
