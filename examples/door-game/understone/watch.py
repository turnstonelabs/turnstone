"""The Watch page — a read-only CRT spectator view of the shared world.

This module is PURE: it imports nothing from ``mcp`` or ``starlette``. It owns
two payload builders and one self-contained HTML page; the server wires them to
HTTP routes. Input never flows through here — the Watch is the lobby TV, not a
controller.

* :func:`build_world_payload` — the STATIC map: dimensions, the legend-coloured
  terrain rows, and the placed locations. Fetched once by the page.
* :func:`build_state_payload` — the DYNAMIC snapshot: every player's position
  and vitals, the recent Herald feed, and the Hall of Legends. Polled.
* :data:`WATCH_HTML` — one inline-everything page (vanilla JS, phosphor CRT
  styling) that paints the base map once and overlays the players each poll.

A correspondence game leaves every adventurer on the board between their turns,
so the state payload reports *all* players, not just the active ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from understone.engine.models import Mode
from understone.engine.satchel import decode_satchel
from understone.screen import texture

if TYPE_CHECKING:
    from understone.engine.log import Event
    from understone.engine.world import World
    from understone.game import Game

# How many of the newest Herald events the Watch shows, oldest-first.
_HERALD_LIMIT = 15
# How many Hall-of-Legends runs the Watch shows.
_HALL_LIMIT = 5


def build_world_payload(world: World) -> dict[str, object]:
    """Return the STATIC map payload the Watch page fetches once.

    ``glyph_rows`` is the base terrain rendered glyph-for-glyph (locations are
    NOT burned in here — they ride in ``locations`` so the client can colour
    them as an overlay). ``legend`` maps every terrain glyph that appears to a
    palette colour *name*; the client owns the name→hex mapping. Completeness is
    a contract: every glyph in ``glyph_rows`` has a ``legend`` entry.

    ``theme`` is the pack's Watch CRT palette (``settings.watch_theme``); the
    page looks it up in its own JS THEME table on fetch and swaps the CSS
    custom-property values, so each world has its own phosphor colour.
    """
    glyph_rows: list[str] = []
    legend: dict[str, str] = {}
    for y in range(world.height):
        chars: list[str] = []
        for x in range(world.width):
            terrain = world.terrain_at(x, y)
            chars.append(terrain.glyph)
            legend.setdefault(terrain.glyph, terrain.color)
        glyph_rows.append("".join(chars))

    locations = [
        {
            "x": loc.x,
            "y": loc.y,
            "glyph": loc.glyph,
            "name": loc.name,
            "color": loc.color,
        }
        for loc in world.locations
    ]
    return {
        "name": world.name,
        "width": world.width,
        "height": world.height,
        "theme": world.settings.watch_theme,
        "glyph_rows": glyph_rows,
        "legend": legend,
        "locations": locations,
    }


def build_state_payload(game: Game) -> dict[str, object]:
    """Return the DYNAMIC snapshot payload the Watch page polls.

    Reports every player (a correspondence game keeps idle pieces on the
    board), the last :data:`_HERALD_LIMIT` events oldest-first, and the top
    :data:`_HALL_LIMIT` completed runs. ``ts`` is the game clock, so a seeded
    test clock drives a deterministic payload.
    """
    players = [
        {
            "name": p.name,
            "x": p.x,
            "y": p.y,
            "level": p.level,
            "wins": p.wins,
            "hp": p.hp,
            "max_hp": p.max_hp,
            "mode": p.mode.value if isinstance(p.mode, Mode) else str(p.mode),
            "gold": p.gold,
            "banked": p.banked,
            "satchel": _satchel_entries(game, p.satchel),
        }
        for p in game.players.values()
    ]
    herald = [
        {"ts": event.ts, "kind": event.kind, "text": event.text} for event in _recent_events(game)
    ]
    hall = [
        {
            "name": entry.name,
            "level_at_win": entry.level_at_win,
            "run_days": entry.run_days,
            "win_ts": entry.win_ts,
        }
        for entry in game.store.top_hall(_HALL_LIMIT)
    ]
    return {
        "ts": game.clock().isoformat(),
        "players": players,
        "herald": herald,
        "hall": hall,
    }


def _satchel_entries(game: Game, satchel: str) -> list[dict[str, object]]:
    """Decode a player's ``"id:qty"`` satchel into ``[{"name", "qty"}, ...]``.

    Decodes the bag through the shared
    :func:`~understone.engine.satchel.decode_satchel` codec, then resolves each
    stack's id to its display name via the world's item table; an id no longer in
    the pack (a save edited out from under it) falls back to the raw id, so the
    lobby TV never shows a blank entry. The Watch is read-only, so it only
    decodes — the name-resolution is the only work that lives here.
    """
    entries: list[dict[str, object]] = []
    for item_id, qty in decode_satchel(satchel):
        item = game.world.item_by_id(item_id)
        entries.append({"name": item.name if item is not None else item_id, "qty": qty})
    return entries


def _recent_events(game: Game) -> list[Event]:
    """Return the last :data:`_HERALD_LIMIT` resident PUBLIC events, oldest-first.

    PRIVATE notes (a non-empty ``target`` — ambush victim alerts, inn mail)
    are filtered out first: the lobby TV is a public broadsheet and must never
    show a message addressed to one player. The façade keeps events in
    ascending id order, so the tail of the public slice IS the newest public
    window — correct even when AUTOINCREMENT ids are sparse.
    """
    public = [event for event in game.events if not event.target]
    return public[-_HERALD_LIMIT:]


# The Watch page. One self-contained document: inline CSS + vanilla JS, no
# external assets, no innerHTML-with-data (every dynamic node is built with
# createElement / textContent). The base map is painted once from world.json;
# players are an absolutely-positioned overlay repainted from state.json every
# two seconds. On a fetch failure the page dims and shows "SIGNAL LOST".
#
# ``__HASH_EXPR__`` is filled below from the texture-module hash constants, so
# the JS index formula tracks a Python-side retune (see _build_watch_html).
_WATCH_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Understone — Live Watch</title>
<style>
  :root {
    --phosphor: #7dffa0;
    --phosphor-dim: #2f7a46;
    --amber: #ffb44d;
    --bg: #050a06;
    --panel: #0a140d;
    --edge: #163a22;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    background: var(--bg);
    color: var(--phosphor);
    font-family: "Noto Sans Mono", "DejaVu Sans Mono", "Liberation Mono", "Courier New", monospace;
    font-size: 14px;
    line-height: 1.2;
  }
  body::after {
    /* Scanline overlay — faint, non-interactive. */
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    background: repeating-linear-gradient(
      to bottom,
      rgba(0, 0, 0, 0) 0px,
      rgba(0, 0, 0, 0) 2px,
      rgba(0, 0, 0, 0.22) 3px,
      rgba(0, 0, 0, 0) 4px
    );
    z-index: 50;
  }
  body.lost { filter: grayscale(0.7) brightness(0.55); }
  header {
    padding: 10px 16px;
    border-bottom: 1px solid var(--edge);
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    text-shadow: 0 0 6px rgba(125, 255, 160, 0.5);
  }
  header h1 {
    margin: 0;
    font-size: 18px;
    letter-spacing: 2px;
    text-transform: uppercase;
  }
  .live {
    color: var(--amber);
    font-size: 13px;
    letter-spacing: 1px;
    text-shadow: 0 0 6px rgba(255, 180, 77, 0.5);
  }
  .live .dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    margin-right: 6px;
    border-radius: 50%;
    background: var(--amber);
    box-shadow: 0 0 8px var(--amber);
    animation: pulse 2s ease-in-out infinite;
  }
  body.lost .live .dot { animation: none; background: var(--phosphor-dim); box-shadow: none; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  main {
    display: flex;
    gap: 16px;
    padding: 16px;
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .map-frame {
    position: relative;
    border: 1px solid var(--edge);
    background: var(--panel);
    padding: 8px;
    overflow: auto;
    max-width: 100%;
    box-shadow: inset 0 0 24px rgba(0, 0, 0, 0.6);
    transition: filter 1.2s ease;
  }
  /* Time-of-day wash, toggled from the UTC hour of the state payload. The
     overlay is non-interactive and sits above the map but below the scanlines.
     night: a subtle blue dim; dawn/dusk: a faint amber wash; day: nothing. */
  .map-frame::after {
    content: "";
    position: absolute;
    inset: 0;
    pointer-events: none;
    opacity: 0;
    transition: opacity 1.2s ease, background-color 1.2s ease;
    z-index: 5;
  }
  .map-frame.night { filter: brightness(0.78) saturate(0.85); }
  .map-frame.night::after { opacity: 1; background-color: rgba(74, 120, 200, 0.16); }
  .map-frame.twilight::after { opacity: 1; background-color: rgba(255, 180, 77, 0.12); }
  #map {
    position: relative;
    white-space: pre;
    text-shadow: 0 0 4px rgba(125, 255, 160, 0.35);
  }
  #map .row { display: block; }
  #overlay {
    position: absolute;
    top: 0;
    left: 0;
    pointer-events: none;
  }
  #overlay .pc {
    position: absolute;
    color: var(--amber);
    text-shadow: 0 0 6px rgba(255, 180, 77, 0.8);
  }
  aside {
    flex: 1 1 280px;
    min-width: 260px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .card {
    border: 1px solid var(--edge);
    background: var(--panel);
    padding: 10px 12px;
  }
  .card h2 {
    margin: 0 0 8px;
    font-size: 13px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--phosphor);
    border-bottom: 1px solid var(--edge);
    padding-bottom: 4px;
  }
  ul { margin: 0; padding: 0; list-style: none; }
  li { padding: 2px 0; }
  .muted { color: var(--phosphor-dim); }
  .subline { font-size: 12px; padding-left: 2px; }
  .adv-name { color: var(--amber); }
  .stars { color: var(--amber); letter-spacing: 1px; }
  .feed li { border-bottom: 1px dotted var(--edge); padding: 4px 0; }
  .feed li:last-child { border-bottom: none; }
  .feed .ts { color: var(--phosphor-dim); margin-right: 6px; }
</style>
</head>
<body>
  <header>
    <h1 id="world-name">The Understone Watch</h1>
    <div class="live"><span class="dot"></span><span id="live-label">CONNECTING…</span></div>
  </header>
  <main>
    <div class="map-frame">
      <div id="map"><div id="overlay"></div></div>
    </div>
    <aside>
      <section class="card">
        <h2>Adventurers</h2>
        <ul id="adventurers"><li class="muted">…</li></ul>
      </section>
      <section class="card">
        <h2>Hall of Legends</h2>
        <ul id="hall"><li class="muted">No legends yet.</li></ul>
      </section>
      <section class="card">
        <h2>The Understone Herald</h2>
        <ul id="herald" class="feed"><li class="muted">…</li></ul>
      </section>
    </aside>
  </main>
<script>
(function () {
  "use strict";

  // Per-world CRT palette. Each named theme is a set of CSS custom-property
  // values applied to :root when world.json arrives (the pack's
  // settings.watch_theme picks one). "phosphor" holds the EXACT values of the
  // :root block above, so the default Vale is pixel-for-pixel unchanged; the
  // others re-tint the whole console:
  //   phosphor — the original green CRT (default).
  //   amber    — a warm gold CRT (classic amber monochrome monitor).
  //   ice      — a pale, cold blue CRT.
  //   ember    — a hot red/orange CRT.
  // The day/night wash from v0.6 composes ON TOP of whichever theme is set.
  var THEMES = {
    phosphor: {
      "--phosphor": "#7dffa0",
      "--phosphor-dim": "#2f7a46",
      "--amber": "#ffb44d",
      "--bg": "#050a06",
      "--panel": "#0a140d",
      "--edge": "#163a22"
    },
    amber: {
      "--phosphor": "#ffc14d",
      "--phosphor-dim": "#7a5320",
      "--amber": "#fff0a8",
      "--bg": "#0a0702",
      "--panel": "#14100a",
      "--edge": "#3a2c16"
    },
    ice: {
      "--phosphor": "#9fe6ff",
      "--phosphor-dim": "#2f5f7a",
      "--amber": "#ffe07d",
      "--bg": "#04080a",
      "--panel": "#0a1014",
      "--edge": "#16303a"
    },
    ember: {
      "--phosphor": "#ff8a6b",
      "--phosphor-dim": "#7a3320",
      "--amber": "#ffd07d",
      "--bg": "#0a0503",
      "--panel": "#140a07",
      "--edge": "#3a1c16"
    }
  };

  // Swap the CSS custom-property values for the pack's theme. Unknown or
  // missing theme names fall back to "phosphor", so the console always has a
  // coherent palette even if a future theme reaches the page unknown.
  function applyTheme(name) {
    var theme = THEMES[name] || THEMES.phosphor;
    for (var prop in theme) {
      if (Object.prototype.hasOwnProperty.call(theme, prop)) {
        document.documentElement.style.setProperty(prop, theme[prop]);
      }
    }
  }

  // Palette colour-name -> phosphor-tinted hex. ONE global map, shared by every
  // world (no per-world or per-theme palettes). Mirrors understone.screen.palette
  // Color values 1:1 — a guard test asserts every Color role has an entry here,
  // so a shipped role can never silently fall back to default. The base map is
  // coloured from this, never from the server.
  var PALETTE = {
    default: "#7dffa0",
    wall: "#5a6b60",
    floor: "#3f7a52",
    player: "#ffb44d",
    other_player: "#ffd089",
    monster: "#ff6b6b",
    item: "#ffe07d",
    water: "#4aa6c8",
    tree: "#3fae6a",
    town: "#ffd089",
    dungeon: "#c98bff",
    // v0.9 expanded terrain/location roles, chosen for hue separation:
    road: "#b89a6a",
    forest: "#6a9f3f",
    scrub: "#9c6038",
    lava: "#ff7a3c",
    barren: "#9a8b7a",
    inn: "#ff9d4d",
    shop: "#ffd24d",
    healer: "#5fd6b0"
  };

  function colorFor(name) {
    return PALETTE[name] || PALETTE.default;
  }

  // Deterministic terrain texture. MUST stay in lockstep with
  // understone.screen.texture: the same base->variants rows and the same
  // index formula. The formula below is INTERPOLATED from texture._HASH_X /
  // _HASH_Y at module build time, so a Python-side retune rewrites this line;
  // only the VARIANTS rows must still be mirrored by hand.
  var VARIANTS = {
    ".": ".,'",
    "\\u224b": "\\u224b\\u2248"
  };

  function textured(ch, x, y) {
    var choices = VARIANTS[ch];
    if (!choices) { return ch; }
    return choices.charAt((__HASH_EXPR__) % choices.length);
  }

  var overlay = document.getElementById("overlay");
  var mapEl = document.getElementById("map");
  var liveLabel = document.getElementById("live-label");
  var dims = null; // {width, height} once the map is painted.

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function clockLabel(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return "--:--:--"; }
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  function stars(wins) {
    if (wins <= 0) { return ""; }
    if (wins <= 5) { return "\\u2605".repeat(wins); }
    return "\\u2605x" + wins;
  }

  // Paint the base map ONCE. Each row is a sequence of <span> runs, a new run
  // only where the legend colour changes, so a row is a handful of spans.
  function paintMap(world) {
    applyTheme(world.theme);
    document.getElementById("world-name").textContent = world.name + " — Live Watch";
    var legend = world.legend || {};
    var rows = world.glyph_rows || [];
    for (var y = 0; y < rows.length; y++) {
      var row = rows[y];
      var rowEl = document.createElement("div");
      rowEl.className = "row";
      var runText = "";
      var runColor = null;
      for (var x = 0; x < row.length; x++) {
        var ch = row.charAt(x);
        // Colour keys off the BASE terrain glyph; the rendered glyph is the
        // position-keyed variant (a variant shares its terrain's colour).
        var col = colorFor(legend[ch]);
        if (runColor === null) { runColor = col; }
        if (col !== runColor) {
          rowEl.appendChild(makeSpan(runText, runColor));
          runText = "";
          runColor = col;
        }
        runText += textured(ch, x, y);
      }
      if (runText.length) { rowEl.appendChild(makeSpan(runText, runColor)); }
      mapEl.insertBefore(rowEl, overlay);
    }
    dims = { width: world.width, height: world.height };
    paintLocations(world.locations || []);
  }

  function makeSpan(text, color) {
    var span = document.createElement("span");
    span.style.color = color;
    span.textContent = text;
    return span;
  }

  // Locations are painted into the overlay layer (above the base terrain) so
  // their glyph and colour win over the terrain beneath the door.
  function paintLocations(locations) {
    for (var i = 0; i < locations.length; i++) {
      var loc = locations[i];
      var el = document.createElement("span");
      el.className = "pc";
      el.style.left = "calc(" + loc.x + " * 1ch)";
      el.style.top = "calc(" + loc.y + " * 1lh)";
      el.style.color = colorFor(loc.color);
      el.style.textShadow = "0 0 6px " + colorFor(loc.color);
      el.textContent = loc.glyph;
      el.title = loc.name;
      overlay.appendChild(el);
    }
  }

  // Player markers live in their own layer, cleared and repainted each poll.
  var pcLayer = document.createElement("div");
  pcLayer.id = "pc-layer";
  overlay.appendChild(pcLayer);

  function paintPlayers(players) {
    while (pcLayer.firstChild) { pcLayer.removeChild(pcLayer.firstChild); }
    for (var i = 0; i < players.length; i++) {
      var p = players[i];
      var el = document.createElement("span");
      el.className = "pc";
      el.style.left = "calc(" + p.x + " * 1ch)";
      el.style.top = "calc(" + p.y + " * 1lh)";
      // Every adventurer on the lobby TV is "another player" (there is no
      // viewer here), so all wear the other-player marker. Mirrors the '☻'
      // the game frame paints for rivals.
      el.textContent = "\\u263b";
      el.title = p.name;
      pcLayer.appendChild(el);
    }
  }

  function renderAdventurers(players) {
    var list = document.getElementById("adventurers");
    while (list.firstChild) { list.removeChild(list.firstChild); }
    if (!players.length) {
      list.appendChild(muted("The Vale is empty."));
      return;
    }
    var sorted = players.slice().sort(function (a, b) {
      return b.level - a.level || a.name.localeCompare(b.name);
    });
    for (var i = 0; i < sorted.length; i++) {
      var p = sorted[i];
      var li = document.createElement("li");
      var name = document.createElement("span");
      name.className = "adv-name";
      name.textContent = p.name;
      li.appendChild(name);
      var star = stars(p.wins);
      if (star) {
        var s = document.createElement("span");
        s.className = "stars";
        s.textContent = " " + star;
        li.appendChild(s);
      }
      var rest = document.createElement("span");
      rest.className = "muted";
      rest.textContent = "  Lv" + p.level + "  HP " + p.hp + "/" + p.max_hp;
      li.appendChild(rest);
      // A dim sub-line: gold on hand and (if any) gold in the vault. The whole
      // shared world is on the lobby TV, so every hero's purse is public here.
      var gold = document.createElement("div");
      gold.className = "muted subline";
      var goldText = (p.gold || 0) + "g";
      if (p.banked) { goldText += "  +" + p.banked + " vault"; }
      gold.textContent = goldText;
      li.appendChild(gold);
      // A second dim sub-line: the satchel stacks ("Name ×qty"), or empty.
      var sat = document.createElement("div");
      sat.className = "muted subline";
      sat.textContent = satchelText(p.satchel || []);
      li.appendChild(sat);
      list.appendChild(li);
    }
  }

  // Render the satchel stacks as a compact dot-joined line, or an empty note.
  function satchelText(stacks) {
    if (!stacks.length) { return "satchel empty"; }
    var parts = [];
    for (var i = 0; i < stacks.length; i++) {
      parts.push(stacks[i].name + " \\u00d7" + stacks[i].qty);
    }
    return parts.join(" \\u00b7 ");
  }

  function renderHall(hall) {
    var list = document.getElementById("hall");
    while (list.firstChild) { list.removeChild(list.firstChild); }
    if (!hall.length) {
      list.appendChild(muted("No legends yet."));
      return;
    }
    for (var i = 0; i < hall.length; i++) {
      var h = hall[i];
      var li = document.createElement("li");
      var star = document.createElement("span");
      star.className = "stars";
      star.textContent = "\\u2605 ";
      li.appendChild(star);
      var name = document.createElement("span");
      name.className = "adv-name";
      name.textContent = h.name;
      li.appendChild(name);
      var rest = document.createElement("span");
      rest.className = "muted";
      rest.textContent = "  Lv" + h.level_at_win + "  " + h.run_days + "d  " + (h.win_ts || "").slice(0, 10);
      li.appendChild(rest);
      list.appendChild(li);
    }
  }

  function renderHerald(herald) {
    var list = document.getElementById("herald");
    while (list.firstChild) { list.removeChild(list.firstChild); }
    if (!herald.length) {
      list.appendChild(muted("The Vale is still."));
      return;
    }
    for (var i = 0; i < herald.length; i++) {
      var e = herald[i];
      var li = document.createElement("li");
      var ts = document.createElement("span");
      ts.className = "ts";
      ts.textContent = clockLabel(e.ts);
      li.appendChild(ts);
      var text = document.createElement("span");
      text.textContent = e.text;
      li.appendChild(text);
      list.appendChild(li);
    }
  }

  function muted(text) {
    var li = document.createElement("li");
    li.className = "muted";
    li.textContent = text;
    return li;
  }

  function setLive(connected, iso) {
    if (connected) {
      document.body.classList.remove("lost");
      liveLabel.textContent = "LIVE \\u2022 updated " + clockLabel(iso);
    } else {
      document.body.classList.add("lost");
      liveLabel.textContent = "SIGNAL LOST";
    }
  }

  var mapFrame = document.querySelector(".map-frame");

  // Tint the map by the UTC hour of the world clock. The bands:
  //   night    20:00-05:59  -> subtle dim + blue ('night' class)
  //   dawn     06:00-07:59  -> faint amber wash ('twilight' class)
  //   dusk     18:00-19:59  -> faint amber wash ('twilight' class)
  //   day      08:00-17:59  -> no tint
  // UTC (not local) so every spectator sees the same sky as the game clock.
  function applyDayPhase(iso) {
    var d = new Date(iso);
    mapFrame.classList.remove("night", "twilight");
    if (isNaN(d.getTime())) { return; }
    var h = d.getUTCHours();
    if (h >= 20 || h < 6) {
      mapFrame.classList.add("night");
    } else if (h < 8 || h >= 18) {
      mapFrame.classList.add("twilight");
    }
  }

  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    });
  }

  function poll() {
    getJSON("./watch/state.json").then(function (state) {
      paintPlayers(state.players || []);
      renderAdventurers(state.players || []);
      renderHall(state.hall || []);
      renderHerald(state.herald || []);
      applyDayPhase(state.ts);
      setLive(true, state.ts);
    }).catch(function () {
      setLive(false, null);
    });
  }

  var POLL_MS = 2000;

  // Bootstrap retries until the base map loads, so a spectator who opens the
  // page during a server blip recovers without a manual reload. The poll
  // interval starts exactly once, on the first successful boot.
  function boot() {
    getJSON("./watch/world.json").then(function (world) {
      paintMap(world);
      poll();
      setInterval(poll, POLL_MS);
    }).catch(function () {
      setLive(false, null);
      setTimeout(boot, POLL_MS);
    });
  }
  boot();
})();
</script>
</body>
</html>
"""


def _build_watch_html() -> str:
    """Fill the texture hash formula into the page template.

    The JS ``textured`` index is interpolated from
    :data:`~understone.screen.texture._HASH_X` / ``_HASH_Y`` so the page's
    formula is a derivation of the same two constants the Python renderer uses;
    a retune of either moves both, and a guard test pins the agreement.
    """
    hash_expr = f"x * {texture._HASH_X} + y * {texture._HASH_Y}"
    return _WATCH_HTML_TEMPLATE.replace("__HASH_EXPR__", hash_expr)


WATCH_HTML = _build_watch_html()
