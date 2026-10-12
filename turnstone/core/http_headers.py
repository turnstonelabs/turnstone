"""What an HTTP header value can hold, for the paths that check one before it is sent."""

from __future__ import annotations

import re

# Printable ASCII, with spaces and tabs only between other characters (RFC 9110's field value,
# without obsolete text); the empty value matches. The HTTP clients refuse most other values (a
# space at either end, a line break, NUL, non-ASCII) with an error that can quote the value, so
# a value is checked against this before it is stored or sent.
HEADER_VALUE_RE = re.compile(r"(?:[!-~](?:[ \x09!-~]*[!-~])?)?")

# Why a value fails HEADER_VALUE_RE, worded without quoting it: the value is usually a credential.
UNFIT_HEADER_VALUE = (
    "a value an HTTP header cannot carry: a space at either end, a control character such as a "
    "trailing line break, or a character outside ASCII"
)


def drop_line_break(value: str) -> str:
    """*value* without one trailing line break, which a value read from a file keeps.

    Everything else is kept verbatim, so a value with more than one still fails
    :data:`HEADER_VALUE_RE`.
    """
    return value.removesuffix("\n").removesuffix("\r")


class HeaderValueError(ValueError):
    """A credential cannot go in a header; the message names where it came from, never the value."""


def checked_header_value(value: object, name: str) -> str:
    """A literal credential as a client can send it: the rule a ``secret://`` value follows.

    One trailing line break is dropped. :class:`HeaderValueError`, naming *name*, is raised for a
    value that is not a string, that a header still cannot carry, or that was only a line break
    (an empty key means the provider SDK's own variable, a different credential). The empty value
    passes.
    """
    if not isinstance(value, str):
        raise HeaderValueError(f"{name} must be a string")
    cleaned = drop_line_break(value)
    if (value and not cleaned) or not HEADER_VALUE_RE.fullmatch(cleaned):
        raise HeaderValueError(f"{name} is {UNFIT_HEADER_VALUE}")
    return cleaned
