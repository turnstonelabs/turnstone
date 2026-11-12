"""Tool definitions — auto-loaded from turnstone/tools/*.json."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
_META_KEYS = {
    "task_agent",
    "coordinator",
    "interactive",
    "auto_approve",
    "primary_key",
    # Per-kind variant overrides.  Schema:
    #   "kind_variants": {
    #       "<kind>": {
    #           "description": "kind-specific description",
    #           "parameter_overrides": {
    #               "<param-name>": {... partial JSON-Schema overlay ...}
    #           }
    #       }
    #   }
    # ``description`` REPLACES the base description for the kind; each
    # entry in ``parameter_overrides`` is dict-merged onto the matching
    # ``parameters.properties.<param>`` entry so a ``scope`` enum can
    # be narrowed per-kind without re-stating the rest of the param
    # schema.  See ``memory.json`` for the canonical example.
    "kind_variants",
    # Working-directory note templates, rendered into the description at
    # session tool-list build time by ``apply_cwd_context``.  Declared by
    # tools whose semantics depend on the process cwd (shell execution,
    # relative-path resolution).  See ``bash.json`` for the canonical
    # example.
    "cwd_note",
    "workspace_note",
    # True when every effect of the tool goes through the session's execution
    # environment (``exec_env.ExecutionEnvironment``): the shell and file
    # tools.  A session whose environment is not this host can offer exactly
    # these and know that nothing the model does lands anywhere else.  A tool
    # that also reaches the network, the stores or other people must not
    # declare it.
    "environment",
}


def _load_tools() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load all .json files from the tools directory.

    Returns (tool_defs, metadata) where:
      - tool_defs: list of OpenAI function-calling dicts
      - metadata: dict mapping tool_name -> {task_agent, coordinator,
                  interactive, auto_approve, primary_key, kind_variants,
                  environment}
    """
    tools = []
    meta = {}
    for path in sorted(_TOOLS_DIR.glob("*.json")):
        with open(path) as f:
            raw = json.load(f)
        name = raw["name"]
        # Extract turnstone metadata, leave only OpenAI schema fields
        tool_meta = {k: raw.pop(k) for k in list(raw) if k in _META_KEYS}
        meta[name] = tool_meta
        tools.append({"type": "function", "function": raw})
    return tools, meta


def _apply_kind_variant(tool: dict[str, Any], kind: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Return a kind-specific copy of ``tool`` with description / params overridden.

    Each kind sees only the surface it can actually use — for ``memory``,
    coord sessions get ``coordinator`` plus the attach-dependent ``project``
    scope, while interactive sessions get global/workstream/user plus
    ``project``.  This keeps the LLM contract tight: the model never sees enum
    values it can't use, and never reads description sentences explaining why
    a scope is forbidden.

    No-op (returns the input tool unchanged) when the tool has no
    ``kind_variants`` metadata or no entry for ``kind``.  Otherwise
    deep-copies the tool's function dict so the per-kind list doesn't
    share mutable state with the union ``TOOLS`` list or the other
    kind's list.
    """
    variants = meta.get("kind_variants") or {}
    variant = variants.get(kind)
    if not variant:
        return tool
    new_tool = copy.deepcopy(tool)
    if "description" in variant:
        new_tool["function"]["description"] = variant["description"]
    overrides = variant.get("parameter_overrides") or {}
    if overrides:
        props = new_tool["function"].get("parameters", {}).get("properties", {})
        for param_name, overlay in overrides.items():
            if param_name in props and isinstance(overlay, dict):
                props[param_name].update(overlay)
    return new_tool


TOOLS, _META = _load_tools()

TASK_AGENT_TOOLS = [t for t in TOOLS if _META[t["function"]["name"]].get("task_agent")]
# COORDINATOR_TOOLS apply the ``coordinator`` kind variant (if any) so a
# coord session sees the coord-tailored description + parameter schema.
COORDINATOR_TOOLS = [
    _apply_kind_variant(t, "coordinator", _META[t["function"]["name"]])
    for t in TOOLS
    if _META[t["function"]["name"]].get("coordinator")
]
# Interactive sessions — the default session kind — must NOT see coordinator-only
# tools (``spawn_workstream`` et al.) in their tool set.  Coordinator-only tools
# require a ``coord_client`` that only console-hosted coordinator sessions
# have, and exposing them to interactive sessions also pollutes the
# tool-search threshold count.
#
# A tool can opt INTO both kinds with ``"interactive": true`` alongside
# ``"coordinator": true`` — used for tools whose behaviour makes sense in
# both contexts (e.g. ``memory``).  Dual-kind tools also apply the
# ``interactive`` kind variant when present so the IC-flavored
# description / param schema replaces the union default.  Without the
# explicit opt-in, ``"coordinator": true`` is read as "coord-only" and
# the tool is stripped from interactive sessions.  ``TOOLS`` stays as
# the union for introspection / schema docs / eval catalogs.
INTERACTIVE_TOOLS = [
    _apply_kind_variant(t, "interactive", _META[t["function"]["name"]])
    for t in TOOLS
    if (
        not _META[t["function"]["name"]].get("coordinator")
        or _META[t["function"]["name"]].get("interactive")
    )
]
INTERACTIVE_TOOL_NAMES = frozenset(t["function"]["name"] for t in INTERACTIVE_TOOLS)
# Tools that act only through the session's execution environment.
ENVIRONMENT_TOOL_NAMES = frozenset(n for n, m in _META.items() if m.get("environment"))
TASK_AUTO_TOOLS = {n for n, m in _META.items() if m.get("auto_approve")}
PRIMARY_KEY_MAP = {n: m["primary_key"] for n, m in _META.items() if "primary_key" in m}


# Keys a malformed call's arguments are searched for: primary and identifying
# ones, so the model sees a minimal salvaged call and resubmits with correct
# JSON.  Coordinator keys (ws_id, message, initial_message, parent_ws_id) keep a
# malformed coordinator call from being a dead end.
_SALVAGE_KEYS = (
    "action",
    "command",
    "code",
    "content",
    "initial_message",
    "message",
    "name",
    "page",
    "parent_ws_id",
    "path",
    "pattern",
    "prompt",
    "query",
    "status",
    "task_id",
    "title",
    "uri",
    "url",
    "ws_id",
)


# How deeply a tool call's parsed arguments may nest objects and lists. JSON this deep still parses
# (to about 10,000 levels on Python 3.13), but code that walks it one level per call does not: an
# MCP call's serializer stops at 255 levels, and Python's recursion limit is about 1,000.
MAX_TOOL_ARGUMENT_DEPTH = 100


def nested_deeper_than(value: Any, limit: int) -> bool:
    """Whether *value*, parsed JSON, nests objects and lists more than *limit* levels deep.

    Walked without recursion, so a value too deep for recursive code is measured safely.
    """
    if not isinstance(value, (dict, list)):
        return False
    stack: list[tuple[dict[Any, Any] | list[Any], int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            return True
        children = node.values() if isinstance(node, dict) else node
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return False


def salvage_tool_arguments(func_name: str, raw_args: str) -> dict[str, Any] | None:
    """Arguments recovered from *raw_args* that are not valid JSON, else ``None``.

    First the string value of a known key (:data:`_SALVAGE_KEYS`), then a bare
    string with no JSON wrapper, taken as the tool's primary key
    (:data:`PRIMARY_KEY_MAP`).
    """
    for key in _SALVAGE_KEYS:
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args)
        if m:
            try:
                val = json.loads('"' + m.group(1) + '"')
            except (json.JSONDecodeError, Exception):
                val = m.group(1)
            return {key: val}
    if raw_args.strip() and not raw_args.strip().startswith("{"):
        pk = PRIMARY_KEY_MAP.get(func_name)
        if pk:
            return {pk: raw_args}
    return None


BUILTIN_TOOL_NAMES = frozenset(_META)


def apply_cwd_context(
    tools: list[dict[str, Any]], working_dir: str, workspace_dir: str
) -> list[dict[str, Any]]:
    """Append rendered working-dir/workspace notes to tools that declare them.

    Tools opt in via ``cwd_note`` / ``workspace_note`` metadata in their JSON
    (stripped from the wire schema by ``_load_tools``): authored sentences
    carrying ``{working_dir}`` / ``{workspace_dir}`` placeholders, appended
    to the tool's description with the placeholder substituted.  Substitution
    is plain ``str.replace``, NOT ``str.format`` — note prose may contain
    literal braces (``${VAR}``) that format() would choke on.  A note whose
    value is empty is skipped entirely, so callers pass ``""`` to drop a fact
    (unresolvable cwd, absent workspace) without conditional template syntax.

    Returns a NEW list.  Noted tools are deep-copied first: the fs tool dicts
    are SHARED between ``TOOLS`` / ``INTERACTIVE_TOOLS`` / ``TASK_AGENT_TOOLS``
    and aliased through ``merge_mcp_tools`` output, so an in-place append
    would corrupt the module constants for every session.  Tools without
    notes (MCP, coordinator) pass through by reference.  NOT idempotent —
    always call on a pristine base list, never on prior output.
    """
    if not (working_dir or workspace_dir):
        return list(tools)
    out: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") or {}
        meta = _META.get(fn.get("name", "")) or {}
        notes: list[str] = []
        cwd_note = meta.get("cwd_note", "")
        if cwd_note and working_dir:
            notes.append(cwd_note.replace("{working_dir}", working_dir))
        workspace_note = meta.get("workspace_note", "")
        if workspace_note and workspace_dir:
            notes.append(workspace_note.replace("{workspace_dir}", workspace_dir))
        if notes:
            tool = copy.deepcopy(tool)
            fn = tool["function"]
            fn["description"] = " ".join([fn.get("description", "").rstrip(), *notes]).strip()
        out.append(tool)
    return out


# Spellings of "no value" that arrive as text: a model writes Python's or
# JSON's null, and a server that parses tool calls from text hands it on as a
# string when it does not recognise it.
_NULL_SPELLINGS = frozenset({"None", "null"})


# How many levels of ``anyOf`` / ``oneOf`` a parameter schema is followed into.  A schema
# is whatever a server sent: one that nests its branches deeper than any real one, or
# holds itself, must not take the reading of it past the interpreter's recursion limit.
_MAX_SCHEMA_BRANCH_DEPTH = 32


def _schema_admits_string(schema: Any, depth: int = 0) -> bool:
    """Whether a parameter schema could mean a string value.

    Anything unclear counts as admitting one: an untyped parameter, a schema
    that is not an object, an ``enum`` that lists strings, a branch of
    ``anyOf`` / ``oneOf`` that admits one, or branches nested deeper than
    they are followed.
    """
    if not isinstance(schema, dict) or depth > _MAX_SCHEMA_BRANCH_DEPTH:
        return True
    enum = schema.get("enum")
    if isinstance(enum, list):
        # The listed values are all there is.
        return any(isinstance(v, str) for v in enum)
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            return any(_schema_admits_string(branch, depth + 1) for branch in branches)
    kind = schema.get("type")
    if kind is None:
        return True
    kinds = kind if isinstance(kind, list) else [kind]
    return "string" in kinds


def _schema_admits_null(schema: Any, depth: int = 0) -> bool:
    """Whether a parameter schema says the value may be null."""
    if not isinstance(schema, dict) or depth > _MAX_SCHEMA_BRANCH_DEPTH:
        return False
    if schema.get("nullable") is True:
        return True
    enum = schema.get("enum")
    if isinstance(enum, list) and any(v is None for v in enum):
        return True
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and any(
            _schema_admits_null(branch, depth + 1) for branch in branches
        ):
            return True
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    return "null" in kinds


# What becomes of an argument whose value is a null spelled as text.
NULL_SPELLING_TO_NULL = "null"
NULL_SPELLING_NOT_GIVEN = "not_given"


def null_spelled_arguments(args: dict[str, Any], tool: dict[str, Any] | None) -> dict[str, str]:
    """Top-level arguments of *tool* that hold a null spelled as text, and what
    each should become.

    A model that means "no value" may write ``None`` or ``null`` for it, and a
    server that parses tool calls out of generated text hands an argument it
    does not recognise on as a string.  For a value that is exactly one of the
    two spellings, the parameter's schema decides:

    - it may be a string: the text may be the value, and stays (not listed);
    - it allows null: the argument becomes null (``NULL_SPELLING_TO_NULL``);
    - it is optional, not a string and does not allow null: the argument counts
      as not given (``NULL_SPELLING_NOT_GIVEN``), which is what the model meant
      and what the schema has a place for;
    - it is required, not a string and does not allow null: the value stays, so
      that the tool's own validation tells the model (not listed).

    A parameter the tool's schema does not describe keeps its value.  *tool* is
    a tool definition in function-calling form, ``None`` when the tool is
    unknown.

    The schema is whatever the tool's server sent, and this is asked for every
    call of the tool, so it never raises over one: a part of the schema that
    is not what a schema holds there says nothing.
    """
    function = tool.get("function") if isinstance(tool, dict) else None
    parameters = function.get("parameters") if isinstance(function, dict) else None
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    if not isinstance(parameters, dict) or not isinstance(properties, dict):
        return {}
    required = parameters.get("required")
    # Only a string names a parameter.  Anything else in the list names none, also
    # what could not be put in a set (a list, an object).
    required_names = (
        {name for name in required if isinstance(name, str)}
        if isinstance(required, list)
        else set()
    )
    found: dict[str, str] = {}
    for name, value in args.items():
        if not isinstance(value, str) or value not in _NULL_SPELLINGS or name not in properties:
            continue
        schema = properties[name]
        if _schema_admits_string(schema):
            continue
        if _schema_admits_null(schema):
            found[name] = NULL_SPELLING_TO_NULL
        elif name not in required_names:
            found[name] = NULL_SPELLING_NOT_GIVEN
    return found


def apply_null_spellings(args: dict[str, Any], tool: dict[str, Any] | None) -> dict[str, str]:
    """Change *args* in place as :func:`null_spelled_arguments` says; returns what it said."""
    found = null_spelled_arguments(args, tool)
    for name, outcome in found.items():
        if outcome == NULL_SPELLING_TO_NULL:
            args[name] = None
        else:
            del args[name]
    return found


def merge_mcp_tools(
    builtin: list[dict[str, Any]], mcp_tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge built-in tools with MCP tools.

    Built-in tools come first so the LLM sees them with natural priority.
    Returns a new list; neither input is mutated.
    """
    return builtin + mcp_tools
