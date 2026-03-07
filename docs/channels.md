# Channel Integrations

The `turnstone-channel` gateway connects external messaging platforms to
turnstone workstreams via Redis MQ. Each platform adapter translates
platform-native events (messages, button clicks, slash commands) into
turnstone MQ messages, and renders workstream output back into the
platform's UI.

Discord ships as the first adapter. The adapter protocol is designed for
future Slack and Teams integrations.

---

## Architecture

```
Discord Gateway
      |
      v
turnstone-channel  (Discord adapter)
      |
      v
  Redis MQ
      |
      v
turnstone-bridge  ──>  turnstone-server
```

Key components:

- **ChannelAdapter protocol** (`turnstone/channels/_protocol.py`) — generic
  interface for any messaging platform. Defines `start()`, `stop()`,
  `send()`, `edit_message()`, `send_approval_request()`,
  `send_plan_review()`, and `create_thread()`.
- **ChannelRouter** (`turnstone/channels/_routing.py`) — maps
  channel/thread IDs to turnstone workstream IDs. Handles workstream
  creation via MQ, stale route detection, and user identity resolution.
- **AsyncRedisBroker** (`turnstone/mq/async_broker.py`) — async Redis
  client compatible with discord.py's event loop. Used by the router for
  pub/sub and queue operations.
- **channel_users table** — maps `(channel_type, channel_user_id)` to a
  turnstone `user_id`. Messages from unlinked users are silently dropped.
- **channel_routes table** — persistent channel-to-workstream mappings.
  Survives bot restarts. Stale routes (evicted workstreams) are detected
  and refreshed on the next message.

---

## Discord Setup

### 1. Create a Discord Application

1. Go to https://discord.com/developers/applications
2. Click **New Application** and give it a name
3. Navigate to the **Bot** tab and click **Reset Token** to generate a
   bot token. Copy it immediately — it is shown only once.
4. On the same **Bot** tab, scroll down to **Privileged Gateway Intents**
   and enable **MESSAGE CONTENT INTENT**
5. Navigate to **OAuth2 > URL Generator**
6. Under **Scopes**, check `bot` and `applications.commands`
7. Under **Bot Permissions**, check:
   - View Channels
   - Send Messages
   - Send Messages in Threads
   - Create Public Threads
   - Read Message History
   - Add Reactions
   - Embed Links
8. Copy the generated URL, open it in a browser, and add the bot to your
   Discord server

### 2. Configure Turnstone

**Environment variables** (recommended for Docker):

```bash
TURNSTONE_DISCORD_TOKEN=your-bot-token-here
TURNSTONE_DISCORD_GUILD=123456789  # optional, restrict to one guild
```

**CLI flags** (bare-metal):

```bash
turnstone-channel \
  --discord-token "your-bot-token" \
  --discord-guild 123456789 \
  --redis-host localhost \
  --redis-port 6379
```

**Docker Compose** (production profile):

```bash
# In .env file:
TURNSTONE_DISCORD_TOKEN=your-bot-token
TURNSTONE_DISCORD_GUILD=123456789
```

Then start the stack:

```bash
docker compose --profile production up
```

The `channel` service starts automatically when
`TURNSTONE_DISCORD_TOKEN` is set.

### 3. Link User Accounts

Discord users must link their account to a turnstone user before they can
interact with the bot. Unlinked users' messages are silently ignored.

1. The user must have a turnstone API token — created via the admin panel
   or `turnstone-admin create-token`
2. In Discord, the user runs `/link`. A modal appears prompting for the
   API token (the token is never visible in Discord audit logs because it
   is submitted via modal, not as a slash command argument).
3. The token is validated against the database. If valid, a
   `channel_users` mapping is created.
4. The user can now @mention the bot or use slash commands.

An admin can also force-link or unlink users via the console admin panel
(Admin > Channels tab).

---

## Usage

### Conversations

- **@mention** the bot in any allowed channel to start a new conversation.
  The bot creates a Discord thread from the message and a turnstone
  workstream behind it.
- All subsequent messages in the thread are routed to the same workstream.
- The bot streams responses via message edits, updated approximately every
  1.5 seconds.
- If the workstream is evicted for capacity, the next message in the
  thread auto-creates a new workstream and atomically resumes the
  previous workstream via the `resume_ws` field on
  `CreateWorkstreamMessage`. The server resumes the workstream during
  creation (same HTTP request), and the bridge emits a
  `WorkstreamResumedEvent` back to the channel. The thread receives a
  *"Resumed: {name} ({count} messages restored)"* confirmation.

### Slash Commands

| Command | Description |
|---------|-------------|
| `/link` | Link Discord account to turnstone (opens modal for API token) |
| `/unlink` | Unlink Discord account |
| `/ask <message>` | Create a new thread and workstream with an initial message |
| `/status` | Show workstream info for the current thread (ephemeral) |
| `/close` | Close the workstream, delete the route, and archive the thread |

### Tool Approvals

When manual approval is enabled (the default), tool calls are displayed as
an orange embed with:

- Tool name and argument preview
- **Approve** (green), **Reject** (red), **Always Approve** (gray) buttons
- Only linked users can interact with approval buttons
- The approval decision is forwarded through MQ to the bridge, which
  relays it to the server

Buttons use static `custom_id` values so they survive bot restarts.
Correlation data (`ws_id`, `correlation_id`) is stored in the embed footer.

**Auto-approval:** When `auto_approve` is true (via `--auto-approve`), or when
all tools in the request match the `auto_approve_tools` list in the adapter
config, the bot auto-responds with approval and posts a
"*Tool auto-approved.*" notice to the thread instead of showing buttons. The
`auto_approve_tools` list is set via the `ChannelConfig.auto_approve_tools`
field (useful for allowing specific tools like `bash` or `read_file` while
still requiring manual approval for others).

### Plan Reviews

Plan review requests are displayed as a blue embed with:

- **Approve Plan** (green) button — approves the plan with empty feedback
- **Request Changes** (gray) button — opens a modal for feedback text
  (up to 2000 characters)
- Feedback is forwarded through MQ as a `PlanFeedbackMessage`

---

## Configuration Reference

| CLI Flag | Env Var | Default | Description |
|----------|---------|---------|-------------|
| `--discord-token` | `TURNSTONE_DISCORD_TOKEN` | — | Bot token (required to enable Discord) |
| `--discord-guild` | — | `0` (all guilds) | Restrict to a single Discord guild |
| `--discord-channels` | — | empty (all) | Comma-separated channel IDs to allow |
| `--redis-host` | `REDIS_HOST` | `localhost` | Redis host |
| `--redis-port` | — | `6379` | Redis port |
| `--redis-password` | `REDIS_PASSWORD` | — | Redis password |
| `--redis-db` | — | `0` | Redis DB number |
| `--model` | — | server default | Default model for new workstreams |
| `--auto-approve` | — | `false` | Auto-approve ALL tool calls (skips approval buttons entirely) |
| `--http-host` | — | `127.0.0.1` | HTTP server bind address for notify endpoint |
| `--http-port` | `TURNSTONE_CHANNEL_PORT` | `8091` | HTTP server port |
| `--auth-token` | `TURNSTONE_CHANNEL_AUTH_TOKEN` | — | Static auth token for `/v1/api/notify` (alternative to JWT) |
| `--log-level` | `TURNSTONE_LOG_LEVEL` | `INFO` | Log level |
| `--log-format` | `TURNSTONE_LOG_FORMAT` | `auto` | Log format (`auto`/`json`/`text`) |

---

## User Identity

- The `channel_users` table maps `(channel_type, channel_user_id)` to a
  turnstone `user_id`
- Self-service linking via the `/link` slash command (modal input, not
  visible in Discord audit logs)
- Admin can force-link or unlink via the console admin panel (Admin >
  Channels tab). Unlinking uses a styled confirmation modal.
- Unlinked users' messages are silently dropped
- A user can be linked across multiple platforms (e.g. Discord + Slack)

See [Security: Database Schema](security.md#database-schema) for the
`channel_users` table definition.

---

## Workstream Lifecycle

1. **Creation** — @mention or `/ask` creates a Discord thread and a
   turnstone workstream. The `ChannelRouter` persists the mapping in the
   `channel_routes` table.
2. **Active** — messages are routed bidirectionally. The bot streams
   responses via message edits (updated every ~1.5 seconds).
3. **Eviction** — the server evicts an idle workstream for capacity. The
   route is preserved and the thread stays open.
4. **Reactivation** — the next message in the thread detects the stale
   route (no MQ owner) and creates a new workstream with the old `ws_id`
   as `resume_ws` on the `CreateWorkstreamMessage`. The server resumes
   the workstream during creation (no separate command or reverse lookup
   needed). The bridge emits a `WorkstreamResumedEvent` to the channel, and
   the thread displays *"Resumed: {name} ({count} messages restored)"*.
   If the old workstream was pruned, a fresh one starts with no error.
5. **Close** — `/close` command closes the workstream via MQ, deletes the
   route, unsubscribes from events, and archives the Discord thread.

---

## Notifications

> See also: [Notification Flow diagram](diagrams/png/17-notify-flow.png)

The `notify` tool allows the LLM to proactively send notifications to
users or channels on external platforms. This is useful for alerting
people about task completion, errors, or important updates without
waiting for them to check in.

### Targeting

Two modes:

- **Username** — provide a turnstone `username`. The gateway resolves
  it via the `channel_users` table and sends to all linked channels
  (e.g. Discord + future Slack).
- **Direct** — provide `channel_type` + `channel_id` to target a
  specific platform channel or user DM.

### Delivery Flow

Notifications bypass MQ for lower latency. The server calls the channel
gateway directly over HTTP:

1. The LLM calls the `notify` tool with a message and target
2. `_exec_notify()` queries the `services` table for healthy channel
   gateways (heartbeat within the last 120 seconds)
3. The server mints a service JWT (`aud: turnstone-channel`) via
   `ServiceTokenManager` and POSTs to the first healthy gateway
4. The gateway validates the JWT, resolves the target, and calls
   `adapter.send()` on the appropriate platform adapter
5. On failure, the server tries the next gateway. If all fail, it
   retries up to 2 more times (delays: 1s, 3s), re-querying the
   service registry on each attempt

### Service Registry

The channel gateway registers itself in the `services` database table
on startup and sends a heartbeat every 30 seconds. On shutdown it
deregisters. Services are considered stale after 120 seconds (4 missed
heartbeats) and are excluded from `list_services()` queries.

The `services` table schema:

| Column | Description |
|--------|-------------|
| `service_type` | Service category (e.g. `"channel"`) |
| `service_id` | Unique instance ID (`channel-<hostname>-<random>`) |
| `url` | HTTP base URL for the service |
| `last_heartbeat` | ISO 8601 timestamp of last heartbeat |
| `created` | ISO 8601 timestamp of initial registration |

### Security

- **Authentication** — the gateway's `POST /v1/api/notify` endpoint
  requires authentication. Configure either `TURNSTONE_JWT_SECRET`
  (the server mints JWTs with `aud: turnstone-channel` automatically)
  or a static token via `--auth-token`. If neither is set, the
  gateway fails closed and rejects all requests with 401. Server JWTs
  (`aud: turnstone-server`) are rejected.
- **Rate limit** — maximum 5 notifications per turn. The counter only
  increments on successful delivery, so failures don't consume the
  budget.
- **SSRF protection** — only `http://` and `https://` service URLs
  are allowed. Other schemes are silently skipped.
- **Mention sanitization** — `discord.utils.escape_mentions()` is
  applied before sending, preventing `@everyone` / `@here` abuse.
- **Error redaction** — generic error messages are returned to the
  LLM. Internal details (service IDs, URLs, exception messages) are
  logged server-side only.

---

## Adding New Adapters

The `ChannelAdapter` protocol defines the interface any platform adapter
must implement:

```python
class ChannelAdapter(Protocol):
    channel_type: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, channel_id: str, content: str) -> str: ...
    async def edit_message(self, channel_id: str, message_id: str, content: str) -> None: ...
    async def send_approval_request(self, channel_id: str, ws_id: str, correlation_id: str, items: list[dict]) -> None: ...
    async def send_plan_review(self, channel_id: str, ws_id: str, correlation_id: str, content: str) -> None: ...
    async def create_thread(self, parent_channel_id: str, name: str, message_id: str = "") -> str: ...
```

To add a new platform:

1. Create `turnstone/channels/<platform>/` package
2. Implement the `ChannelAdapter` protocol
3. Add a `--<platform>-token` flag and detection logic in
   `turnstone/channels/cli.py`
4. Add the optional dependency in `pyproject.toml` (e.g.
   `turnstone[slack]`)

See `turnstone/channels/discord/` as a reference implementation.
