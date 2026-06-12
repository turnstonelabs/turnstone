# Understone

A small, multiplayer, BBS-style **ANSI door game** served over the Model
Context Protocol (MCP). It is a text RPG in the spirit of *Legend of the Red
Dragon* — explore an overworld of box-drawing maps, fight wandering monsters,
shop and rest in town, and descend a dungeon — except the "door" is an MCP
server and the player drives it by talking to an AI assistant.

The server is the rules engine and the single source of truth. Players share
**one persistent world**: your assistant calls tools, the server returns
authoritative frames and facts, and the assistant narrates the story around
them.

This is a self-contained reference example. It depends only on `mcp` — there
is no dependency on Turnstone itself — so it runs against any MCP client.

## How to play

There is **no prompt to paste and no persona to configure**. The tool schema
is the whole interface. Once the server is registered with your assistant:

1. Tell your assistant you'd like to play an ANSI door game / text dungeon
   RPG (it can discover the tools by name and description).
2. The assistant calls `door_help` to learn how to run the world, then
   `door_join` with your adventurer's name.
3. Play unfolds as a conversation: "head east", "fight it", "rest at the inn".

Everything the assistant needs to run the game well is returned by
`door_help`.

## Installation

This example uses [`uv`](https://docs.astral.sh/uv/). From the example
directory:

```bash
cd examples/door-game
uv venv
uv pip install -e .
```

That installs the `understone` entry point into the environment.

To run the tests and quality gates:

```bash
uv pip install -e ".[test,dev]"
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy understone/
```

## Running the server

By default the server speaks the **stdio** transport, which is how MCP clients
launch a per-session subprocess:

```bash
understone
```

To host one shared world over HTTP for several clients, run the
**streamable-http** transport as a single long-lived process:

```bash
UNDERSTONE_TRANSPORT=streamable-http understone
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UNDERSTONE_DB` | `./understone.db` | SQLite database file for the world's state. |
| `UNDERSTONE_WORLD` | _(packaged pack)_ | Directory of a content pack to load instead of the bundled Vale of Understone. |
| `UNDERSTONE_TRANSPORT` | `stdio` | `stdio` or `streamable-http`. |
| `UNDERSTONE_HOST` | `127.0.0.1` | Bind host (streamable-http only). |
| `UNDERSTONE_PORT` | `8077` | Bind port (streamable-http only). |
| `UNDERSTONE_PATH` | `/mcp` | HTTP path for the MCP endpoint (streamable-http only). |

## Registering with Turnstone

Understone is an ordinary MCP server, so it plugs into Turnstone's MCP client
config two ways.

**Stdio (per-session subprocess).** Turnstone launches the `understone`
command for each session. Each session gets its own subprocess, so for a
truly shared world prefer the HTTP form below; stdio is simplest for solo
play.

```toml
[mcp.servers.understone]
command = "understone"

[mcp.servers.understone.env]
UNDERSTONE_DB = "/var/lib/understone/world.db"
```

**Streamable-HTTP (one shared world).** Run a single Understone process with
`UNDERSTONE_TRANSPORT=streamable-http` and point every client at its URL. This
is the right setup for multiplayer: one process, one database, one world that
all adventurers share.

```toml
[mcp.servers.understone]
url = "http://localhost:8077/mcp"
```

> **Operator note.** For multiplayer, start exactly one shared process —
> `UNDERSTONE_TRANSPORT=streamable-http understone` — and have all clients use
> the url form. The world lives in a single SQLite file written by that one
> process.

## The tools

| Tool | What it does |
|------|--------------|
| `door_help` | The game-master manual. Start here. |
| `door_join` | Create or resume an adventurer; returns the opening map. |
| `door_status` | The character sheet (read-only). |
| `door_look` | Redraw the current view — overworld map or location menu. |
| `door_move` | Walk the overworld (free; no daily turn spent). |
| `door_action` | Context verbs: fight, flee, rest, buy, sell, heal, descend, leave. |
| `door_log` | Catch up on what happened in the world while away. |
| `door_rank` | The leaderboard. |
| `door_bestow` | Game-master grant of a little gold/healing for a story beat. |

## A note on identity and safety

This example is an **easter egg**, not a hardened service. Identity is
**self-asserted**: a "player" is just a name passed to the tools, and there is
**no authentication** — anyone who can reach the server can act as any name.
That is fine for a shared toy world among people who trust each other, and
deliberately out of scope for a game. Do not store anything sensitive in it,
and if you expose the HTTP transport beyond localhost, put it behind whatever
access control your environment already provides.

The game master's `door_bestow` channel can only grant small, capped amounts
of in-game gold and healing — never items, never turns — and every grant is
written to the public in-world log, so its reach is bounded by design.
