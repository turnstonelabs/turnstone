// ---------------------------------------------------------------------------
// Structured MCP error envelope — shared detector + card builder.
//
// Lifted from interactive.js (#725) so BOTH conversation surfaces render the
// same consent / re-consent / forbidden / operator card: the interactive pane
// and the coordinator pane (whose sessions carry the same MCP surface,
// persona-gated like interactive).  The card is self-contained — the Connect /
// Re-consent button opens the relative /v1/api/mcp/oauth/start popup, which
// both hosts serve — and the optional onConsent callback is a notification
// hook only (the interactive pane threads it to the standalone consent badge
// through host.onConsentDetected; the coordinator pane omits it).
// ---------------------------------------------------------------------------

import { redactCredentials, tryPrettyJson } from "./redact_credentials.js";
import { showToast } from "./toast.js";

function _mcpErrorCategory(code) {
  if (code === "mcp_consent_required" || code === "mcp_insufficient_scope") {
    return "actionable";
  }
  if (
    code === "mcp_token_undecryptable_key_unknown" ||
    code === "mcp_oauth_url_insecure"
  ) {
    return "operator";
  }
  if (code === "mcp_refresh_unavailable") {
    // Soft, retryable state — a transient refresh failure, not a hard denial.
    return "transient";
  }
  // Default for any other mcp_*_forbidden / unrecognised mcp_ code.
  return "forbidden";
}

function _mcpErrorTitle(err) {
  switch (err.code) {
    case "mcp_consent_required":
      return "Consent required";
    case "mcp_insufficient_scope":
      return "Re-consent required (insufficient scope)";
    case "mcp_token_undecryptable_key_unknown":
    case "mcp_oauth_url_insecure":
      return "Operator action required";
    case "mcp_refresh_unavailable":
      return "Temporarily unavailable";
    default:
      return "Forbidden";
  }
}

export function tryParseMcpError(text) {
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    return null;
  }
  if (!obj || typeof obj !== "object") return null;
  const err = obj.error;
  if (!err || typeof err !== "object") return null;
  if (typeof err.code !== "string" || err.code.indexOf("mcp_") !== 0)
    return null;
  return err;
}

export function buildMcpErrorEmbed(err, rawJson, onConsent) {
  const category = _mcpErrorCategory(err.code);
  const wrapper = document.createElement("div");
  wrapper.className = "mcp-error-card mcp-error-" + category;

  const icon = document.createElement("div");
  icon.className = "mcp-error-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "⚠";
  wrapper.appendChild(icon);

  const body = document.createElement("div");
  body.className = "mcp-error-body";

  const title = document.createElement("div");
  title.className = "mcp-error-title";
  title.textContent = _mcpErrorTitle(err);
  body.appendChild(title);

  if (err.detail) {
    const detail = document.createElement("div");
    detail.className = "mcp-error-detail";
    detail.textContent = String(err.detail);
    body.appendChild(detail);
  }

  if (err.server) {
    const serverLine = document.createElement("div");
    serverLine.className = "mcp-error-server";
    serverLine.appendChild(document.createTextNode("server: "));
    const serverCode = document.createElement("code");
    serverCode.textContent = String(err.server);
    serverLine.appendChild(serverCode);
    body.appendChild(serverLine);
  }

  if (Array.isArray(err.scopes_required) && err.scopes_required.length) {
    const scopesLine = document.createElement("div");
    scopesLine.className = "mcp-error-scopes";
    scopesLine.appendChild(document.createTextNode("scopes: "));
    for (let i = 0; i < err.scopes_required.length; i++) {
      const pill = document.createElement("span");
      pill.className = "mcp-scope-pill";
      pill.textContent = String(err.scopes_required[i]);
      scopesLine.appendChild(pill);
    }
    body.appendChild(scopesLine);
  }

  // Render the Connect / Re-consent button only when the dispatcher actually
  // supplied a per-server consent URL. An "actionable"-category code with no
  // consent_url means there is no per-server consent flow for this server —
  // sign-in passthrough (oauth_obo) mints from the user's Turnstone sign-in and
  // is deliberately absent from the Settings connections list and rejected by
  // /start — so a button here would dead-end ("no consent URL; open Settings"
  // pointing at a panel with nothing to connect). In that case the honest
  // remedy is the detail text (sign in again / ask your administrator), so we
  // show the card without a broken affordance.
  //
  // This never wrongly hides a needed button for oauth_user: the backend
  // invariant is that _build_consent_url returns a /v1/api/mcp/oauth/start URL
  // for EVERY oauth_user row and None only for non-oauth_user auth types, so an
  // oauth_user actionable error always carries a valid consent_url and always
  // renders its button. The removed click-time "open Settings" fallback guarded
  // a producer path that that invariant makes unreachable.
  const consentUrl = err.consent_url;
  const hasConsentAffordance =
    typeof consentUrl === "string" &&
    consentUrl.startsWith("/v1/api/mcp/oauth/start");
  if (category === "actionable" && hasConsentAffordance) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mcp-error-action-btn";
    const serverLabel = err.server ? String(err.server) : "server";
    btn.textContent =
      err.code === "mcp_insufficient_scope"
        ? "Re-consent with new scopes →"
        : "Connect to " + serverLabel + " →";
    btn.setAttribute(
      "aria-label",
      err.code === "mcp_insufficient_scope"
        ? "Re-consent with new scopes for " + serverLabel
        : "Connect to " + serverLabel,
    );
    btn.addEventListener("click", function () {
      // Defence-in-depth: the render gate already proved the prefix, but
      // re-check at click time — a non-prefix value would indicate producer
      // drift or a compromised dispatcher, and window.open("javascript:...")
      // would be catastrophic. Never rely on the producer-side guarantee alone.
      if (!consentUrl.startsWith("/v1/api/mcp/oauth/start")) {
        showToast("Invalid consent URL");
        return;
      }
      const sep = consentUrl.indexOf("?") >= 0 ? "&" : "?";
      const url =
        consentUrl +
        sep +
        "return_url=" +
        encodeURIComponent(window.location.href);
      window.open(url, "_blank", "noopener");
    });
    body.appendChild(btn);
    if (onConsent) onConsent(err.server);
  }

  wrapper.appendChild(body);

  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "raw payload";
  details.appendChild(summary);
  const pre = document.createElement("pre");
  pre.className = "tool-output";
  pre.textContent = tryPrettyJson(rawJson) || redactCredentials(rawJson);
  details.appendChild(pre);
  wrapper.appendChild(details);

  return wrapper;
}
