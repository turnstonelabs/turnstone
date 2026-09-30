// redact_credentials.js — client-side credential redaction for tool call cards.
//
// Visual-only censorship of credentials in tool output BEFORE it hits the DOM.
// Mirrors the backend patterns in turnstone/core/output_guard.py so the
// frontend and backend redaction stay consistent.
//
// ES module — imported by conversation.js (shared substrate) and by
// interactive.js directly (which also replaces its legacy _redactApiKeys).
// Pure function, no DOM dependency, safe to test via `node -e`.
//
// Patterns (in order of application):
//   1. PEM private key blocks        → [REDACTED:private_key]
//   2. Connection strings            → user:[REDACTED:password]@host
//   3. Well-known API key formats    → [REDACTED:api_key]
//      (sk-proj-, sk-, ghp_, gho_, AKIA, AIza, Bearer token, JWT,
//      credential-named query parameters, token=, key=)
//   4. Query-string api_key/token    → key=***  (backward compat)
//   5. JSON-style key/value          → "key": "***"  (backward compat)
//   6. JSON secret keys              → "secret": "[REDACTED:secret]"
//   7. ENV secret lines              → SECRET_KEY=[REDACTED:secret]
//
// A single prefilter scan (_RE_PREFILTER) bails out before all of the
// above when the text cannot contain any credential — the common case
// for plain-log tool output.
//
// House style: no innerHTML, no DOM access, no side-effects.

// ---------------------------------------------------------------------------
// PEM private key blocks (multiline, whole-block replacement)
// ---------------------------------------------------------------------------
const _RE_PRIVATE_KEY_BLOCK =
  /-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY-----/g;

// ---------------------------------------------------------------------------
// Connection strings — preserves protocol + user, redacts only the password
//   postgresql://user:pass@host   →   postgresql://user:[REDACTED:password]@host
//   https://user:token@api.example.com → https://user:[REDACTED:password]@api.example.com
// The optional +suffix covers SQLAlchemy dialect+driver URLs
// (postgresql+psycopg2, postgresql+asyncpg, mysql+pymysql) and
// mongodb+srv — enumerating drivers is a losing game, the suffix
// shape isn't.  Schemes are case-insensitive per RFC 3986 (/i):
// POSTGRESQL:// leaks the same password postgresql:// does.
// ---------------------------------------------------------------------------
// The password stops where another connection string could start; scanning
// on to the next "@" rescanned a long run of nested "https://" once per
// scheme (quadratic).  A password of any length, "/" and "://" included, is
// still matched unless it holds one of these schemes itself, and the user
// part already stops at the ":" every scheme contains.  The driver suffix is
// bounded so that every place a match can start also ends a password.
const _CONNECTION_SCHEME =
  "(?:postgresql|mysql|mongodb|rediss?|amqps?|sqlite|https?)(?:\\+[a-z0-9]{0,20})?:\\/\\/";
const _RE_CONNECTION_STRING = new RegExp(
  _CONNECTION_SCHEME + "[^:@\\s]+:(?:(?!" + _CONNECTION_SCHEME + ")[^@\\s])+@",
  "gi",
);

const _RE_CONN_USERINFO = /:\/\/([^:@\s]+):([^@\s]+)@/;

function _redactConnPassword(match) {
  return match.replace(_RE_CONN_USERINFO, "://$1:[REDACTED:password]@");
}

// ---------------------------------------------------------------------------
// Well-known API key / token formats (ordered most-specific first)
// ---------------------------------------------------------------------------
const _CREDENTIAL_REPLACEMENTS = [
  // JSON Web Tokens: base64url header and payload (both JSON objects, so both
  // start "eyJ") and a signature, empty for an unsigned token.  Runs first,
  // with the query rule below: the key-prefix rules after them recognise only a
  // key's start and would redact just the start of a longer value, and the
  // token=/key= values stop at the first dot.  The match must start a run of
  // token characters: base64 of JSON repeats "eyJ" inside one long run, and
  // letting each start a match rescans the run every time (quadratic).  A
  // percent-escape (%3D), a JSON \u escape or a \n-style escape may also
  // precede it; each begins outside the run, so the scan stays linear.
  [
    /(?:(?<![a-zA-Z0-9_\-])|(?<=%[0-9A-Fa-f]{2})|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\[nrt]))eyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{5,}\.[a-zA-Z0-9_\-]*/g,
    "[REDACTED:api_key]",
  ],
  // Credential-named URL query or fragment parameters (?token=, &api_key=,
  // #access_token=, ...) anchored to the ?, & or # that starts them, to an
  // escaped & (&amp;, \u0026), or to a percent-encoded ?, & or # (%3F, %26,
  // %23, with %3D for =).  8+ chars, like the JSON secret keys; the value is
  // its token characters and a password's common punctuation (! * $ @ :); after
  // %3D it also ends at an encoded & or # (%26, %23), which are data in a plain
  // query.  Any other character ends it, so a URL in quotes, brackets or a list
  // keeps its surroundings, and an earlier rule's [REDACTED:...] marker is not
  // redacted again.  The token names follow the token= rule below, plus id,
  // with or without the underscore.  Bare key= is left out, as in
  // _RE_QUERY_CRED below.
  [
    /(?:(?<=[?&#])|(?<=&amp;)|(?<=\\u0026)|(?<=%3F)|(?<=%2[36]))(?:(?:(?:access|refresh|id|auth|api|session|bearer|secret)_?)?token|api[_-]?key|(?:client_)?secret|passw(?:or)?d)(?:=[a-zA-Z0-9._~+/=%!*$@:\-]{8,}|%3D(?:(?!%2[36])[a-zA-Z0-9._~+/=%!*$@:\-]){8,})/gi,
    "[REDACTED:secret]",
  ],
  // OpenAI project-scoped keys   sk-proj-xxxxxxxxxx...
  [/sk-proj-[a-zA-Z0-9\-]{20,}/g, "[REDACTED:api_key]"],
  // OpenAI standard keys          sk-xxxxxxxxxx...
  [/sk-[a-zA-Z0-9]{20,}/g, "[REDACTED:api_key]"],
  // GitHub personal access tokens ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  [/ghp_[a-zA-Z0-9]{36}/g, "[REDACTED:api_key]"],
  // GitHub OAuth tokens           gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  [/gho_[a-zA-Z0-9]{36}/g, "[REDACTED:api_key]"],
  // AWS access key IDs            AKIAxxxxxxxxxxxxxxxx
  [/AKIA[0-9A-Z]{16}/g, "[REDACTED:api_key]"],
  // Google API keys               AIzaxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  [/AIza[a-zA-Z0-9_\-]{35}/g, "[REDACTED:api_key]"],
  // Bearer tokens — min 20 chars of JWT/opaque token (scheme is
  // case-insensitive per RFC 7235, so match bearer/BEARER too)
  [/Bearer\s+[a-zA-Z0-9._~+/=\-]{20,}/gi, "[REDACTED:api_key]"],
  // token=<value> (20+).  Specific credential key prefixes only — not
  // unbounded [a-zA-Z0-9_]* which would match innocent identifiers
  // like "monkey=" or "turkey=".  Bare "token=" included via negative
  // lookbehind so standalone assignments still match (token=abcdef...)
  // without matching word suffixes like "over_tokenized=".
  [
    /(?:(?:access|refresh|auth|api|session|bearer|secret)_?token|(?<![a-zA-Z0-9_])token)=[a-zA-Z0-9]{20,}/g,
    "[REDACTED:api_key]",
  ],
  // key=<value> (20+).  Same bounded prefix approach: api_key=/secret_key= etc.
  // but not monkey= or turkey=.  Bare "key=" included with negative lookbehind.
  // Multi-segment keys secret_access_key / aws_secret_access_key included explicitly.
  [
    /(?:(?:api|secret|session|auth|encryption|signing|private|public|access|secret_access|aws_secret_access)_?key|(?<![a-zA-Z0-9_])key)=[a-zA-Z0-9]{20,}/g,
    "[REDACTED:api_key]",
  ],
];

// ---------------------------------------------------------------------------
// Query-string api_key / token / secret / password / auth redaction
//   ?api_key=abc123   →   ?api_key=***   (legacy _redactApiKeys compat)
//   &secret=value     →   &secret=***
// ---------------------------------------------------------------------------
// A value an earlier pattern replaced whole keeps its [REDACTED:...] marker when
// &, whitespace, a quote or the end follows it, so the display matches the
// backend's redaction; anything else after the marker, a secret's tail
// included, is masked along with it.
const _RE_QUERY_CRED =
  /(?:api_key|apiKey|api-key|(?<![a-zA-Z0-9_])token|secret|password|auth)=(?!\[REDACTED:[a-z_]+\](?:[&\s"]|$))[^&\s"]+/g;

// ---------------------------------------------------------------------------
// JSON-style simple redaction (legacy _redactApiKeys compat)
//   {"api_key": "abc"}   →   {"api_key": "***"}
// ---------------------------------------------------------------------------
const _RE_JSON_STYLE_CRED =
  /(["'](?:api_key|apiKey|api-key|token)["']\s*:\s*["'])([^"']*)(['"])/gi;

// ---------------------------------------------------------------------------
// JSON secret keys — comprehensive set matching backend
//   "api_key": "sk-abcdefghijklmnopqrst"  →  "api_key": "[REDACTED:secret]"
// ---------------------------------------------------------------------------
// Double-quoted form (standard JSON).  The single-quoted sibling below covers
// Python dict reprs / JS object literals, e.g. {'Authorization': 'Bearer ...'}.
// $1 captures the key + colon + opening quote; only the value is replaced, so
// the key stays intact even when value == key name.  /i already covers casing,
// so keys are listed once (no separate |Authorization alternative needed).
const _RE_JSON_SECRET_DQ =
  /("(?:api_key|apikey|api_secret|secret_key|secret|password|passwd|token|access_token|refresh_token|auth_token|private_key|client_secret|webhook_secret|signing_key|encryption_key|x_api_key|x-api-key|authorization)"\s*:\s*")[^"]{8,}"/gi;
const _RE_JSON_SECRET_SQ =
  /('(?:api_key|apikey|api_secret|secret_key|secret|password|passwd|token|access_token|refresh_token|auth_token|private_key|client_secret|webhook_secret|signing_key|encryption_key|x_api_key|x-api-key|authorization)'\s*:\s*')[^']{8,}'/gi;

// ---------------------------------------------------------------------------
// ENV secret line redaction — matches the backend's two-regex pipeline
//   SECRET_KEY=abc123           →   SECRET_KEY=[REDACTED:secret]
//   DATABASE_URL=postgres://…   →   DATABASE_URL=[REDACTED:secret]
//   FOO=bar                     →   not redacted (no secret-bearing key name)
// ---------------------------------------------------------------------------
const _RE_ENV_SECRET_LINE = /[A-Z][A-Z_0-9]+=\S+/g;
const _RE_ENV_SECRET_KEY =
  /(?:^|_)(?:SECRET|TOKEN|PASSWORD|CREDENTIAL|DSN)(?:_|$)|(?:^|_)KEY(?:_|$)|^(?:DATABASE_URL|TURNSTONE_DB_URL|DB_URL)$/i;

function _redactEnvLine(match) {
  const eqIdx = match.indexOf("=");
  if (eqIdx < 0) return match;
  const key = match.slice(0, eqIdx);
  if (_RE_ENV_SECRET_KEY.test(key)) {
    return key + "=[REDACTED:secret]";
  }
  return match;
}

// ---------------------------------------------------------------------------
// Prefilter — one early-exit scan deciding whether the pipeline can match.
// MUST remain a superset of every pattern above: each pattern requires at
// least one of these substrings, so skipping on a prefilter miss is sound.
// Anchor → patterns:
//   =           env lines, key=/token= assignments, query-string creds
//   " '         JSON-style and JSON-secret forms
//   @           connection-string userinfo
//   -----BEGIN  PEM private key blocks
//   sk- ghp_ gho_ AKIA AIza bearer   well-known key prefixes ("bearer" is
//               case-insensitive per RFC 7235; /i over-approximates the
//               case-sensitive prefixes, which only costs a full scan)
//   eyJ         JSON Web Tokens (no = or quote need appear around one)
//   %3D         percent-encoded credential query parameters (%3Ftoken%3D...)
// Adding a pattern above without an anchor here is a SILENT REDACTION
// BYPASS — extend this regex and the runtime smoke test together
// (tests/test_app_js.py::test_redact_credentials_runtime_smoke).
// ---------------------------------------------------------------------------
const _RE_PREFILTER = /[='"@]|-----BEGIN|sk-|ghp_|gho_|AKIA|AIza|bearer|eyJ|%3D/i;

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Redact known credential patterns in a string for display.
 *
 * Matches the backend's output_guard._redact_credentials patterns, applied
 * in priority order so more-specific patterns take precedence.  Pure function,
 * no side-effects.
 *
 * @param {string} text - The raw text to redact
 * @returns {string} Text with credential values replaced by redaction markers
 */
export function redactCredentials(text) {
  if (!text) return text;

  let result = String(text);

  // Fast bailout — most tool output (plain logs, timestamps, table data)
  // carries no anchor substring; one early-exit scan skips the sixteen
  // replace passes below.  Soundness argument lives on _RE_PREFILTER.
  if (!_RE_PREFILTER.test(result)) return result;

  // 1. PEM private key blocks (whole-block removal)
  result = result.replace(_RE_PRIVATE_KEY_BLOCK, "[REDACTED:private_key]");

  // 2. Connection string passwords (preserve user)
  result = result.replace(_RE_CONNECTION_STRING, _redactConnPassword);

  // 3. Well-known API key / token formats
  for (const [re, replacement] of _CREDENTIAL_REPLACEMENTS) {
    result = result.replace(re, replacement);
  }

  // 4. Query-string credential params (backward compat with _redactApiKeys)
  result = result.replace(_RE_QUERY_CRED, (m) => {
    const eq = m.indexOf("=");
    return eq >= 0 ? m.slice(0, eq) + "=***" : m;
  });

  // 5. JSON-style simple redaction (backward compat with _redactApiKeys)
  // NOTE: runs BEFORE step 6 so small values (< 8 chars) under api_key/token
  // keys still get redacted.  Authorization keys are intentionally omitted
  // here so step 6's comprehensive regex handles them with the full
  // [REDACTED:secret] marker instead.
  result = result.replace(_RE_JSON_STYLE_CRED, "$1***$3");

  // 6. JSON secret key values (double- and single-quoted; backend-parity).
  // $1 is the key + colon + opening quote; only the value is replaced.
  result = result.replace(_RE_JSON_SECRET_DQ, '$1[REDACTED:secret]"');
  result = result.replace(_RE_JSON_SECRET_SQ, "$1[REDACTED:secret]'");

  // 7. ENV secret lines
  result = result.replace(_RE_ENV_SECRET_LINE, _redactEnvLine);

  return result;
}

// Pretty-print JSON WITH redaction — the sanctioned way to render a raw
// JSON payload for display (interactive.js raw-payload views, the shared
// MCP error card).  Lives here, next to the redaction it wraps, so a
// future caller cannot pretty-print without redacting.  Returns null for
// non-JSON input; callers fall back to redactCredentials(text) directly.
export function tryPrettyJson(text) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    return null;
  }
  return redactCredentials(JSON.stringify(obj, null, 2));
}
