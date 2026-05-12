# MCP OAuth in headless / scheduled / channel-driven runs

**Constraint**: OAuth-MCP servers (`auth_type=oauth_user`) require browser-based user consent. Users must pre-consent via the web UI before any run that cannot drive a browser redirect.

**Affected surfaces**:

- Scheduled workstreams (`turnstone-console` task scheduler).
- Discord adapter runs.
- Slack adapter runs.
- Any future channel adapter without an interactive browser session.

**What happens when consent is missing**:

A tool call against an `oauth_user` server returns a structured `mcp_consent_required` error to the agent. The agent surfaces the deferred work in its output. Turnstone persists a record to `mcp_pending_consent` so the dashboard badge surfaces the deferred consent need to the user on next login.

**Recovery**:

The user opens the dashboard, sees the gear-icon badge counting pending consents, opens the settings modal, clicks Connect for each affected server, and completes the OAuth dance. The pending-consent record is cleared by the OAuth callback handler on success. Subsequent scheduled / channel runs use the freshly-stored token.

**Pre-consent recipe**:

Before scheduling a workstream that depends on an `oauth_user` MCP server, the user should:

1. Open the dashboard.
2. Open the settings modal (gear icon).
3. Click Connect on each MCP server the schedule will use.
4. Confirm consent in the popup.

This stores tokens that the scheduled run will reuse. Refresh-token rotation is handled transparently on the run side; only the first consent requires browser interaction.
