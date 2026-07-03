"""Unit tests for ``_substitute_skill_args`` — SKILL.md spec placeholder
substitution applied to skill bodies at load time.

Covers every placeholder form Turnstone implements — including the
``${TURNSTONE_*}`` canonical env vars and the ``${CLAUDE_SESSION_ID}`` /
``${CLAUDE_EFFORT}`` back-compat aliases (there is deliberately NO
``CLAUDE_SKILL_DIR`` alias), ``${TURNSTONE_SKILL_DIR}`` resolution when the
caller supplies a materialized bundle path, plus the spec's "append
ARGUMENTS at end if no placeholder" rule and the single-pass guarantee
against re-expansion of user-supplied values that happen to contain
placeholder syntax.
"""

from __future__ import annotations

from turnstone.core.session import _substitute_skill_args


def _sub(content: str, *, args: str = "", names: list[str] | None = None) -> str:
    """Compact test helper — defaults env values to fixed sentinels."""
    return _substitute_skill_args(
        content,
        arguments_str=args,
        arg_names=names or [],
        ws_id="ws-abc",
        effort="high",
    )


class TestArgumentsLiteral:
    def test_full_args_expands(self) -> None:
        assert _sub("Run $ARGUMENTS now", args="alpha bravo") == "Run alpha bravo now"

    def test_empty_args_no_placeholder_unchanged(self) -> None:
        assert _sub("Hello world", args="") == "Hello world"

    def test_empty_args_with_placeholder_substitutes_empty(self) -> None:
        # $ARGUMENTS with no args present → empty string (placeholder cleared).
        assert _sub("Prefix $ARGUMENTS suffix", args="") == "Prefix  suffix"

    def test_append_when_args_present_but_no_placeholder(self) -> None:
        """Spec: args passed + body has no $ARGUMENTS → append at end."""
        out = _sub("Skill body without placeholder.", args="x y")
        assert out.endswith("\n\nARGUMENTS: x y")
        assert out.startswith("Skill body without placeholder.")

    def test_no_append_when_args_present_and_placeholder_used(self) -> None:
        out = _sub("Run $ARGUMENTS.", args="x y")
        assert out == "Run x y."
        # Critical: no trailing append, no double-rendering.
        assert "ARGUMENTS:" not in out.removeprefix("Run ")

    def test_indexed_form_does_not_count_as_literal(self) -> None:
        """``$ARGUMENTS[0]`` is a different placeholder; if it's the only
        form in the body and args were passed, the append-at-end rule
        still fires because the BARE ``$ARGUMENTS`` literal is absent."""
        out = _sub("First: $ARGUMENTS[0]", args="a b")
        assert "First: a" in out
        assert out.endswith("\n\nARGUMENTS: a b")


class TestPositional:
    """Positional substitution.  Bodies in this group don't use the bare
    ``$ARGUMENTS`` placeholder, so the spec's "append at end" rule fires —
    tests assert ``startswith`` on the substituted prefix rather than full
    equality to keep the focus on the substitution itself."""

    def test_short_form(self) -> None:
        assert _sub("$0 then $1", args="alpha bravo").startswith("alpha then bravo")

    def test_bracketed_form(self) -> None:
        out = _sub("$ARGUMENTS[0] then $ARGUMENTS[1]", args="alpha bravo")
        assert out.startswith("alpha then bravo")

    def test_shell_quoted_input(self) -> None:
        """Spec: ``"hello world" second`` parses via shlex so $0='hello world'."""
        out = _sub("$0 / $1", args='"hello world" second')
        assert out.startswith("hello world / second")

    def test_out_of_range_substitutes_empty(self) -> None:
        out = _sub("$0 $5", args="only-one")
        assert out.startswith("only-one ")  # second placeholder → empty

    def test_unbalanced_quotes_falls_back_to_whitespace_split(self) -> None:
        """A typo (unmatched quote) shouldn't blow up the substitution —
        fall back to whitespace split so the prompt still renders.  The
        fallback split on whitespace gives ``['alpha', '"bravo']``."""
        out = _sub("$0 $1", args='alpha "bravo')
        assert out.startswith('alpha "bravo')


class TestNamedArguments:
    def test_named_arg_substitutes_by_position(self) -> None:
        out = _sub("issue $issue branch $branch", args="123 main", names=["issue", "branch"])
        assert out.startswith("issue 123 branch main")

    def test_unknown_name_left_as_literal(self) -> None:
        """``$foo`` with ``foo`` not in arg_names stays as ``$foo`` —
        forgiving behaviour matches ``_render_template``."""
        out = _sub("$known $unknown", args="x y", names=["known"])
        assert out.startswith("x $unknown")

    def test_known_name_with_missing_positional_substitutes_empty(self) -> None:
        """Named arg whose position is past the end of supplied args → ``""``.
        No args passed so no append-at-end either."""
        assert _sub("got $name", args="", names=["name"]) == "got "

    def test_arguments_uppercase_not_matched_as_named(self) -> None:
        """``$ARGUMENTS`` must not be matched by the named-arg regex —
        the bare ``$ARGUMENTS`` alternative in the combined regex sits
        earlier in the precedence chain.  Pin so a future regex tweak
        can't break this."""
        # No args, no names → bare $ARGUMENTS substitutes to empty
        # via the literal branch, not via the named-arg branch.
        assert _sub("$ARGUMENTS", args="", names=[]) == ""

    def test_uppercase_name_substitutes(self) -> None:
        """The broadened named-arg regex accepts uppercase identifiers.
        Pin so a SKILL.md author who declares ``arguments: [USER_ID]``
        and references ``$USER_ID`` gets the substitution, not a
        literal."""
        out = _sub("user $USER_ID", args="alice", names=["USER_ID"])
        assert out.startswith("user alice")

    def test_underscore_prefix_name_substitutes(self) -> None:
        """Identifier names starting with ``_`` are valid Python
        identifiers; the broadened regex matches them."""
        out = _sub("got $_internal", args="value", names=["_internal"])
        assert out.startswith("got value")


class TestEnvironment:
    def test_session_id_substitutes(self) -> None:
        assert _sub("session ${CLAUDE_SESSION_ID}") == "session ws-abc"

    def test_effort_substitutes(self) -> None:
        assert _sub("effort ${CLAUDE_EFFORT}") == "effort high"

    def test_unknown_env_left_as_literal(self) -> None:
        assert _sub("${CLAUDE_UNKNOWN_FOO}") == "${CLAUDE_UNKNOWN_FOO}"


class TestEnvironmentAliases:
    """``TURNSTONE_*`` is the canonical, vendor-neutral spelling;
    ``CLAUDE_*`` is a permanent back-compat alias so skills imported from
    Claude Code / skills.sh keep resolving.  Both map to one value."""

    def test_turnstone_session_id(self) -> None:
        assert _sub("session ${TURNSTONE_SESSION_ID}") == "session ws-abc"

    def test_turnstone_effort(self) -> None:
        assert _sub("effort ${TURNSTONE_EFFORT}") == "effort high"

    def test_canonical_and_alias_agree(self) -> None:
        assert _sub("${CLAUDE_SESSION_ID}") == _sub("${TURNSTONE_SESSION_ID}") == "ws-abc"
        assert _sub("${CLAUDE_EFFORT}") == _sub("${TURNSTONE_EFFORT}") == "high"

    def test_unknown_turnstone_var_left_as_literal(self) -> None:
        assert _sub("${TURNSTONE_UNKNOWN_FOO}") == "${TURNSTONE_UNKNOWN_FOO}"


class TestSkillDir:
    """``${TURNSTONE_SKILL_DIR}`` resolves to the materialized bundle path when
    the caller supplies one (it materializes resources BEFORE substituting) and
    degrades to a literal placeholder when the skill bundles no resources.
    ``${CLAUDE_SKILL_DIR}`` is NOT a turnstone alias — it always stays literal
    (that name is the host's in bash; turnstone claims neither surface)."""

    def test_turnstone_skill_dir_resolves(self) -> None:
        out = _substitute_skill_args(
            "cd ${TURNSTONE_SKILL_DIR}/scripts",
            arguments_str="",
            arg_names=[],
            ws_id="ws-abc",
            effort="high",
            skill_dir="/tmp/skill-xyz",
        )
        assert out == "cd /tmp/skill-xyz/scripts"

    def test_claude_skill_dir_not_aliased(self) -> None:
        # Even with a materialized bundle, ${CLAUDE_SKILL_DIR} is left literal:
        # turnstone owns TURNSTONE_SKILL_DIR only, so the two never diverge from
        # the bash env (which likewise never sets CLAUDE_SKILL_DIR).
        out = _substitute_skill_args(
            "cd ${CLAUDE_SKILL_DIR}",
            arguments_str="",
            arg_names=[],
            ws_id="ws-abc",
            effort="high",
            skill_dir="/tmp/skill-xyz",
        )
        assert out == "cd ${CLAUDE_SKILL_DIR}"

    def test_skill_dir_left_literal_when_unset(self) -> None:
        # Default skill_dir="" → placeholder stays literal (graceful),
        # not an empty path.
        assert _sub("${TURNSTONE_SKILL_DIR}") == "${TURNSTONE_SKILL_DIR}"
        assert _sub("${CLAUDE_SKILL_DIR}") == "${CLAUDE_SKILL_DIR}"


class TestSinglePassGuarantee:
    """A placeholder VALUE containing another placeholder must not be
    re-expanded — matches spec's "Substitution runs once" rule."""

    def test_arg_value_containing_placeholder_not_reexpanded(self) -> None:
        # $0 value is the literal string "$1"; the rendered body should
        # contain "$1" verbatim, not the substituted value of $1.  Append
        # rule fires because the body has no bare ``$ARGUMENTS`` literal —
        # split the output to isolate the body from the appended echo.
        out = _sub("$0", args='"$1" actual')
        body, _, _appended = out.partition("\n\nARGUMENTS: ")
        # Body contains "$1" once — substituted in from $0 → "$1",
        # NOT re-expanded to "actual".
        assert body == "$1"

    def test_arg_value_containing_dollar_arguments_not_reexpanded(self) -> None:
        # $0 = "$ARGUMENTS" — would loop without single-pass.
        out = _sub("got $0", args='"$ARGUMENTS"')
        assert out.startswith("got $ARGUMENTS")
        # The "$ARGUMENTS" inside the value MUST NOT be re-substituted
        # into the args string.  Append-at-end rule adds a trailing
        # "ARGUMENTS: $ARGUMENTS" line — that's an as-typed echo, not a
        # re-substitution.
        assert "got $ARGUMENTS\n\nARGUMENTS:" in out


class TestIntegration:
    def test_all_forms_in_one_body(self) -> None:
        body = (
            "Session ${CLAUDE_SESSION_ID} at effort ${CLAUDE_EFFORT}.\n"
            "First $0, second $1.\n"
            "Named: $issue resolved on $branch.\n"
            "Full: $ARGUMENTS"
        )
        out = _sub(body, args="123 main", names=["issue", "branch"])
        assert out == (
            "Session ws-abc at effort high.\n"
            "First 123, second main.\n"
            "Named: 123 resolved on main.\n"
            "Full: 123 main"
        )


class TestSubstituteArgsToggle:
    """``substitute_args=False`` (capability contexts: defaults, task_agent)
    leaves every invocation-arg form LITERAL while still resolving env vars,
    so literal ``$1`` / ``$ARGUMENTS`` prose or shell text isn't blanked."""

    def test_arg_forms_left_literal(self) -> None:
        out = _substitute_skill_args(
            "run $0 $1 $ARGUMENTS $ARGUMENTS[2] $named",
            arguments_str="a b c",  # present, but ignored under substitute_args=False
            arg_names=["named"],
            ws_id="ws-abc",
            effort="high",
            substitute_args=False,
        )
        assert out == "run $0 $1 $ARGUMENTS $ARGUMENTS[2] $named"

    def test_env_still_resolves(self) -> None:
        out = _substitute_skill_args(
            "id ${TURNSTONE_SESSION_ID} at ${TURNSTONE_EFFORT} in ${TURNSTONE_SKILL_DIR}",
            arguments_str="",
            arg_names=[],
            ws_id="ws-abc",
            effort="high",
            skill_dir="/tmp/skill-x",
            substitute_args=False,
        )
        assert out == "id ws-abc at high in /tmp/skill-x"

    def test_no_append_when_args_disabled(self) -> None:
        # The append-ARGUMENTS-at-end rule must not fire when arg substitution
        # is off, even if arguments_str is non-empty.
        out = _substitute_skill_args(
            "body with no placeholder",
            arguments_str="x y",
            arg_names=[],
            ws_id="ws-abc",
            effort="high",
            substitute_args=False,
        )
        assert out == "body with no placeholder"
