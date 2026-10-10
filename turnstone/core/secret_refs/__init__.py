"""``secret://`` references: credentials Turnstone fetches from a store at use time.

A model definition's ``api_key`` or an MCP server's static header may hold a
reference such as ``secret://vault/turnstone/openai#api_key`` or
``secret://file/run/secrets/openai`` instead of the literal value. The
reference is not itself secret: it is stored and shown verbatim, and the value
it names is fetched through the backends configured under ``[secrets]`` in
config.toml, cached for a short time, and held only in memory.

Everything that is not a reference passes through untouched without reading
any configuration, so a deployment that configures no store never pays for
this package. The package is not named ``secrets`` because the hosts import
the standard library module of that name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turnstone.core.secret_refs._errors import (
    SecretBackendError,
    SecretConfigError,
    SecretError,
    SecretReferenceError,
    SecretResolveError,
)
from turnstone.core.secret_refs._reference import (
    REFERENCE_SCHEME,
    SecretReference,
    is_reference,
    parse_reference,
)
from turnstone.core.secret_refs._resolver import (
    SecretResolver,
    get_resolver,
    invalidate_cache,
    load_secrets_config,
    reset_for_tests,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def resolve(value: str) -> str:
    """Return the value a reference names; a non-reference is returned unchanged."""
    if not is_reference(value):
        return value
    return get_resolver().fetch(value)


def resolve_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve every string value of *mapping* that is a reference; copy the rest."""
    if not has_reference(mapping):
        return dict(mapping)
    return get_resolver().resolve_mapping(mapping)


def has_reference(mapping: Mapping[str, Any]) -> bool:
    """Return whether any string value of *mapping* is a reference."""
    return any(is_reference(v) for v in mapping.values())


def contains_reference_text(value: object) -> bool:
    """Return whether *value* mentions the reference scheme anywhere but the start.

    A reference must be the whole value: a header written as
    ``Bearer secret://...`` would be sent verbatim, so the console refuses it.
    """
    return isinstance(value, str) and REFERENCE_SCHEME in value and not is_reference(value)


def validate_reference(value: str) -> None:
    """Raise :class:`SecretReferenceError` (or :class:`SecretConfigError`) unless
    *value* is a well-formed reference to a backend configured on this process."""
    get_resolver().validate(value)


__all__ = [
    "REFERENCE_SCHEME",
    "SecretBackendError",
    "SecretConfigError",
    "SecretError",
    "SecretReference",
    "SecretReferenceError",
    "SecretResolveError",
    "SecretResolver",
    "contains_reference_text",
    "get_resolver",
    "has_reference",
    "invalidate_cache",
    "is_reference",
    "load_secrets_config",
    "parse_reference",
    "reset_for_tests",
    "resolve",
    "resolve_mapping",
    "validate_reference",
]
