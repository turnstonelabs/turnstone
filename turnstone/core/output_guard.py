"""Output guard — heuristic evaluation of tool execution results.

Facet 2 of the three-facet intent validation system.  The judge (Facet 1)
evaluates tool calls BEFORE execution.  The output guard evaluates tool
RESULTS AFTER execution but BEFORE they enter the conversation context.

All checks run in priority order within a wall-clock time budget.  If the
budget is exhausted, the assessment is returned with whatever flags have
been collected so far.  The guard annotates but never gates — callers
decide how to act on the assessment.

Performance target: <30s wall clock (configurable).  Default raised from
5s to 30s in 1.6 to give expanded pattern sets (camouflage detection
added per arXiv:2605.22001) headroom on large tool outputs.  Operators
tune via ``judge.output_guard_budget_seconds``.  Dependencies: stdlib only.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from turnstone.core import fence

if TYPE_CHECKING:
    from collections.abc import Mapping

# -- Priority 1: Prompt injection markers (HIGH) ---------------------------

_RE_OVERRIDE_PHRASES = re.compile(
    r"ignore\s+(?:(?:your|all|any|my|the)\s+)?(?:(?:previous|prior|earlier|existing)\s+)?instructions"
    r"|you\s+are\s+now\b|new\s+instructions\s*:"
    r"|disregard\s+(?:all\s+)?(?:previous|prior)\b|forget\s+your\s+rules"
    r"|ignore\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|rules|guidelines)",
    re.IGNORECASE,
)
_RE_ROLE_INJECTION = re.compile(
    r'\{"role"\s*:\s*"system"|<\|im_start\|>system|<\|im_sep\|>'
    r"|</tool_result>|</function_output>",
)
_RE_INSTRUCTION_OVERRIDE = re.compile(
    r"system\s+prompt\s*:|MANDATORY\s*:|OVERRIDE\s*:|\[SYSTEM\]|\[INST\]",
)
_RE_META_INJECTION = re.compile(
    r"(?:pretend|act\s+as\s+if)\s+you\s+are"
    r"|your\s+new\s+(?:role|identity|persona)\s+is"
    r"|from\s+now\s+on\s+you\s+(?:are|will|must|should)"
    r"|I\s+am\s+your\s+(?:new\s+)?(?:admin|operator|developer|creator)",
    re.IGNORECASE,
)

# -- Priority 1b: Domain-camouflaged injection (MEDIUM) --------------------
# Pair-of-signals (recommendation + authority|caps-action) targets the
# camouflage shape from arXiv:2605.22001 while bounding FP rate on legit
# executive/financial prose.  Partial detection — not sufficient defense
# on its own against capable adversaries; see issue #560 for context.

_RE_CAMO_RECOMMENDATION = re.compile(
    r"\b(?:appropriate|recommended|correct|preferred|optimal|right|advisable|prudent)\s+"
    r"(?:recommendation|action|response|course\s+of\s+action|next\s+step|"
    r"decision|determination|directive|disposition|treatment)\b",
    re.IGNORECASE,
)
_RE_CAMO_AUTHORITY_FRAME = re.compile(
    r"\b(?:consistent\s+with|per|in\s+(?:line|accordance)\s+with|"
    r"as\s+(?:dictated|required|mandated|specified)\s+by|"
    r"pursuant\s+to|in\s+keeping\s+with)\s+"
    r"(?:our|the|company|organizational|established|internal|standard)\s+"
    r"(?:risk\s+framework|policy|policies|mandate|directive|guidelines?|"
    r"standard\s+operating\s+procedures?|protocol|compliance\s+framework|"
    r"governance\s+model)\b",
    re.IGNORECASE,
)
_RE_CAMO_CAPS_ACTION = re.compile(
    r"(?:^|[\s,;.\"'(\[])(?:SELL|BUY|HOLD|TRANSFER|DELETE|REVOKE|APPROVE|EXECUTE|"
    r"DISBURSE|RELEASE|DISCLOSE|EXPORT|TERMINATE|ESCALATE|REROUTE|REDIRECT|"
    r"GRANT|DENY|REMOVE|FORWARD|WIPE|RESET|OVERRIDE)\b",
)

# -- Priority 2: Credential / secret leakage (HIGH) ------------------------

_RE_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY-----"
    r"[\s\S]*?"
    r"-----END\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY-----",
)
# Database connection strings AND http(s) URLs that carry RFC-3986
# userinfo (``user:pass@host``).  Adding http(s) here means a
# misconfigured ``OPENAI_BASE_URL=https://user:pass@host`` that lands
# in an httpx ``ConnectError.__str__`` is redacted by every caller of
# ``redact_credentials`` — error persistence, audit details,
# coordinator inspect/wait surfaces.  The structural form ``[^:@\s]+:
# [^@\s]+@`` is specific enough that ``https://example.com:8080/path``
# (host:port without ``@``) doesn't match.
_RE_CONNECTION_STRING = re.compile(
    r"(?:postgresql\+?(?:psycopg)?|mysql|mongodb(?:\+srv)?|rediss?|amqps?|sqlite|https?)"
    r"://[^:@\s]+:[^@\s]+@",
)
_RE_ENV_SECRET_LINE = re.compile(r"[A-Z][A-Z_0-9]+=\S+")
_RE_ENV_SECRET_KEY = re.compile(
    r"(?:^|_)(?:SECRET|TOKEN|PASSWORD|CREDENTIAL|DSN)(?:_|$)"
    r"|(?:^|_)KEY(?:_|$)"
    r"|^(?:DATABASE_URL|TURNSTONE_DB_URL|DB_URL)$",
    re.IGNORECASE,
)
_RE_JSON_SECRET = re.compile(
    r'"(?:api_key|apikey|api_secret|secret_key|secret|password|passwd|'
    r"token|access_token|refresh_token|auth_token|private_key|"
    r"client_secret|webhook_secret|signing_key|encryption_key|"
    r'x_api_key|x-api-key|'
    r'authorization)"\s*:\s*"([^"]{8,})"',
    re.IGNORECASE,
)
# Single-quoted sibling — Python dict reprs / JS object literals emit single
# quotes (e.g. {'Authorization': 'Bearer ...'}); the double-quoted form above
# misses them.  Same key set, same 8-char value floor, group(1) == value.
_RE_JSON_SECRET_SQ = re.compile(
    r"'(?:api_key|apikey|api_secret|secret_key|secret|password|passwd|"
    r"token|access_token|refresh_token|auth_token|private_key|"
    r"client_secret|webhook_secret|signing_key|encryption_key|"
    r"x_api_key|x-api-key|"
    r"authorization)'\s*:\s*'([^']{8,})'",
    re.IGNORECASE,
)

# (pattern, redact_label) — ordered most-specific first for redaction.
_CREDENTIAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-proj-[a-zA-Z0-9\-]{20,}"), "api_key"),
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "api_key"),
    (re.compile(r"ghp_[a-zA-Z0-9]{36}"), "api_key"),
    (re.compile(r"gho_[a-zA-Z0-9]{36}"), "api_key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "api_key"),
    (re.compile(r"AIza[a-zA-Z0-9_\-]{35}"), "api_key"),
    (re.compile(r"Bearer\s+[a-zA-Z0-9._~+/=\-]{20,}", re.IGNORECASE), "api_key"),
    # Specific credential key suffixes so these swallow the whole
    # access_token=/api_key=/secret_key=/auth_token= assignment instead of
    # chewing only the tail into a garbled "access_[REDACTED:api_key]", while
    # NOT matching innocent identifiers like monkey=, turkey=, over_tokenized=.
    # The trailing _? allows both snake_case and compact forms (api_key / apikey).
    (re.compile(
        r"(?:(?:api|secret|session|auth|encryption|signing|private|public|access)_?key)="
        r"[a-zA-Z0-9]{20,}"
    ), "api_key"),
    (re.compile(
        r"(?:(?:access|refresh|auth|api|session)_?token)="
        r"[a-zA-Z0-9]{20,}"
    ), "api_key"),
]

# -- Priority 3: Encoded / obfuscated payloads (MEDIUM) --------------------

_RE_LARGE_BASE64 = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
_RE_SCRIPT_DATA_URI = re.compile(
    r"data:(?:text/html|application/javascript)(?:;base64,)?",
    re.IGNORECASE,
)
_RE_HEX_SHELLCODE = re.compile(r"(?:\\x[0-9a-fA-F]{2}){10,}")
_RE_BASE64_IMAGE_CONTEXT = re.compile(r"data:image|\.png|\.jpg|\.jpeg|\.gif|\.webp|\.svg")
_RE_BASE64_EXEC_CONTEXT = re.compile(
    r"eval|exec|script|javascript|payload|shell|command|decode|import",
)

# -- Priority 4: Adversarial URLs (MEDIUM) ---------------------------------

_RE_URL_CRED_PARAM = re.compile(
    r"[?&](?:token|key|secret|password|auth|api_key)=",
    re.IGNORECASE,
)
_RE_CLOUD_METADATA = re.compile(
    r"169\.254\.169\.254|metadata\.google\.internal|100\.100\.100\.200",
)

# -- Priority 5: System information disclosure (LOW) -----------------------

_RE_PRIVATE_IP = re.compile(
    r"(?<!\d)(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})(?!\d)",
)
_RE_CLOUD_IDENTITY_DOC = re.compile(
    r"instance-identity|computeMetadata|IMDS|ami-id\b|instance-id\b",
    re.IGNORECASE,
)
_RE_SENSITIVE_PATH = re.compile(
    r"\.env\b|\.ssh/|/credentials\b|\.aws/|\.kube/|\.gnupg/"
    r"|id_rsa\b|id_ecdsa\b|\.pem\b",
    re.IGNORECASE,
)

# -- Helpers ----------------------------------------------------------------

_RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _max_risk(a: str, b: str) -> str:
    return a if _RISK_ORDER.get(a, 0) >= _RISK_ORDER.get(b, 0) else b


def _add_flag(flags: list[str], flag: str) -> None:
    """Append *flag* only if not already present."""
    if flag not in flags:
        flags.append(flag)


# -- Data structures --------------------------------------------------------


@dataclass(frozen=True)
class OutputAssessment:
    """Risk assessment of tool execution output."""

    flags: list[str] = field(default_factory=list)
    risk_level: str = "none"  # "none" | "low" | "medium" | "high"
    annotations: list[str] = field(default_factory=list)
    sanitized: str | None = None

    def to_dict(self, *, include_sanitized: bool = False) -> dict[str, Any]:
        """Serialize for JSON / SSE transport.

        ``sanitized`` is excluded by default to prevent accidental leakage
        of tool output content through SSE or MQ events.
        """
        d: dict[str, Any] = {
            "flags": list(self.flags),
            "risk_level": self.risk_level,
            "annotations": list(self.annotations),
        }
        if include_sanitized:
            d["sanitized"] = self.sanitized
        return d


def _clean() -> OutputAssessment:
    """Return a fresh no-risk assessment (avoids mutable singleton sharing)."""
    return OutputAssessment()


def merge_guard_display_payload(
    *,
    heuristic_risk: str,
    heuristic_flags: list[str] | tuple[str, ...],
    redacted: bool,
    llm_succeeded: bool,
    heuristic_annotations: list[str] | tuple[str, ...] = (),
    llm_risk: str = "none",
    llm_flags: list[str] | tuple[str, ...] = (),
    llm_reasoning: str = "",
    llm_confidence: float = 0.0,
    llm_model: str = "",
) -> dict[str, Any] | None:
    """Merge the heuristic + LLM-judge findings into the single chip payload.

    This is the ONE projection both the live path
    (``ChatSession._evaluate_output`` → ``on_output_warning``) and the
    replay path (``history_decoration``) call, so the inline finding chip
    renders identically live and on reconnect — the wire-shape parity the
    output guard needs.

    Merge rule (issue #560, "show, annotated"):

    * ``risk_level`` = max(heuristic, llm) — a positive from EITHER detector
      surfaces.  A negative ("none") or failed/absent LLM never *lowers* a
      heuristic positive: the judge reads adversarial tool output, so it may
      raise an alarm but must not be able to suppress a deterministic regex
      finding.  Credential redaction is a heuristic-only signal handled by
      the caller via ``redacted`` and is likewise never overridden.
    * ``flags`` = union(heuristic, llm).
    * ``annotations`` = the heuristic's human-readable messages (the only
      prose a regex-only finding carries — the LLM's prose lives in the
      separate ``reasoning`` field).  Emitted only when non-empty.
    * When the LLM produced a verdict (``llm_succeeded``) its own risk
      (``judge_risk``), confidence, reasoning, and model ride along as
      annotation under ``tier="llm"`` — so the operator sees the judge's
      opinion even when it disagrees with the displayed (merged) risk.  A
      failed / disabled LLM leaves ``tier="heuristic"`` and no badge.

    Returns ``None`` when there is nothing to show — merged risk ``"none"``
    and no redaction — the skip-on-clean contract both surfaces rely on.
    """
    risk = _max_risk(heuristic_risk or "none", llm_risk if llm_succeeded else "none")
    flags: list[str] = []
    candidate_flags = list(heuristic_flags) + (list(llm_flags) if llm_succeeded else [])
    for f in candidate_flags:
        if f and f not in flags:
            flags.append(f)

    if risk == "none" and not redacted:
        return None

    payload: dict[str, Any] = {
        "risk_level": risk,
        "flags": flags,
        "redacted": bool(redacted),
    }
    if heuristic_annotations:
        payload["annotations"] = list(heuristic_annotations)
    if llm_succeeded:
        payload["tier"] = "llm"
        payload["judge_risk"] = llm_risk
        payload["confidence"] = llm_confidence
        if llm_reasoning:
            payload["reasoning"] = llm_reasoning
        if llm_model:
            payload["judge_model"] = llm_model
    else:
        payload["tier"] = "heuristic"
    return payload


@dataclass(frozen=True)
class OutputGuardPatternDef:
    """A pattern definition for output guard scanning."""

    name: str
    category: str  # prompt_injection/credentials/encoded_payloads/adversarial_urls/info_disclosure
    risk_level: str  # high/medium/low
    compiled: re.Pattern[str]  # pre-compiled regex
    flag_name: str  # e.g. "prompt_injection", "credential_leak"
    annotation: str  # human-readable message
    is_credential: bool = False  # triggers redaction
    redact_label: str = ""  # e.g. "api_key"
    priority: int = 0  # order within category (higher = first)


# -- Built-in pattern definitions (consumed by rule_registry.RuleRegistry) ---

_BUILTIN_OG_PATTERNS: list[OutputGuardPatternDef] = [
    # -- prompt_injection (priority 1, high) --
    OutputGuardPatternDef(
        name="override_phrases",
        category="prompt_injection",
        risk_level="high",
        compiled=_RE_OVERRIDE_PHRASES,
        flag_name="prompt_injection",
        annotation="Output contains phrases that attempt to override agent instructions.",
        priority=40,
    ),
    OutputGuardPatternDef(
        name="role_injection",
        category="prompt_injection",
        risk_level="high",
        compiled=_RE_ROLE_INJECTION,
        flag_name="role_injection",
        annotation="Output contains role/message injection markers.",
        priority=30,
    ),
    OutputGuardPatternDef(
        name="instruction_override",
        category="prompt_injection",
        risk_level="high",
        compiled=_RE_INSTRUCTION_OVERRIDE,
        flag_name="instruction_override",
        annotation="Output contains instruction-override keywords (MANDATORY, OVERRIDE, etc.).",
        priority=20,
    ),
    OutputGuardPatternDef(
        name="meta_injection",
        category="prompt_injection",
        risk_level="high",
        compiled=_RE_META_INJECTION,
        flag_name="meta_injection",
        annotation="Output attempts to redefine the agent's identity or persona.",
        priority=10,
    ),
    # -- credentials (priority 2, high) --
    OutputGuardPatternDef(
        name="credential_sk_proj",
        category="credentials",
        risk_level="high",
        compiled=re.compile(r"sk-proj-[a-zA-Z0-9\-]{20,}"),
        flag_name="credential_leak",
        annotation="Output contains what appears to be an API key or token.",
        is_credential=True,
        redact_label="api_key",
        priority=90,
    ),
    OutputGuardPatternDef(
        name="credential_sk",
        category="credentials",
        risk_level="high",
        compiled=re.compile(r"sk-[a-zA-Z0-9]{20,}"),
        flag_name="credential_leak",
        annotation="Output contains what appears to be an API key or token.",
        is_credential=True,
        redact_label="api_key",
        priority=80,
    ),
    OutputGuardPatternDef(
        name="credential_ghp",
        category="credentials",
        risk_level="high",
        compiled=re.compile(r"ghp_[a-zA-Z0-9]{36}"),
        flag_name="credential_leak",
        annotation="Output contains what appears to be an API key or token.",
        is_credential=True,
        redact_label="api_key",
        priority=70,
    ),
    OutputGuardPatternDef(
        name="credential_gho",
        category="credentials",
        risk_level="high",
        compiled=re.compile(r"gho_[a-zA-Z0-9]{36}"),
        flag_name="credential_leak",
        annotation="Output contains what appears to be an API key or token.",
        is_credential=True,
        redact_label="api_key",
        priority=60,
    ),
    OutputGuardPatternDef(
        name="credential_akia",
        category="credentials",
        risk_level="high",
        compiled=re.compile(r"AKIA[0-9A-Z]{16}"),
        flag_name="credential_leak",
        annotation="Output contains what appears to be an API key or token.",
        is_credential=True,
        redact_label="api_key",
        priority=50,
    ),
    OutputGuardPatternDef(
        name="credential_aiza",
        category="credentials",
        risk_level="high",
        compiled=re.compile(r"AIza[a-zA-Z0-9_\-]{35}"),
        flag_name="credential_leak",
        annotation="Output contains what appears to be an API key or token.",
        is_credential=True,
        redact_label="api_key",
        priority=40,
    ),
    OutputGuardPatternDef(
        name="credential_bearer",
        category="credentials",
        risk_level="high",
        compiled=re.compile(r"Bearer\s+[a-zA-Z0-9._~+/=\-]{20,}", re.IGNORECASE),
        flag_name="credential_leak",
        annotation="Output contains what appears to be an API key or token.",
        is_credential=True,
        redact_label="api_key",
        priority=30,
    ),
    OutputGuardPatternDef(
        name="credential_token_param",
        category="credentials",
        risk_level="high",
        compiled=re.compile(
            r"(?:(?:access|refresh|auth|api|session)_?token)="
            r"[a-zA-Z0-9]{20,}"
        ),
        flag_name="credential_leak",
        annotation="Output contains what appears to be an API key or token.",
        is_credential=True,
        redact_label="api_key",
        priority=20,
    ),
    OutputGuardPatternDef(
        name="credential_key_param",
        category="credentials",
        risk_level="high",
        compiled=re.compile(
            r"(?:(?:api|secret|session|auth|encryption|signing|private|public|access)_?key)="
            r"[a-zA-Z0-9]{20,}"
        ),
        flag_name="credential_leak",
        annotation="Output contains what appears to be an API key or token.",
        is_credential=True,
        redact_label="api_key",
        priority=10,
    ),
    # NOTE: private_key_block and connection_string are NOT in _BUILTIN_OG_PATTERNS
    # because they require custom redaction logic (preserve protocol/username in
    # connection strings, match PEM block boundaries).  They are handled by
    # _check_credentials_complex() instead.
    # -- encoded_payloads (priority 3, medium) --
    OutputGuardPatternDef(
        name="script_data_uri",
        category="encoded_payloads",
        risk_level="medium",
        compiled=_RE_SCRIPT_DATA_URI,
        flag_name="script_data_uri",
        annotation="Output contains a data URI with executable content.",
        priority=30,
    ),
    OutputGuardPatternDef(
        name="hex_shellcode",
        category="encoded_payloads",
        risk_level="medium",
        compiled=_RE_HEX_SHELLCODE,
        flag_name="hex_shellcode",
        annotation="Output contains hex-encoded byte sequences resembling shellcode.",
        priority=20,
    ),
    # -- adversarial_urls (priority 4, medium) --
    OutputGuardPatternDef(
        name="url_cred_param",
        category="adversarial_urls",
        risk_level="medium",
        compiled=_RE_URL_CRED_PARAM,
        flag_name="url_credential_param",
        annotation="Output contains URLs with credential-bearing query parameters.",
        priority=20,
    ),
    OutputGuardPatternDef(
        name="cloud_metadata",
        category="adversarial_urls",
        risk_level="medium",
        compiled=_RE_CLOUD_METADATA,
        flag_name="cloud_metadata_access",
        annotation="Output references cloud metadata endpoints.",
        priority=10,
    ),
    # -- info_disclosure (priority 5, low) --
    OutputGuardPatternDef(
        name="cloud_identity_doc",
        category="info_disclosure",
        risk_level="low",
        compiled=_RE_CLOUD_IDENTITY_DOC,
        flag_name="cloud_identity_disclosure",
        annotation="Output contains cloud instance identity metadata.",
        priority=20,
    ),
    OutputGuardPatternDef(
        name="sensitive_path",
        category="info_disclosure",
        risk_level="low",
        compiled=_RE_SENSITIVE_PATH,
        flag_name="sensitive_path_disclosure",
        annotation="Output references sensitive file paths (.env, .ssh/, .aws/, etc.).",
        priority=10,
    ),
]


# -- Check functions (one per priority tier) --------------------------------


def _check_prompt_injection(text: str, flags: list[str], ann: list[str]) -> str:
    """Priority 1: prompt injection markers.  Returns risk contribution."""
    risk = "none"
    if _RE_OVERRIDE_PHRASES.search(text):
        flags.append("prompt_injection")
        ann.append("Output contains phrases that attempt to override agent instructions.")
        risk = "high"
    if _RE_ROLE_INJECTION.search(text):
        _add_flag(flags, "prompt_injection")
        flags.append("role_injection")
        ann.append("Output contains role/message injection markers.")
        risk = _max_risk(risk, "high")
    if _RE_INSTRUCTION_OVERRIDE.search(text):
        _add_flag(flags, "prompt_injection")
        flags.append("instruction_override")
        ann.append("Output contains instruction-override keywords (MANDATORY, OVERRIDE, etc.).")
        risk = _max_risk(risk, "high")
    if _RE_META_INJECTION.search(text):
        _add_flag(flags, "prompt_injection")
        flags.append("meta_injection")
        ann.append("Output attempts to redefine the agent's identity or persona.")
        risk = _max_risk(risk, "high")
    risk = _max_risk(risk, _check_camouflage(text, flags, ann))
    return risk


def _check_credentials(
    text: str,
    flags: list[str],
    ann: list[str],
) -> tuple[str, str | None]:
    """Priority 2: credential leakage.  Returns (risk, sanitized_or_None)."""
    risk = "none"
    found = False

    for pattern, _label in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            _add_flag(flags, "credential_leak")
            if "Output contains what appears to be an API key or token." not in ann:
                ann.append("Output contains what appears to be an API key or token.")
            found = True
            risk = "high"
            break  # one hit is enough to flag + trigger redaction

    if _RE_PRIVATE_KEY_BLOCK.search(text):
        _add_flag(flags, "credential_leak")
        flags.append("private_key_leak")
        ann.append("Output contains a PEM-encoded private key block.")
        found = True
        risk = "high"

    if _RE_CONNECTION_STRING.search(text):
        _add_flag(flags, "credential_leak")
        flags.append("connection_string_leak")
        ann.append("Output contains a connection string with embedded credentials.")
        found = True
        risk = "high"

    env_lines = _RE_ENV_SECRET_LINE.findall(text)
    if any(_RE_ENV_SECRET_KEY.search(ln.split("=", 1)[0]) for ln in env_lines):
        _add_flag(flags, "credential_leak")
        flags.append("env_file_leak")
        ann.append("Output contains .env-style assignments with secret-bearing keys.")
        found = True
        risk = "high"

    if _RE_JSON_SECRET.search(text) or _RE_JSON_SECRET_SQ.search(text):
        _add_flag(flags, "credential_leak")
        flags.append("json_secret_leak")
        ann.append(
            "Output contains JSON with secret-bearing keys (api_key, password, token, etc.)."
        )
        found = True
        risk = "high"

    return risk, _redact_credentials(text) if found else None


def redact_credentials(text: str) -> str:
    """Public entry point — apply the credential-redaction pattern set
    to an arbitrary string and return the sanitized form.

    Used by call sites (close_reason persistence, audit details) that
    want the same secret-scrubbing the output guard applies to tool
    output without paying for the full evaluate_output assessment.
    """
    return _redact_credentials(text)


def _redact_credentials(text: str) -> str:
    """Replace detected credentials with redaction markers."""
    result = _RE_PRIVATE_KEY_BLOCK.sub("[REDACTED:private_key]", text)

    def _redact_conn(m: re.Match[str]) -> str:
        return re.sub(r"://([^:@\s]+):([^@\s]+)@", r"://\1:[REDACTED:password]@", m.group())

    result = _RE_CONNECTION_STRING.sub(_redact_conn, result)
    for pattern, redact_type in _CREDENTIAL_PATTERNS:
        result = pattern.sub(f"[REDACTED:{redact_type}]", result)

    def _redact_env(m: re.Match[str]) -> str:
        key = m.group().split("=", 1)[0]
        return key + "=[REDACTED:secret]" if _RE_ENV_SECRET_KEY.search(key) else m.group()

    result = _RE_ENV_SECRET_LINE.sub(_redact_env, result)

    def _redact_json_secret(m: re.Match[str]) -> str:
        # Positional replacement to avoid corrupting key when value == key name
        start = m.start(1) - m.start()
        end = m.end(1) - m.start()
        full = m.group()
        return full[:start] + "[REDACTED:secret]" + full[end:]

    result = _RE_JSON_SECRET.sub(_redact_json_secret, result)
    result = _RE_JSON_SECRET_SQ.sub(_redact_json_secret, result)
    return result


# -- Configurable-mode helpers (used when patterns kwarg is provided) --------

# Category → parent flag (idempotently added for each pattern match in that category)
_CATEGORY_PARENT_FLAGS: dict[str, str] = {
    "prompt_injection": "prompt_injection",
    "credentials": "credential_leak",
}


def _check_patterns(
    text: str,
    category_patterns: tuple[OutputGuardPatternDef, ...],
    flags: list[str],
    ann: list[str],
    parent_flag: str = "",
) -> tuple[str, str | None]:
    """Run configurable patterns for a category.  Returns (risk, sanitized_or_None)."""
    risk = "none"
    sanitized: str | None = None
    need_redact = False
    for pat in category_patterns:
        if pat.compiled.search(text):
            if parent_flag:
                _add_flag(flags, parent_flag)
            _add_flag(flags, pat.flag_name)
            if pat.annotation not in ann:
                ann.append(pat.annotation)
            risk = _max_risk(risk, pat.risk_level)
            if pat.is_credential:
                need_redact = True
    if need_redact:
        sanitized = _redact_with_patterns(text, category_patterns)
    return risk, sanitized


def _redact_with_patterns(
    text: str,
    patterns: tuple[OutputGuardPatternDef, ...],
) -> str:
    """Redact text using credential patterns from the given pattern set."""
    result = text
    for pat in patterns:
        if pat.is_credential and pat.redact_label:
            result = pat.compiled.sub(f"[REDACTED:{pat.redact_label}]", result)
    return result


def _check_camouflage(text: str, flags: list[str], ann: list[str]) -> str:
    """Complex check for domain-camouflaged prompt injection.

    Pair-of-signals to keep FP rate manageable: a lone authority frame
    or a lone caps action verb is too common in legitimate executive /
    financial / legal content; combined with an imperative recommendation
    structure, it matches the camouflage shape from arXiv:2605.22001.

    Risk level is MEDIUM and the annotation explicitly notes partial
    detection — operators are expected to layer a semantic evaluator
    on capable models for high-risk inbound surfaces.
    """
    has_recommendation = bool(_RE_CAMO_RECOMMENDATION.search(text))
    if not has_recommendation:
        return "none"
    has_authority = bool(_RE_CAMO_AUTHORITY_FRAME.search(text))
    has_caps_action = bool(_RE_CAMO_CAPS_ACTION.search(text))
    if not (has_authority or has_caps_action):
        return "none"
    _add_flag(flags, "prompt_injection")
    _add_flag(flags, "camouflaged_injection")
    ann.append(
        "Output contains an imperative recommendation paired with an authority "
        "frame or caps-action verb — possible domain-camouflaged injection "
        "(see arXiv:2605.22001). Partial detection; consider semantic review."
    )
    return "medium"


# Trust-fence markers (``[start system-reminder…]`` operator fold,
# ``[start tool_output…]`` judge fence, ``[start sender-label…]`` shared-
# workstream attribution — see :mod:`turnstone.core.fence`).  None of these is
# ever legitimate *inside* tool output, so their appearance there is a forgery
# signal.  Built from :func:`fence.detection_pattern` so the detector tracks
# the exact marker shape :func:`fence.wrap` emits; group 1 captures the
# optional ``_<hex>`` nonce suffix, so a nonced marker is caught whether or not
# the hex is this session's real token.
_RE_FENCE_MARKER = fence.detection_pattern(
    (fence.SYSTEM_REMINDER_TAG, fence.TOOL_OUTPUT_TAG, fence.SENDER_LABEL_TAG)
)


def _check_marker_forgery(
    text: str,
    flags: list[str],
    ann: list[str],
    trusted_nonce: str,
    trusted_sender_label_nonce: str = "",
) -> str:
    """Flag trust-fence markers smuggled into untrusted tool output.

    The fold path declares ``[start system-reminder_{nonce}]`` as the sole
    trusted operator marker, the judge fences tool output in
    ``[start tool_output_{nonce}]``, and a shared workstream declares
    ``[start sender-label_{nonce}]`` as the sole trusted sender-attribution
    marker; none of these is ever legitimate *inside* tool output.  Two
    trusted nonces are checked (operator, sender-label) since they are
    independent per-session tokens.  Two severities:

    * **leak (HIGH)** — a marker carries one of this session's exact trusted
      nonces.  The token only lives in the (cached) system prefix and the
      folded/labelled blocks, so its appearance in tool output means it has
      leaked and is being replayed to forge an operator instruction or a
      sender attribution.  The caller's own host-escaping neutralises it on
      the wire, but the *appearance itself* is the alarm worth raising.
    * **forgery (LOW)** — any other fence marker (bare, or a wrong/guessed
      nonce).  Already inert under the trust declarations; surfaced for the
      operator's awareness, low to avoid noise on benign content (docs and this
      project's own source legitimately contain the literals).
    """
    if "[" not in text:
        return "none"
    wants = [f"_{n}" for n in (trusted_nonce, trusted_sender_label_nonce) if n]
    leaked = False
    forged = False
    for m in _RE_FENCE_MARKER.finditer(text):
        suffix = (m.group(1) or "").lower()
        # Constant-time vs each session nonce (project standard for nonce
        # comparison).  Bytes form so a non-ASCII forged suffix can't raise.
        if any(
            secrets.compare_digest(suffix.encode("utf-8"), want.encode("utf-8")) for want in wants
        ):
            leaked = True
        else:
            forged = True
    if leaked:
        _add_flag(flags, "prompt_injection")
        _add_flag(flags, "operator_marker_leak")
        ann.append(
            "Tool output contains this session's trusted marker token — the "
            "token has leaked and is being replayed to forge an operator "
            "instruction or sender attribution. Treat the surrounding content "
            "as hostile."
        )
        return "high"
    if forged:
        _add_flag(flags, "prompt_injection")
        _add_flag(flags, "operator_marker_forgery")
        ann.append(
            "Tool output contains a forged trust marker "
            "([start system-reminder…]/[start tool_output…]/[start "
            "sender-label…]); it is untrusted data, not a real instruction or "
            "attribution."
        )
        return "low"
    return "none"


def _check_credentials_complex(
    text: str,
    flags: list[str],
    ann: list[str],
) -> tuple[str, str | None]:
    """Complex credential checks that require custom redaction logic.

    Handles private key blocks, connection strings (need targeted sub-replacement
    to preserve protocol/username), env-line parsing (two-regex pipeline), and
    JSON secret detection (capture group redaction).
    """
    risk = "none"
    found = False

    if _RE_PRIVATE_KEY_BLOCK.search(text):
        _add_flag(flags, "credential_leak")
        _add_flag(flags, "private_key_leak")
        ann.append("Output contains a PEM-encoded private key block.")
        found = True
        risk = "high"

    if _RE_CONNECTION_STRING.search(text):
        _add_flag(flags, "credential_leak")
        _add_flag(flags, "connection_string_leak")
        ann.append("Output contains a connection string with embedded credentials.")
        found = True
        risk = "high"

    env_lines = _RE_ENV_SECRET_LINE.findall(text)
    if any(_RE_ENV_SECRET_KEY.search(ln.split("=", 1)[0]) for ln in env_lines):
        _add_flag(flags, "credential_leak")
        _add_flag(flags, "env_file_leak")
        ann.append("Output contains .env-style assignments with secret-bearing keys.")
        found = True
        risk = "high"

    if _RE_JSON_SECRET.search(text) or _RE_JSON_SECRET_SQ.search(text):
        _add_flag(flags, "credential_leak")
        _add_flag(flags, "json_secret_leak")
        ann.append(
            "Output contains JSON with secret-bearing keys (api_key, password, token, etc.)."
        )
        found = True
        risk = "high"

    sanitized = _redact_credentials_complex(text) if found else None
    return risk, sanitized


def _redact_credentials_complex(text: str) -> str:
    """Redact private keys, connection strings, env-lines, and JSON secrets.

    Uses targeted sub-replacement to preserve context (protocol, username)
    in connection strings and PEM block boundaries.
    """
    result = _RE_PRIVATE_KEY_BLOCK.sub("[REDACTED:private_key]", text)

    def _redact_conn(m: re.Match[str]) -> str:
        return re.sub(r"://([^:@\s]+):([^@\s]+)@", r"://\1:[REDACTED:password]@", m.group())

    result = _RE_CONNECTION_STRING.sub(_redact_conn, result)

    def _redact_env(m: re.Match[str]) -> str:
        key = m.group().split("=", 1)[0]
        return key + "=[REDACTED:secret]" if _RE_ENV_SECRET_KEY.search(key) else m.group()

    result = _RE_ENV_SECRET_LINE.sub(_redact_env, result)

    def _redact_json_secret(m: re.Match[str]) -> str:
        start = m.start(1) - m.start()
        end = m.end(1) - m.start()
        full = m.group()
        return full[:start] + "[REDACTED:secret]" + full[end:]

    result = _RE_JSON_SECRET.sub(_redact_json_secret, result)
    result = _RE_JSON_SECRET_SQ.sub(_redact_json_secret, result)
    return result


def _check_encoded_payloads_complex(
    text: str,
    flags: list[str],
    ann: list[str],
) -> str:
    """Complex encoded payload check (base64 context analysis)."""
    risk = "none"
    for m in _RE_LARGE_BASE64.finditer(text):
        ctx = text[max(0, m.start() - 100) : m.start()].lower()
        if _RE_BASE64_IMAGE_CONTEXT.search(ctx):
            continue
        if _RE_BASE64_EXEC_CONTEXT.search(ctx):
            _add_flag(flags, "encoded_payload")
            ann.append("Output contains a large base64 block in an executable context.")
            risk = _max_risk(risk, "medium")
            break
    return risk


def _check_info_disclosure_complex(
    text: str,
    flags: list[str],
    ann: list[str],
) -> str:
    """Complex info disclosure check (private IP with 127.0.0.1 exclusion)."""
    risk = "none"
    private_ips = [ip for ip in _RE_PRIVATE_IP.findall(text) if ip != "127.0.0.1"]
    if private_ips:
        _add_flag(flags, "private_ip_disclosure")
        ann.append("Output contains internal/private IP addresses (RFC 1918 ranges).")
        risk = "low"
    return risk


# -- Legacy check functions (one per priority tier) -------------------------


def _check_encoded_payloads(text: str, flags: list[str], ann: list[str]) -> str:
    """Priority 3: encoded / obfuscated payloads."""
    risk = "none"
    if _RE_SCRIPT_DATA_URI.search(text):
        flags.append("script_data_uri")
        ann.append("Output contains a data URI with executable content.")
        risk = "medium"
    if _RE_HEX_SHELLCODE.search(text):
        flags.append("hex_shellcode")
        ann.append("Output contains hex-encoded byte sequences resembling shellcode.")
        risk = "medium"
    for m in _RE_LARGE_BASE64.finditer(text):
        ctx = text[max(0, m.start() - 100) : m.start()].lower()
        if _RE_BASE64_IMAGE_CONTEXT.search(ctx):
            continue
        if _RE_BASE64_EXEC_CONTEXT.search(ctx):
            flags.append("encoded_payload")
            ann.append("Output contains a large base64 block in an executable context.")
            risk = _max_risk(risk, "medium")
            break
    return risk


def _check_adversarial_urls(text: str, flags: list[str], ann: list[str]) -> str:
    """Priority 4: adversarial URLs."""
    risk = "none"
    if _RE_URL_CRED_PARAM.search(text):
        flags.append("url_credential_param")
        ann.append("Output contains URLs with credential-bearing query parameters.")
        risk = "medium"
    if _RE_CLOUD_METADATA.search(text):
        flags.append("cloud_metadata_access")
        ann.append("Output references cloud metadata endpoints.")
        risk = "medium"
    if _RE_SCRIPT_DATA_URI.search(text) and "script_data_uri" not in flags:
        flags.append("script_data_uri")
        ann.append("Output contains a data URI with script content.")
        risk = "medium"
    return risk


def _check_info_disclosure(text: str, flags: list[str], ann: list[str]) -> str:
    """Priority 5: system information disclosure."""
    risk = "none"
    private_ips = [ip for ip in _RE_PRIVATE_IP.findall(text) if ip != "127.0.0.1"]
    if private_ips:
        flags.append("private_ip_disclosure")
        ann.append("Output contains internal/private IP addresses (RFC 1918 ranges).")
        risk = "low"
    if _RE_CLOUD_IDENTITY_DOC.search(text):
        flags.append("cloud_identity_disclosure")
        ann.append("Output contains cloud instance identity metadata.")
        risk = _max_risk(risk, "low")
    if _RE_SENSITIVE_PATH.search(text):
        flags.append("sensitive_path_disclosure")
        ann.append("Output references sensitive file paths (.env, .ssh/, .aws/, etc.).")
        risk = _max_risk(risk, "low")
    return risk


# -- Public API -------------------------------------------------------------


_CATEGORY_ORDER = (
    "prompt_injection",
    "credentials",
    "encoded_payloads",
    "adversarial_urls",
    "info_disclosure",
)


def evaluate_output(
    output: str,
    *,
    func_name: str = "",
    call_id: str = "",
    budget_seconds: float = 30.0,
    patterns: Mapping[str, tuple[OutputGuardPatternDef, ...]] | None = None,
    trusted_marker_nonce: str = "",
    trusted_sender_label_nonce: str = "",
) -> OutputAssessment:
    """Evaluate tool output for security signals.

    Runs pattern checks in priority order within the time budget.
    Returns immediately when the budget is exhausted with partial results.

    Args:
        output: The raw tool execution output string.
        func_name: Name of the tool that produced the output (for future use).
        call_id: Unique call identifier (for future correlation).
        budget_seconds: Maximum wall-clock seconds to spend on evaluation.
        patterns: Optional category-grouped patterns from :class:`RuleRegistry`.
            When provided, configurable patterns are used instead of the
            hard-coded check functions.  Complex multi-step checks (env-line
            parsing, base64 context analysis, etc.) always run regardless.
        trusted_marker_nonce: This session's operator-fence nonce (see
            :mod:`turnstone.core.fence`).  When set, tool output is scanned for
            forged trust-fence markers; an exact-nonce match is flagged HIGH
            (token leaked + replayed), any other marker LOW.  Empty disables the
            check (e.g. native models that don't use the fold fence).
        trusted_sender_label_nonce: This session's sender-label nonce (shared
            workstreams only), checked the same way and independently of
            ``trusted_marker_nonce`` — either token's leak is a HIGH finding.
            Empty disables that half of the check (single-user workstreams).

    Returns:
        Frozen OutputAssessment with flags, risk level, annotations, and
        optionally a sanitized copy of the output (credential redaction only).
    """
    if not output:
        return _clean()

    deadline = time.monotonic() + budget_seconds
    flags: list[str] = []
    ann: list[str] = []
    risk = "none"
    sanitized: str | None = None

    if patterns is not None:
        # Configurable mode: use registry patterns + complex checks
        for cat in _CATEGORY_ORDER:
            cat_pats = patterns.get(cat, ())
            if cat_pats:
                parent = _CATEGORY_PARENT_FLAGS.get(cat, "")
                pat_risk, pat_sanitized = _check_patterns(
                    output,
                    cat_pats,
                    flags,
                    ann,
                    parent,
                )
                risk = _max_risk(risk, pat_risk)
                if pat_sanitized:
                    sanitized = pat_sanitized if sanitized is None else pat_sanitized
            # Run hard-coded complex checks for categories that need them
            if cat == "prompt_injection":
                risk = _max_risk(risk, _check_camouflage(output, flags, ann))
                risk = _max_risk(
                    risk,
                    _check_marker_forgery(
                        output, flags, ann, trusted_marker_nonce, trusted_sender_label_nonce
                    ),
                )
            elif cat == "credentials":
                # Chain redaction: apply complex checks to already-sanitized text
                cred_input = sanitized if sanitized is not None else output
                cred_risk, cred_san = _check_credentials_complex(cred_input, flags, ann)
                risk = _max_risk(risk, cred_risk)
                if cred_san:
                    sanitized = cred_san
            elif cat == "encoded_payloads":
                risk = _max_risk(
                    risk,
                    _check_encoded_payloads_complex(output, flags, ann),
                )
            elif cat == "info_disclosure":
                risk = _max_risk(
                    risk,
                    _check_info_disclosure_complex(output, flags, ann),
                )
            if time.monotonic() > deadline:
                return _build(flags, risk, ann, sanitized)
        return _build(flags, risk, ann, sanitized)

    # Legacy mode: hard-coded patterns (backward compat)

    # Priority 1: prompt injection (always run, highest priority)
    risk = _max_risk(risk, _check_prompt_injection(output, flags, ann))
    risk = _max_risk(
        risk,
        _check_marker_forgery(output, flags, ann, trusted_marker_nonce, trusted_sender_label_nonce),
    )
    if time.monotonic() > deadline:
        return _build(flags, risk, ann, sanitized)

    # Priority 2: credential leakage
    cred_risk, sanitized = _check_credentials(output, flags, ann)
    risk = _max_risk(risk, cred_risk)
    if time.monotonic() > deadline:
        return _build(flags, risk, ann, sanitized)

    # Priority 3: encoded / obfuscated payloads
    risk = _max_risk(risk, _check_encoded_payloads(output, flags, ann))
    if time.monotonic() > deadline:
        return _build(flags, risk, ann, sanitized)

    # Priority 4: adversarial URLs
    risk = _max_risk(risk, _check_adversarial_urls(output, flags, ann))
    if time.monotonic() > deadline:
        return _build(flags, risk, ann, sanitized)

    # Priority 5: system information disclosure
    risk = _max_risk(risk, _check_info_disclosure(output, flags, ann))

    return _build(flags, risk, ann, sanitized)


def _build(
    flags: list[str],
    risk_level: str,
    annotations: list[str],
    sanitized: str | None,
) -> OutputAssessment:
    """Construct a frozen OutputAssessment, deduplicating flags and annotations."""
    seen: set[str] = set()
    unique: list[str] = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    seen_ann: set[str] = set()
    unique_ann: list[str] = []
    for a in annotations:
        if a not in seen_ann:
            seen_ann.add(a)
            unique_ann.append(a)
    return OutputAssessment(
        flags=unique,
        risk_level=risk_level,
        annotations=unique_ann,
        sanitized=sanitized,
    )
