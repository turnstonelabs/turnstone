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


def _recent_events(game: Game) -> list[Event]:
    """Return the last :data:`_HERALD_LIMIT` resident events, oldest-first.

    The façade keeps events in ascending id order, so the list tail IS the
    newest window — a plain slice is correct even when event ids are sparse
    (AUTOINCREMENT gaps must not shrink the feed).
    """
    return game.events[-_HERALD_LIMIT:]


# The Watch page. One self-contained document: inline CSS + vanilla JS, no
# external assets, no innerHTML-with-data (every dynamic node is built with
# createElement / textContent). The base map is painted once from world.json;
# players are an absolutely-positioned overlay repainted from state.json every
# two seconds. On a fetch failure the page dims and shows "SIGNAL LOST".
WATCH_HTML = """\
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
    font-family: "DejaVu Sans Mono", "Liberation Mono", "Courier New", monospace;
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
  }
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

  // Palette colour-name -> phosphor-tinted hex. Mirrors understone.screen.palette
  // Color values; the base map is coloured from this, never from the server.
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
    dungeon: "#c98bff"
  };

  function colorFor(name) {
    return PALETTE[name] || PALETTE.default;
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
        var col = colorFor(legend[ch]);
        if (runColor === null) { runColor = col; }
        if (col !== runColor) {
          rowEl.appendChild(makeSpan(runText, runColor));
          runText = "";
          runColor = col;
        }
        runText += ch;
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
      el.textContent = "@";
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
      list.appendChild(li);
    }
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
