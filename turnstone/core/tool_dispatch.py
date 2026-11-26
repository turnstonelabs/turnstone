"""The ``call_tool`` dispatcher behind ``tools.search_delivery = "dispatch"``.

In dispatch delivery the wire tool list never changes: the model gets
``tool_search`` and one generic ``call_tool(name, arguments)``, a search
returns each match's JSON Schema as text, and ``call_tool`` is unwrapped into
the real tool before anything keys on the tool name.  Nothing in this module
runs unless that setting is on.

The provider no longer checks the inner arguments against a declared schema,
so this module does, in two steps:

1. **Repair** what can be repaired without guessing.  Some models (or their
   server's tool-call parser) send every scalar inside an untyped object as a
   string: ``"3600"`` for an integer, ``"false"`` for a boolean.  The schema
   says what was meant, so the value is converted.  Measured on a 31B local
   model, this one slip failed 15 of 33 tasks and the model never corrected it
   from the error text; with the repair it failed none.
2. **Validate** the result and, on failure, say where and show the schema.

Pure functions: no session state, no I/O.
"""

from __future__ import annotations

import json
import re
from typing import Any

CALL_TOOL_NAME = "call_tool"

_INTEGER_RE = re.compile(r"-?\d+")
_NUMBER_RE = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?")


def call_tool_definition() -> dict[str, Any]:
    """The synthetic ``call_tool`` function tool.  Static: no catalog data in it."""
    return {
        "type": "function",
        "function": {
            "name": CALL_TOOL_NAME,
            "description": (
                "Call a tool that tool_search returned. Pass the tool's exact name and "
                "an arguments object that matches the schema tool_search showed for it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact tool name from a tool_search result.",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments for that tool.",
                    },
                },
                "required": ["name", "arguments"],
            },
        },
    }


def unwrap_call(args: Any) -> tuple[str, dict[str, Any] | None, str]:
    """Split a ``call_tool`` argument object into ``(name, arguments, error)``.

    Two shapes other than the declared one are accepted because each has one
    reading: ``arguments`` sent as a JSON-encoded string, and the inner
    arguments written beside ``name`` with no ``arguments`` key.  *error* is
    non-empty when the call cannot be read; *arguments* is then ``None``.
    """
    if not isinstance(args, dict):
        return "", None, "call_tool takes an object with `name` and `arguments`."
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return "", None, "call_tool needs `name`: the exact tool name from a tool_search result."
    name = name.strip()
    if "arguments" not in args:
        return name, {key: value for key, value in args.items() if key != "name"}, ""
    inner = args["arguments"]
    if inner is None:
        return name, {}, ""
    if isinstance(inner, str):
        try:
            inner = json.loads(inner) if inner.strip() else {}
        except (ValueError, RecursionError):
            inner = None
    if not isinstance(inner, dict):
        return (
            name,
            None,
            "call_tool: `arguments` must be a JSON object matching the tool's schema, "
            'for example {"name": "' + name + '", "arguments": {"key": "value"}}.',
        )
    return name, inner, ""


def repair_arguments(schema: Any, value: Any, notes: list[str], path: str = "") -> Any:
    """Convert string values to the type the schema declares, where that is lossless.

    ``"3600"`` becomes ``3600`` for an integer, ``"false"`` becomes ``False``
    for a boolean, and a JSON-encoded string becomes the array or object it
    encodes.  A string that does not read as the declared type is left alone
    for validation to report.  Each conversion is appended to *notes*.
    """
    if not isinstance(schema, dict):
        return value
    kind = schema.get("type")
    if isinstance(value, str) and kind in ("integer", "number", "boolean", "array", "object"):
        text = value.strip()
        fixed: Any = None
        if kind == "integer" and _INTEGER_RE.fullmatch(text):
            fixed = int(text)
        elif kind == "number" and _NUMBER_RE.fullmatch(text):
            fixed = float(text)
        elif kind == "boolean" and text.lower() in ("true", "false"):
            fixed = text.lower() == "true"
        elif kind in ("array", "object"):
            try:
                parsed = json.loads(text)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, list if kind == "array" else dict):
                fixed = parsed
        if fixed is None:
            return value
        notes.append(f"{path or '(top level)'}: string to {kind}")
        value = fixed
    properties = schema.get("properties")
    if isinstance(value, dict) and isinstance(properties, dict):
        return {
            key: repair_arguments(properties.get(key), item, notes, f"{path}/{key}".lstrip("/"))
            for key, item in value.items()
        }
    items = schema.get("items")
    if isinstance(value, list) and isinstance(items, dict):
        return [
            repair_arguments(items, item, notes, f"{path}/{index}".lstrip("/"))
            for index, item in enumerate(value)
        ]
    return value


def validation_error(tool_name: str, schema: Any, args: dict[str, Any]) -> str:
    """``""`` when *args* fits *schema*, else text the model can act on.

    A schema the validator itself rejects is not the model's fault and is not
    reported: the call goes through as it would have in expand delivery.
    """
    if not isinstance(schema, dict) or not schema:
        return ""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError, best_match
    from jsonschema.validators import validator_for

    try:
        validator_class = validator_for(schema, default=Draft202012Validator)
        validator_class.check_schema(schema)
        error = best_match(validator_class(schema).iter_errors(args))
    except (SchemaError, RecursionError, TypeError, ValueError):
        return ""
    if error is None:
        return ""
    where = "/".join(str(part) for part in error.absolute_path) or "(top level)"
    return (
        f"Invalid arguments for {tool_name}: at {where}: {error.message}\n"
        f"Expected schema: {json.dumps(schema, separators=(',', ':'))}\n"
        "Fix the arguments and call it again."
    )
