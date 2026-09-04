"""Model registry — named model configurations with fallback routing.

Manages multiple LLM API backends so workstreams can select their model at
creation time or switch mid-session.  Supports a fallback chain for
resilience when the primary model is unreachable.
"""

from __future__ import annotations

import contextlib
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from turnstone.core.rerank import RerankLane, RerankRuntime

from turnstone.core.admission import ModelAdmission
from turnstone.core.config import load_config
from turnstone.core.log import get_logger
from turnstone.core.providers import (
    LOCAL_PROVIDERS,
    LLMProvider,
    create_client,
    create_provider,
)

log = get_logger(__name__)

# Window used when a model definition leaves ``context_window`` at 0 (the
# Models tab's "auto-detect") and nothing can supply a number: the provider
# has no capability table and its endpoint did not report one. Deliberately
# conservative — an overestimate overflows the real window and hard-fails
# the turn, an underestimate only compacts early.
FALLBACK_CONTEXT_WINDOW = 32768

# Per-endpoint budget for the startup / hot-reload window probe. Shorter than
# the admin Detect button's 10 s: a reachable server answers ``/v1/models``
# in milliseconds, and an unreachable one should not hold boot or a console
# sync for long. Probes run concurrently, so this is also the wall-clock cost
# of a load whose endpoints are all down.
_LOADER_PROBE_TIMEOUT = 5.0


@contextlib.contextmanager
def _deferred_close_actions(actions: list[Callable[[], None]]) -> Iterator[None]:
    """Run collected transport closes after the enclosing registry lock exits."""
    try:
        yield
    finally:
        for close in actions:
            try:
                close()
            except Exception:
                log.warning("rerank.deferred_close_failed", exc_info=True)


MODEL_AUTH_MODES = frozenset({"static", "entra_obo", "entra_app", "rfc8693_obo"})

# One bound for the backend-auth text columns (obo_audience, obo_scopes),
# shared with the console write path's cleaner so the two layers cannot
# drift into "console stores it, registry refuses to load it".
MODEL_AUTH_TEXT_MAX_LEN = 2048

# ``model_definitions.max_concurrency`` is an ``INTEGER`` on both supported
# databases.  Keep the public/config/API bound aligned with PostgreSQL's
# signed 32-bit representation so a value accepted on SQLite cannot fail when
# the same definition is moved to PostgreSQL.
MAX_MODEL_CONCURRENCY = 2_147_483_647

# Derived, never hand-listed (fail-safe defaults): any mode later added to
# MODEL_AUTH_MODES lands in this set BY CONSTRUCTION unless it is literally
# "static", so membership tests fail CLOSED for modes nobody classified.
# Import THIS wherever "is a dynamic auth mode" is asked; a hand-spelled
# tuple is a drift seam where one missed site classifies a new mode as
# static and skips the write gate entirely.
DYNAMIC_MODEL_AUTH_MODES = frozenset(MODEL_AUTH_MODES) - {"static"}

# Modes whose mint reads ``obo_scopes`` (the RFC 8693 exchange-leg scope
# request). The console write path refuses a NEW non-empty obo_scopes on any
# other mode, and the session dispatch passes scopes to the mint only for
# members, so a value stored outside this set is inert by construction.
SCOPES_MODEL_AUTH_MODES = frozenset({"rfc8693_obo"})

# Modes that mint as the deployment's own app identity — no acting user
# required. Every OTHER dynamic mode delegates the acting user's credential,
# so the session's no-user-context refusal derives from this set's
# complement: an unclassified future mode demands a user and fails closed
# (loudly wrong for an app-identity mode, silently unsafe never).
APP_IDENTITY_MODEL_AUTH_MODES = frozenset({"entra_app"})

# Required ``[oidc] obo_grant_profile`` per dynamic mode — the type-pairing
# the console validator enforces when a write CHOOSES a pair, and the grant
# leg the session pins at mint time. Values are spelled as literals rather
# than imported from mcp_oauth's OBO_GRANT_PROFILES: lightweight consumers
# import this module and must not pull the mint stack with it; the
# registry-vs-legs agreement (and full coverage of the dynamic set) is
# pinned by test_model_auth_mode_profile_map_matches_mint_legs.
MODEL_AUTH_MODE_PROFILES: Mapping[str, str] = MappingProxyType(
    {
        "entra_obo": "entra",
        "entra_app": "entra",
        "rfc8693_obo": "rfc8693",
    }
)


def _is_dynamic_auth_mode(mode: str) -> bool:
    """The one in-module spelling of "this mode mints at runtime".

    ``has_dynamic_auth`` (boot-guard input) and :func:`dynamic_auth_key_error`
    (install/swap-guard input) both delegate here, so the two guards cannot
    disagree about which registries need the encryption key.
    """
    return mode in DYNAMIC_MODEL_AUTH_MODES


class ModelAuthConfigError(ValueError):
    """A model definition contains unsafe or internally inconsistent auth settings."""


class ModelConcurrencyConfigError(ValueError):
    """A model definition has an invalid per-alias concurrency limit."""


class ModelClientConstructionError(ValueError):
    """A registry alias exists but its binding could not be constructed.

    Covers BOTH construction legs: the SDK client (``create_client``) and
    the provider adapter (``create_provider`` refusing the row's provider /
    api_surface pairing, re-typed in :meth:`ModelRegistry.resolve_binding`).

    A ``ValueError`` subclass so the HTTP routes' existing ValueError arms keep
    mapping it unchanged, while in-process callers — the session bind path —
    can distinguish :class:`UnknownModelAliasError` from "the alias is present
    but its binding cannot be built" and surface the construction cause.
    Conflating the two gave self-contradictory diagnoses, e.g. a ``/model``
    switch reporting the alias unknown while listing it as available.
    """


class UnknownModelAliasError(ValueError):
    """A registry lookup named an alias that is not present.

    The ``ValueError`` base preserves route and caller compatibility while the
    structured ``alias`` field lets lifecycle code distinguish an alias-removal
    race from unrelated validation failures without parsing exception text.
    """

    def __init__(self, alias: str) -> None:
        self.alias = alias
        super().__init__(f"Unknown model alias: {alias}")


class DynamicAuthKeyError(RuntimeError):
    """A registry install or swap was refused: dynamic auth present, key absent.

    Deliberately NOT a ``ValueError``: the node reload endpoint maps
    ``ValueError`` to 422 (bad registry arguments), while this is a 503-class
    deployment fault — the same classification the console write validator
    gives the identical state. A distinct type keeps the two exits from being
    conflated by a broad ``except`` arm.
    """


# Pre-lifespan swaps only. The node builds and re-shapes its registry in
# ``main()`` before the app exists, so there is no token store to check yet —
# ``initialize_mcp_crypto_state`` (SystemExit at boot) owns key enforcement
# for that process phase moments later. Passing this sentinel says exactly
# that and nothing else: every post-lifespan caller hands the real
# ``app.state`` so :meth:`ModelRegistry.reload` can refuse. The parameter is
# required rather than defaulted so a new call site must actively choose —
# fail-safe defaults, not fail-open ones.
KEY_GUARD_DEFERRED_TO_LIFESPAN: Any = object()


def dynamic_auth_key_error(models: Mapping[str, ModelConfig], app_state: Any) -> str:
    """The dynamic-auth key requirement, shared by every install/swap site.

    One derivation so no two sites can drift: a registry carrying
    dynamic-auth aliases must not become live on a host whose token store is
    absent, or every mint fails per-call while the operator sees nothing.
    Used by the swap chokepoint in :meth:`ModelRegistry.reload` and by the
    console's first-install bootstrap paths. Returns ``""`` when permitted,
    else the refusal message. Token-store presence is process-constant, so a
    refusal here is stable until restart.
    """
    if not any(_is_dynamic_auth_mode(cfg.auth_mode) for cfg in models.values()):
        return ""
    if getattr(app_state, "mcp_token_store", None) is not None:
        return ""
    # Function-local: model_registry is imported by lightweight consumers
    # that never touch crypto; keep the cryptography dependency off this
    # module's import graph.
    from turnstone.core.mcp_crypto import STARTUP_KEY_REQUIRED_HINT

    # Mode list derived from the frozenset above, so a fourth dynamic mode
    # cannot make this refusal name only the modes it was written against.
    return (
        f"dynamic model auth ({'/'.join(sorted(DYNAMIC_MODEL_AUTH_MODES))}) "
        "is configured but " + STARTUP_KEY_REQUIRED_HINT
    )


def profile_mismatched_aliases(
    models: Mapping[str, ModelConfig], obo_grant_profile: str
) -> list[tuple[str, str, str]]:
    """Dynamic aliases whose mode's paired profile is not *obo_grant_profile*.

    Pure projection for the swap/boot visibility warnings: such a row is
    legal to keep (same-pair edits and re-arms pass the write validator) but
    can never mint on this deployment — its mint refuses with
    ``grant_profile_mismatch``. Returns sorted ``(alias, auth_mode,
    required_profile)``. Static and unmapped modes are skipped: static never
    mints, and an unmapped dynamic mode is refused at pair-choose and fails
    closed at dispatch, so neither is a *profile* mismatch.
    """
    rows = [
        (alias, cfg.auth_mode, MODEL_AUTH_MODE_PROFILES[cfg.auth_mode])
        for alias, cfg in models.items()
        if cfg.auth_mode in DYNAMIC_MODEL_AUTH_MODES
        and cfg.auth_mode in MODEL_AUTH_MODE_PROFILES
        and MODEL_AUTH_MODE_PROFILES[cfg.auth_mode] != obo_grant_profile
    ]
    return sorted(rows)


def warn_profile_mismatched_aliases(models: Mapping[str, ModelConfig], app_state: Any) -> None:
    """Warn for every alias whose mode can never mint on this deployment.

    The one spelling of the visibility pass both swap surfaces run —
    :meth:`ModelRegistry.reload` and the lifespan boot — so the wording and
    the profile extraction cannot drift. Not a gate: such a row stays legal
    to keep (same-pair edits pass the write validator), but its mint always
    refuses. No-op unless OIDC is ENABLED and names a grant profile: the
    runtime refuses at the enabled check first (the loaded config defaults
    ``obo_grant_profile`` even when OIDC is off), so a mismatch warning on a
    disabled deployment would name a remedy — flip the profile — that cannot
    make the alias mint. The named cause matches what the alias's mint
    actually records at refusal: the app-identity mint refuses a non-entra
    profile as ``unsupported_grant_profile``; the delegated legs refuse as
    ``grant_profile_mismatch`` — so an operator can grep the runtime
    heartbeat for exactly the token this warning names.
    """
    oidc_config = getattr(app_state, "oidc_config", None)
    if oidc_config is None or not getattr(oidc_config, "enabled", False):
        return
    profile = str(getattr(oidc_config, "obo_grant_profile", "") or "")
    if not profile:
        return
    for alias, mode, required in profile_mismatched_aliases(models, profile):
        cause = (
            "unsupported_grant_profile"
            if mode in APP_IDENTITY_MODEL_AUTH_MODES
            else "grant_profile_mismatch"
        )
        log.warning(
            "model alias %r auth_mode %r requires obo_grant_profile=%r "
            "(configured: %r) — it will not mint (cause=%s)",
            alias,
            mode,
            required,
            profile,
            cause,
        )


# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    """Immutable configuration for a single model endpoint."""

    alias: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    context_window: int = FALLBACK_CONTEXT_WINDOW
    # True when ``context_window`` came from this definition's own endpoint
    # (auto-detect), so a hot-reload can keep it without asking again. An
    # explicit, table or fallback window is never carried into a later
    # auto-detect resolution.
    context_window_detected: bool = False
    provider: str = "openai"
    capabilities: dict[str, Any] = field(default_factory=dict)
    source: str = ""  # "config", "db", or "" (CLI default)
    # Per-model sampling overrides (None = use global default from ConfigStore)
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    # Per-model reasoning-persistence flags (db-backed, admin-toggleable).
    # surface_persisted_reasoning controls UI rehydration of stored reasoning text in
    # /history responses; replay_reasoning_to_model controls whether
    # reasoning blocks ride the wire on subsequent provider calls.
    surface_persisted_reasoning: bool = True
    replay_reasoning_to_model: bool = False
    # Server compatibility settings for openai-compatible backends.
    # Populated from capabilities["server_compat"] during load.
    server_compat: dict[str, Any] = field(default_factory=dict)
    # Backend credential mode. ``static`` (default) sends ``api_key`` unchanged;
    # ``entra_obo`` mints a caller-delegated Entra token; ``entra_app`` mints a
    # shared app-identity token; ``rfc8693_obo`` mints a caller-delegated token
    # via RFC 8693 token exchange. Dynamic tokens are bound as the SDK
    # credential (x-api-key for Anthropic surfaces, Authorization: Bearer for
    # OpenAI-style). ``obo_audience`` is the exact operator-approved resource
    # identifier; ``obo_scopes`` is the space-separated scope list the
    # rfc8693 exchange leg requests (inert for every other mode).
    auth_mode: str = "static"
    obo_audience: str = ""
    obo_scopes: str = ""
    # Per-process admission limit for this alias.  Zero preserves the
    # historical unlimited behavior.  Operational admission changes do not
    # change binding identity, hence ``compare=False``.
    max_concurrency: int = field(default=0, compare=False)

    def __post_init__(self) -> None:
        # ``bool`` is an ``int`` subclass, so use exact type equality.  A
        # permissive coercion here could turn ``true`` into a one-request cap
        # or a typo into unlimited operation.
        if (
            type(self.max_concurrency) is not int
            or self.max_concurrency < 0
            or self.max_concurrency > MAX_MODEL_CONCURRENCY
        ):
            raise ModelConcurrencyConfigError(
                f"Model {self.alias!r} max_concurrency must be an integer "
                f"between 0 and {MAX_MODEL_CONCURRENCY}"
            )


def strip_control_characters(value: str) -> str:
    """Remove every C0 (U+0000–U+001F) and DEL (U+007F) character.

    The ONE spelling of the control-character class every backend-auth text
    surface guards — the scopes sanitize below, the registry's refuse
    predicate, the mint entry points' audience/alias hygiene, and the
    console's OAuth text cleaner all delegate here, so a later widening of
    the class (or a narrowing) lands everywhere at once instead of silently
    splitting the write path's strip from the load path's refusal.
    """
    return "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127)


def sanitize_backend_auth_scopes(value: Any) -> str:
    """The ONE spelling of the backend-auth scopes sanitize.

    The sanctioned separators — tab, newline, CR — read as spaces FIRST
    (stripping a tab-separated list outright would CONCATENATE the scopes
    the tab separates), then every remaining C0/DEL control character is
    stripped, and finally whitespace runs collapse to single spaces. The
    separator vocabulary deliberately matches the registry guard's: the C0
    separator block (U+001C–U+001F) is a CONTROL here, never a separator —
    a bare ``str.split()`` would silently promote it to one — so a control
    byte inside a token strips-and-joins rather than splitting the token
    into two valid-looking scopes. No length cap and no refusal — policy
    (caps, and refuse-vs-strip on garbage) stays with each consuming
    layer; this function only fixes the shared spelling those policies
    measure, so the console store, the registry load, and the mint request
    can never disagree on what a scopes value *is*.
    """
    blessed = re.sub(r"[\t\n\r]", " ", str(value or ""))
    return " ".join(strip_control_characters(blessed).split())


def _check_auth_text(alias: str, field: str, value: str) -> None:
    """Refuse control characters and over-length in a backend-auth text field.

    The registry's REFUSE policy, shared by the audience and scopes arms of
    :func:`_normalize_auth_mode` so the two cannot drift on wording or
    bounds: the write path sanitizes, this layer refuses what sanitization
    would have prevented, so DB-direct garbage fails loud rather than
    loading.
    """
    if strip_control_characters(value) != value:
        raise ModelAuthConfigError(f"Model '{alias}' {field} contains control characters")
    if len(value) > MODEL_AUTH_TEXT_MAX_LEN:
        raise ModelAuthConfigError(
            f"Model '{alias}' {field} exceeds {MODEL_AUTH_TEXT_MAX_LEN} characters"
        )


def _normalize_auth_mode(
    alias: str, mode: Any, audience: Any, scopes: Any = ""
) -> tuple[str, str, str]:
    """Validate and normalize one model's backend-auth configuration.

    DB and config.toml rows share this path so a typo cannot silently downgrade
    dynamic authentication to a static key. Audience and scope values remain
    literal: environment expansion would make authorization node-dependent and
    could bypass the admin allow-list and length boundary.

    ``obo_scopes`` gets shape checks only, and — like the audience's shape
    checks — they run REGARDLESS of auth_mode: the write path sanitizes,
    this layer refuses what sanitization would have prevented, so DB-direct
    garbage fails loud rather than loading. What IS mode-tolerant is the
    coupling: a stored value on a mode outside SCOPES_MODEL_AUTH_MODES is
    accepted exactly like a stale audience on a static row — the console
    refuses NEW staging, an already-stored value must not make the alias
    unloadable, and the dispatch never reads it, so it is inert.
    """
    # The alias is the identity every mint-cache, cooldown, cause and purge
    # key derives from, so it takes the same refuse-not-strip guard as the
    # auth text fields: a control-bearing alias would silently collide with
    # its stripped twin at the key builders (which strip controls as a
    # raw-caller seam), merging two definitions onto one identity.
    _check_auth_text(alias, "alias", str(alias or ""))
    normalized_mode = str(mode or "static").strip() or "static"
    normalized_audience = str(audience or "").strip()
    if normalized_mode not in MODEL_AUTH_MODES:
        raise ModelAuthConfigError(
            f"Model '{alias}' has invalid auth_mode {normalized_mode!r}; "
            f"expected one of {sorted(MODEL_AUTH_MODES)}"
        )
    if normalized_mode != "static" and not normalized_audience:
        raise ModelAuthConfigError(
            f"Model '{alias}' requires obo_audience when auth_mode is {normalized_mode!r}"
        )
    _check_auth_text(alias, "obo_audience", normalized_audience)
    # The guard sees the separator-tolerated spelling, NOT the sanitized
    # one: the sanctioned separators — tab/newline/CR, the config spellings
    # the corpus blesses — read as collapsed spaces, while every OTHER C0
    # byte stays visible for the refusal. A bare str.split() would swallow
    # the C0 separator block (U+001C–U+001F counts as Python whitespace)
    # before the guard could see it, silently loading bytes the write path
    # could never have stored (pinned:
    # test_obo_scopes_normalizers_agree_across_modules). Once the guard
    # passes, the spellings converge, so the returned value routes through
    # the shared transform.
    tolerated = re.sub(r"[ \t\n\r]+", " ", str(scopes or "")).strip()
    _check_auth_text(alias, "obo_scopes", tolerated)
    normalized_scopes = sanitize_backend_auth_scopes(scopes)
    return normalized_mode, normalized_audience, normalized_scopes


def _api_surface_of(cfg: ModelConfig) -> str | None:
    """Extract the operator-pinned api_surface from *cfg*, or ``None``.

    Used both at provider-cache lookup time and at reload-eviction time so the
    two sites stay in sync.  Returns ``None`` when the field is absent, blank,
    or not a string — matching the "inherit provider default" semantics.
    """
    raw = cfg.server_compat.get("api_surface") if isinstance(cfg.server_compat, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _validate_registry_args(
    models: dict[str, ModelConfig],
    default: str,
    fallback: list[str] | None,
    agent_model: str | None,
    task_model: str | None,
) -> None:
    """Validate ModelRegistry construction / reload arguments.

    An empty ``models`` is permitted: it is the degraded "no models configured
    yet" state a server boots into before any model definition exists (models
    are added later via the admin panel and picked up by a hot reload — see
    ``internal_model_reload``).  In that state ``default`` must be unset (``""``)
    and every alias lookup raises until a model is added.  A non-empty registry
    validates exactly as before: ``default`` and any routing alias must exist.
    """
    if not models:
        if default:
            raise ValueError(f"Default model '{default}' not found in empty registry")
        return
    if default not in models:
        raise ValueError(f"Default model '{default}' not found in registry")
    if fallback:
        for alias in fallback:
            if alias not in models:
                raise ValueError(f"Fallback model '{alias}' not found in registry")
    if agent_model and agent_model not in models:
        raise ValueError(f"Agent model '{agent_model}' not found in registry")
    if task_model and task_model not in models:
        raise ValueError(f"Task model '{task_model}' not found in registry")


@dataclass(frozen=True)
class _RerankRuntimeConfig:
    """Relevant immutable configuration for one cached rerank runtime."""

    base_url: str
    api_key: str = field(repr=False)
    model: str
    provider: str
    auth_mode: str
    obo_audience: str
    obo_scopes: str
    rerank_mode: str
    supports_rerank: bool
    supports_prefill_rerank: bool
    instruction: str


def _rerank_runtime_config(cfg: ModelConfig, instruction: str) -> _RerankRuntimeConfig:
    """Project a model definition onto fields that change rerank behavior."""
    caps = cfg.capabilities
    return _RerankRuntimeConfig(
        base_url=cfg.base_url.strip(),
        api_key=cfg.api_key,
        model=cfg.model.strip(),
        provider=cfg.provider,
        auth_mode=cfg.auth_mode,
        obo_audience=cfg.obo_audience,
        obo_scopes=cfg.obo_scopes,
        rerank_mode=str(caps.get("rerank_mode") or "endpoint"),
        supports_rerank=bool(caps.get("supports_rerank")),
        supports_prefill_rerank=bool(caps.get("supports_prefill_rerank")),
        instruction=instruction.strip(),
    )


@dataclass(frozen=True)
class _RerankRuntimeEntry:
    runtime: RerankRuntime
    config: _RerankRuntimeConfig = field(repr=False)


class ModelRegistry:
    """Holds named model configurations with thread-safe lazy client creation.

    Args:
        models: Mapping of alias → ModelConfig.
        default: Alias of the default model.
        fallback: Ordered list of aliases to try when the primary model fails.
        agent_model: Optional alias for the task_agent sub-agent (single-knob
            fallback used when ``task_model`` is unset).
        task_model: Optional alias for the task_agent sub-agent.  Overrides
            ``agent_model`` for task calls; falls back to it when unset.
        task_effort: Reasoning effort for task_agent.  ``None`` means inherit
            the parent session's reasoning effort.
    """

    def __init__(
        self,
        models: dict[str, ModelConfig],
        default: str,
        fallback: list[str] | None = None,
        agent_model: str | None = None,
        task_model: str | None = None,
        task_effort: str | None = None,
    ) -> None:
        _validate_registry_args(models, default, fallback, agent_model, task_model)

        self._models = dict(models)
        self.default = default
        self.fallback = list(fallback) if fallback else []
        self.agent_model = agent_model
        self.task_model = task_model
        self.task_effort = task_effort
        self._clients: dict[str, Any] = {}
        self._providers: dict[str, LLMProvider] = {}
        self._rerank_runtimes: dict[str, _RerankRuntimeEntry] = {}
        self._admissions = {
            alias: ModelAdmission(alias, int(getattr(cfg, "max_concurrency", 0)))
            for alias, cfg in self._models.items()
        }
        self._client_lock = threading.Lock()
        # Monotone count of completed reload() swaps. A counter, not a field
        # diff: sessions re-resolve on ANY difference at the next send, so an
        # in-place swap propagates even when the backend model id is
        # unchanged, and future auth-relevant columns are covered by
        # construction.
        self._generation = 0

    # -- query methods -------------------------------------------------------

    def get_client(self, alias: str) -> Any:
        """Get or lazily create an API client for *alias*. Thread-safe."""
        with self._client_lock:
            return self._get_client_locked(alias)

    def _get_client_locked(self, alias: str) -> Any:
        """``get_client`` body; the caller holds ``_client_lock``."""
        if alias not in self._models:
            raise UnknownModelAliasError(alias)
        if alias not in self._clients:
            cfg = self._models[alias]
            # An entra_obo / entra_app backend authenticates per-call via a
            # minted token bound with ``client.with_options(api_key=...)``, so
            # the cached client only needs to CONSTRUCT — feed a placeholder
            # when no static fallback key is set (the SDKs reject an empty key
            # that also has no env fallback).  The real credential is supplied
            # per call and never rides on this base client object.
            client_key = cfg.api_key
            if not client_key and _is_dynamic_auth_mode(cfg.auth_mode):
                client_key = "backend-auth-placeholder-unused"
            try:
                self._clients[alias] = create_client(
                    cfg.provider, base_url=cfg.base_url, api_key=client_key
                )
            except ValueError as exc:
                # create_client's own misconfig errors already carry
                # remediation text — keep the message verbatim, add the
                # type so callers can tell "construction failed" from
                # "alias missing".
                raise ModelClientConstructionError(str(exc)) from exc
            except Exception as exc:
                # SDK construction can fail on environment problems the
                # config never sees — e.g. httpx resolving a CA-bundle
                # path that a venv rebuild deleted (FileNotFoundError).
                # Routes map ValueError to a 503 with the message;
                # anything else surfaces as an opaque 500, so re-type
                # here where the alias is known.  The message text is
                # echoed to HTTP callers, so it carries only the
                # exception TYPE — arbitrary SDK exception text can
                # embed filesystem paths; the full detail goes to the
                # server log instead.
                log.warning(
                    "Client construction failed for model alias %r (provider %s)",
                    alias,
                    cfg.provider,
                    exc_info=True,
                )
                raise ModelClientConstructionError(
                    f"failed to construct {cfg.provider} client for model "
                    f"alias {alias!r}: {type(exc).__name__} (details in server log)"
                ) from exc
        return self._clients[alias]

    def get_provider(self, alias: str) -> LLMProvider:
        """Get the ``LLMProvider`` for *alias*. Thread-safe, cached."""
        with self._client_lock:
            return self._get_provider_locked(alias)

    def _get_provider_locked(self, alias: str) -> LLMProvider:
        """``get_provider`` body; the caller holds ``_client_lock``."""
        if alias not in self._models:
            raise UnknownModelAliasError(alias)
        if alias not in self._providers:
            cfg = self._models[alias]
            self._providers[alias] = create_provider(cfg.provider, api_surface=_api_surface_of(cfg))
        return self._providers[alias]

    def get_config(self, alias: str) -> ModelConfig:
        """Return the ModelConfig for *alias*."""
        if alias not in self._models:
            raise UnknownModelAliasError(alias)
        return self._models[alias]

    def get_admission(self, alias: str) -> ModelAdmission:
        """Return the stable per-alias admission gate."""
        with self._client_lock:
            gate = self._admissions.get(alias)
            if gate is None:
                raise UnknownModelAliasError(alias)
            return gate

    def resolve_rerank_lane(
        self,
        alias: str,
        *,
        instruction: str = "",
        config_version: int = 0,
    ) -> RerankLane:
        """Resolve one coherent endpoint-backed rerank binding.

        Unlike :meth:`resolve_binding`, this path deliberately constructs no
        LLM provider or SDK client: a Cohere/Jina rerank route is not a chat
        model surface. It shares only the alias's stable admission object and
        registry generation with normal model lanes.
        """
        from turnstone.core.rerank import RerankLane, RerankRuntime, resolve_rerank_client

        close_actions: list[Callable[[], None]] = []
        instruction = instruction.strip()
        with _deferred_close_actions(close_actions), self._client_lock:
            cfg = self._models.get(alias)
            if cfg is None:
                raise UnknownModelAliasError(alias)
            if not cfg.base_url.strip() or not cfg.capabilities.get("supports_rerank"):
                raise ValueError(f"Model alias {alias!r} is not a configured reranker")

            runtime_config = _rerank_runtime_config(cfg, instruction)
            entry = self._rerank_runtimes.get(alias)
            if entry is None or entry.config != runtime_config:
                client = resolve_rerank_client(
                    cfg.base_url,
                    model=cfg.model,
                    api_key=cfg.api_key,
                    instruction=instruction,
                )
                if client is None:  # guarded above; retain a fail-closed boundary
                    raise ValueError(f"Model alias {alias!r} has no rerank endpoint")
                replacement = _RerankRuntimeEntry(
                    runtime=RerankRuntime(client, alias=alias, model=cfg.model),
                    config=runtime_config,
                )
                if entry is not None:
                    close = entry.runtime.begin_retirement()
                    if close is not None:
                        close_actions.append(close)
                    log.info("rerank.runtime_retired alias=%s reason=config", alias)
                self._rerank_runtimes[alias] = replacement
                entry = replacement

            # The Reranker role selects one alias per process. Retire a
            # previously selected alias when a settings change resolves its
            # replacement; active calls drain on their old immutable lanes.
            for other_alias, other in list(self._rerank_runtimes.items()):
                if other_alias == alias:
                    continue
                close = other.runtime.begin_retirement()
                if close is not None:
                    close_actions.append(close)
                del self._rerank_runtimes[other_alias]
                log.info("rerank.runtime_retired alias=%s reason=role_change", other_alias)

            lane = RerankLane(
                runtime=entry.runtime,
                alias=alias,
                model=cfg.model,
                admission=self._admissions[alias],
                registry_generation=self._generation,
                config_version=config_version,
            )
        return lane

    def deactivate_rerank_runtime(self) -> None:
        """Retire any selected rerank runtime after the role becomes empty/invalid."""
        close_actions: list[Callable[[], None]] = []
        with _deferred_close_actions(close_actions), self._client_lock:
            for alias, entry in list(self._rerank_runtimes.items()):
                close = entry.runtime.begin_retirement()
                if close is not None:
                    close_actions.append(close)
                log.info("rerank.runtime_retired alias=%s reason=disabled", alias)
            self._rerank_runtimes.clear()

    def has_alias(self, alias: str) -> bool:
        """Check if *alias* exists in the registry."""
        return alias in self._models

    def has_dynamic_auth(self) -> bool:
        """Return whether any alias needs a runtime-minted backend credential."""
        return any(_is_dynamic_auth_mode(cfg.auth_mode) for cfg in self._models.values())

    def list_aliases(self) -> list[str]:
        """Return all registered model aliases."""
        return list(self._models.keys())

    def resolve(self, alias: str | None = None) -> tuple[Any, str, ModelConfig, int]:
        """Resolve *alias* to ``(client, model_name, config, generation)``.

        Uses the default alias when *alias* is ``None``. One lock
        acquisition, so config, client and generation all come from the same
        registry snapshot and a caller stamping the returned generation
        beside the returned client holds an exactly-paired binding.
        """
        with self._client_lock:
            alias = alias or self.default
            cfg = self._models.get(alias)
            if cfg is None:
                raise UnknownModelAliasError(alias)
            return self._get_client_locked(alias), cfg.model, cfg, self._generation

    def resolve_binding(
        self, alias: str | None = None
    ) -> tuple[Any, str, ModelConfig, LLMProvider, ModelAdmission, int]:
        """Resolve client, model, config, provider, admission, and generation.

        The session bind primitive: everything a rebind commits, read under
        ONE lock acquisition, so a :meth:`reload` landing between separate
        ``resolve()`` / ``get_provider()`` calls cannot tear the binding by
        pairing old-map client and config with a new-map provider. The
        generation is read in the same hold, so the caller's stamp is
        exactly the snapshot its binding came from.

        Provider-leg construction failures are re-typed to
        :class:`ModelClientConstructionError`, matching the client leg: the
        alias provably exists here, so a plain ``ValueError`` would be
        misread by the bind path as alias-missing.
        """
        with self._client_lock:
            alias = alias or self.default
            cfg = self._models.get(alias)
            if cfg is None:
                raise UnknownModelAliasError(alias)
            client = self._get_client_locked(alias)
            try:
                provider = self._get_provider_locked(alias)
            except ModelClientConstructionError:
                raise
            except ValueError as exc:
                raise ModelClientConstructionError(str(exc)) from exc
            return (
                client,
                cfg.model,
                cfg,
                provider,
                self._admissions[alias],
                self._generation,
            )

    def resolve_agent_alias(self, kind: str) -> str | None:
        """Return the configured alias for a sub-agent ``kind``.

        The per-kind override (``task_model``) wins over the legacy
        single-knob ``agent_model``.  Returns ``None`` when nothing is
        configured (caller should fall back to the session model).

        Recognised kind: ``"task"``.  Any other value (e.g. ``"agent"``,
        eval/utility paths) returns the legacy ``agent_model`` as-is —
        preserves prior behaviour for non-task callers.
        """
        if kind == "task":
            return self.task_model or self.agent_model
        return self.agent_model

    def resolve_agent_effort(self, kind: str) -> str | None:
        """Return the reasoning effort for a sub-agent ``kind``.

        Task returns ``None`` to indicate the caller should fall through
        to the session default.
        """
        if kind == "task":
            return self.task_effort
        return None

    @property
    def count(self) -> int:
        """Number of registered models."""
        return len(self._models)

    @property
    def generation(self) -> int:
        """Monotone count of completed :meth:`reload` swaps.

        Consumers compare by EQUALITY against the generation their binding
        was resolved from; any difference means "re-resolve everything
        derived from here". Never compare by ordering.
        """
        return self._generation

    @property
    def models(self) -> dict[str, ModelConfig]:
        """Return a copy of the models dict (public accessor for reload)."""
        return dict(self._models)

    # -- lifecycle -----------------------------------------------------------

    def reload(
        self,
        models: dict[str, ModelConfig],
        default: str,
        fallback: list[str] | None = None,
        agent_model: str | None = None,
        *,
        app_state: Any,
        task_model: str | None = None,
        task_effort: str | None = None,
    ) -> None:
        """Hot-reload all model configs. Thread-safe; clears cached clients.

        THE model-registry swap chokepoint (complete mediation): every
        live-registry swap on every host routes through here, so the
        dynamic-auth-needs-key refusal below cannot be forgotten at a call
        site (fail-safe defaults). ``app_state`` is required; pre-lifespan
        boot paths pass :data:`KEY_GUARD_DEFERRED_TO_LIFESPAN` (see its
        comment for why that is not a bypass). Raises
        :class:`DynamicAuthKeyError` WITHOUT mutating when refused, and the
        caller applies per-host policy.

        Validates arguments before mutating state so a bad reload
        does not leave the registry in an inconsistent state.

        Every completed swap bumps :attr:`generation`; live sessions compare
        it per send and re-resolve on mismatch, so the swap reaches them even
        when an alias keeps its backend model id (see
        ``ChatSession._refresh_model_from_registry``).
        """
        if app_state is not KEY_GUARD_DEFERRED_TO_LIFESPAN:
            key_err = dynamic_auth_key_error(models, app_state)
            if key_err:
                raise DynamicAuthKeyError(key_err)
            # Visibility, not a gate: a persisted row whose mode names the
            # other grant dialect stays valid to keep, but its mint always
            # refuses on this deployment — say so at every swap chokepoint.
            warn_profile_mismatched_aliases(models, app_state)
        _validate_registry_args(models, default, fallback, agent_model, task_model)
        rerank_close_actions: list[Callable[[], None]] = []
        with _deferred_close_actions(rerank_close_actions), self._client_lock:
            # FIRST write inside the lock, deliberately BEFORE the map swap
            # and the client teardown. The per-send refresh reads the maps
            # lock-free and samples the generation AFTER them (see
            # ``ChatSession._refresh_model_from_registry``); with the bump
            # ordered first, that reader can observe new-generation +
            # old-maps — a benign extra rebind, since ``resolve_binding()``
            # takes this lock and lands on the completed swap — but never
            # stale-generation + new-maps, which would let the skip-compare
            # pass and route the turn into a client this reload is about to
            # close. Sessions' STAMPED values come from
            # resolve()/resolve_binding() under this same lock, so a stamp
            # can never be newer than the binding it vouches for. Never
            # bumped on a refused reload: both guards raise above, before
            # any mutation.
            self._generation += 1
            old_models = self._models
            self._models = dict(models)
            self.default = default
            self.fallback = list(fallback) if fallback else []
            self.agent_model = agent_model
            self.task_model = task_model
            self.task_effort = task_effort
            # Admission is strictly per alias.  Resize surviving gates in
            # place so live lanes and new resolutions coordinate through the
            # same FIFO even when the alias moves to a different endpoint.
            for alias, cfg in self._models.items():
                limit = int(getattr(cfg, "max_concurrency", 0))
                gate = self._admissions.get(alias)
                if gate is None:
                    self._admissions[alias] = ModelAdmission(alias, limit)
                else:
                    gate.set_limit(limit)
            # Rerank runtimes have their own transport and breaker state. A
            # cap-only or unrelated model reload preserves them; any relevant
            # endpoint/model/auth/capability change retires the old runtime now.
            for alias, entry in list(self._rerank_runtimes.items()):
                rerank_cfg = self._models.get(alias)
                if (
                    rerank_cfg is None
                    or not rerank_cfg.base_url.strip()
                    or not rerank_cfg.capabilities.get("supports_rerank")
                    or entry.config != _rerank_runtime_config(rerank_cfg, entry.config.instruction)
                ):
                    close = entry.runtime.begin_retirement()
                    if close is not None:
                        rerank_close_actions.append(close)
                    del self._rerank_runtimes[alias]
                    log.info("rerank.runtime_retired alias=%s reason=registry_reload", alias)
            # Removed aliases remain as tombstones for this registry's
            # lifetime.  A stale lane may still hold or queue on that object;
            # re-adding the alias must reconfigure the same gate rather than
            # split old and new work across two independent limits.
            # Selective teardown — close + drop only clients whose
            # construction/connection target changed (alias removed, or
            # base_url / api_key / provider / auth_mode differs). Keeps connection
            # pools warm for the common admin-edit case where only
            # ``model`` / ``temperature`` / ``context_window`` changed.
            for alias, client in list(self._clients.items()):
                old_cfg = old_models.get(alias)
                new_cfg = self._models.get(alias)
                if (
                    new_cfg is None
                    or old_cfg is None
                    or old_cfg.base_url != new_cfg.base_url
                    or old_cfg.api_key != new_cfg.api_key
                    or old_cfg.provider != new_cfg.provider
                    or old_cfg.auth_mode != new_cfg.auth_mode
                ):
                    if hasattr(client, "close"):
                        client.close()
                    del self._clients[alias]
            # Providers are keyed on alias and depend on (cfg.provider,
            # cfg.server_compat["api_surface"]) — drop when either changes
            # or the alias was removed.
            for alias in list(self._providers.keys()):
                old_cfg = old_models.get(alias)
                new_cfg = self._models.get(alias)
                if (
                    new_cfg is None
                    or old_cfg is None
                    or old_cfg.provider != new_cfg.provider
                    or _api_surface_of(old_cfg) != _api_surface_of(new_cfg)
                ):
                    del self._providers[alias]

    def shutdown(self) -> None:
        """Close all cached client connections."""
        rerank_close_actions: list[Callable[[], None]] = []
        with _deferred_close_actions(rerank_close_actions), self._client_lock:
            for entry in self._rerank_runtimes.values():
                close = entry.runtime.begin_retirement()
                if close is not None:
                    rerank_close_actions.append(close)
            self._rerank_runtimes.clear()
            for client in self._clients.values():
                if hasattr(client, "close"):
                    client.close()
            self._clients.clear()
            self._providers.clear()


# ---------------------------------------------------------------------------
# Loading from config
# ---------------------------------------------------------------------------


def _resolve_env_vars(value: str) -> str:
    """Expand ``${VAR}`` patterns in *value* using environment variables.

    Unresolved variables are replaced with empty strings.
    """
    import os
    import re

    def _replace(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _replace, value)


def _resolve_openai_provider(provider: str, base_url: str) -> str:
    """Distinguish commercial OpenAI from local OpenAI-compatible servers.

    When ``provider`` is ``"openai"`` but the ``base_url`` does not point to
    ``api.openai.com``, the model is on a local server (vLLM, llama.cpp, etc.)
    and should use the Chat Completions provider (``"openai-compatible"``).
    """
    if provider == "openai" and base_url and "api.openai.com" not in base_url:
        try:
            from urllib.parse import urlparse

            hostname = urlparse(base_url).hostname or ""
        except Exception:
            hostname = ""
        if hostname.endswith(".googleapis.com"):
            return "google"
        return "openai-compatible"
    return provider


def _coerce_context_window(raw: Any, alias: str) -> int:
    """Read a definition's ``context_window``; garbage and negatives become 0."""
    if raw is None or isinstance(raw, bool):
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        log.warning("Model '%s' has invalid context_window %r, auto-detecting", alias, raw)
        return 0
    return max(0, value)


def _probe_context_window(cfg: ModelConfig) -> tuple[int | None, str]:
    """Ask *cfg*'s own endpoint for its window.

    Returns ``(window, note)`` on a hit — *note* is empty, or says the
    number came from a single-model server whose served id differs from
    the definition — and ``(None, why)`` on a miss. Endpoint metadata only
    (vLLM ``max_model_len``, llama.cpp ``n_ctx_train``): the commercial
    OpenAI table is never consulted for a local server, so a model served
    under a commercial id cannot inherit that model's window.

    The round trip runs on a daemon thread under a wall-clock deadline: the
    SDK timeout bounds the socket phases, but a wedged resolver would pin a
    non-daemon worker past process exit.
    """
    from turnstone.core.deadline import DeadlineExceededError, run_with_deadline

    try:
        result = run_with_deadline(
            lambda: probe_model_endpoint(
                cfg.provider,
                cfg.base_url,
                cfg.api_key,
                target_model=cfg.model,
                static_table=False,
                timeout=_LOADER_PROBE_TIMEOUT,
            ),
            timeout=_LOADER_PROBE_TIMEOUT + 1.0,
            poll=0.05,
            thread_name="ctx-probe-io",
        )
    except DeadlineExceededError:
        return None, f"the endpoint probe did not answer within {_LOADER_PROBE_TIMEOUT:.0f} s"
    except Exception as exc:  # noqa: BLE001 - a probe failure is a miss, never a boot crash
        return None, f"the endpoint probe raised {type(exc).__name__}"
    if result.get("error"):
        return None, f"the endpoint probe failed ({result['error']})"
    ctx = result.get("context_window")
    if not isinstance(ctx, int) or ctx <= 0:
        return None, "the endpoint does not report a context window"
    if result.get("model_found"):
        return ctx, ""
    # A single-model server is this model whatever it calls itself (vLLM
    # often serves a path, not the alias name); on a multi-model endpoint
    # the number must belong to this model.
    listed = result.get("available_models") or []
    if len(listed) == 1:
        return ctx, f"single-model server, served as {listed[0]!r}"
    return None, f"model {cfg.model!r} is not in the endpoint's model list"


def _same_endpoint(a: ModelConfig, b: ModelConfig) -> bool:
    return (a.provider, a.base_url.rstrip("/"), a.model) == (
        b.provider,
        b.base_url.rstrip("/"),
        b.model,
    )


def _resolve_context_windows(
    configs: dict[str, ModelConfig],
    *,
    detect: bool,
    inherited: int = 0,
    prior: Mapping[str, ModelConfig] | None = None,
) -> dict[str, ModelConfig]:
    """Replace every ``context_window == 0`` with an auto-detected window.

    ``0`` is the Models tab's "auto-detect" sentinel. In order:

    1. A definition with a capability-table provider (anthropic, openai,
       xai) takes its table entry. Google publishes no table, so its
       definitions need an explicit window.
    2. A caller-supplied *inherited* window — the CLI's own bootstrap
       detection or ``--context-window`` — applies to the local-server
       definitions the CLI would otherwise probe.
    3. A local-server definition is asked directly when *detect* is set —
       one ``/v1/models`` round trip against its own endpoint, identical
       definitions sharing one probe, concurrently on bounded daemon
       threads — so a backend restarted with a different window is picked
       up by the next hot-reload.
    4. When that probe misses, a definition the registry being hot-reloaded
       (*prior*) had detected from the same endpoint and model keeps that
       window: a backend mid-restart cannot shrink live sessions. A prior
       that was explicit, or a fallback, is not carried.
    5. Anything still unresolved — an unlisted commercial id, a silent or
       unreachable endpoint — gets :data:`FALLBACK_CONTEXT_WINDOW` and a
       warning naming the alias and the reason. A rerank-only definition has
       no chat window and takes the fallback silently.
    """
    from dataclasses import replace

    from turnstone.core.providers import lookup_model_capabilities

    unresolved: dict[str, str] = {}
    to_probe: list[str] = []
    for alias, cfg in list(configs.items()):
        if cfg.context_window > 0:
            continue
        if cfg.capabilities.get("supports_rerank"):
            configs[alias] = replace(cfg, context_window=FALLBACK_CONTEXT_WINDOW)
            continue
        if cfg.provider not in LOCAL_PROVIDERS:
            try:
                known = lookup_model_capabilities(cfg.provider, cfg.model)
            except ValueError:
                # An unrecognised provider string fails at first use, as it
                # always has; the window must not turn it into a boot crash.
                unresolved[alias] = f"provider {cfg.provider!r} is not recognised"
                continue
            if known is None:
                # The provider default would be a guess that can exceed the
                # real window and hard-fail turns; say so instead.
                unresolved[alias] = f"{cfg.provider} has no capability entry for {cfg.model!r}"
                continue
            configs[alias] = replace(cfg, context_window=int(known["context_window"]))
            continue
        if inherited > 0:
            configs[alias] = replace(cfg, context_window=inherited)
            continue
        if _is_dynamic_auth_mode(cfg.auth_mode):
            unresolved[alias] = "the endpoint needs a per-call credential, so it is not probed"
        elif not cfg.base_url:
            unresolved[alias] = "the definition has no base_url"
        elif not detect:
            unresolved[alias] = "endpoint detection is off in this process"
        else:
            to_probe.append(alias)

    if to_probe:
        from concurrent.futures import ThreadPoolExecutor

        # Identical definitions (same endpoint, credential and model) share
        # one round trip; each worker only waits on its own deadline-bounded
        # daemon thread, so the pool drains within a few probe budgets
        # however many endpoints are down.
        by_identity: dict[tuple[str, str, str, str, str], list[str]] = {}
        for alias in to_probe:
            cfg = configs[alias]
            key = (cfg.provider, cfg.base_url.rstrip("/"), cfg.api_key, cfg.auth_mode, cfg.model)
            by_identity.setdefault(key, []).append(alias)
        groups = list(by_identity.values())
        with ThreadPoolExecutor(
            max_workers=min(16, len(groups)), thread_name_prefix="ctx-probe"
        ) as pool:
            outcomes = list(
                pool.map(_probe_context_window, [configs[aliases[0]] for aliases in groups])
            )
        for aliases, (ctx, note) in zip(groups, outcomes, strict=True):
            for alias in aliases:
                if ctx is None:
                    unresolved[alias] = note
                    continue
                cfg = configs[alias]
                configs[alias] = replace(cfg, context_window=ctx, context_window_detected=True)
                log.info(
                    "Model '%s': context_window %s (detected from %s%s)",
                    alias,
                    f"{ctx:,}",
                    cfg.base_url,
                    f"; {note}" if note else "",
                )

    for alias, why in unresolved.items():
        cfg = configs[alias]
        previous = prior.get(alias) if prior else None
        if (
            previous is not None
            and previous.context_window_detected
            and _same_endpoint(previous, cfg)
        ):
            configs[alias] = replace(
                cfg, context_window=previous.context_window, context_window_detected=True
            )
            log.info(
                "Model '%s': keeping the previously detected context_window %s (%s)",
                alias,
                f"{previous.context_window:,}",
                why,
            )
            continue
        emit = log.warning if detect else log.debug
        emit(
            "Model '%s': context_window is 0 (auto-detect) but %s — using %s until "
            "context_window is set on the model definition",
            alias,
            why,
            f"{FALLBACK_CONTEXT_WINDOW:,}",
        )
        configs[alias] = replace(configs[alias], context_window=FALLBACK_CONTEXT_WINDOW)
    return configs


def load_model_registry(
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    context_window: int = 0,
    provider: str = "openai",
    storage: Any | None = None,
    strict: bool = False,
    allow_empty: bool = False,
    detect_context_windows: bool = False,
    prior: Mapping[str, ModelConfig] | None = None,
) -> ModelRegistry:
    """Build a ModelRegistry from CLI args, ``config.toml``, and database.

    Precedence (highest to lowest):

    1. ``[models.*]`` sections in config.toml define named models
       (``source="config"``).  These override DB entries with the same
       alias in-memory only — the DB rows are never modified.
    2. Database model definitions (``source="db"``), loaded when
       *storage* is provided.
    3. The CLI's ``--base-url`` / ``--api-key`` / ``--model`` synthesize a
       ``"default"`` entry when nothing else defines a model. The server
       passes none of these: its models come from the DB and config.toml only.
    4. ``[model].default``, ``[model].fallback``, ``[model].agent_model``,
       ``[model].task_model``, ``[model].task_effort`` control routing.
       ``task_model`` overrides ``agent_model`` for the task sub-agent;
       it falls back to ``agent_model`` when unset.

    ``strict``: when True, a storage read failure during the DB-rows step
    re-raises instead of degrading to a config.toml-only registry.
    Callers that hot-reload an existing registry need this so a transient
    DB outage doesn't silently drop every DB-sourced alias when the
    truncated result is applied via ``ModelRegistry.reload``.  Callers
    that build a fresh registry from scratch (CLI, lifespan startup) want
    the default behaviour — boot succeeds with a config-only fallback
    rather than crashing on a flaky DB.

    ``allow_empty``: when True, returns an empty registry instead of raising
    when no models are defined anywhere.  Server entry points pass this so a
    node boots and registers with no models yet, to be configured live via the
    admin panel.  The CLI leaves it False — a REPL with no model is unusable
    and there's no live panel to fix it.

    ``context_window``: the CLI's bootstrap window. It sizes the synthesized
    ``"default"`` entry and, when non-zero, every
    local-server definition whose own ``context_window`` is ``0``. The
    server passes ``0``, and ``0`` (the Models tab's "auto-detect") is then
    resolved per definition by :func:`_resolve_context_windows` — from the
    provider's capability table, from the registry being hot-reloaded
    (``prior``), or from the definition's own endpoint when
    ``detect_context_windows`` is set (server boot and hot-reload, console,
    doctor). Callers that must not touch the network leave it False and
    unresolved local definitions fall back to :data:`FALLBACK_CONTEXT_WINDOW`.
    """
    import json as _json

    cfg = load_config()
    models_section: dict[str, Any] = cfg.get("models", {})
    model_section: dict[str, Any] = cfg.get("model", {})
    api_section = cfg.get("api")
    api_base_url_present = isinstance(api_section, dict) and bool(api_section.get("base_url"))

    configs: dict[str, ModelConfig] = {}

    # 1. Load DB model definitions (lowest priority, overridden by config.toml)
    if storage is not None:
        try:
            for row in storage.list_model_definitions(enabled_only=True):
                alias = row["alias"]
                caps: dict[str, Any] = {}
                if row.get("capabilities"):
                    try:
                        parsed = _json.loads(row["capabilities"])
                        if isinstance(parsed, dict):
                            caps = parsed
                    except (_json.JSONDecodeError, TypeError):
                        pass  # falls back to empty capabilities
                # Extract server_compat from capabilities (namespaced key)
                row_server_compat = caps.pop("server_compat", {})
                if not isinstance(row_server_compat, dict):
                    row_server_compat = {}
                raw_row_base_url = str(row.get("base_url") or "")
                row_base_url = _resolve_env_vars(raw_row_base_url)
                if raw_row_base_url and not row_base_url:
                    # An unset ${VAR} would resolve to the provider's public
                    # endpoint under a credential meant for the local server.
                    log.warning(
                        "Model '%s' has a base_url whose ${VAR} is unset — export it or "
                        "set the URL in the Models tab; skipping",
                        alias,
                    )
                    continue
                row_provider = _resolve_openai_provider(row.get("provider", "openai"), row_base_url)
                if row_provider in LOCAL_PROVIDERS and not row_base_url:
                    # The console refuses this at save time; a row that predates
                    # that check would only fail on a user's first turn.
                    log.warning(
                        "Model '%s' is a %s definition without a base_url — set it in "
                        "the Models tab; skipping",
                        alias,
                        row_provider,
                    )
                    continue
                row_model = row["model"]
                # 0 = auto-detect, resolved per definition below
                row_ctx = _coerce_context_window(row.get("context_window"), alias)
                # Per-model sampling overrides (None = use global default)
                row_temperature = row.get("temperature")
                row_max_tokens = row.get("max_tokens")
                row_reasoning_effort = row.get("reasoning_effort")
                # Per-model reasoning flags. Defaults match the dataclass so a
                # pre-052 row missing these columns degrades gracefully.
                row_surface_persisted_reasoning = bool(row.get("surface_persisted_reasoning", True))
                row_replay_reasoning = bool(row.get("replay_reasoning_to_model", False))
                # Per-user OBO auth (defaults match a pre-068/pre-069 row
                # missing the columns → "static", no audience/scopes →
                # unchanged behaviour).
                row_auth_mode, row_obo_audience, row_obo_scopes = _normalize_auth_mode(
                    alias,
                    row.get("auth_mode"),
                    row.get("obo_audience"),
                    row.get("obo_scopes"),
                )
                configs[alias] = ModelConfig(
                    alias=alias,
                    base_url=row_base_url,
                    api_key=_resolve_env_vars(row.get("api_key") or api_key),
                    model=row_model,
                    context_window=row_ctx,
                    provider=row_provider,
                    capabilities=caps,
                    source="db",
                    temperature=float(row_temperature) if row_temperature is not None else None,
                    max_tokens=int(row_max_tokens) if row_max_tokens is not None else None,
                    reasoning_effort=row_reasoning_effort
                    if row_reasoning_effort is not None
                    else None,
                    surface_persisted_reasoning=row_surface_persisted_reasoning,
                    replay_reasoning_to_model=row_replay_reasoning,
                    server_compat=row_server_compat,
                    auth_mode=row_auth_mode,
                    obo_audience=row_obo_audience,
                    obo_scopes=row_obo_scopes,
                    max_concurrency=row.get("max_concurrency", 0),
                )
        except (ModelAuthConfigError, ModelConcurrencyConfigError):
            # Configuration errors are authoritative row content, not a
            # transient storage-read failure. Never degrade past them into a
            # config-only registry or provider SDK environment credentials.
            raise
        except Exception:
            if strict:
                raise
            log.warning("Failed to load model definitions from storage", exc_info=True)

    # 2. Build configs from [models.*] sections (overrides DB for same alias)
    for alias, entry in models_section.items():
        if not isinstance(entry, dict):
            continue
        model_name = entry.get("model", "")
        if not model_name:
            log.warning("Model entry '%s' has no model name, skipping", alias)
            continue
        if not base_url and "base_url" not in entry and "provider" not in entry:
            # No caller endpoint to inherit, and the default provider without
            # a base_url is the commercial OpenAI API: an entry written for a
            # local server would send its prompts there. Refuse it instead.
            log.warning(
                "Model '%s' names neither base_url nor provider — set base_url "
                "(local server) or provider (commercial API) on the [models.%s] "
                "entry; skipping it (a database definition of the same alias, "
                "if any, stays in force)",
                alias,
                alias,
            )
            continue
        entry_base_url = _resolve_env_vars(entry.get("base_url", base_url))
        if "base_url" in entry and not entry_base_url:
            # Same hazard through the back door: an empty value or an unset
            # ${VAR} would resolve to the provider's public endpoint.
            log.warning(
                "Model '%s' has an empty base_url (or its ${VAR} is unset) — set it "
                "on the [models.%s] entry; skipping it (a database definition of "
                "the same alias, if any, stays in force)",
                alias,
                alias,
            )
            continue
        # Per-model sampling overrides from config.toml — invalid values
        # are logged and treated as None (inherit global default).
        entry_temp: float | None = None
        entry_max_tokens: int | None = None
        entry_effort: str | None = None
        raw_temp = entry.get("temperature")
        if raw_temp is not None:
            try:
                entry_temp = float(raw_temp)
                if not 0.0 <= entry_temp <= 2.0:
                    log.warning(
                        "Model '%s' temperature %.2f out of range [0, 2], ignoring",
                        alias,
                        entry_temp,
                    )
                    entry_temp = None
            except (ValueError, TypeError):
                log.warning("Model '%s' has invalid temperature %r, ignoring", alias, raw_temp)
        raw_mt = entry.get("max_tokens")
        if raw_mt is not None:
            try:
                entry_max_tokens = int(raw_mt)
                if entry_max_tokens < 1:
                    log.warning("Model '%s' max_tokens %d < 1, ignoring", alias, entry_max_tokens)
                    entry_max_tokens = None
            except (ValueError, TypeError):
                log.warning("Model '%s' has invalid max_tokens %r, ignoring", alias, raw_mt)
        raw_effort = entry.get("reasoning_effort")
        if raw_effort is not None:
            entry_effort = str(raw_effort)
        entry_caps = (
            dict(entry.get("capabilities", {}))
            if isinstance(entry.get("capabilities"), dict)
            else {}
        )
        entry_server_compat = entry_caps.pop("server_compat", {})
        if not isinstance(entry_server_compat, dict):
            entry_server_compat = {}
        entry_provider = _resolve_openai_provider(entry.get("provider", "openai"), entry_base_url)
        if entry_provider in LOCAL_PROVIDERS and not entry_base_url:
            log.warning(
                "Model '%s' is a %s entry without a base_url — set it on the "
                "[models.%s] entry; skipping",
                alias,
                entry_provider,
                alias,
            )
            continue
        if (
            not base_url
            and entry_provider == "openai"
            and "base_url" not in entry
            and api_base_url_present
        ):
            # provider = "openai" with no base_url is the commercial API. A
            # file that still carries [api] base_url was written when such an
            # entry inherited that endpoint; sending its prompts to the
            # commercial API instead is the one outcome to refuse.
            log.warning(
                "Model '%s' names provider \"openai\" without a base_url while "
                "config.toml still carries [api] base_url, which the server no "
                "longer applies — set base_url on the [models.%s] entry (or drop "
                "[api] if the commercial API is intended); skipping",
                alias,
                alias,
            )
            continue
        if (
            not base_url
            and entry_provider not in LOCAL_PROVIDERS
            and entry_provider != "openai"
            and "base_url" not in entry
            and api_base_url_present
        ):
            # Empty base_url is the normal shape for these providers (the
            # SDK default), so the entry loads — but say where it now goes.
            log.warning(
                "Model '%s' (provider %s) has no base_url and config.toml still carries "
                "[api] base_url, which the server no longer applies — the entry targets "
                "the provider's public endpoint; set base_url on [models.%s] if a local "
                "server was intended",
                alias,
                entry_provider,
                alias,
            )
        entry_auth_mode, entry_obo_audience, entry_obo_scopes = _normalize_auth_mode(
            alias,
            entry.get("auth_mode", "static"),
            entry.get("obo_audience", ""),
            entry.get("obo_scopes", ""),
        )
        configs[alias] = ModelConfig(
            alias=alias,
            base_url=entry_base_url,
            api_key=_resolve_env_vars(entry.get("api_key", api_key)),
            model=model_name,
            # 0 = auto-detect, resolved per definition below
            context_window=_coerce_context_window(entry.get("context_window"), alias),
            provider=entry_provider,
            capabilities=entry_caps,
            source="config",
            temperature=entry_temp,
            max_tokens=entry_max_tokens,
            reasoning_effort=entry_effort,
            server_compat=entry_server_compat,
            auth_mode=entry_auth_mode,
            obo_audience=entry_obo_audience,
            obo_scopes=entry_obo_scopes,
            max_concurrency=entry.get("max_concurrency", 0),
        )

    # 3. Back-compat shim: synthesize a "default" alias from CLI/auto-detected
    # ``--base-url`` + ``--model`` only when no DB or config.toml models exist.
    # Auto-creating "default" alongside DB models leaks a non-routing alias
    # into the public list — the LLM picks it in task_agent
    # ``model=`` and silently bypasses the operator's per-role
    # task_alias override (the "default" alias points at
    # whatever --base-url was at boot, not at the configured default).
    if not configs and model:
        configs["default"] = ModelConfig(
            alias="default",
            base_url=base_url,
            api_key=api_key,
            model=model,
            context_window=context_window,
            provider=_resolve_openai_provider(provider, base_url),
        )

    if not configs:
        if not allow_empty:
            raise ValueError(
                "No model definitions found. Provide --model, configure [models.*] "
                "in config.toml, or add model definitions in the admin panel."
            )
        # Degraded boot: the server starts with no models configured yet and
        # registers normally. Models added via the admin panel hot-reload in
        # without a restart (POST /v1/api/_internal/model-reload). Until then
        # every model lookup raises, so requests fail cleanly rather than the
        # process refusing to start.
        log.warning(
            "No model definitions found — starting with an empty registry. "
            "Add models in the admin panel; they load without a restart."
        )
        return ModelRegistry(models={}, default="")

    configs = _resolve_context_windows(
        configs, detect=detect_context_windows, inherited=context_window, prior=prior
    )

    # Determine default alias
    default_alias = model_section.get("default", "default")
    if default_alias not in configs:
        if "default" in configs:
            default_alias = "default"
        else:
            default_alias = next(iter(configs))
            log.debug(
                "No '%s' model alias; using '%s' as default",
                model_section.get("default", "default"),
                default_alias,
            )

    # Fallback chain
    fallback_raw = model_section.get("fallback", [])
    fallback: list[str] = []
    if isinstance(fallback_raw, list):
        for alias in fallback_raw:
            if alias in configs:
                fallback.append(alias)
            else:
                log.warning("Fallback alias '%s' not found in models, ignoring", alias)

    # Agent model (legacy single-knob fallback for the task_agent sub-agent)
    agent_model = model_section.get("agent_model")
    if agent_model and agent_model not in configs:
        log.warning("Configured agent_model '%s' not found, ignoring", agent_model)
        agent_model = None

    # Per-kind sub-agent model — overrides agent_model for the task role
    task_model = model_section.get("task_model")
    if task_model and task_model not in configs:
        log.warning("Configured task_model '%s' not found, ignoring", task_model)
        task_model = None

    # Per-kind reasoning effort.  None means task inherits the session.
    # Typos in config.toml shouldn't silently flow to the provider — log and
    # drop unknown values, mirroring the model-not-found warning above.
    valid_efforts = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}

    def _validate_effort(value: Any, key: str) -> str | None:
        if value is None:
            return None
        # Treat empty / whitespace as unset.  Operators commonly write
        # `task_effort = ""` to make "leave it default" explicit; warning
        # on that benign case would just be noise.
        coerced = str(value).strip().lower()
        if not coerced:
            return None
        if coerced not in valid_efforts:
            log.warning(
                "Configured %s '%s' is not a recognised effort level "
                "(expected one of %s), ignoring",
                key,
                coerced,
                sorted(valid_efforts),
            )
            return None
        return coerced

    task_effort = _validate_effort(model_section.get("task_effort"), "task_effort")

    return ModelRegistry(
        models=configs,
        default=default_alias,
        fallback=fallback,
        agent_model=agent_model,
        task_model=task_model,
        task_effort=task_effort,
    )


# ---------------------------------------------------------------------------
# Model auto-detection
# ---------------------------------------------------------------------------


def _select_best_model(model_ids: list[str], provider: str) -> str:
    """Pick the best default model from a list of available model IDs.

    - **anthropic**: latest Opus model (highest generation number).
    - **openai**: latest base GPT-N.N model (not mini/nano/pro variants).
    - **other** (local servers): first model in the list.
    """
    import re

    if provider == "anthropic":
        # Prefer opus, then sonnet, then haiku — highest generation first
        opus = [m for m in model_ids if "opus" in m]
        if opus:
            opus.sort(reverse=True)
            return opus[0]
        sonnet = [m for m in model_ids if "sonnet" in m]
        if sonnet:
            sonnet.sort(reverse=True)
            return sonnet[0]
        return model_ids[0]

    if provider == "xai":
        # Prefer base grok-N.N reasoning models (skip image/voice/video
        # variants and dated multi-agent snapshots when a base flagship
        # is available).  Use tuple-of-ints version ordering so
        # ``grok-4.20`` sorts after ``grok-4.3`` — ``float`` would
        # mis-order them (``float("4.20") == 4.2``).
        grok_base_pattern = re.compile(r"^grok-(\d+(?:\.\d+)?)$")
        grok_base_models: list[tuple[tuple[int, ...], str]] = []
        for m in model_ids:
            match = grok_base_pattern.match(m)
            if match:
                grok_base_models.append((_version_tuple(match.group(1)), m))
        if grok_base_models:
            grok_base_models.sort(key=lambda x: x[0], reverse=True)
            return grok_base_models[0][1]
        return model_ids[0]

    if provider == "openai":
        # Prefer base gpt-N.N (not mini/nano/pro/codex/chat variants).
        # Same tuple-of-ints rationale as the xai branch — guards
        # against future ``gpt-5.10`` mis-sorting under ``gpt-5.2``.
        base_pattern = re.compile(r"^gpt-(\d+(?:\.\d+)?)(?:-\d+)?$")
        base_models: list[tuple[tuple[int, ...], str]] = []
        for m in model_ids:
            match = base_pattern.match(m)
            if match:
                base_models.append((_version_tuple(match.group(1)), m))
        if base_models:
            base_models.sort(key=lambda x: x[0], reverse=True)
            return base_models[0][1]
        return model_ids[0]

    return model_ids[0]


def _version_tuple(version_str: str) -> tuple[int, ...]:
    """Parse a dotted version like ``"4.20"`` into ``(4, 20)`` for
    correct numeric ordering.

    ``float`` parsing collapses ``"4.20"`` and ``"4.2"`` to the same
    value, mis-ordering minor-version-20 releases under minor-version-3.
    Tuple comparison treats each component as an integer so
    ``(4, 20) > (4, 3)`` as intended.
    """
    return tuple(int(p) for p in version_str.split("."))


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _extract_context_window(model_obj: Any, provider: str) -> int | None:
    """Extract context window from a model object returned by ``/v1/models``.

    Handles Anthropic and xAI (static capability tables) and the keys the
    OpenAI-compatible servers and gateways put on a model card: vLLM, NIM
    and SGLang (``max_model_len``); OpenRouter, Together and Fireworks
    (``context_length``, OpenRouter also under ``top_provider``); Groq
    (``context_window``); Mistral (``max_context_length``); llama.cpp
    (``meta.n_ctx_train``). Ollama, LM Studio, LiteLLM and DeepSeek put
    nothing on the OpenAI path, and neither do the cloud-hosted paths —
    Azure OpenAI deployments, Bedrock's and Vertex AI's OpenAI-compatible
    endpoints, OCI Generative AI, Databricks serving — so those definitions
    need an explicit window. Returns ``None`` when not available.
    """
    if provider == "anthropic":
        from turnstone.core.providers._anthropic import AnthropicProvider

        return AnthropicProvider().get_capabilities(model_obj.id).context_window
    if provider == "xai":
        from turnstone.core.providers._xai import lookup_grok_capabilities

        return lookup_grok_capabilities(model_obj.id).context_window
    model_data = model_obj.model_dump()
    for key in ("max_model_len", "context_length", "context_window", "max_context_length"):
        found = _positive_int(model_data.get(key))
        if found is not None:
            return found
    meta = model_data.get("meta")
    if isinstance(meta, dict):
        found = _positive_int(meta.get("n_ctx_train"))
        if found is not None:
            return found
    top_provider = model_data.get("top_provider")
    if isinstance(top_provider, dict):
        found = _positive_int(top_provider.get("context_length"))
        if found is not None:
            return found
    return None


def detect_model(
    client: Any,
    log_fn: Any = print,
    provider: str = "openai",
    *,
    fatal: bool = True,
) -> tuple[str | None, int | None]:
    """Auto-detect the model and context window from the API's models endpoint.

    Returns ``(model_id, context_window)`` where *context_window* is
    ``None`` when the backend does not expose it.

    For multi-model APIs (Anthropic, OpenAI), selects a sensible default:
    latest Opus for Anthropic, latest base GPT model for OpenAI.
    For local single-model servers (vLLM, llama.cpp), uses the first model.

    Calls ``log_fn`` for informational messages (defaults to ``print``).

    When *fatal* is ``True`` (default), raises ``SystemExit`` on failure.
    When ``False``, returns ``(None, None)`` so the caller can carry on
    without a bootstrap model.
    """
    try:
        # Use a short timeout for startup detection — the default OpenAI client
        # timeout is 600s read which blocks the main thread for minutes when the
        # backend is unreachable (TCP SYN dropped → kernel retransmit timeout).
        # Disable retries (default 2) to avoid compounding the delay.
        fast_client = client.with_options(timeout=10.0, max_retries=0)
        models = fast_client.models.list()
        if not models.data:
            if fatal:
                log_fn("Error: No models found at server. Use --model to specify.")
                raise SystemExit(1)
            log_fn("Warning: No models found at server — starting in degraded mode.")
            return None, None

        all_ids = [x.id for x in models.data]
        selected_id = _select_best_model(all_ids, provider)
        m = next(x for x in models.data if x.id == selected_id)

        if len(models.data) > 1:
            log_fn(f"Available models: {', '.join(all_ids)}")
            log_fn(f"Using: {m.id} (override with --model)")

        ctx = _extract_context_window(m, provider)
        return m.id, ctx
    except SystemExit:
        raise
    except Exception as e:
        if fatal:
            log_fn(f"Error: Could not connect to server: {e}")
            log_fn("Is the model server running? Start it or use --base-url to point elsewhere.")
            raise SystemExit(1) from e
        log_fn(f"Warning: Could not connect to LLM backend: {e}")
        log_fn("Starting in degraded mode — requests will fail until backend is reachable.")
        return None, None


def probe_model_endpoint(
    provider: str,
    base_url: str,
    api_key: str,
    target_model: str = "",
    *,
    static_table: bool = True,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Stateless probe of a model endpoint.

    Creates a temporary SDK client, calls ``/v1/models``, and returns
    reachability status, available model IDs, detected context window,
    and server type.  Used by the admin *Detect* button and by the
    registry's ``context_window = 0`` resolution — never persists state
    or stores the API key.

    *static_table* lets an OpenAI-compatible endpoint that reports no
    window fall back to the commercial OpenAI table by model id; the
    Detect button shows that number to a human, the registry loader
    passes ``False`` so it is never applied to a local server unseen.
    """
    from turnstone.core.providers import create_client

    result: dict[str, Any] = {
        "reachable": False,
        "model_found": None,
        "available_models": [],
        "context_window": None,
        "server_type": None,
        "error": None,
    }
    client = None
    try:
        client = create_client(provider, base_url=base_url, api_key=api_key)
        fast = client.with_options(timeout=timeout, max_retries=0)
        models = fast.models.list()
        if not models.data:
            result["reachable"] = True
            result["error"] = "No models found at endpoint"
            return result

        all_ids = [m.id for m in models.data]
        result["reachable"] = True
        result["available_models"] = all_ids

        # Determine which model to inspect for context_window
        if target_model:
            result["model_found"] = target_model in all_ids
            inspect_id = target_model if result["model_found"] else all_ids[0]
        else:
            inspect_id = all_ids[0]

        inspect_obj = next((m for m in models.data if m.id == inspect_id), None)

        # --- context window detection ---
        if provider == "anthropic":
            from turnstone.core.providers import lookup_model_capabilities

            known = lookup_model_capabilities("anthropic", inspect_id)
            if known is not None:
                result["context_window"] = known["context_window"]
            result["server_type"] = "anthropic"
        elif provider == "xai":
            from turnstone.core.providers import lookup_model_capabilities

            known = lookup_model_capabilities("xai", inspect_id)
            if known is not None:
                result["context_window"] = known["context_window"]
            result["server_type"] = "xai"
        elif provider == "anthropic-compatible":
            # No static table for local models; vLLM exposes max_model_len
            # as an extra field the Anthropic SDK preserves (extra="allow").
            if inspect_obj is not None:
                max_len = inspect_obj.model_dump().get("max_model_len")
                if isinstance(max_len, int) and max_len > 0:
                    result["context_window"] = max_len
            result["server_type"] = "anthropic-compatible"
        else:
            # OpenAI-compatible path
            _detect_openai_compat(
                result, inspect_obj, inspect_id, base_url, static_table=static_table
            )
    except Exception as exc:
        err_msg = str(exc)
        if len(err_msg) > 500:
            err_msg = err_msg[:500] + "..."
        result["error"] = err_msg
    finally:
        if client is not None and hasattr(client, "close"):
            client.close()
    return result


def _detect_openai_compat(
    result: dict[str, Any],
    model_obj: Any,
    model_id: str,
    base_url: str,
    *,
    static_table: bool = True,
) -> None:
    """Fill context_window and server_type for an OpenAI-compatible endpoint."""

    meta: dict[str, Any] | None = None
    owned_by: str = ""
    dumped: dict[str, Any] = {}
    if model_obj is not None:
        dumped = model_obj.model_dump()
        raw_meta = dumped.get("meta")
        if isinstance(raw_meta, dict):
            meta = raw_meta
        owned_by = str(dumped.get("owned_by", ""))

    # Context window: prefer backend metadata, fall back to static table.
    if model_obj is not None:
        ctx = _extract_context_window(model_obj, "openai")
        if ctx is not None:
            result["context_window"] = ctx
    if result["context_window"] is None and static_table:
        from turnstone.core.providers import lookup_model_capabilities

        known = lookup_model_capabilities("openai", model_id)
        if known is not None:
            result["context_window"] = known["context_window"]

    # Server type heuristics
    from urllib.parse import urlparse

    _normalized = (base_url if "://" in base_url else f"https://{base_url}") if base_url else ""
    _hostname = urlparse(_normalized).hostname or "" if _normalized else ""
    if base_url and (_hostname == "api.openai.com" or _hostname.endswith(".openai.com")):
        result["server_type"] = "openai"
    elif base_url and (_hostname == "api.x.ai" or _hostname.endswith(".x.ai")):
        result["server_type"] = "xai"
    elif meta is not None and "n_ctx_train" in meta:
        result["server_type"] = "llama.cpp"
    elif "sglang" in owned_by.lower():
        result["server_type"] = "sglang"
    elif "/" in (model_id or ""):
        result["server_type"] = "vllm"
    else:
        result["server_type"] = "openai-compatible"

    # Suggest capabilities and server compat based on detected server_type
    from turnstone.core.server_compat import suggest_profile

    suggested = suggest_profile(result.get("server_type", ""), model_id)
    if suggested.get("capabilities"):
        result["suggested_capabilities"] = suggested["capabilities"]
    if suggested.get("server_compat"):
        result["suggested_server_compat"] = suggested["server_compat"]
