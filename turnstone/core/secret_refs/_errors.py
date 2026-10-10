"""Exception hierarchy for secret references and their backends."""

from __future__ import annotations


class SecretError(Exception):
    """Base class for every secret-reference failure."""


class SecretConfigError(SecretError):
    """The ``[secrets]`` configuration is malformed."""


class SecretReferenceError(SecretError):
    """A ``secret://`` reference is malformed or names an unconfigured backend."""


class SecretBackendError(SecretError):
    """A backend could not produce the referenced value.

    ``retryable`` is True for failures that say nothing about the secret itself
    (the store is unreachable, sealed, rate-limited, or refused the login): the
    resolver may then serve the value it last fetched. It is False for a
    definitive answer (not found, permission denied, the field is absent or not
    a string).

    The message is safe to show to an operator through an API response: a
    category and the path from the reference. ``detail`` carries what only the
    log should see (addresses, HTTP status codes, response bodies).
    """

    def __init__(self, message: str, *, retryable: bool, detail: str = "") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.detail = detail


class SecretResolveError(SecretError):
    """A reference could not be resolved and nothing cached could stand in.

    ``retryable`` mirrors the backend failure that caused it: True when the
    store may answer later (unreachable, sealed, login refused), False for a
    definitive answer about the reference itself.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
