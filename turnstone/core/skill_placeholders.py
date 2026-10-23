"""Placeholders a skill's text can carry, and the rendering that fills them in.

``{{variable}}`` template variables, and the SKILL.md spec forms
(``$ARGUMENTS``, ``$ARGUMENTS[N]``, ``$N``, ``$name``, ``${TURNSTONE_*}`` and
``${CLAUDE_*}``).  The session renders a skill's text with these; the risk scan
reads the text as it renders when no placeholder has a value.
"""

from __future__ import annotations

import re
import shlex

TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")

# SKILL.md spec placeholders.  Single combined
# regex so substitution is one-pass — a positional arg whose VALUE
# happens to contain ``$ARGUMENTS`` (etc.) doesn't get re-expanded
# on a second sweep.  The verbose form keeps the alternation
# readable; precedence is left-to-right, so the bracketed
# ``$ARGUMENTS[N]`` form is tried before the bare ``$ARGUMENTS``.
SPEC_PLACEHOLDER_RE = re.compile(
    r"""
    \$ARGUMENTS\[(?P<idx_bracket>\d+)\]            # $ARGUMENTS[N]
    | \$ARGUMENTS\b(?!\[)                             # $ARGUMENTS (bare)
    | \$\{(?P<env>(?:CLAUDE|TURNSTONE)_[A-Z_]+)\}     # ${TURNSTONE_*} / ${CLAUDE_*}
    | \$(?P<idx_short>\d+)\b                          # $N
    | \$(?P<named>[A-Za-z_][A-Za-z0-9_]*)\b           # $name
    """,
    re.VERBOSE,
)

# Detector for "did the original body include the bare ``$ARGUMENTS``
# placeholder?" — used to decide whether to append ``ARGUMENTS: ...``
# at the end per spec when the user passed args.  Negative lookahead
# excludes the ``$ARGUMENTS[N]`` form which is a different placeholder.
_SPEC_ARGUMENTS_LITERAL_RE = re.compile(r"\$ARGUMENTS\b(?!\[)")


def render_template(content: str, context: dict[str, str]) -> str:
    """Replace ``{{variable}}`` placeholders in a single pass.

    Unresolvable placeholders are kept as-is.  Single-pass avoids
    cross-variable injection (e.g. a model name containing ``{{ws_id}}``).
    """

    def _replace(m: re.Match[str]) -> str:
        return context.get(m.group(1), m.group(0))

    return TEMPLATE_VAR_RE.sub(_replace, content)


def substitute_skill_args(
    content: str,
    *,
    arguments_str: str,
    arg_names: list[str],
    ws_id: str,
    effort: str,
    skill_dir: str = "",
    substitute_args: bool = True,
) -> str:
    """Apply SKILL.md spec placeholder substitution.

    When *substitute_args* is False the invocation-arg forms
    (``$ARGUMENTS`` / ``$ARGUMENTS[N]`` / ``$N`` / ``$<name>``) are left
    LITERAL and only the ``${…}`` env vars resolve — for capability
    contexts (``task_agent``) that are never invoked with args,
    where a literal ``$1`` or ``$ARGUMENTS`` in the body is prose or shell
    text, not a placeholder to blank.

    Handles every spec placeholder form:

    * ``$ARGUMENTS`` — full argument string as the user typed it
    * ``$ARGUMENTS[N]`` / ``$N`` — Nth positional arg (0-indexed),
      shell-quoted at parse time so ``"hello world" second`` yields
      ``$0='hello world'``, ``$1='second'``
    * ``$<name>`` — named arg from the SKILL.md ``arguments:``
      frontmatter list, paired with the positional arg at the same
      index
    * ``${TURNSTONE_SESSION_ID}`` / ``${CLAUDE_SESSION_ID}`` — current
      workstream id.  ``TURNSTONE_`` is canonical (Turnstone is
      multi-provider); the ``CLAUDE_`` spelling is a permanent
      back-compat alias so imported skills written with it keep
      resolving.  Both map to the same value.
    * ``${TURNSTONE_EFFORT}`` / ``${CLAUDE_EFFORT}`` — reasoning_effort
    * ``${TURNSTONE_SKILL_DIR}`` — absolute path of the materialized
      resource bundle, resolved when *skill_dir* is supplied (the caller
      materializes resources BEFORE substituting) and left literal
      otherwise, so a skill that bundles no resources degrades gracefully
      rather than emitting an empty path.  Unlike the session/effort vars
      there is deliberately NO ``CLAUDE_SKILL_DIR`` alias: that name also
      lives in bash, where a turnstone node running inside another agent
      host must not shadow the host's value — so turnstone claims it in
      neither surface and leaves the foreign name entirely to the host.

    Single-pass: a value containing a placeholder token (e.g.
    ``$0='$ARGUMENTS'``) does not get re-expanded.  Matches the
    spec's "Substitution runs once over the original file" rule.

    Spec rule: when *arguments_str* is non-empty and the body has
    no bare ``$ARGUMENTS`` placeholder, the full string is appended
    at the end as ``ARGUMENTS: ...`` so the model still sees what
    the user typed.  Indexed forms (``$ARGUMENTS[N]``) don't count
    as the bare placeholder for this purpose.

    Unresolvable placeholders (e.g. ``$unknown`` when ``unknown``
    isn't in *arg_names*, or ``${CLAUDE_FOO}`` when ``CLAUDE_FOO``
    isn't a known env key) are left as literals — matches the
    forgiving behaviour of :func:`render_template`.
    """
    try:
        positional = shlex.split(arguments_str) if arguments_str else []
    except ValueError:
        # Unbalanced quotes — fall back to whitespace split so a
        # typo in user-supplied args doesn't make the entire
        # substitution a no-op (and silently leave ``$ARGUMENTS``
        # placeholders in the prompt).
        positional = arguments_str.split() if arguments_str else []

    name_to_idx = {name: i for i, name in enumerate(arg_names)}
    # ``TURNSTONE_*`` canonical + ``CLAUDE_*`` back-compat alias, same value.
    env = {
        "TURNSTONE_SESSION_ID": ws_id,
        "CLAUDE_SESSION_ID": ws_id,
        "TURNSTONE_EFFORT": effort,
        "CLAUDE_EFFORT": effort,
    }
    if skill_dir:
        # TURNSTONE_SKILL_DIR only — NO CLAUDE_SKILL_DIR alias.  That name is
        # the host's in bash (see _skill_resource_env), so turnstone claims it
        # in neither surface to avoid a prompt-vs-bash divergence.
        env["TURNSTONE_SKILL_DIR"] = skill_dir

    def _at(idx_str: str) -> str:
        idx = int(idx_str)  # regex guarantees digits
        return positional[idx] if 0 <= idx < len(positional) else ""

    def _replace(m: re.Match[str]) -> str:
        if m.group("env") is not None:
            return env.get(m.group("env"), m.group(0))
        if not substitute_args:
            # Capability contexts never receive invocation args, so a literal
            # $ARGUMENTS/$N/$name is prose or shell text — leave it untouched
            # rather than blanking it.  Only the env vars above resolve.
            return m.group(0)
        if m.group("idx_bracket") is not None:
            return _at(m.group("idx_bracket"))
        if m.group("idx_short") is not None:
            return _at(m.group("idx_short"))
        if m.group("named") is not None:
            name = m.group("named")
            idx = name_to_idx.get(name)
            if idx is None:
                return m.group(0)
            return positional[idx] if idx < len(positional) else ""
        # Match is the bare $ARGUMENTS literal (no named groups).
        return arguments_str or ""

    rendered = SPEC_PLACEHOLDER_RE.sub(_replace, content)

    # The spec's append-ARGUMENTS-at-end rule only applies when args are being
    # substituted at all — capability contexts (substitute_args=False) ignore
    # invocation args entirely, so they must not append them either.  The
    # literal-$ARGUMENTS scan is only consumed on that path, so defer it behind
    # the guard rather than scanning every task_agent capability body.
    if substitute_args and arguments_str:
        had_literal_arguments = bool(_SPEC_ARGUMENTS_LITERAL_RE.search(content))
        if not had_literal_arguments:
            rendered = f"{rendered}\n\nARGUMENTS: {arguments_str}"

    return rendered


def unfilled_readings(text: str) -> list[str]:
    """The texts *text* can render as when no placeholder has a value, other than itself.

    A placeholder that renders as nothing joins the text on either side of it,
    so a word split by one is whole in what the model reads.  Two readings
    cover this: a load without arguments, rendered with every ``{{variable}}``
    empty and every ``$name`` kept as an undeclared one is; and the text with
    every placeholder removed, as a declared ``$name`` renders with no argument
    and as a shell expands an unset variable (``|$x bash`` pipes into bash).
    """
    no_arguments = substitute_skill_args(
        TEMPLATE_VAR_RE.sub("", text), arguments_str="", arg_names=[], ws_id="", effort=""
    )
    blanked = SPEC_PLACEHOLDER_RE.sub("", TEMPLATE_VAR_RE.sub("", text))
    return [reading for reading in dict.fromkeys((no_arguments, blanked)) if reading != text]
