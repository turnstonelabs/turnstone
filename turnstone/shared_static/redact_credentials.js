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
//      (sk-proj-, sk-, ghp_, gho_, AKIA, AIza, Bearer token, token=, key=)
//   4. Query-string api_key/token    → key=***  (backward compat)
//   5. JSON-style key/value          → "key": "***"  (backward compat)
//   6. JSON secret keys              → "secret": "[REDACTED:secret]"
//   7. ENV secret lines              → SECRET_KEY=[REDACTED:secret]
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
// ---------------------------------------------------------------------------
const _RE_CONNECTION_STRING =
  /(?:postgresql\+?(?:psycopg)?|mysql|mongodb(?:\+srv)?|rediss?|amqps?|sqlite|https?):\/\/[^:@\s]+:[^@\s]+@/g;

const _RE_CONN_USERINFO = /:\/\/([^:@\s]+):([^@\s]+)@/;

function _redactConnPassword(match) {
  return match.replace(_RE_CONN_USERINFO, "://$1:[REDACTED:password]@");
}

// ---------------------------------------------------------------------------
// Well-known API key / token formats (ordered most-specific first)
// ---------------------------------------------------------------------------
const _CREDENTIAL_REPLACEMENTS = [
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
  // like "monkey=" or "turkey=".  Covers access_token=/auth_token=/api_token= etc.
  [/(?:(?:access|refresh|auth|api|session)_?token)=[a-zA-Z0-9]{20,}/g, "[REDACTED:api_key]"],
  // key=<value> (20+).  Same bounded prefix approach: api_key=/secret_key= etc.
  // but not monkey= or turkey=.  The _? allows both snake_case and compact forms.
  [/(?:(?:api|secret|session|auth|encryption|signing|private|public|access)_?key)=[a-zA-Z0-9]{20,}/g, "[REDACTED:api_key]"],
];

// ---------------------------------------------------------------------------
// Query-string api_key / token redaction (legacy _redactApiKeys compat)
//   ?api_key=abc123   →   ?api_key=***
//   &apiKey=abc       →   &apiKey=***
// ---------------------------------------------------------------------------
const _RE_QUERY_CRED = /(?:api_key|apiKey|api-key|token|secret|password|auth)=[^&\s"]+/g;

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
