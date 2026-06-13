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

## Gameplay

A run is a little RPG loop, played a bit each day:

- **Explore** the overworld of box-drawing maps. Walking is free, but the wild
  country has texture — a step may turn up a wandering monster, a purse of
  gold, a healing spring, a small trap (which can never kill you), or a scrap
  of old Vale lore. Only one such find happens per move, and the non-combat
  ones don't interrupt your walk.
- **Fight, shop, and heal** in and around town. Fighting and descending the
  dungeon gauntlet each spend one of your daily turns; resting, shopping and
  moving do not.
- **Win the game** by slaying **the Wyrm Below**. Once your hero is seasoned
  enough, `challenge` it at the dungeon. A victory frees the Vale, carves your
  run into the **Hall of Legends**, and — in the tradition of *Legend of the
  Red Dragon* — begins a new life: your character resets to first-day gear and
  stats but keeps a permanent ★ for every Wyrm slain, ready to do it all again.
- **Read the news.** `door_log` is the **Understone Herald**, a shared
  broadsheet of notable deeds across the whole world — who joined, who rose a
  level, who was dragged home by a goblin, and who freed the Vale.

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

## The Watch — a live spectator view

When the server runs under the **streamable-http** transport, it also serves a
read-only **Watch** page: the lobby TV of the Vale. Point a browser at

```
http://127.0.0.1:8077/watch
```

(the host and port follow `UNDERSTONE_HOST` / `UNDERSTONE_PORT`). It is a
period **CRT spectator console** — a green-and-amber phosphor map of the whole
world with every adventurer's `@` marker, a live **Understone Herald** feed, the
**Hall of Legends**, and a roster of who is currently abroad. It refreshes every
couple of seconds; if it loses contact it dims and reads `SIGNAL LOST` until the
server returns.

The Watch is **strictly read-only**. Input never flows through it — there are no
controls, no forms, nothing that can change the world. It reads the same shared
state the tools do and paints it; that is all. There is no authentication, in
keeping with the rest of this easter-egg server (see the safety note below), so
treat the page as you would the MCP endpoint itself.

> _Screenshot: the Watch console — a phosphor-green overworld map with amber
> `@` markers, the Herald feed and Hall of Legends down the right-hand rail.
> (Image placeholder; run the server and open the URL to see it live.)_

When the Watch is up, the `door_join` welcome and the `door_help` manual both
print its URL so players (and the assistant narrating for them) know it exists.
If you bind to `0.0.0.0` to share the world across a network, advertise a host
that browsers can actually reach (your machine's LAN address or hostname) rather
than `0.0.0.0` itself — the link is composed from `UNDERSTONE_HOST`.

## Authoring worlds

The Vale of Understone is just the *bundled* world. The whole game — its map,
monsters, economy, and endgame — is a **content pack**: a directory of six JSON
files the server loads at start. Nothing about the Vale is privileged; point
the server at another pack and it runs that world instead. This is the seam
where the game becomes its own authoring target: a pack is plain data, so a
person *or an LLM* can write one, and the same zero-setup philosophy that makes
the game playable with no prompt makes it **authorable with no code**.

The loop has three commands:

```bash
understone newpack mypack        # scaffold a pack (copies the Vale as a template)
# ...edit or LLM-generate the JSON in mypack/ to describe your world...
understone validate mypack       # check it; prints a report or names what's wrong
UNDERSTONE_WORLD=mypack understone   # serve your world
```

`newpack` writes a starting template plus an `AUTHORING.md` manual — the
file-by-file schema, the enforced limits, and design guidance — written to be
followed cold by a model. `validate` loads the pack through exactly the same
hardened loader the server uses and either prints a summary ending **"This pack
is sound. The door stands open."** or fails with one precise line naming the
file, the row, and the field at fault.

Packs are validated **hard** at load: glyphs may not collide with the frame's
box-drawing lines or the player markers, dimensions and counts are bounded,
display names are length-checked, and every cross-reference (a legend
character, a starting item, the boss monster, a dungeon tier) must resolve.
Because packs are now routinely untrusted, generated output, those error
messages are not a nuisance — they are the **feedback loop**. Iterate against
them until the door stands open.

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
| `door_action` | Context verbs: fight, flee, rest, buy, sell, heal, descend, challenge (the Wyrm), leave. |
| `door_log` | The Understone Herald — the shared feed of notable deeds. |
| `door_rank` | The leaderboard, plus the Hall of Legends (★ marks Wyrm kills). |
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
