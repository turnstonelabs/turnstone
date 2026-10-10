"""``secret://<backend>/<path>[#<key>]`` references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from turnstone.core.secret_refs._errors import SecretReferenceError

REFERENCE_SCHEME = "secret://"

_BACKEND_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class SecretReference:
    """A parsed reference: the backend name, the path inside it and an optional key."""

    backend: str
    path: str
    key: str | None
    text: str

    def __str__(self) -> str:
        return self.text


def is_reference(value: object) -> bool:
    """Return whether *value* is written as a ``secret://`` reference."""
    return isinstance(value, str) and value.startswith(REFERENCE_SCHEME)


def parse_reference(value: str) -> SecretReference:
    """Parse *value* or raise :class:`SecretReferenceError` naming what is wrong.

    The backend is the authority part, the path is everything after it up to
    the fragment, and the fragment names a field inside the secret. The path
    is kept strict on purpose: every segment is ``[A-Za-z0-9._-]+`` and never
    ``.`` or ``..``, so a reference can only name a location below the
    backend's root or mount (an HTTP client normalises dot segments away, so
    a prefix check on the raw text would not protect anything). The query
    string is reserved and refused, and ``${`` is refused because environment
    expansion never applies inside a reference.
    """
    if not is_reference(value):
        raise SecretReferenceError(f"not a secret reference (expected {REFERENCE_SCHEME}...)")
    if "${" in value:
        raise SecretReferenceError(
            f"{value!r}: ${{VAR}} is not expanded inside a reference; write the path literally"
        )
    parts = urlsplit(value)
    backend = parts.netloc
    if not backend or not _BACKEND_RE.match(backend):
        raise SecretReferenceError(
            f"{value!r}: the backend name after {REFERENCE_SCHEME} must be lowercase letters, "
            "digits, '-' or '_'"
        )
    if parts.query:
        raise SecretReferenceError(f"{value!r}: a query string is not supported")
    path = parts.path
    segments = path.strip("/").split("/") if path.strip("/") else []
    if not segments:
        raise SecretReferenceError(f"{value!r}: the path after the backend name is empty")
    for segment in segments:
        if segment in (".", "..") or not _SEGMENT_RE.match(segment):
            raise SecretReferenceError(
                f"{value!r}: path segments must be letters, digits, '.', '_' or '-' "
                "(never empty, '.' or '..')"
            )
    key = parts.fragment or None
    if value.endswith("#"):
        raise SecretReferenceError(f"{value!r}: '#' must be followed by a field name")
    if key is not None and not _KEY_RE.match(key):
        raise SecretReferenceError(
            f"{value!r}: the field name after '#' must be letters, digits, '.', '_' or '-'"
        )
    return SecretReference(backend=backend, path="/" + "/".join(segments), key=key, text=value)
