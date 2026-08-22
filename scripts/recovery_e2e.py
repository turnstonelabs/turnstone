#!/usr/bin/env python3
"""Browser-level SSE recovery livepass — boots the REAL interactive.js
``InteractivePane`` against a REAL Turnstone node and drives the two
headline recovery scenarios through headless Chrome over CDP, stamping
``document.title`` verdicts (``RECOVERY-READY-*`` / ``RECOVERY-FAILED-*``)
the livepass convention.

Unlike ``scripts/livepass.py`` (which stubs ``window.authFetch`` with
canned fixtures), this page uses the REAL auth + REAL EventSource against a
REAL node: the page is served same-origin by the node itself (so cookie
auth and EventSource just work), the node runs a scripted-provider
workstream so a REST ``/send`` drives a real bash storm, and the pane's
own state machine (interactive.js) does the recovery.

Usage::

    python3 scripts/recovery_e2e.py                 # run every scenario
    python3 scripts/recovery_e2e.py --scenario storm
    python3 scripts/recovery_e2e.py --scenario restart
    python3 scripts/recovery_e2e.py --scenario coord-restart
    python3 scripts/recovery_e2e.py --scenario fail-refetch      # D (#890)
    python3 scripts/recovery_e2e.py --scenario stale-ref-reload  # E1 (#890)
    python3 scripts/recovery_e2e.py --scenario rewind-window     # E2 (#890)
    python3 scripts/recovery_e2e.py --scenario rewind-failed-window  # E3 (#890)
    python3 scripts/recovery_e2e.py --scenario stale-backstop    # E4 (#890)
    python3 scripts/recovery_e2e.py --scenario hidden-retry      # E5 (#900)
    python3 scripts/recovery_e2e.py --scenario await-window-gate # E6 (#900)
    python3 scripts/recovery_e2e.py --scenario destroy-invalidation # E7 (#900)
    python3 scripts/recovery_e2e.py --scenario reconnect-in-await # E8 (#900 r2)
    python3 scripts/recovery_e2e.py --scenario coord-rewind-window        # G1 (#894)
    python3 scripts/recovery_e2e.py --scenario coord-rewind-failed-window # G2 (#894)
    python3 scripts/recovery_e2e.py --scenario coord-stale-backstop       # G3 (#894)
    python3 scripts/recovery_e2e.py --scenario coord-heal-midturn         # G4 (#894)
    python3 scripts/recovery_e2e.py --scenario coord-hidden-retry         # G5 (#894)
    python3 scripts/recovery_e2e.py --scenario coord-orphan-rewind        # G6 (#894 r6)
    python3 scripts/recovery_e2e.py --scenario coord-joined-flight        # G7 (#894 r8)
    python3 scripts/recovery_e2e.py --scenario handoff-repair-budget
    python3 scripts/recovery_e2e.py --scenario coord-handoff-repair-budget
    python3 scripts/recovery_e2e.py --scenario user-turn-two-pane
    python3 scripts/recovery_e2e.py --scenario tool-turn-two-pane
    python3 scripts/recovery_e2e.py --scenario roster-restart    # F1 (#881)
    python3 scripts/recovery_e2e.py --scenario roster-restart-native  # F2 (#881)
    python3 scripts/recovery_e2e.py --scenario both   # A+B only (legacy)
    python3 scripts/recovery_e2e.py --keep-open 8971  # serve the storm page
                                                      # for manual inspection

The #890 guard-before-wipe family (against the interactive pane; the
coordinator ports are the G family below):

Scenario D (fail-refetch): clones B, but arms one forced /history 500
(``RecoveryServer.fail_history``) just before the show edge so the first
truncated resync FAILS.  Asserts the failed fetch is a DOM/ref no-op — the
pre-restart rows stay, no empty-state, sentinel un-healed — with the backend
proof ``history_fail_remaining == 0``; then the connect-chokepoint retry's
second /history heals it.  Stamps ``RECOVERY-READY-FAILFETCH-stale1-healed1``.

Scenario E1 (stale-ref-reload): a mid-content transport teardown leaves a
live assistant-bubble ref; a same-ws UNARMED re-auth reload (the factory's
onLogin fan-out) must reset it so the next turn's text builds a FRESH bubble
instead of concatenating into the stale one.  Stamps
``RECOVERY-READY-STALEREF-fresh1``.

Scenario E2 (rewind-window): three completed turns; a REAL rewind click POSTs
and its clear_ui refetch is held open (``RecoveryServer.delay_history``),
keeping the quiesce armed; a second REAL rewind click mid-rebuild must be
gated by ``busy || _historyStale`` and never reach the server.  Backend proof
``rewind_requests == 1``.  Stamps ``RECOVERY-READY-REWINDWIN-posts1``.

Scenario E3 (rewind-failed-window): the FAILED-refetch sibling of E2.  Three
completed turns; a REAL rewind click POSTs and its clear_ui refetch is forced
to 500 (``RecoveryServer.fail_history``) instead of held open.  The failed
fetch releases the transient ``_replayQueue`` quiesce but the ``_historyStale``
latch SURVIVES (cleared ONLY by a successful ``replayHistory`` render), so a
second REAL rewind click over the stale-but-real transcript must stay gated by
``busy || _historyStale`` and never reach the server — the exact aftermath
where pre-latch (quiesce-only) code reopened the gate and let a second rewind
over-rewind.  The bounded 2s retry then heals the transcript (rewound to ONE
user row) and reopens the gate so a fresh rewind legitimately lands.  Backend
proofs: ``rewind_requests`` 1 -> (gated) 1 -> 2, ``history_fail_remaining ==
0``, ``history_requests >= 2``.  Stamps
``RECOVERY-READY-REWINDFAIL-posts2-heal1``.

Scenario E4 (stale-backstop): the DOUBLE-failure sibling of E3, proving the
``_historyStale`` latch's TRANSPORT-FREE idle-edge backstop (#890, the
round-5 critical).  Three completed turns; a REAL rewind click POSTs and
BOTH its clear_ui refetch AND its one bounded 2s retry are forced to 500
(``RecoveryServer.fail_history(2)``), so the latch cannot self-heal and
rewind/edit stay latch-gated over the stale-but-real transcript (a row-0
rewind click stays gated, ``rewind_requests`` holds at 1; the three user
rows survive).  The recovery server publishes a real per-workstream idle
edge through the loaded UI, firing a quiesced, same-token REST
``_refetchHistory`` without admitting another user row
(deliberately NOT ``_loadHistoryThenConnect`` — the old reload backstop drew
the server's synthetic ``state_change:idle`` on its fresh reconnect and
re-triggered itself, a zero-backoff reconnect/refetch storm against a
recovering node).  With the fault budget now exhausted the refetch succeeds
and rebuilds the single post-rewind user row, clearing the latch, so a
fresh rewind on a remaining row lands (``rewind_requests`` -> 2).  THE r5
PROOF, both counted at the fault layer: ``events_requests`` is UNCHANGED
across the whole heal episode (``sse0`` — zero new EventSource connections;
the storm would have opened one per reconnect) and ``history_requests`` grew
by exactly ONE (the backstop's single fetch).  All polls are deadline-bounded
so a regressed looping backstop
stamps a clean FAILED, never a hang.  Stamps
``RECOVERY-READY-STALEBACKSTOP-heal1-sse0``.

Scenario E5 (hidden-retry): the clear_ui retry's stream-liveness fire guard
(#900 — the interactive mirror of G5, filed from the #894 campaign's
backport set).  ``disconnectSSE`` deliberately leaves the 2s retry ARMED
(transport-only redials keep the pending heal intent), so a close-on-hide
inside the arm window lets it fire with the transport DOWN.  A seedless
``_refetchHistory`` then renders /history's as-of-now truth while
``_lastEventId`` stays frozen at the last delivered id — and the show-edge
reconnect presents that frozen cursor, so the server's replay_ok slice
repaints every turn the hidden render just committed (only ``system_turn``
and compaction markers carry id dedup; content and tool rows do not).  The
detector is a NON-OCCURRENCE counted at the fault layer: ``history_requests``
UNCHANGED across the hidden window (``hidden0``), which regresses to
``hidden1`` the moment the guard's transport clause is removed.  Note the
scope: a hide nulls ``evtSource``, so this exercises the PRESENCE term only —
the ``readyState`` half needs a redial in progress, which no fault primitive
produces deterministically (see the runner's docstring).  A replay_ok
reconnect carries no synthetic ``state_change``, so
the latch survives ``__show`` — the accepted liveness lag — and the heal
rides a server-origin idle edge into the transport-free backstop.  This avoids
admitting a live ``user_turn`` and starting unrelated model work; exactly ONE
new SSE open remains across show + heal.  Stamps
``RECOVERY-READY-HIDDENRETRY-hidden0-heal1``.

Scenario E6 (await-window-gate): the seedless render's cursor-safety gate
(#900) — the AWAIT-window half E5 cannot reach, since E5's retry never
fetches at all.  Here the retry fires with the transport OPEN (its fire
guard passes) and its /history is HELD at the fault layer while a
close-on-hide drops the stream underneath it, so the payload resolves onto a
dead transport: rendering it would commit as-of-now truth with
``_lastEventId`` frozen BELOW the rows painted — E5's double-render, reached
through a window no fire-time check can see.  The render-time gate declines
and takes the failed-fetch path instead.  DETECTOR: the stale-but-real
PRE-rewind transcript must survive the RESOLVED fetch — THREE user rows and
the latch still SET (``replayHistory`` is the latch's only clear site, so a
held latch proves no render ran); without the gate it stamps rows1-latch0.
The repair still arrives on the organic settle the transport-free backstop
owns.  Stamps ``RECOVERY-READY-AWAITGATE-rows3-latch1``.

Scenario E8 (reconnect-in-await): the stream-generation term (#900 r2), and
the FIRST detector here that can observe a double render at all.  Every other
scenario counts ``.msg.user`` rows; that count stays constant when an
assistant/tool slice is painted twice, so none of them can see the artefact
this campaign prevents. E8 counts a sentinel's OCCURRENCES in the transcript
text instead.  The retry
fires with the transport OPEN, its /history is held, and inside that await the
transport DROPS and RE-ESTABLISHES: ``readyState`` reads OPEN afterwards
exactly as before, so neither the presence nor the readyState term can tell
the two apart, but the redial re-presented the frozen cursor, the server
answered replay_ok, and the quiesce BUFFERED that slice — rendering commits
the turn and the flush repaints it.  The connection generation is the only
term that sees it (object identity cannot: a native reconnect reuses the same
EventSource).  Verified by control: stripping the term stamps ``dupes2``.
Stamps ``RECOVERY-READY-RECONNECTAWAIT-dupes1-healed1``.

The #894 coordinator ports (G family — the coord pane's ``historyStale``
latch; coord state is closure-private, so where E2-E4 read pane fields the
G runners read the fault layer's authoritative counters and prove gate
closure by POST NON-occurrence; the latch-cleared proof is the reopen POST):

Scenario G1 (coord-rewind-window): E2's port.  The clear_ui refetch is held
open; the in-flight edge is the ``history_requests`` bump (counted on
arrival, before the hold sleeps — the latch was set synchronously before
that fetch dispatched).  A second rewind click mid-hold must be gated by
``busy || historyStale``.  Stamps ``RECOVERY-READY-COORDREWINDWIN-posts1``.

Scenario G2 (coord-rewind-failed-window): E3's port.  The failed refetch
keeps the latch set (its only clear sits below the ``if (!hist) return``
failure guard); the retry's held /history defers the heal so the gated
click provably lands in the aftermath window; the bounded 2s retry then
heals and reopens.  Stamps ``RECOVERY-READY-COORDREWINDFAIL-posts2-heal1``.

Scenario G3 (coord-stale-backstop): E4's port.  Double failure exhausts
clear_ui refetch + retry; a server-origin idle edge fires the TRANSPORT-FREE
backstop (plain seedless ``refetchHistory``, never
``loadHistoryThenReconnect``).  Same storm proof: ``events_requests``
UNCHANGED, ``history_requests`` +1.  Stamps
``RECOVERY-READY-COORDSTALEBACKSTOP-heal1-sse0``.

Scenario G4 (coord-heal-midturn, #894 r4): the render-time live-tool gate.  A
server-origin idle edge starts a held backstop fetch, then a real
``tool_pending`` event makes the event-owned live-call set non-empty.  The
good response must be declined without wiping the stale transcript or the
live tool shell (``mid1``); a matching result plus another idle edge then
heals.  ``events_requests`` stays unchanged and ``history_requests`` grows by
exactly two (declined fetch + heal).  Stamps
``RECOVERY-READY-COORDHEALMIDTURN-heal1-mid1-sse0``.

Scenario G5 (coord-hidden-retry, #894 r4): the retry's stream-liveness
fire guard.  A retry armed before close-on-hide must NOT fetch while the
transport is down (a seedless render past the frozen ``lastEventId``
double-renders on the show-edge replay): ``history_requests`` UNCHANGED
across the hidden fire window (``hidden0``).  A replay_ok reconnect
carries no synthetic state_change (only fresh/truncated replays do), so
post-show the latch stays closed (the accepted liveness-lag residual)
until a server-origin idle edge fires the TRANSPORT-FREE backstop on the live
stream (exactly ONE new SSE open across show + heal — the user-driven
reconnect; the heal adds zero).  Stamps
``RECOVERY-READY-COORDHIDDENRETRY-hidden0-heal1``.

Scenario G6 (coord-orphan-rewind, #894 r6/r7): the poisoned-pane
detector.  A HARD mid-tool node kill (``stop(hard=True)`` — graceful
close would synthesize a cancel result and mask the state; boot-time
rehydration synthesizes nothing, r7-verified) leaves the committed
tool_calls turn unresulted; recovery paints it as a ``--running``
placeholder no result ever strips.  The r5 DOM-probed gate read that
residue as live and skipped every seedless render — rewind/edit
permanently dead.  The event-driven live-call set is empty for the
orphan, so the rewind must render THROUGH it: residue asserted PRESENT
post-recovery, then the orphan is wiped and the rewound truth paints
(the server keeps the user message and removes the unresulted assistant
turn), posts 1, ``history_requests`` grew.  Stamps
``RECOVERY-READY-COORDORPHANREWIND-posts1-rows1-orphan0``.

Scenario G7 (coord-joined-flight, #894 r8): the joined-flight window.
One browser plus a background GET ("viewer B") on one ws;
``delay_load`` parks B's pre-rewind /history flight open INSIDE
``load_messages`` (the flight layer — the fault
layer's ``delay_history`` cannot overlap flights); A rewinds mid-hold.
A's clear_ui refetch must MISS the held flight — the server folds the
truncation generation into the #884 flight key — proven by
``load_calls`` growing TWO (a joined request never enters
``load_messages``) and A rendering the post-rewind single row.
Pre-fix, A JOINED the pre-rewind flight (loads1), painted three stale
rows as fresh truth, and cleared the latch — the over-rewind window
through the server seam.  Stamps
``RECOVERY-READY-COORDJOINEDFLIGHT-posts1-loads2-rows1``.

Scenario A (storm): the page connects, POSTs ``/send`` on stream-open (so
the listener is registered first), the node runs a 4-parallel-bash
``seq 1 500`` storm plus a task_agent whose sub-tools are chatty bashes;
the page asserts the final DOM has the expected top-level tool rows, the
task_agent card nests its sub-tool rows (NO child escaped to the top
level), and a second one-turn task_agent leaves no empty finished card. Both
agents report >80% prompt usage so the page can latch the real context badge,
its warning style, and its accessible label before terminal cleanup. The runner
reloads during a paced sub-tool and proves the new pane repaints the synthetic
snapshot before another model call; the final agent recycles the prior call id
and has its parent event delayed in-browser to exercise context-before-parent
relinking. The composer must settle idle. Stamps ``RECOVERY-READY-STORM-<n>``.

Scenario B (hide mid-turn -> restart -> show): the runner hides the tab
the moment the first streamed line paints (freezing the pane's cursor at
a mid-turn event id — the MessageEvent ``lastEventId`` capture is what
makes that cursor real; the pre-2026-07 object-form read left it null and
this whole path unassertable), lets the turn and a follow-up text commit
while hidden, restarts the node on the SAME port (fresh empty ring,
storage-seeded counter), then shows the tab.  The show-edge reconnect
presents the stale cursor, MUST draw ``replay_truncated`` (asserted:
trunc>=1), the truncated resync rebuilds from /history, and the turns
committed during the hide window MUST be present afterwards (asserted:
``healed`` — the 'turn disappeared' field symptom).  Stamps
``RECOVERY-READY-RESTART-rows<n>-trunc<n>``.  The exact ``lost_count``
arithmetic and the failed-resync retry stay at the server-contract level
in Tier 1's ``test_restart_truncated_honesty`` /
``test_failed_resync_retries_via_truncation_record``.

A NOTE ON THE BROWSER OVERFLOW (server-side poison): a real listener-queue
poison needs the browser to STOP reading the socket so TCP backpressure
reaches the server. A backgrounded/CPU-throttled tab does NOT do this --
Chrome's network stack keeps draining the socket regardless of JS
throttling, and interactive.js deliberately CLOSES the stream on tab-hide
rather than starving it. So the server-side overflow -> stream_overflow ->
reconnect path is NOT reliably forcible from a real browser (which is why
that field bug was subtle); it is proven at the server-contract level in
``tests/test_sse_recovery_e2e.py::test_slow_consumer_overflow_then_lossless_reconnect``.
Scenario A here proves the OTHER half at the browser level: fix-3's
de-amplified storm renders correctly with no escaped sub-agent children.

MANUAL RUNBOOK (if Chrome/CDP is unavailable): run this with
``--keep-open PORT`` to boot the node + serve the storm page, open the
printed URL in a browser (the script prints the auth cookie to set), and
watch ``document.title``. For the restart scenario, boot with a fixed
port, load the restart page, background the tab, restart the node
(``RecoveryServer`` on the same port), foreground the tab, and watch the
title settle to ``RECOVERY-READY-RESTART``.

Scenario C (coord-restart): the REAL coordinator pane
(console/static/coordinator/coordinator.js — the #882 parity port of the
same truncated-recovery machinery) driven through the SAME hide -> restart
-> show sequence as Scenario B.  The coordinator only runs under the
console app in production, and the console's coordinator subsystems build
inside its server lifespan against a config-resolved model registry — no
``create_app(prebuilt SessionManager)`` seam for this harness's scripted
provider.  So the scenario mounts the pane against the interactive
recovery node instead: the node serves the console's coordinator static
tree at ``/coord-static`` (a distinct prefix — the node's own ``/static``
mount would swallow the console path) and a pane-only page at
``/coord-recovery``; the pane's module imports are all absolute
``/shared/*`` and resolve against the node.  Fidelity caveats, all inert
for the recovery machinery under test: the workstream is
interactive-kind (no coordinator status events — the status bar keeps its
placeholder), and ``/children`` + ``/tasks`` 404 here (the pane's loaders
catch and render empty by design).  What IS real: the full chrome
(buildCoordChrome), cookie auth, EventSource + MessageEvent cursor
capture, the connect chokepoint, the dead-stream resync
(loadHistoryThenReconnect), the churn limiter, and the jitter.  Asserted:
the show-edge reconnect draws ``replay_truncated`` (trunc>=1, counted at
the transport by a page-side EventSource wrapper — the coordinator's
handleEvent, cursor, and even its SSE indicator are closure-private or
deliberately absent from the chrome), the resync rebuilds from /history
with the hidden-window turns present (``healed``), tool rows intact, the
stream re-opened post-show, the status bar not stuck dim, and idle
asserted server-side by the runner.  Stamps
``RECOVERY-READY-COORD-rows<n>-trunc<n>``.

MANUAL COORDINATOR RUNBOOK (real console topology, no CDP): boot a dev
console + one node (docker-compose dev cluster), open a coordinator with
running children, hide the tab mid-turn, restart the CONSOLE process (the
coordinator ring lives there), show the tab, and verify: the pane draws
one truncated full rebuild (no blank pane), the mid-run turn's tool rows
re-appear inside their batch (no standalone top-level orphan bubbles),
and turns committed while hidden are present.

Scenario F1 (roster-restart, #881): the REAL node dashboard (``/`` +
ui/static/app.js) driven through a node restart on the GLOBAL stream —
the last silent-gap class from the truncated-recovery campaign.  Unlike
the pane scenarios there is no custom page: the runner navigates to the
production dashboard and injects transport instrumentation via CDP
``Page.addScriptToEvaluateOnNewDocument`` (an EventSource wrapper scoped
to ``/events/global`` URLs — app.js's stream + cursor are module-private
and the roster machinery exposes no counters).  Phase A (negative
control): two live workstreams render as roster rows; a scripted turn on
one drives global ``ws_state`` frames, proving the page holds a live
epoch-tagged cursor with ZERO ``replay_truncated`` observed — the live
cursor must not false-positive.  Phase B: hide, close the global
EventSource (the browser-gave-up CLOSED state — a closed source never
auto-retries, so the show edge's MANUAL reconnect is the only
reconnect), restart the node on the same port re-opening only ONE
workstream, show.  ``_reconnectDeadSSEs`` fires ``connectGlobalSSE``,
which presents the stored pre-restart cursor via ``?last_event_id=``
(counted off the wrapper's URL — the item-6 client half); the reborn
node's epoch mismatch draws ``replay_truncated`` (``reason:
boot_epoch``) + a fresh node_snapshot, and the in-stream snapshot's
``evict`` sweep removes the not-reopened workstream's row — the exact
ghost-roster field symptom (pre-#881 the reborn ring answered
``replay_ok``-empty: no envelope, no snapshot, ghost row forever).
Asserted: browser-observed trunc>=1 + cursor-presented>=1 (transport
wrapper), ghost GONE + keeper PRESENT in the roster MODEL
(``TS_APP.getClusterState()``) and the RAIL (``fireRender``'s surface) —
deliberately not the dashboard TABLE, whose membership refreshes only on
interaction by design (see ``_roster_has_ws``) — and the backend proof
``global_events_requests>=1`` on the restarted node (the reconnect hit
the real endpoint).  Server-side, both cursor transports run the same
parse (header-using tests cover it in Tier-1
``test_global_sse_boot_epoch``); the CLIENT-side native path is a
different animal and is Scenario F2's job.
Stamps/returns ``RECOVERY-READY-ROSTER-trunc<n>-cursor<n>``.

Scenario F2 (roster-restart-native, #881): the NATIVE-transport sibling
of F1, and the only behavioral coverage of two pure-browser semantics no
Tier-1 harness can express (the mechanism-5 lesson): the auto-reconnect
header echo, and id-less frames inheriting the connection's persisted
``lastEventId`` (WHATWG) — the mechanism that forced app.js's
node_snapshot-branch cursor clear.  Phase A as F1.  Phase B: restart
with the tab VISIBLE and the EventSource left OPEN, so the browser's own
retry carries the stale pre-restart header — ``trunc>=1`` with
``cursor==0`` IS the header-transport proof (only a presented-and-stale
cursor draws the envelope; the query-param counter stays untouched) —
and the heal must evict the ghost from model+rail.  Phase C (the
round-3 fix's discriminator): immediately after the heal — guarded by
an idFrames-unchanged precondition so the ~10s aggregate tick cannot
race in unnoticed — force a manual reconnect (close the wrapper-held
EventSource + hide/show).  It must go CURSORLESS with ``trunc`` still at
1: pre-fix, the id-less snapshot frame re-captured the dead cursor and
this reconnect presented it for a redundant second truncated round
(``cursor1-trunc2``).  ``__globalOpens`` proves the second connection
happened at the transport.  Stamps/returns
``RECOVERY-READY-ROSTERNATIVE-trunc<n>-cursor0-opens<n>``.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Healed-gap sentinel for scenario B: injected as the scripted turn-2 text
# AND threaded to the page via ``?healed=`` (read into ``healedSentinel``),
# so the injected text and the DOM check share one definition.  Must never
# collide with rendered command/output text — the bash command row paints
# its shell source verbatim, which contains the keyword ``done``.
HEALED_SENTINEL = "HEALED-e5b1"

# Second-turn sentinel for scenario E1 (stale-ref-reload): injected as the
# scripted turn-2 final text AND threaded to the page via ``?second=``.
# Same collision-proof discipline as HEALED_SENTINEL — it must not appear in
# turn 1's assistant bubble or any command/output, so "the sentinel landed in
# a fresh bubble, not the stale one" is an honest DOM check.
SECOND_SENTINEL = "SECOND-a7f3"

# Heal sentinel for scenario E6 (await-window-gate): its scripted fourth turn
# drives the later organic repair edge and proves that repair rendered.
BACKSTOP_SENTINEL = "BACKSTOP-b2e4"
# Scenario E8's duplicate-detector sentinel.  Its whole job is to be COUNTED,
# not merely found: the artefact #900's stream-generation term prevents is a
# turn rendered twice, and every other detector in this file counts
# ``.msg.user`` rows — a duplicated assistant/tool slice does not change that
# count, so none of them can see it.
DUPLICATE_SENTINEL = "DUPE-c9a7"

# Row-count selectors, hoisted above every runner that uses them (#900 r2):
# eight inline copies had accumulated ABOVE the old definition site, so the
# duplication was invisible to anyone reading top-down.  Python resolves
# module names at CALL time, so the old placement worked — it just could
# not be adopted by the runners that needed it most.
_ROWS_JS = "window.__pane.messagesEl.querySelectorAll('.msg.user').length"
_COORD_ROWS_JS = "document.getElementById('coord-messages').querySelectorAll('.msg.user').length"

# ---------------------------------------------------------------------------
# The recovery page — served same-origin by the node at /recovery.
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>recovery livepass</title>
    <link rel="stylesheet" href="/shared/base.css" />
    <link rel="stylesheet" href="/shared/ui-base.css" />
    <link rel="stylesheet" href="/shared/chat.css" />
    <link rel="stylesheet" href="/shared/conversation.css" />
    <link rel="stylesheet" href="/shared/cards.css" />
    <link rel="stylesheet" href="/shared/interactive.css" />
    <style>
      body { margin: 0; background: var(--bg); color: var(--ink); }
      #mount { height: 100vh; display: flex; }
      #mount > * { flex: 1; min-height: 0; }
    </style>
  </head>
  <body>
    <div id="header"><div id="status-bar"></div></div>
    <div id="mount"></div>
    <script>
      // Minimal globals interactive.js reads on the standalone path.
      window.showToast = function (m) { console.log("toast:", m); };
      window.showLogin = function () {};
    </script>
    <script type="module">
      import {
        InteractivePane,
        createInteractivePane,
      } from "/shared/interactive.js";

      const q = new URLSearchParams(location.search);
      const wsId = q.get("ws_id");
      const scenario = q.get("scenario") || "storm";
      const expectRows = parseInt(q.get("rows") || "4", 10);
      const agentResume = q.get("agent_resume") === "1";
      // Healed-gap sentinel, threaded from the runner (HEALED_SENTINEL)
      // so the injected turn text and this check cannot drift apart.
      const healedSentinel = q.get("healed") || "";
      // Turn-2 sentinel for stale-ref-reload (SECOND_SENTINEL), same
      // single-source discipline via the ?second= param.
      const secondSentinel = q.get("second") || "";

      // REAL pane against THIS origin (base=""): real authFetch (cookie) and
      // real EventSource. The default host provides all SSE seams.
      //
      // E7 mounts through the FACTORY instead, because the factory closure is
      // the only thing that owns destroy() — every other interactive scenario
      // drives the Pane class directly, so the controller's TERMINAL seam had
      // no browser coverage at all.  Scenario-scoped on purpose: the factory
      // owns its own connect()/recover-beat lifecycle, and switching the
      // other scenarios onto it would change what they are testing.
      let ctl = null;
      let pane;
      if (
        scenario === "destroy-invalidation" ||
        scenario === "handoff-repair-budget"
      ) {
        ctl = createInteractivePane(document.getElementById("mount"), wsId, {
          base: "",
        });
        pane = ctl.pane;
        window.__ctl = ctl;
      } else {
        pane = new InteractivePane(wsId, { base: "" });
        document.getElementById("mount").appendChild(pane.el);
      }
      pane.wsId = wsId;
      window.__pane = pane;
      let peerPane = null;
      if (
        scenario === "user-turn-two-pane" ||
        scenario === "tool-turn-two-pane"
      ) {
        // The standalone default host is a shared singleton. Give each pane a
        // shallow copy before wrapping onStreamOpen so instrumentation remains
        // per-pane instead of nesting twice on the same callback object.
        pane._host = { ...pane._host };
        peerPane = new InteractivePane(wsId, { base: "" });
        peerPane._host = { ...peerPane._host };
        peerPane.wsId = wsId;
        document.getElementById("mount").appendChild(peerPane.el);
        window.__peerPane = peerPane;
      }

      window.__hide = function () {
        Object.defineProperty(document, "hidden", { configurable: true, value: true });
        Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
        document.dispatchEvent(new Event("visibilitychange"));
      };
      window.__show = function () {
        Object.defineProperty(document, "hidden", { configurable: true, value: false });
        Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
        document.dispatchEvent(new Event("visibilitychange"));
      };

      // Latch the transient task-agent context badge while it is genuinely
      // visible. The terminal tool_result intentionally clears it, so a final
      // DOM-only assertion would miss whether the real SSE event ever rendered
      // (and handleEvent catches renderer exceptions to keep the stream alive).
      window.__agentContextProbe = {
        seen: false,
        warning: false,
        contextOnly: false,
        resumeSeen: false,
        text: "",
        aria: "",
      };
      function captureAgentContext() {
        const badge = pane.messagesEl.querySelector(
          '.conv-agent[data-state="running"] .conv-agent-context:not([hidden])',
        );
        if (!badge || !(badge.textContent || "").trim()) return;
        const card = badge.closest(".conv-agent");
        const toggle = card && card.querySelector(".conv-agent-toggle");
        const warning = badge.classList.contains("conv-agent-context--warning");
        window.__agentContextProbe.seen = true;
        if (warning) {
          window.__agentContextProbe.warning = true;
          if (agentResume) window.__agentContextProbe.resumeSeen = true;
          window.__agentContextProbe.contextOnly =
            !!card && card.dataset.contextOnly === "true";
          window.__agentContextProbe.text = (badge.textContent || "").trim();
          window.__agentContextProbe.aria =
            (toggle && toggle.getAttribute("aria-label")) || "";
        }
      }
      if (scenario === "storm") {
        const agentContextObserver = new MutationObserver(captureAgentContext);
        agentContextObserver.observe(pane.messagesEl, {
          childList: true,
          subtree: true,
          attributes: true,
          attributeFilter: ["class", "hidden", "data-state", "aria-label"],
          characterData: true,
        });

        // Runtime reducer trap for recycled provider call ids. The runner arms
        // this only for the final real task-agent turn. Delay its parent paint
        // until the real agent_context event arrives, forcing the combined
        // ordering where _toolRow still resolves the prior terminal task1 row;
        // then replay the real parent events and require the retained reading
        // to relink to the successor occurrence.
        const handleAgentEvent = pane.handleEvent.bind(pane);
        let delayedReusedParents = [];
        window.__reuseDelayArmed = false;
        window.__reuseContextBeforeParent = false;
        window.__reuseRelinked = false;
        window.__armReusedParentDelay = function () {
          delayedReusedParents = [];
          window.__reuseDelayArmed = true;
        };
        pane.handleEvent = function (evt) {
          const delayedParent =
            window.__reuseDelayArmed &&
            evt &&
            (evt.type === "tool_pending" || evt.type === "tool_info") &&
            Array.isArray(evt.items) &&
            evt.items.some(
              (item) =>
                item && item.call_id === "task1" && !item.parent_call_id,
            );
          if (delayedParent) {
            delayedReusedParents.push(evt);
            return;
          }
          const result = handleAgentEvent(evt);
          if (
            window.__reuseDelayArmed &&
            evt &&
            evt.type === "agent_context" &&
            evt.parent_call_id === "task1" &&
            delayedReusedParents.length
          ) {
            window.__reuseContextBeforeParent =
              pane._agentContexts.has("task1");
            window.__reuseDelayArmed = false;
            const delayed = delayedReusedParents;
            delayedReusedParents = [];
            delayed.forEach((parentEvt) => handleAgentEvent(parentEvt));
            const row = pane._toolRow("task1");
            window.__reuseRelinked = !!(
              row &&
              row.querySelector(
                '.conv-agent[data-state="running"] .conv-agent-context:not([hidden])',
              )
            );
          }
          return result;
        };
      }

      // Count top-level tool rows and escaped sub-agent children.
      function domCounts() {
        const topRows = pane.messagesEl.querySelectorAll(
          ".conv-batch > .conv-row[data-call-id]"
        );
        let topLevel = 0;
        let escapedChildren = 0;
        topRows.forEach((r) => {
          const cid = r.dataset.callId || "";
          if (cid.includes("::")) escapedChildren += 1;  // a child at the top level
          else topLevel += 1;
        });
        const agentCard = pane.messagesEl.querySelector(".conv-agent");
        const nested = pane.messagesEl.querySelectorAll(
          ".conv-agent .conv-row[data-call-id]"
        ).length;
        const visibleAgentCards = pane.messagesEl.querySelectorAll(
          ".conv-agent:not([hidden])"
        ).length;
        const hiddenContextCards = pane.messagesEl.querySelectorAll(
          '.conv-agent[data-context-only="true"][hidden]'
        ).length;
        return {
          topLevel,
          escapedChildren,
          agentCard: !!agentCard,
          nested,
          visibleAgentCards,
          hiddenContextCards,
        };
      }

      let sent = false;
      function sendOnce(msg) {
        if (sent) return;
        sent = true;
        // The pane's SSE is open (host.onStreamOpen fired), so the listener is
        // registered before this /send -- no missed events.
        window
          .authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/send", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: msg }),
          })
          .catch((e) => { document.title = "RECOVERY-FAILED-send-" + e; });
      }

      // Drive the production optimistic composer path for the multi-pane
      // user_turn projection check. The pane mints client_send_id itself;
      // the origin must replace that exact bubble while the peer paints the
      // canonical row from the same event.
      window.__sendProjectedUserTurn = function (msg) {
        pane.inputEl.value = msg;
        pane.sendMessage();
        return true;
      };

      // Drive /send once the stream is live (wrap the default host hook).
      const origOpen = pane._host.onStreamOpen.bind(pane._host);
      pane._host.onStreamOpen = function (p) {
        origOpen(p);
        window.__streamOpen = (window.__streamOpen || 0) + 1;
        if (scenario === "storm" && !agentResume) sendOnce("run the storm");
        else if (
          (scenario === "restart" ||
            scenario === "fail-refetch" ||
            scenario === "stale-ref-reload") &&
          window.__streamOpen === 1
        )
          sendOnce("run a turn");
        // rewind-window drives its turns SERVER-side before navigation, when
        // no page listener exists; the initial /history paints those rows, so
        // the page never auto-sends there.
      };

      if (peerPane) {
        const peerOrigOpen = peerPane._host.onStreamOpen.bind(peerPane._host);
        peerPane._host.onStreamOpen = function (p) {
          peerOrigOpen(p);
          window.__peerStreamOpen = (window.__peerStreamOpen || 0) + 1;
        };
      }

      // Shared transport instrumentation for the truncated-recovery
      // scenarios: count replay_truncated envelopes (the original restart
      // idiom) and, for stale-ref-reload, tear the transport down the
      // instant the first assistant `content` paints.
      function installStreamWrap() {
        window.__truncatedSeen = 0;
        window.__teardownDone = 0;
        const origHandle = pane.handleEvent.bind(pane);
        pane.handleEvent = function (ev) {
          if (ev && ev.type === "replay_truncated") window.__truncatedSeen += 1;
          const r = origHandle(ev);
          // stale-ref-reload: the fix's regression trap needs a NON-null
          // streaming ref surviving into an unarmed same-ws reload.  The
          // scripted provider emits `content` atomically and the segment's
          // stream_end frame (which nulls those refs) follows with no
          // pollable gap, so the teardown must RIDE the content event: once
          // it has been applied (currentAssistantEl now set), close the
          // EventSource via the REAL disconnectSSE() before stream_end can
          // dispatch — leaving exactly the stale ref a mid-content transport
          // drop would.
          if (
            scenario === "stale-ref-reload" &&
            !window.__teardownDone &&
            ev &&
            ev.type === "content"
          ) {
            window.__teardownDone = 1;
            pane.disconnectSSE();
            // Non-vacuity guard: record that the teardown genuinely left a
            // live streaming ref (the stale-ref precondition).  If the
            // segment's stream_end had already nulled it, the reload reset
            // is a no-op and a green verdict would be meaningless — so
            // __verifyStaleRef fails loudly when this is false.
            window.__staleRefWasSet = pane.currentAssistantBodyEl != null;
          }
          return r;
        };
      }

      // First paint the REAL way: /history then connect SSE.  Under the
      // factory that load is owned by connect() (which also seeds the
      // empty state), and going through it is the point of E7 — the
      // in-flight load destroy() must invalidate is the one connect()
      // starts.
      if (ctl) ctl.connect();
      else {
        pane._loadHistoryThenConnect(wsId);
        if (peerPane) peerPane._loadHistoryThenConnect(wsId);
      }

      // Count a sentinel's OCCURRENCES in the transcript text.  Every other
      // detector in this harness counts `.msg.user` rows; an assistant/tool
      // slice rendered twice leaves that count unchanged. Counting text is
      // structure-agnostic: it catches a
      // duplicate assistant bubble, a duplicate tool block, or both.
      window.__countSentinel = function (s) {
        const text = pane.messagesEl.textContent || "";
        let n = 0;
        let i = text.indexOf(s);
        while (i !== -1) {
          n += 1;
          i = text.indexOf(s, i + s.length);
        }
        return n;
      };
      // Drop and re-establish the transport in place — scenario E8's
      // reconnect-inside-the-await.  Deliberately NOT __hide()/__show():
      // a hide leaves evtSource null, which the gate's presence term
      // already decides, so the negative control would pass for the wrong
      // reason.  This lands OPEN with a bumped generation, which only the
      // generation term can see.
      window.__redial = function () {
        pane.disconnectSSE();
        pane.connectSSE(pane.wsId);
      };

      // Shared by the rewind scenarios (E2/E3): click the REAL rewind
      // button on the idx-th user row.  Depends only on `pane`.
      window.__clickRewind = function (idx) {
        const rows = pane.messagesEl.querySelectorAll(".msg.user");
        const row = rows[idx];
        if (!row) return false;
        const icon = row.querySelector(".icon-rewind");
        const btn = icon ? icon.closest("button") : null;
        if (!btn) return false;
        btn.click();
        return true;
      };

      if (scenario === "storm") {
        const deadline = Date.now() + 40000;
        const poll = () => {
          const c = domCounts();
          const idle = !pane.busy;
          captureAgentContext();
          const context = window.__agentContextProbe;
          const contextReady =
            context.seen &&
            context.warning &&
            context.contextOnly &&
            context.resumeSeen &&
            context.text === "27k / 33k" &&
            context.aria.startsWith("Show or hide sub-agent steps. ") &&
            context.aria.includes("0 steps. running. Warning: ") &&
            context.aria.endsWith(" context tokens used");
          if (
            c.topLevel >= expectRows &&
            c.agentCard &&
            c.nested >= 2 &&
            c.visibleAgentCards === 1 &&
            c.hiddenContextCards >= 1 &&
            window.__reuseContextBeforeParent &&
            window.__reuseRelinked &&
            contextReady &&
            idle
          ) {
            document.title = c.escapedChildren
              ? "RECOVERY-FAILED-escaped-" + c.escapedChildren
              : "RECOVERY-READY-STORM-" + c.topLevel + "-nested-" + c.nested +
                "-context-warning1-resume1-reuse1-empty0";
            return;
          }
          if (Date.now() > deadline) {
            document.title =
              "RECOVERY-FAILED-STORM-top" + c.topLevel + "-agent" + (c.agentCard ? 1 : 0) +
              "-nested" + c.nested + "-escaped" + c.escapedChildren +
              "-busy" + (pane.busy ? 1 : 0) +
              "-visible" + c.visibleAgentCards + "-hiddenctx" + c.hiddenContextCards +
              "-ctxseen" + (context.seen ? 1 : 0) + "-ctxwarn" + (context.warning ? 1 : 0) +
              "-ctxowner" + (context.contextOnly ? 1 : 0) +
              "-ctxresume" + (context.resumeSeen ? 1 : 0) +
              "-reusepre" + (window.__reuseContextBeforeParent ? 1 : 0) +
              "-relink" + (window.__reuseRelinked ? 1 : 0);
            return;
          }
          setTimeout(poll, 200);
        };
        setTimeout(poll, 400);
      } else if (scenario === "restart") {
        // The runner drives hide -> (restart node) -> show via window.__hide/
        // __show. We watch for the truncated-triggered rebuild + idle settle.
        installStreamWrap();
        window.__verifyRestart = function () {
          // Browser-level restart RECOVERY, full contract: the runner hid
          // the tab MID-turn (cursor frozen below the commits that land
          // while hidden), so the show-edge reconnect must present the
          // stale cursor and draw ``replay_truncated`` (REQUIRED since the
          // MessageEvent lastEventId capture fix — the pre-fix object-form
          // read left manual reconnects cursorless and this envelope
          // unreachable, which is why trunc used to report 0), the
          // truncated resync must rebuild from /history, and the turns
          // committed DURING the hide window must be present afterwards
          // (``healed`` — the 'turn disappeared' field symptom).  Composer
          // idle, status bar not stuck disconnected.
          const c = domCounts();
          const idle = !pane.busy;
          const disc = document.querySelector(".ws-sb-disconnected") !== null;
          // Sentinel must be collision-proof against everything else the
          // transcript renders: the paced bash COMMAND row paints its
          // shell text verbatim (buildConvCmd), which contains the
          // keyword ``done`` — a plain-word sentinel is vacuously
          // present whether or not the hidden-window turn survived.
          // The value rides the ?healed= param (single source:
          // HEALED_SENTINEL in the runner).
          const healed =
            healedSentinel !== "" &&
            (pane.messagesEl.textContent || "").includes(healedSentinel);
          const ok =
            c.topLevel >= 1 &&
            idle &&
            !disc &&
            healed &&
            window.__truncatedSeen >= 1;
          document.title = ok
            ? "RECOVERY-READY-RESTART-rows" + c.topLevel + "-trunc" + window.__truncatedSeen
            : "RECOVERY-FAILED-RESTART-rows" + c.topLevel +
              "-busy" + (pane.busy ? 1 : 0) + "-disc" + (disc ? 1 : 0) +
              "-healed" + (healed ? 1 : 0) + "-trunc" + window.__truncatedSeen;
        };
      } else if (scenario === "fail-refetch") {
        // Scenario D — a FAILED truncated-resync /history (the runner arms
        // node.fail_history(1) just before the show edge).  PHASE 1: the
        // failed fetch is a DOM/ref no-op (#890 guard-before-wipe) — the
        // pre-restart rows survive, no empty-state is appended, and the
        // hidden-window sentinel is NOT healed yet.  PHASE 2: the connect
        // chokepoint's retry redraws replay_truncated and the SECOND
        // /history succeeds, healing the gap.
        installStreamWrap();
        window.__failFetchStale = { rows: 0, ok: false };
        window.__verifyFailFetchStale = function () {
          const rows = pane.messagesEl.querySelectorAll(
            ".conv-row[data-call-id]",
          ).length;
          const emptyState =
            pane.messagesEl.querySelector(".empty-state") !== null;
          const healedAbsent = !(
            pane.messagesEl.textContent || ""
          ).includes(healedSentinel);
          const ok =
            rows >= 1 &&
            !emptyState &&
            healedAbsent &&
            window.__truncatedSeen >= 1;
          window.__failFetchStale = {
            rows: rows,
            emptyState: emptyState,
            healedAbsent: healedAbsent,
            ok: ok,
          };
          return window.__failFetchStale;
        };
        window.__verifyFailFetch = function () {
          const rows = pane.messagesEl.querySelectorAll(
            ".conv-row[data-call-id]",
          ).length;
          const healed = (pane.messagesEl.textContent || "").includes(
            healedSentinel,
          );
          const stale = window.__failFetchStale || { ok: false };
          const ok = stale.ok && healed && rows >= 1 && !pane.busy;
          document.title = ok
            ? "RECOVERY-READY-FAILFETCH-stale1-healed1"
            : "RECOVERY-FAILED-FAILFETCH-stale" +
              (stale.ok ? 1 : 0) +
              "-healed" +
              (healed ? 1 : 0) +
              "-rows" +
              rows +
              "-busy" +
              (pane.busy ? 1 : 0) +
              "-trunc" +
              window.__truncatedSeen;
        };
      } else if (scenario === "stale-ref-reload") {
        // Scenario E1 — regression trap for the #890 streaming-ref reset.
        // installStreamWrap's teardown hook left a stale currentAssistantEl;
        // the runner then re-auth reloads (unarmed, same-ws) and drives turn
        // 2.  Phase 1 (post reload, pre turn-2) captures the FIRST assistant
        // bubble element + its text; __verifyStaleRef proves turn 2's
        // sentinel landed in a DIFFERENT (fresh) bubble and the captured
        // bubble is byte-for-byte unchanged.  Pre-fix the unarmed reload kept
        // the stale ref and turn 2 concatenated into the old bubble.
        installStreamWrap();
        window.__stalePhase1 = { rows: 0, text: "" };
        window.__captureStaleRefPhase1 = function () {
          const first = pane.messagesEl.querySelector(".msg.assistant");
          window.__staleFirstBubble = first || null;
          const text = first ? first.textContent || "" : "";
          const rows =
            pane.messagesEl.querySelectorAll(".msg.assistant").length;
          window.__stalePhase1 = { rows: rows, text: text };
          return window.__stalePhase1;
        };
        window.__verifyStaleRef = function () {
          const bubbles = pane.messagesEl.querySelectorAll(".msg.assistant");
          let sentinelEl = null;
          bubbles.forEach(function (b) {
            if (!sentinelEl && (b.textContent || "").includes(secondSentinel))
              sentinelEl = b;
          });
          const first = window.__staleFirstBubble;
          const firstTextNow = first ? first.textContent || "" : "";
          const present = !!sentinelEl;
          const fresh = present && sentinelEl !== first;
          const unchanged = firstTextNow === (window.__stalePhase1.text || "");
          const staleSet = window.__staleRefWasSet === true;
          const ok = staleSet && present && fresh && unchanged;
          document.title = ok
            ? "RECOVERY-READY-STALEREF-fresh1"
            : "RECOVERY-FAILED-STALEREF-staleset" +
              (staleSet ? 1 : 0) +
              "-present" +
              (present ? 1 : 0) +
              "-fresh" +
              (fresh ? 1 : 0) +
              "-unchanged" +
              (unchanged ? 1 : 0);
        };
      } else if (scenario === "rewind-window") {
        // Scenario E2 — the row affordance gate (busy || _historyStale).  The
        // runner clicks a REAL rewind button (POSTs), then a SECOND one while
        // the runner-delayed clear_ui refetch holds the quiesce armed; the
        // gated click must return before POSTing.  ``posts`` is the
        // authoritative server-side rewind count the runner threads in.
        // (__clickRewind is hoisted above the scenario dispatch.)
        window.__verifyRewindWindow = function (posts) {
          const userRows =
            pane.messagesEl.querySelectorAll(".msg.user").length;
          // One rewind took effect (the 2nd-of-3 user row = rewind 2 turns =>
          // one user row left); the gated 1st-row click never reached the
          // server (posts stays 1).  A broken gate => posts 2, rows 0.
          const ok = posts === 1 && userRows === 1;
          document.title = ok
            ? "RECOVERY-READY-REWINDWIN-posts" + posts
            : "RECOVERY-FAILED-REWINDWIN-posts" + posts + "-rows" + userRows;
        };
      } else if (scenario === "rewind-failed-window") {
        // Scenario E3 — the FAILED clear_ui refetch aftermath (#890).  The
        // row affordance gate is the _historyStale LATCH, not the transient
        // _replayQueue quiesce: on a failed clear_ui refetch the quiesce
        // releases (_replayQueue -> null) but the latch SURVIVES (only a
        // successful replayHistory render clears it), so rewind/edit stay
        // gated over the stale transcript.  Pre-latch code reopened the gate
        // the moment the failed fetch released the quiesce, letting a second
        // rewind over-rewind — this scenario is that regression's trap.  Same
        // real-button click helper hoisted above the scenario dispatch; the
        // runner threads the authoritative server-side counts into the
        // verdict.
        window.__verifyRewindFail = function (posts, closedPosts, healed) {
          const userRows =
            pane.messagesEl.querySelectorAll(".msg.user").length;
          // Three legs, all runner-observed and threaded in:
          //  - closedPosts === 1: the FIRST-row rewind, clicked while the
          //    latch was set (the failed refetch already released the
          //    quiesce), was gated before POSTing — the leg that regresses to
          //    2 on the pre-latch quiesce-only gate;
          //  - healed: the bounded 2s retry re-fetched and rebuilt the
          //    rewound transcript to ONE user row;
          //  - posts === 2: the healing render cleared the latch, so a fresh
          //    rewind on the remaining row reopened the gate and landed.
          const ok = closedPosts === 1 && healed && posts === 2;
          document.title = ok
            ? "RECOVERY-READY-REWINDFAIL-posts2-heal1"
            : "RECOVERY-FAILED-REWINDFAIL-closed" +
              closedPosts +
              "-heal" +
              (healed ? 1 : 0) +
              "-posts" +
              posts +
              "-rows" +
              userRows;
        };
      } else if (scenario === "stale-backstop") {
        // Scenario E4 — the _historyStale latch's TRANSPORT-FREE idle-edge
        // backstop (#890, the round-5 critical).  A rewind's clear_ui refetch
        // AND its one bounded 2s retry both 500, so the latch cannot
        // self-heal and rewind/edit stay gated over the stale-but-real
        // transcript.  A server-origin idle state edge fires the backstop
        // without admitting another user row — a quiesced, same-token REST
        // _refetchHistory, NOT
        // _loadHistoryThenConnect (the old reload backstop drew the server's
        // synthetic state_change:idle on its fresh reconnect and re-triggered
        // itself: a zero-backoff reconnect/refetch storm).  The runner
        // observes the heal + threads the authoritative fault-layer counters
        // in; the r5 headline is sseDelta === 0 — the heal opened ZERO new
        // SSE connections.  (__clickRewind is hoisted above the dispatch.)
        window.__verifyStaleBackstop = function (
          healed,
          sseDelta,
          histDelta,
          gatedPosts,
          posts,
        ) {
          const userRows =
            pane.messagesEl.querySelectorAll(".msg.user").length;
          // heal1 = the backstop's quiesced REST refetch rebuilt the single
          //   post-rewind user row and cleared the latch.  sse0 = it touched
          //   the transport ZERO times
          //   (sseDelta 0 — the storm regression opens one EventSource per
          //   reconnect).  histDelta 1 = the backstop's single fetch.
          //   gatedPosts 1 = the row-0
          //   rewind stayed latch-gated while stale.  posts 2 = the healed
          //   render reopened the gate and a fresh rewind landed.
          const ok =
            healed &&
            sseDelta === 0 &&
            histDelta === 1 &&
            gatedPosts === 1 &&
            posts === 2;
          document.title = ok
            ? "RECOVERY-READY-STALEBACKSTOP-heal1-sse0"
            : "RECOVERY-FAILED-STALEBACKSTOP-heal" +
              (healed ? 1 : 0) +
              "-sse" +
              sseDelta +
              "-hist" +
              histDelta +
              "-gated" +
              gatedPosts +
              "-posts" +
              posts +
              "-rows" +
              userRows;
        };
      } else if (scenario === "hidden-retry") {
        // Scenario E5 — the retry's stream-liveness fire guard (#900, the
        // E-family mirror of coord's G5).  disconnectSSE deliberately
        // LEAVES the 2s retry armed (transport-only redials keep the heal
        // intent), so a close-on-hide inside the arm window lets it fire
        // with the transport down.  A seedless _refetchHistory then paints
        // /history's as-of-now truth while _lastEventId stays frozen where
        // the stream stopped delivering — and the show-edge reconnect
        // presents that frozen cursor, so the server's replay_ok slice
        // repaints every turn the hidden render already committed (content
        // and tool rows carry no id dedup).  The fire guard's
        // ``evtSource.readyState === OPEN`` term skips the hidden firing
        // instead: hiddenDelta 0 is the NON-OCCURRENCE detector, and it
        // regresses to 1 the moment the transport clause is removed.  Scope:
        // a hide nulls evtSource, so this reaches the PRESENCE term only —
        // not the readyState half, which needs a redial in progress.
        //
        // A replay_ok reconnect carries no synthetic state_change (only
        // fresh/truncated replays do), so the latch stays closed across
        // __show — the accepted liveness-lag residual, not a defect.  The
        // heal rides a server-origin idle edge through the real UI event
        // path, firing the transport-free backstop on the live stream.
        window.__verifyHiddenRetry = function (
          hiddenDelta,
          healed,
          showSse,
          posts,
        ) {
          const userRows =
            pane.messagesEl.querySelectorAll(".msg.user").length;
          // hidden0 = the retry did NOT fetch while the transport was down
          //   (the guard held — the whole point of the scenario).
          // heal1 = after __show + the idle pulse, the backstop's refetch
          //   rebuilt the single post-rewind user row.
          // showSse 1 = exactly ONE new EventSource across show + heal (the
          //   show edge's own reconnect; the transport-free heal adds none).
          // posts 2 = the healed render reopened the gate and a fresh
          //   rewind landed.
          const ok =
            hiddenDelta === 0 && healed && showSse === 1 && posts === 2;
          document.title = ok
            ? "RECOVERY-READY-HIDDENRETRY-hidden0-heal1"
            : "RECOVERY-FAILED-HIDDENRETRY-hidden" +
              hiddenDelta +
              "-heal" +
              (healed ? 1 : 0) +
              "-sse" +
              showSse +
              "-posts" +
              posts +
              "-rows" +
              userRows;
        };
      } else if (scenario === "await-window-gate") {
        // Scenario E6 — the seedless render's cursor-safety gate (#900).
        // The retry fires on a LIVE stream (its fire guard passes) and its
        // /history is held open while a close-on-hide drops the transport
        // underneath it.  Rendering that payload would commit as-of-now
        // truth with _lastEventId frozen BELOW the rows painted — E5's
        // double-render, reached through a window no fire-time check can
        // see.  The render-time gate declines and takes the failed-fetch
        // path: no wipe, quiesce released, latch intact.
        window.__verifyAwaitWindowGate = function (rows, latchHeld, healed) {
          // rows3 = the stale-but-real PRE-rewind transcript survived a
          //   resolved fetch (without the gate the post-rewind render lands
          //   and this reads 1).  latch1 = nothing cleared it — replayHistory
          //   is its only clear site, so a held latch proves no render ran.
          //   heal1 = the repair still arrives, on the organic settle the
          //   transport-free backstop owns.
          const ok = rows === 3 && latchHeld && healed;
          document.title = ok
            ? "RECOVERY-READY-AWAITGATE-rows3-latch1"
            : "RECOVERY-FAILED-AWAITGATE-rows" +
              rows +
              "-latch" +
              (latchHeld ? 1 : 0) +
              "-heal" +
              (healed ? 1 : 0);
        };
      } else if (scenario === "reconnect-in-await") {
        // Scenario E8 — the stream-generation term (#900 r2).  A transport
        // that DROPS and finishes RE-ESTABLISHING inside a seedless
        // /history await reads back readyState OPEN, indistinguishable from
        // one that never moved.  It is not: the redial re-presented the
        // frozen _lastEventId, the server answered replay_ok, and the
        // quiesce BUFFERED that slice — so a render here commits turns the
        // flush is about to repaint on top.  Only a connection counter can
        // see it; object identity cannot, because a NATIVE reconnect reuses
        // the same EventSource object.
        //
        // THE DETECTOR IS A COUNT, not a presence check.  Both the fixed and
        // the broken build end up SHOWING the turn — the difference is
        // whether it appears once (render declined, the flushed replay
        // paints it) or twice (render committed, then the flush repeats it).
        window.__verifyReconnectInAwait = function (dupes, healed) {
          // dupes is THE discriminator: the turn committed during the
          //   transport gap must appear EXACTLY once.  2 is the double
          //   render this scenario exists for.
          // healed is the CONVERGENCE leg, not a discriminator — it holds in
          //   both builds, and is asserted so a "no duplicates" verdict can
          //   never be earned by rendering nothing at all.  Unlike E6 (tab
          //   stays hidden, so the declined render's flushed settle edge
          //   finds a dead transport and the latch stays set), here the
          //   stream is LIVE at flush time: the queued settle fires the
          //   transport-free backstop, whose own fetch has a stable
          //   generation and lands.  A declined render still converges.
          const ok = dupes === 1 && healed;
          document.title = ok
            ? "RECOVERY-READY-RECONNECTAWAIT-dupes1-healed1"
            : "RECOVERY-FAILED-RECONNECTAWAIT-dupes" +
              dupes +
              "-healed" +
              (healed ? 1 : 0);
        };
      } else if (scenario === "destroy-invalidation") {
        // Scenario E7 — destroy()'s load-token bump (#900).  destroy() used
        // to bump nothing, so a /history still in flight at teardown kept
        // passing every post-await gate: its .finally reopened an
        // EventSource on the DETACHED pane and re-registered the
        // document-level visibilitychange listener destroy had just
        // removed, and that stream's onerror re-armed the host recover beat
        // forever (it gives up only on `dead`, which destroy never sets).
        // A closed tab therefore held a node connection and a 5s reconnect
        // beat for the life of the page.
        window.__verifyDestroyInvalidation = function (sseOpens) {
          // detached = destroy() removed the pane element; visNull = no
          // handler was re-registered behind it; sseOpens 0 = the held load
          // resolved WITHOUT reconnecting (the fault-layer non-occurrence
          // detector — it stamps 1 the moment the bump is removed).
          const detached = !pane.el.parentNode;
          const visNull = pane._visHandler === null;
          const ok = sseOpens === 0 && detached && visNull;
          document.title = ok
            ? "RECOVERY-READY-DESTROYINVAL-sse0-vis0"
            : "RECOVERY-FAILED-DESTROYINVAL-sse" +
              sseOpens +
              "-detached" +
              (detached ? 1 : 0) +
              "-vis" +
              (visNull ? 1 : 0);
        };
      }
    </script>
  </body>
</html>
"""

# ---------------------------------------------------------------------------
# The coordinator recovery page — served same-origin by the node at
# /coord-recovery.  A near-clone of the production standalone page
# (console/static/coordinator/index.html): the same /shared script
# substrate (classic theme.js first, then the deferred module set), the
# same createCoordinatorPane(document.body, wsId, {standalone:true}) +
# connect() bootstrap — with the coordinator files imported from
# /coord-static (see the module docstring) and Google-fonts dropped
# (hermetic run).  Scenario instrumentation reads only public chrome ids.
# ---------------------------------------------------------------------------

COORD_PAGE_HTML = r"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>coord recovery livepass</title>
    <link rel="stylesheet" href="/shared/base.css" />
    <link rel="stylesheet" href="/shared/ui-base.css" />
    <link rel="stylesheet" href="/shared/chat.css" />
    <link rel="stylesheet" href="/shared/conversation.css" />
    <link rel="stylesheet" href="/shared/mcp_error.css" />
    <link rel="stylesheet" href="/coord-static/coordinator.css" />
    <link rel="stylesheet" href="/coord-static/coord-chrome.css" />
    <style>
      body { height: 100vh; margin: 0; }
    </style>
  </head>
  <body>
    <script>
      // Transport-level instrumentation: the coordinator's handleEvent and
      // cursor are closure-private (unlike interactive's class methods), and
      // its chrome deliberately builds NO header/SSE indicator — so every
      // scenario signal is read off the wire by wrapping EventSource BEFORE
      // any module loads (classic script = runs before the deferred module
      // set, so the pane's connectSSE always constructs the wrapper):
      //   __truncatedSeen — replay_truncated frames (the envelope);
      //   __esOpens      — stream opens (drives the send; a listener is
      //                    registered before /send so no events are missed);
      //   __idFrames     — id-bearing frames, i.e. exactly the frames that
      //                    advance the pane's reconnect cursor (same
      //                    ``!= null && !== ""`` guard as the pane) — the
      //                    hide fires only after this proves a live mid-turn
      //                    cursor.
      window.__truncatedSeen = 0;
      window.__esOpens = 0;
      window.__idFrames = 0;
      (function () {
        const RealES = window.EventSource;
        function CountingES(url, opts) {
          window.__lastEventSourceUrl = String(url);
          const es = new RealES(url, opts);
          es.addEventListener("open", function () {
            window.__esOpens += 1;
          });
          es.addEventListener("message", function (e) {
            if (e.lastEventId != null && e.lastEventId !== "") {
              window.__idFrames += 1;
            }
            try {
              const d = JSON.parse(e.data);
              if (d && d.type === "replay_truncated") window.__truncatedSeen += 1;
            } catch (_) {}
          });
          return es;
        }
        CountingES.prototype = RealES.prototype;
        CountingES.CONNECTING = RealES.CONNECTING;
        CountingES.OPEN = RealES.OPEN;
        CountingES.CLOSED = RealES.CLOSED;
        window.EventSource = CountingES;
      })();
    </script>
    <script src="/shared/theme.js"></script>
    <script type="module" src="/shared/utils.js"></script>
    <script type="module" src="/shared/toast.js"></script>
    <script type="module" src="/shared/auth.js"></script>
    <script type="module" src="/shared/kb.js"></script>
    <script type="module" src="/shared/composer.js"></script>
    <script type="module" src="/shared/composer_attachments.js"></script>
    <script type="module" src="/shared/composer_queue.js"></script>
    <script type="module" src="/shared/status_bar.js"></script>
    <script type="module" src="/shared/renderer.js"></script>
    <script type="module">
      import { createCoordinatorPane } from "/coord-static/coordinator.js";

      const q = new URLSearchParams(location.search);
      const wsId = q.get("ws_id");
      const healedSentinel = q.get("healed") || "";
      const scenario = q.get("scenario") || "coord-restart";

      const pane = createCoordinatorPane(document.body, wsId, {
        standalone: true,
      });
      window.__pane = pane;
      if (pane) pane.connect();

      window.__hide = function () {
        Object.defineProperty(document, "hidden", { configurable: true, value: true });
        Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
        document.dispatchEvent(new Event("visibilitychange"));
      };
      window.__show = function () {
        Object.defineProperty(document, "hidden", { configurable: true, value: false });
        Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
        document.dispatchEvent(new Event("visibilitychange"));
      };

      // Drive /send once the stream has OPENED at the transport (__esOpens —
      // the pane's listener is registered by then, so no events are missed).
      // The chrome has no SSE pill to poll: the header was deliberately
      // dropped (see buildCoordChrome's comment).  coord-restart only: the
      // #894 rewind scenarios (G1-G3) seed their turns server-side before
      // navigation and must not inject an extra one.
      let sent = false;
      function sendOnce(msg) {
        if (sent) return;
        sent = true;
        window
          .authFetch("/v1/api/workstreams/" + encodeURIComponent(wsId) + "/send", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: msg }),
          })
          .catch((e) => { document.title = "RECOVERY-FAILED-COORD-send-" + e; });
      }
      if (scenario === "coord-restart") {
        const sendPoll = setInterval(() => {
          if (window.__esOpens >= 1) {
            clearInterval(sendPoll);
            sendOnce("run a turn");
          }
        }, 100);
      }

      // Shared by the #894 rewind scenarios (G1/G2/G3): click the REAL
      // rewind button on the idx-th user row.  The coordinator pane object
      // exposes NO messagesEl and NO latch/quiesce fields (closure-private
      // state, unlike interactive's class) — every probe on this page reads
      // the public #coord-messages container, and the runner threads the
      // authoritative fault-layer counters into the verdicts.
      window.__clickCoordRewind = function (idx) {
        const rows = document
          .getElementById("coord-messages")
          .querySelectorAll(".msg.user");
        const row = rows[idx];
        if (!row) return false;
        const icon = row.querySelector(".icon-rewind");
        const btn = icon ? icon.closest("button") : null;
        if (!btn) return false;
        btn.click();
        return true;
      };
      function _coordUserRows() {
        return document
          .getElementById("coord-messages")
          .querySelectorAll(".msg.user").length;
      }

      // G1 — the row affordance gate (busy || historyStale) under a
      // HELD-OPEN clear_ui refetch.  Mirrors __verifyRewindWindow; coord has
      // no quiesce, so the runner's in-flight edge is the fault layer's
      // history_requests bump (counted on arrival, before the hold).
      window.__verifyCoordRewindWindow = function (posts) {
        const userRows = _coordUserRows();
        // One rewind took effect (2nd-of-3 user row = rewind 2 turns => one
        // user row left); the gated 1st-row click never reached the server
        // (posts stays 1).  A broken gate => posts 2, rows 0.
        const ok = posts === 1 && userRows === 1;
        document.title = ok
          ? "RECOVERY-READY-COORDREWINDWIN-posts" + posts
          : "RECOVERY-FAILED-COORDREWINDWIN-posts" + posts + "-rows" + userRows;
      };

      // G2 — the FAILED clear_ui refetch aftermath.  Mirrors
      // __verifyRewindFail: the latch (not a refetch-in-flight flag) must
      // hold the gate closed AFTER the failed fetch, then the bounded 2s
      // retry heals and reopens.  Pre-latch coord regresses to closed2.
      window.__verifyCoordRewindFail = function (posts, closedPosts, healed) {
        const userRows = _coordUserRows();
        const ok = closedPosts === 1 && healed && posts === 2;
        document.title = ok
          ? "RECOVERY-READY-COORDREWINDFAIL-posts2-heal1"
          : "RECOVERY-FAILED-COORDREWINDFAIL-closed" +
            closedPosts +
            "-heal" +
            (healed ? 1 : 0) +
            "-posts" +
            posts +
            "-rows" +
            userRows;
      };

      // G3 — the TRANSPORT-FREE idle-edge backstop.  Mirrors
      // __verifyStaleBackstop; the storm assertion is sseDelta === 0 (the
      // heal opened ZERO EventSource connections — a reconnecting backstop
      // bumps events_requests once per reconnect and self-triggers).  The
      // latch-cleared proof is the reopen POST (posts 2), not a field read:
      // coord's latch is closure-private.
      window.__verifyCoordStaleBackstop = function (
        healed,
        sseDelta,
        histDelta,
        gatedPosts,
        posts,
      ) {
        const userRows = _coordUserRows();
        const ok =
          healed &&
          sseDelta === 0 &&
          histDelta === 1 &&
          gatedPosts === 1 &&
          posts === 2;
        document.title = ok
          ? "RECOVERY-READY-COORDSTALEBACKSTOP-heal1-sse0"
          : "RECOVERY-FAILED-COORDSTALEBACKSTOP-heal" +
            (healed ? 1 : 0) +
            "-sse" +
            sseDelta +
            "-hist" +
            histDelta +
            "-gated" +
            gatedPosts +
            "-posts" +
            posts +
            "-rows" +
            userRows;
      };

      // G4 — the render-time tool gate (#894 r4): a live tool_pending phase
      // starts during a held seedless /history.  The good response must be
      // declined, leaving both the stale transcript and live tool shell
      // continuously visible.  midturnSurvived carries that observation.
      window.__verifyCoordHealMidturn = function (
        healed,
        midturnSurvived,
        sseDelta,
        histDelta,
        posts,
      ) {
        const userRows = _coordUserRows();
        const ok =
          healed &&
          midturnSurvived &&
          sseDelta === 0 &&
          histDelta === 2 &&
          posts === 2;
        document.title = ok
          ? "RECOVERY-READY-COORDHEALMIDTURN-heal1-mid1-sse0"
          : "RECOVERY-FAILED-COORDHEALMIDTURN-heal" +
            (healed ? 1 : 0) +
            "-mid" +
            (midturnSurvived ? 1 : 0) +
            "-sse" +
            sseDelta +
            "-hist" +
            histDelta +
            "-posts" +
            posts +
            "-rows" +
            userRows;
      };

      // G5 — the retry's stream-liveness fire guard (#894 r4): a retry
      // armed before a close-on-hide must NOT fetch while the transport
      // is down (hiddenDelta 0 — a seedless render past the frozen
      // cursor double-renders on the show-edge replay); the show edge
      // only restores the transport (replay_ok carries no synthetic
      // state_change), and the heal rides a server-origin idle edge through
      // the real UI event path (one user-driven SSE open, rows healed, gate
      // reopened).
      window.__verifyCoordHiddenRetry = function (
        hiddenDelta,
        healed,
        showSse,
        posts,
      ) {
        const userRows = _coordUserRows();
        const ok = hiddenDelta === 0 && healed && showSse === 1 && posts === 2;
        document.title = ok
          ? "RECOVERY-READY-COORDHIDDENRETRY-hidden0-heal1"
          : "RECOVERY-FAILED-COORDHIDDENRETRY-hidden" +
            hiddenDelta +
            "-heal" +
            (healed ? 1 : 0) +
            "-sse" +
            showSse +
            "-posts" +
            posts +
            "-rows" +
            userRows;
      };

      // G6 — the poisoned-pane detector (#894 r6/r7): a HARD node kill
      // leaves the tool call genuinely unresulted (only a graceful
      // close synthesizes a cancel result), so recovery paints a
      // --running orphan no result ever strips.  The event-driven
      // live-call set is empty for it, so the seedless rewind flow
      // must work THROUGH the residue: orphan wiped, rewound truth
      // painted (the user message stays — rewind-for-retry), posts 1.
      // A DOM-probed gate reads the residue as live and never renders.
      window.__verifyCoordOrphanRewind = function (posts, histDelta) {
        const userRows = _coordUserRows();
        const orphan =
          document
            .getElementById("coord-messages")
            .querySelector(".conv-batch--running") !== null;
        // Rewound truth: the server removes the UNRESULTED assistant turn
        // but keeps the user message (rewind-for-retry), so success is
        // ONE user row with the residue gone.  The failure mode (a gate
        // that reads the residue as live and skips) is rows1 WITH
        // orphan1 and no render — the orphan bit discriminates.
        const ok = posts === 1 && histDelta >= 1 && userRows === 1 && !orphan;
        document.title = ok
          ? "RECOVERY-READY-COORDORPHANREWIND-posts1-rows1-orphan0"
          : "RECOVERY-FAILED-COORDORPHANREWIND-posts" +
            posts +
            "-hist" +
            histDelta +
            "-rows" +
            userRows +
            "-orphan" +
            (orphan ? 1 : 0);
      };

      // G7 — the joined-flight window (#894 r8): a clear_ui refetch
      // dispatched after a rewind must MISS any in-flight pre-rewind
      // /history reconstruction (the server folds the truncation
      // generation into its #884 flight key).  Pre-fix, the join handed
      // A a pre-rewind payload that painted as fresh truth and cleared
      // the latch — histDelta 1 (joined, one reconstruction) and three
      // stale rows; fixed, A draws its own flight — histDelta 2, the
      // rewound single row.
      window.__verifyCoordJoinedFlight = function (posts, loadDelta) {
        const userRows = _coordUserRows();
        // loadDelta === 2: A's post-rewind dispatch entered load_messages
        // itself (a joined request never does — the flight-layer miss
        // proof).  userRows === 1: the post-rewind truth painted; the
        // joined pre-rewind payload paints three.
        const ok = posts === 1 && loadDelta === 2 && userRows === 1;
        document.title = ok
          ? "RECOVERY-READY-COORDJOINEDFLIGHT-posts1-loads2-rows1"
          : "RECOVERY-FAILED-COORDJOINEDFLIGHT-posts" +
            posts +
            "-loads" +
            loadDelta +
            "-rows" +
            userRows;
      };

      window.__verifyCoordRestart = function () {
        // Same contract as Scenario B, read off the coordinator's public
        // chrome + the transport wrapper (idle is asserted SERVER-side by
        // the runner — the chrome has no state text element): the show-edge
        // reconnect must present the frozen mid-turn cursor and draw
        // replay_truncated (trunc>=1), the dead-stream resync must rebuild
        // from /history with the hidden-window turns present (healed), the
        // stream must have re-opened after the show (__esOpens >= 2), the
        // status bar must not be stuck dim (.ws-sb-disconnected removed by
        // the post-recovery onopen), and the tool rows must be intact.
        const messages = document.getElementById("coord-messages");
        const rows = messages
          ? messages.querySelectorAll(".conv-row[data-call-id]").length
          : 0;
        const reopened = window.__esOpens >= 2;
        const disc =
          document.querySelector("#coord-status-bar.ws-sb-disconnected") !== null;
        const healed =
          healedSentinel !== "" &&
          ((messages && messages.textContent) || "").includes(healedSentinel);
        const ok =
          rows >= 1 && reopened && !disc && healed && window.__truncatedSeen >= 1;
        document.title = ok
          ? "RECOVERY-READY-COORD-rows" + rows + "-trunc" + window.__truncatedSeen
          : "RECOVERY-FAILED-COORD-rows" + rows +
            "-reopened" + (reopened ? 1 : 0) +
            "-disc" + (disc ? 1 : 0) + "-healed" + (healed ? 1 : 0) +
            "-trunc" + window.__truncatedSeen;
      };
    </script>
  </body>
</html>
"""

# ---------------------------------------------------------------------------
# Minimal dependency-free CDP client (WebSocket over a raw socket).
# ---------------------------------------------------------------------------


class CDP:
    """Just enough Chrome DevTools Protocol: navigate, evaluate, set cookie."""

    def __init__(self, ws_url: str) -> None:
        from urllib.parse import urlsplit

        u = urlsplit(ws_url)
        self._sock = socket.create_connection((u.hostname, u.port or 80), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        handshake = (
            f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(handshake.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self._sock.recv(4096)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"CDP websocket handshake failed: {resp[:80]!r}")
        self._id = 0
        self._rbuf = b""

    def _send(self, payload: bytes) -> None:
        header = bytearray([0x81])  # FIN + text opcode
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self._sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _recv_exact(self, n: int) -> bytes:
        while len(self._rbuf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("CDP socket closed")
            self._rbuf += chunk
        out, self._rbuf = self._rbuf[:n], self._rbuf[n:]
        return out

    def _recv_message(self) -> str:
        data = b""
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            data += self._recv_exact(length)
            if fin:
                return data.decode("utf-8", "replace")

    def cmd(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15) -> Any:
        self._id += 1
        mid = self._id
        self._send(json.dumps({"id": mid, "method": method, "params": params or {}}).encode())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._sock.settimeout(max(0.1, deadline - time.monotonic()))
            obj = json.loads(self._recv_message())
            if obj.get("id") == mid:
                if "error" in obj:
                    raise RuntimeError(f"{method}: {obj['error']}")
                return obj.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expression: str) -> Any:
        r = self.cmd(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return r.get("result", {}).get("value")

    def title(self) -> str:
        return str(self.evaluate("document.title") or "")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._sock.close()


# ---------------------------------------------------------------------------
# Chrome launch + node boot
# ---------------------------------------------------------------------------


def _find_chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _launch_chrome(chrome: str, profile: Path) -> tuple[subprocess.Popen[bytes], int]:
    cdp_port = _free_port()
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--password-store=basic",
            "--disable-extensions",
            "--disable-background-timer-throttling",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, cdp_port


def _page_ws_url(cdp_port: int, timeout: float = 15) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json", timeout=2) as r:
                targets = json.loads(r.read())
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return str(t["webSocketDebuggerUrl"])
        except Exception:
            pass
        time.sleep(0.2)
    raise TimeoutError("no CDP page target")


def _page_route() -> Any:
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    async def recovery_page(_request: Any) -> HTMLResponse:
        return HTMLResponse(PAGE_HTML)

    return Route("/recovery", recovery_page)


def _coord_routes() -> list[Any]:
    """The coordinator scenario's same-origin extras: the pane page, and the
    console's coordinator static tree under the ``/coord-static`` prefix —
    a DISTINCT prefix because the node's own ``/static`` mount (ui/static)
    matches first and would 404 the console path from inside its own tree.
    coordinator.js's module imports are all absolute ``/shared/*``, which
    the node already serves."""
    from starlette.responses import HTMLResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    import turnstone

    coord_dir = Path(turnstone.__file__).resolve().parent / "console" / "static" / "coordinator"

    async def coord_recovery_page(_request: Any) -> HTMLResponse:
        return HTMLResponse(COORD_PAGE_HTML)

    return [
        Route("/coord-recovery", coord_recovery_page),
        Mount("/coord-static", app=StaticFiles(directory=str(coord_dir)), name="coord-static"),
    ]


def _boot_node(port: int = 0, sock: Any = None) -> Any:
    from tests._sse_recovery_server import RecoveryServer
    from turnstone.core.storage import init_storage, reset_storage

    # The page route must bypass auth on first load (the cookie is set by the
    # runner via CDP BEFORE navigation), so make it public by prefixing under
    # a public path is unavailable here; instead the runner sets the cookie so
    # /recovery passes the middleware. init storage per boot (shared singleton).
    # ``sock``: a pre-bound placeholder for the gap-free restart handoff
    # (see RecoveryServer's ``sock`` parameter).
    reset_storage()
    init_storage("sqlite", path=os.path.join(_scratch(), "recovery_e2e.db"), run_migrations=True)
    return RecoveryServer(extra_routes=[_page_route(), *_coord_routes()], port=port, sock=sock)


def _scratch() -> str:
    d = os.environ.get("RECOVERY_E2E_TMP") or "/tmp/recovery_e2e"
    os.makedirs(d, exist_ok=True)
    return d


def _set_cookie_and_navigate(cdp: CDP, base_url: str, token: str, page_url: str) -> None:
    cdp.cmd("Page.enable")
    cdp.cmd("Runtime.enable")
    cdp.cmd("Network.enable")
    cdp.cmd(
        "Network.setCookie",
        {
            "name": "turnstone_auth_server",
            "value": token,
            "url": base_url,
            "path": "/",
        },
    )
    cdp.cmd("Page.navigate", {"url": page_url})


def _poll_title(cdp: CDP, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = cdp.title()
        if last.startswith("RECOVERY-"):
            return last
        time.sleep(0.3)
    return last or "RECOVERY-FAILED-timeout"


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def _storm_scripts() -> tuple[Any, ...]:
    """A bash storm plus task agents with and without nested sub-tools.

    Both agents report warning-level prompt usage. The nested agent therefore
    cooperatively compacts and resumes before finishing. The browser proves
    live and refresh-restored context badges, accessibility, nested routing,
    recycled-id relinking, and context-only terminal cleanup.
    """
    from tests._sse_recovery_server import final_text_script, parallel_bash_script

    storm = parallel_bash_script({f"call_{i}": "seq 1 500" for i in range(4)})
    task = dict(
        tool_calls=[
            {
                "id": "task1",
                "name": "task_agent",
                "arguments": json.dumps({"prompt": "sub tools"}),
            }
        ],
        finish_reason="tool_calls",
    )
    sub = dict(
        tool_calls=[
            {
                "id": "s_a",
                "name": "bash",
                "arguments": json.dumps(
                    {"command": (": a; for i in $(seq 1 200); do echo a-$i; sleep 0.05; done")}
                ),
            },
            {"id": "s_b", "name": "bash", "arguments": json.dumps({"command": ": b; seq 1 200"})},
        ],
        finish_reason="tool_calls",
        prompt_tokens=27_000,
    )
    no_step_task = dict(
        tool_calls=[
            {
                # Deliberately recycle task1 across turns. The browser delays
                # this successor's parent paint until agent_context arrives.
                "id": "task1",
                "name": "task_agent",
                "arguments": json.dumps({"prompt": "answer directly"}),
            }
        ],
        finish_reason="tool_calls",
    )
    # Turn 1: the 4-bash storm; turn 2: a task_agent with 2 chatty sub-bashes;
    # turn 3: a one-model-turn task_agent with no sub-tool rows.
    return (
        storm,
        final_text_script("storm done"),
        task,
        sub,
        {**final_text_script("sub done"), "prompt_tokens": 27_000},
        # The warning-level no-tool response above cooperatively winds down.
        # Supply the private summary call and the resumed agent completion
        # before the parent harness continues.
        final_text_script("task compaction summary"),
        final_text_script("sub done after compaction"),
        final_text_script("all done"),
        no_step_task,
        {**final_text_script("direct answer"), "prompt_tokens": 27_000},
        final_text_script("all done again"),
    )


def run_storm(chrome: str) -> str:
    node = _boot_node()
    ws_id = node.create_workstream(*_storm_scripts(), name="browser-storm")
    profile = Path(_scratch()) / "chrome-storm"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        # The page POSTs the storm turn on stream-open. Follow it with a paced
        # nested agent, reload while its first context reading is the only one
        # available, then run a no-step successor that recycles the call id.
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=storm&rows=4"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        node.wait_turn(ws_id, timeout=40)
        node.send(ws_id, "spawn the sub agent")
        if not _poll_until(
            lambda: cdp.evaluate(
                "!!(window.__agentContextProbe && window.__agentContextProbe.warning)"
            ),
            15,
            0.05,
        ):
            return "RECOVERY-FAILED-STORM-context-live0"

        # A brand-new Pane must repaint from the server's active-context
        # snapshot. The paced first sub-tool keeps the agent between model
        # calls while the navigation completes; the call-count assertion below
        # proves the observed badge cannot be the next live model reading.
        resume_url = url + "&agent_resume=1"
        cdp.cmd("Page.navigate", {"url": resume_url})
        if not _poll_until(
            lambda: cdp.evaluate(
                "!!(window.__agentContextProbe && window.__agentContextProbe.resumeSeen)"
            ),
            8,
            0.05,
        ):
            return "RECOVERY-FAILED-STORM-context-resume0"
        calls_at_resume = node.model_call_count(ws_id)
        if calls_at_resume != 4:
            return f"RECOVERY-FAILED-STORM-context-resume-late-calls{calls_at_resume}"

        node.wait_turn(ws_id, timeout=40)
        calls_after_nested = node.model_call_count(ws_id)
        if calls_after_nested != 8:
            return f"RECOVERY-FAILED-STORM-context-compaction-calls{calls_after_nested}"
        if not _poll_until(
            lambda: cdp.evaluate("!!window.__pane && !window.__pane.busy"),
            5,
            0.05,
        ):
            return "RECOVERY-FAILED-STORM-nested-busy1"
        cdp.evaluate("window.__armReusedParentDelay()")
        node.send(ws_id, "spawn the direct sub agent")
        return _poll_title(cdp, 45)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_restart(chrome: str) -> str:
    from tests._sse_recovery_server import final_text_script, parallel_bash_script

    port = _free_port()
    node = _boot_node(port=port)
    # A PACED turn so the tab can hide MID-turn: the browser cursor
    # freezes at a mid-stream event id, the rest of turn 1 plus the
    # turn-2 text commit while hidden, and the restarted node's seeded
    # counter therefore sits ABOVE the frozen cursor -> the show-edge
    # reconnect draws ``replay_truncated`` and must heal the gap.
    paced = parallel_bash_script({"r0": "for i in $(seq 1 40); do echo r-$i; sleep 0.05; done"})
    # The turn-2 text is the healed-gap sentinel — it must be a token
    # that cannot appear in any rendered command/output (the bash
    # command row contains the shell keyword ``done``, so the obvious
    # word is vacuously present; see __verifyRestart).  Single source:
    # the same constant is injected as the scripted turn text AND
    # threaded to the page via ?healed=, so the two sides cannot drift.
    ws_id = node.create_workstream(
        paced, final_text_script(HEALED_SENTINEL), name="browser-restart"
    )
    profile = Path(_scratch()) / "chrome-restart"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=restart&healed={HEALED_SENTINEL}"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # Hide as soon as the FIRST streamed line has painted (proof the
        # pane holds a live mid-turn cursor) — NOT after wait_turn, which
        # would leave the cursor at/above the committed counter and the
        # reconnect on the lossless replay_ok path (trunc0).
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            painted = cdp.evaluate("document.querySelector('.tool-output-stream') !== null")
            if painted:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("restart scenario: first streamed line never painted")
        cdp.evaluate("window.__hide && window.__hide()")
        # The turn (and the follow-up text) commits while the tab is hidden.
        node.wait_turn(ws_id, timeout=30)
        # Restart the node on the SAME port (fresh empty ring, seeded counter).
        node.stop()
        node = _boot_node(port=port)
        node.open_workstream(ws_id)
        # Show the tab -> stale-cursor reconnect -> truncated -> jittered
        # resync (0-10s) -> /history rebuild.  Settle past the worst-case
        # jitter before the verdict.
        cdp.evaluate("window.__show && window.__show()")
        time.sleep(12.0)
        cdp.evaluate("window.__verifyRestart && window.__verifyRestart()")
        return _poll_title(cdp, 20)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


# Injected into the REAL dashboard page via Page.addScriptToEvaluateOnNewDocument
# (runs before any page script on every navigation).  Same transport-level
# discipline as the coord page's inline wrapper, but scoped to GLOBAL-stream
# URLs only — the dashboard also opens per-ws EventSources whose frames and
# truncated envelopes must not pollute the roster counters.  ``__globalES``
# keeps the live instance reachable so the runner can force the CLOSED state
# (app.js's module-private ``globalEvtSource`` is otherwise untouchable);
# ``new CountingES(...)`` returns the REAL EventSource, so app.js's
# ``readyState`` checks and handlers see the genuine object.
ROSTER_INJECT_JS = r"""
window.__globalTruncated = 0;
window.__globalIdFrames = 0;
window.__globalCursorPresented = 0;
window.__globalOpens = 0;
window.__globalES = null;
window.__hide = function () {
  Object.defineProperty(document, "hidden", { configurable: true, value: true });
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
  document.dispatchEvent(new Event("visibilitychange"));
};
window.__show = function () {
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  document.dispatchEvent(new Event("visibilitychange"));
};
(function () {
  const RealES = window.EventSource;
  function CountingES(url, opts) {
    const es = new RealES(url, opts);
    if (String(url).indexOf("/events/global") !== -1) {
      window.__globalES = es;
      window.__globalOpens += 1;
      if (String(url).indexOf("last_event_id=") !== -1) {
        window.__globalCursorPresented += 1;
      }
      es.addEventListener("message", function (e) {
        if (e.lastEventId != null && e.lastEventId !== "") {
          window.__globalIdFrames += 1;
        }
        try {
          const d = JSON.parse(e.data);
          if (d && d.type === "replay_truncated") window.__globalTruncated += 1;
        } catch (_) {}
      });
    }
    return es;
  }
  CountingES.prototype = RealES.prototype;
  CountingES.CONNECTING = RealES.CONNECTING;
  CountingES.OPEN = RealES.OPEN;
  CountingES.CLOSED = RealES.CLOSED;
  window.EventSource = CountingES;
})();
"""


def _roster_has_ws(cdp: CDP, ws_id: str, name: str) -> bool:
    """The ws is in the healed roster: present in the MODEL
    (``TS_APP.getClusterState()`` — the projection of app.js's
    ``workstreams`` map, the state the snapshot evict mutates) AND
    rendered in the RAIL (``fireRender``'s surface; rows are name-keyed
    via ``title``).  Deliberately NOT the dashboard TABLE
    (``#dash-ws-table``): its MEMBERSHIP refreshes only on interaction
    (``loadDashboard`` on show/boot/delete) for live ws_created/ws_closed
    too — only existing rows live-patch (see app.js
    ``updateTabIndicator``) — so a lingering dash row is the table's
    documented refresh model, not a failed heal."""
    in_model = bool(
        cdp.evaluate(
            "(window.TS_APP && window.TS_APP.getClusterState) ? "
            "window.TS_APP.getClusterState().nodes.local.workstreams.some("
            f"function (w) {{ return w.id === {json.dumps(ws_id)}; }}) : false"
        )
    )
    in_rail = bool(
        cdp.evaluate(
            "Array.prototype.some.call(document.querySelectorAll('button.row'), "
            f"function (b) {{ return (b.title || '').indexOf({json.dumps(name)}) === 0; }})"
        )
    )
    return in_model and in_rail


def _roster_absent_ws(cdp: CDP, ws_id: str, name: str) -> bool:
    """The ws is fully evicted: absent from BOTH the model and the rail.

    NOT ``not _roster_has_ws(...)`` — that De Morgans into
    "absent from EITHER surface", which would stamp a heal HEALED while
    a model-evicted ghost's row still lingers in the rail DOM (the exact
    render-propagation regression the roster scenarios exist to catch;
    round-4 review)."""
    in_model = bool(
        cdp.evaluate(
            "(window.TS_APP && window.TS_APP.getClusterState) ? "
            "window.TS_APP.getClusterState().nodes.local.workstreams.some("
            f"function (w) {{ return w.id === {json.dumps(ws_id)}; }}) : true"
        )
    )
    in_rail = bool(
        cdp.evaluate(
            "Array.prototype.some.call(document.querySelectorAll('button.row'), "
            f"function (b) {{ return (b.title || '').indexOf({json.dumps(name)}) === 0; }})"
        )
    )
    return not in_model and not in_rail


def run_roster_restart(chrome: str) -> str:
    from tests._sse_recovery_server import bash_toolcall_script, final_text_script

    port = _free_port()
    node = _boot_node(port=port)
    # keeper FIRST (its pending scripted client is never consumed — no send
    # targets it), then ghost so the ghost's turn script is the pending one
    # its /send consumes.
    keeper_id = node.create_workstream(final_text_script("keeper idle"), name="roster-keeper")
    ghost_id = node.create_workstream(
        bash_toolcall_script("g1", "echo ghost-activity"),
        final_text_script("ghost done"),
        name="roster-ghost",
    )
    profile = Path(_scratch()) / "chrome-roster"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        cdp.cmd("Page.enable")
        cdp.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": ROSTER_INJECT_JS})
        _set_cookie_and_navigate(cdp, node.base_url, node.token, node.base_url + "/")

        # -- Phase A: live roster + live cursor, zero false truncated -------
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _roster_has_ws(cdp, ghost_id, "roster-ghost") and _roster_has_ws(
                cdp, keeper_id, "roster-keeper"
            ):
                break
            time.sleep(0.3)
        else:
            return "RECOVERY-FAILED-ROSTER-rows-never-rendered"
        # A scripted turn on the ghost drives ws_state frames down the global
        # stream — the id-bearing frames that make the page's stored cursor
        # (and the wrapper's count) real.
        node.send(ghost_id)
        node.wait_turn(ghost_id, timeout=30)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            frames = cdp.evaluate("window.__globalIdFrames || 0")
            if isinstance(frames, int) and frames >= 1:
                break
            time.sleep(0.3)
        else:
            return "RECOVERY-FAILED-ROSTER-no-global-id-frames"
        # Negative control: a LIVE cursor over a live ring must not draw the
        # envelope, and no manual reconnect has happened yet.
        if cdp.evaluate("window.__globalTruncated || 0") != 0:
            return "RECOVERY-FAILED-ROSTER-truncated-false-positive"
        if cdp.evaluate("window.__globalCursorPresented || 0") != 0:
            return "RECOVERY-FAILED-ROSTER-premature-cursor-presentation"

        # -- Phase B: deaf browser -> restart -> manual stale-cursor heal ---
        cdp.evaluate("window.__hide && window.__hide()")
        # Force the browser-gave-up state: a CLOSED source never auto-retries,
        # so the show edge's manual ``connectGlobalSSE`` is the ONLY reconnect
        # and the ``?last_event_id=`` presentation is deterministic (a native
        # retry landing first would consume the staleness and clear the
        # stored cursor — the native transport is Tier-1-pinned instead).
        cdp.evaluate("window.__globalES && window.__globalES.close()")
        node.stop()
        node = _boot_node(port=port)
        # Only the keeper comes back — the ghost stays unloaded, exactly a
        # restarted node's roster truth the pre-#881 silent gap never showed.
        node.open_workstream(keeper_id)
        cdp.evaluate("window.__show && window.__show()")

        deadline = time.monotonic() + 15
        healed = False
        while time.monotonic() < deadline:
            trunc = cdp.evaluate("window.__globalTruncated || 0")
            cursor = cdp.evaluate("window.__globalCursorPresented || 0")
            ghost_gone = _roster_absent_ws(cdp, ghost_id, "roster-ghost")
            keeper_here = _roster_has_ws(cdp, keeper_id, "roster-keeper")
            if (
                isinstance(trunc, int)
                and trunc >= 1
                and isinstance(cursor, int)
                and cursor >= 1
                and ghost_gone
                and keeper_here
            ):
                healed = True
                break
            time.sleep(0.3)
        if not healed:
            trunc = cdp.evaluate("window.__globalTruncated || 0")
            cursor = cdp.evaluate("window.__globalCursorPresented || 0")
            return (
                f"RECOVERY-FAILED-ROSTER-trunc{trunc}-cursor{cursor}"
                f"-ghost{'0' if _roster_absent_ws(cdp, ghost_id, 'roster-ghost') else '1'}"
                f"-keeper{'1' if _roster_has_ws(cdp, keeper_id, 'roster-keeper') else '0'}"
            )
        # Backend proof: the reconnect hit the restarted node's REAL global
        # endpoint (the counter lives on the reborn server, so >=1 can only
        # come from a post-restart connection).
        if node.global_events_requests < 1:
            return "RECOVERY-FAILED-ROSTER-no-backend-global-connect"
        verdict = f"RECOVERY-READY-ROSTER-trunc{trunc}-cursor{cursor}"
        # Stamp the title too so the manual runbook flow reads the same way.
        cdp.evaluate(f"document.title = {json.dumps(verdict)}")
        return verdict
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_roster_restart_native(chrome: str) -> str:
    from tests._sse_recovery_server import bash_toolcall_script, final_text_script

    port = _free_port()
    node = _boot_node(port=port)
    keeper_id = node.create_workstream(final_text_script("keeper idle"), name="roster-keeper")
    ghost_id = node.create_workstream(
        bash_toolcall_script("g1", "echo ghost-activity"),
        final_text_script("ghost done"),
        name="roster-ghost",
    )
    profile = Path(_scratch()) / "chrome-roster-native"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        cdp.cmd("Page.enable")
        cdp.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": ROSTER_INJECT_JS})
        _set_cookie_and_navigate(cdp, node.base_url, node.token, node.base_url + "/")

        # -- Phase A: identical to F1 ---------------------------------------
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _roster_has_ws(cdp, ghost_id, "roster-ghost") and _roster_has_ws(
                cdp, keeper_id, "roster-keeper"
            ):
                break
            time.sleep(0.3)
        else:
            return "RECOVERY-FAILED-ROSTERNATIVE-rows-never-rendered"
        node.send(ghost_id)
        node.wait_turn(ghost_id, timeout=30)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            frames = cdp.evaluate("window.__globalIdFrames || 0")
            if isinstance(frames, int) and frames >= 1:
                break
            time.sleep(0.3)
        else:
            return "RECOVERY-FAILED-ROSTERNATIVE-no-global-id-frames"
        if cdp.evaluate("window.__globalTruncated || 0") != 0:
            return "RECOVERY-FAILED-ROSTERNATIVE-truncated-false-positive"
        if cdp.evaluate("window.__globalCursorPresented || 0") != 0:
            return "RECOVERY-FAILED-ROSTERNATIVE-premature-cursor-presentation"

        # -- Phase B: NATIVE heal — visible tab, EventSource left open ------
        # The browser's own retry (the server's 2.5-4.5s ``retry:``
        # directive) carries the persisted pre-restart id as the
        # Last-Event-ID HEADER — the transport no manual path can
        # exercise.  ``trunc>=1`` with the query-param counter still 0
        # is itself the header proof: only a presented-and-stale cursor
        # draws the envelope (a cursorless fresh connect draws
        # snapshot-only).
        #
        # Gap-free handoff: a failed EventSource reconnect ATTEMPT is
        # terminal per WHATWG (fail-the-connection → CLOSED, no second
        # retry), so the single retry must never hit a refused port.
        # The placeholder binds (SO_REUSEPORT) while node1 still lives,
        # its backlog completes the retry's TCP handshake during the
        # boot, and node2's uvicorn drains it once serving.
        from tests._sse_recovery_server import make_listen_socket

        placeholder = make_listen_socket(port)
        node.stop()
        node = _boot_node(port=port, sock=placeholder)
        node.open_workstream(keeper_id)

        deadline = time.monotonic() + 25
        healed = False
        while time.monotonic() < deadline:
            trunc = cdp.evaluate("window.__globalTruncated || 0")
            ghost_gone = _roster_absent_ws(cdp, ghost_id, "roster-ghost")
            keeper_here = _roster_has_ws(cdp, keeper_id, "roster-keeper")
            if isinstance(trunc, int) and trunc >= 1 and ghost_gone and keeper_here:
                healed = True
                break
            time.sleep(0.4)
        if not healed:
            trunc = cdp.evaluate("window.__globalTruncated || 0")
            # es state: 0/1 = the retry is still pending (boot too slow /
            # stream never dropped), 2 = the browser gave up (a refused
            # window reached the single terminal retry).
            es_state = cdp.evaluate("window.__globalES ? window.__globalES.readyState : -1")
            return (
                f"RECOVERY-FAILED-ROSTERNATIVE-trunc{trunc}"
                f"-ghost{'0' if _roster_absent_ws(cdp, ghost_id, 'roster-ghost') else '1'}"
                f"-keeper{'1' if _roster_has_ws(cdp, keeper_id, 'roster-keeper') else '0'}"
                f"-es{es_state}"
            )
        if cdp.evaluate("window.__globalCursorPresented || 0") != 0:
            return "RECOVERY-FAILED-ROSTERNATIVE-native-leg-used-query-cursor"
        if node.global_events_requests < 1:
            return "RECOVERY-FAILED-ROSTERNATIVE-no-backend-global-connect"

        # -- Phase C: the round-3 fix's discriminator -----------------------
        # The heal's id-less snapshot frame re-captured the dead cursor
        # on THIS (native) path until app.js's node_snapshot-branch
        # clear; prove the clear works by forcing a manual reconnect
        # NOW and asserting it goes CURSORLESS with no second truncated
        # round.  Precondition: no id-bearing frame may have landed
        # since the heal (an aggregate tick would legitimately re-arm a
        # LIVE cursor and void the probe — ~10s cadence vs this
        # sub-second window; a distinct verdict keeps a freak race
        # readable as environment, not regression).
        frames_at_heal = cdp.evaluate("window.__globalIdFrames || 0")
        opens_before = cdp.evaluate("window.__globalOpens || 0")
        cdp.evaluate("window.__globalES && window.__globalES.close()")
        cdp.evaluate("window.__hide && window.__hide()")
        cdp.evaluate("window.__show && window.__show()")
        if cdp.evaluate("window.__globalIdFrames || 0") != frames_at_heal:
            return "RECOVERY-FAILED-ROSTERNATIVE-tick-raced-the-probe"

        deadline = time.monotonic() + 10
        reopened = False
        while time.monotonic() < deadline:
            opens = cdp.evaluate("window.__globalOpens || 0")
            if isinstance(opens, int) and isinstance(opens_before, int) and opens > opens_before:
                reopened = True
                break
            time.sleep(0.3)
        if not reopened:
            return "RECOVERY-FAILED-ROSTERNATIVE-manual-reconnect-never-fired"
        cursor = cdp.evaluate("window.__globalCursorPresented || 0")
        trunc = cdp.evaluate("window.__globalTruncated || 0")
        opens = cdp.evaluate("window.__globalOpens || 0")
        if cursor != 0:
            # Pre-fix shape: the dead cursor survived the snapshot frame
            # and the manual reconnect presented it (cursor1 → a
            # redundant second truncated round follows).
            return f"RECOVERY-FAILED-ROSTERNATIVE-dead-cursor-represented-cursor{cursor}"
        if trunc != 1:
            return f"RECOVERY-FAILED-ROSTERNATIVE-redundant-truncated-trunc{trunc}"
        verdict = f"RECOVERY-READY-ROSTERNATIVE-trunc{trunc}-cursor0-opens{opens}"
        cdp.evaluate(f"document.title = {json.dumps(verdict)}")
        return verdict
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_coord_restart(chrome: str) -> str:
    from tests._sse_recovery_server import final_text_script, parallel_bash_script

    port = _free_port()
    node = _boot_node(port=port)
    # Same shape as Scenario B: a PACED turn so the tab can hide MID-turn.
    # The coordinator renders no streamed tool output (no tool_output_chunk
    # case), but the chunk frames still advance the pane's cursor in
    # onmessage BEFORE dispatch — so the hide freezes a genuinely mid-turn
    # cursor even though the paint signal differs (see below).  The closing
    # assistant text after the bash is the healed-gap sentinel, committed
    # while hidden.
    paced = parallel_bash_script({"c0": "for i in $(seq 1 40); do echo c-$i; sleep 0.05; done"})
    ws_id = node.create_workstream(
        paced, final_text_script(HEALED_SENTINEL), name="browser-coord-restart"
    )
    profile = Path(_scratch()) / "chrome-coord-restart"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/coord-recovery?ws_id={ws_id}&healed={HEALED_SENTINEL}"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # Hide once the BROWSER has captured a live mid-turn cursor: the
        # coordinator chrome has no status/SSE text elements (the header was
        # deliberately dropped) and paints no streamed output line, so the
        # signal is transport-level — id-bearing frames received by the page
        # (__idFrames; exactly the frames that advance the pane's reconnect
        # cursor).  The turn must also still be RUNNING server-side, or the
        # frozen cursor could sit at/above the committed counter and the
        # reconnect would take the lossless replay_ok path (trunc0).  The
        # paced bash runs >=2s, so this lands mid-turn.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            frames = cdp.evaluate("window.__idFrames || 0")
            if isinstance(frames, int) and frames >= 2 and node.ws_state(ws_id) == "running":
                break
            time.sleep(0.2)
        else:
            raise AssertionError(
                "coord-restart scenario: no mid-turn cursor captured "
                "(id frames never reached the page while running)"
            )
        time.sleep(0.5)
        cdp.evaluate("window.__hide && window.__hide()")
        # The turn (and the sentinel closing text) commits while hidden.
        node.wait_turn(ws_id, timeout=30)
        # Restart the node on the SAME port (fresh empty ring, seeded counter).
        node.stop()
        node = _boot_node(port=port)
        node.open_workstream(ws_id)
        # Show the tab -> stale-cursor reconnect -> truncated -> jittered
        # resync (0-10s) -> /history rebuild.  Settle past the worst-case
        # jitter, assert idle SERVER-side (the chrome has no state text to
        # read), then take the in-page verdict.
        cdp.evaluate("window.__show && window.__show()")
        time.sleep(12.0)
        _wait_state(node, ws_id, "idle", 15)
        cdp.evaluate("window.__verifyCoordRestart && window.__verifyCoordRestart()")
        return _poll_title(cdp, 20)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def _poll_until(pred: Any, timeout: float, interval: float = 0.1) -> bool:
    """Poll ``pred()`` until truthy or the deadline elapses; return whether it
    became truthy.  The livepass convention — prefer an observable edge to a
    bare sleep wherever one exists."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _send_in_page(cdp: CDP, message: str) -> None:
    """POST /send from inside the page via the pane's own authFetch (cookie
    auth, node-proxy base) — the one shared shape for scenarios that drive a
    turn mid-flight (the shared shape for every scenario that drives a
    turn outside the composer). The accepted row is projected live as
    ``user_turn`` just like a composer send."""
    cdp.evaluate(
        "window.authFetch('/v1/api/workstreams/' + "
        "encodeURIComponent(window.__pane.wsId) + '/send', {method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body: JSON.stringify({message:" + json.dumps(message) + "})})"
        ".then(function(r){return 'sent-'+r.status;})"
        ".catch(function(e){return 'err-'+e;})"
    )


def run_fail_refetch(chrome: str) -> str:
    """Scenario D — a FAILED truncated-resync /history must PRESERVE the pane
    (#890 guard-before-wipe), and the connect-chokepoint retry must then heal
    the gap.  Clones run_restart's hide -> restart -> show flow, but arms one
    forced /history failure before the show edge so the first jittered resync
    500s.  PHASE 1 asserts the failed fetch left the pre-restart rows on
    screen with no empty-state and the sentinel un-healed (plus the backend
    proof node.history_fail_remaining == 0); PHASE 2 asserts the retry's
    second /history healed it."""
    from tests._sse_recovery_server import final_text_script, parallel_bash_script

    port = _free_port()
    node = _boot_node(port=port)
    paced = parallel_bash_script({"r0": "for i in $(seq 1 40); do echo r-$i; sleep 0.05; done"})
    ws_id = node.create_workstream(
        paced, final_text_script(HEALED_SENTINEL), name="browser-fail-refetch"
    )
    profile = Path(_scratch()) / "chrome-fail-refetch"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = (
            f"{node.base_url}/recovery?ws_id={ws_id}&scenario=fail-refetch&healed={HEALED_SENTINEL}"
        )
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # Hide the moment the first streamed line paints (mid-turn cursor
        # frozen below the hidden-window commits) — the same edge run_restart
        # uses to force a truncated show-edge reconnect.
        if not _poll_until(
            lambda: cdp.evaluate("document.querySelector('.tool-output-stream') !== null"),
            15,
            0.2,
        ):
            raise AssertionError("fail-refetch: first streamed line never painted")
        cdp.evaluate("window.__hide && window.__hide()")
        node.wait_turn(ws_id, timeout=30)
        # Restart on the SAME port, then ARM one /history failure BEFORE the
        # show edge: the show-edge reconnect draws replay_truncated and the
        # truncated resync's FIRST /history 500s.
        node.stop()
        node = _boot_node(port=port)
        node.open_workstream(ws_id)
        node.fail_history(1)
        cdp.evaluate("window.__show && window.__show()")
        # PHASE 1 edge: the failed fetch consumed the fail budget (the
        # jittered resync fired, <=10s).  Poll the backend rather than sleep,
        # and capture the stale-but-preserved DOM the instant it lands —
        # before the retry can heal it.
        if not _poll_until(lambda: node.history_fail_remaining == 0, 20):
            raise AssertionError("fail-refetch: forced /history failure never fired")
        stale = cdp.evaluate("JSON.stringify(window.__verifyFailFetchStale())")
        # Backend proof the failure actually happened (never scripted absence).
        assert node.history_fail_remaining == 0, "fail-refetch: fail budget not consumed"
        # PHASE 2 edge: the connect-chokepoint retry redraws replay_truncated
        # and the SECOND /history succeeds — poll the DOM for the heal.
        _poll_until(
            lambda: cdp.evaluate(
                "(window.__pane.messagesEl.textContent||'').includes("
                + json.dumps(HEALED_SENTINEL)
                + ")"
            ),
            20,
            0.2,
        )
        print(f"  fail-refetch phase-1 (stale): {stale}")
        # The heal must have come from the connect-chokepoint RETRY,
        # not a scripted accident: the restart reset the counter to 0
        # and the first resync 500'd (1), so the healing fetch makes it
        # >= 2.  ">= 2" not "== 2": a legitimate extra jitter/churn
        # resync cycle may add a third.  Load-bearing counter — do not
        # let history_requests drop back to write-only.
        assert node.history_requests >= 2, (
            f"fail-refetch: heal did not re-fetch (history_requests={node.history_requests})"
        )
        cdp.evaluate("window.__verifyFailFetch && window.__verifyFailFetch()")
        return _poll_title(cdp, 20)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_stale_ref(chrome: str) -> str:
    """Scenario E1 — regression test for the #890 streaming-ref reset.  A
    mid-content transport teardown leaves a live assistant bubble ref; a
    same-ws UNARMED re-auth reload (the factory's onLogin fan-out) must reset
    it, so the NEXT turn's text builds a FRESH bubble instead of concatenating
    into the stale one.  No node restart: the page's teardown hook drops the
    transport on turn 1's content, the runner re-auth reloads through a forced
    /history failure, then drives turn 2 and proves the sentinel landed in a
    different bubble with the first bubble unchanged."""
    node = _boot_node()
    # Turn 1 carries assistant CONTENT (the bubble that must go stale) AND a
    # paced bash, so the turn is genuinely mid-flight when the transport drops
    # (unlike run_restart's pure-bash turn, which sets no content ref — the
    # concatenation bug is specifically about a content bubble).  The content
    # event trips the page's teardown hook; the bash + closing text then
    # complete server-side during the outage.
    turn1 = {
        "content": "First-turn assistant answer.",
        "tool_calls": [
            {
                "id": "r0",
                "name": "bash",
                "arguments": json.dumps(
                    {"command": "for i in $(seq 1 40); do echo r-$i; sleep 0.05; done"}
                ),
            }
        ],
        "finish_reason": "tool_calls",
    }
    from tests._sse_recovery_server import final_text_script

    ws_id = node.create_workstream(
        turn1,
        final_text_script("turn one closed"),
        final_text_script(SECOND_SENTINEL),
        name="browser-stale-ref",
    )
    profile = Path(_scratch()) / "chrome-stale-ref"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = (
            f"{node.base_url}/recovery?ws_id={ws_id}"
            f"&scenario=stale-ref-reload&second={SECOND_SENTINEL}"
        )
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # The page auto-sends turn 1; its content event trips the teardown
        # hook (REAL pane.disconnectSSE() mid-segment).  Poll the exposed flag
        # rather than guess a sleep.
        if not _poll_until(lambda: cdp.evaluate("window.__teardownDone === 1"), 20):
            raise AssertionError("stale-ref: mid-content teardown never fired")
        # Turn 1 completes server-side during the outage.
        node.wait_turn(ws_id, timeout=30)
        # Arm one /history failure, then re-auth reload EXACTLY as the
        # factory's onLogin does — same-ws, unarmed (no truncation cursor was
        # ever recorded, so nothing schedules a jittered resync).
        node.fail_history(1)
        cdp.evaluate("window.__pane._loadHistoryThenConnect(window.__pane.wsId)")
        # Failed fetch + cursorless reconnect; the only backend edge is the
        # fail budget draining to 0 (no resync jitter here).
        if not _poll_until(lambda: node.history_fail_remaining == 0, 10, 0.05):
            raise AssertionError("stale-ref: forced /history failure never fired")
        # The reconnect must be open before turn 2 (its listener catches the
        # content), and the reload must have reset the stale ref.
        _poll_until(lambda: cdp.evaluate("(window.__streamOpen || 0) >= 2"), 10)
        phase1 = cdp.evaluate("JSON.stringify(window.__captureStaleRefPhase1())")
        # Drive turn 2 in-page via authFetch (the SECOND_SENTINEL final text).
        _send_in_page(cdp, "second turn")
        _poll_until(
            lambda: cdp.evaluate(
                "(window.__pane.messagesEl.textContent||'').includes("
                + json.dumps(SECOND_SENTINEL)
                + ")"
            ),
            20,
            0.2,
        )
        print(f"  stale-ref phase-1 (bubble): {phase1}")
        cdp.evaluate("window.__verifyStaleRef && window.__verifyStaleRef()")
        return _poll_title(cdp, 20)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def _seed_three_completed_turns(name: str, extra_scripts: tuple[Any, ...] = ()) -> tuple[Any, str]:
    """Boot a node and drive THREE completed ``final_text`` turns, returning
    ``(node, ws_id)`` — the byte-identical seeding the rewind scenarios
    (E2/E3/E4) share, extracted so their rewind arithmetic provably reads off
    the SAME transcript. Each of the three ``node.send`` calls consumes one
    scripted turn before a browser listener exists, so the initial page-load
    /history render paints all three user rows.

    ``extra_scripts`` are appended to the scripted client AFTER the three
    seeding scripts and left UNSENT for scenarios that later drive a real
    turn.  Their positional scripts stay in sync because the three seeding
    sends consume exactly the three leading scripts."""
    from tests._sse_recovery_server import final_text_script

    node = _boot_node()
    ws_id = node.create_workstream(
        final_text_script("one"),
        final_text_script("two"),
        final_text_script("three"),
        *extra_scripts,
        name=name,
    )
    for msg in ("first", "second", "third"):
        node.send(ws_id, msg)
        node.wait_turn(ws_id)
    return node, ws_id


def run_rewind_window(chrome: str) -> str:
    """Scenario E2 — the row affordance gate (``busy || _historyStale``, #890).
    Three completed turns => three user rows; a REAL rewind click on the
    second row POSTs and its clear_ui refetch is held open by
    node.delay_history, keeping the quiesce armed; a second REAL rewind click
    (first row) mid-rebuild must return before POSTing.  The backend proof is
    node.rewind_requests == 1 (only the first click reached the server)."""
    node, ws_id = _seed_three_completed_turns("browser-rewind-window")
    profile = Path(_scratch()) / "chrome-rewind-window"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=rewind-window"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # Wait for the initial /history to paint all three user rows.
        if not _poll_until(
            lambda: cdp.evaluate(_ROWS_JS) == 3,
            20,
            0.2,
        ):
            raise AssertionError("rewind-window: three user rows never rendered")
        # The REST rows paint before the initial EventSource registration is
        # guaranteed to finish.  Clicking in that legitimate handoff gap can
        # advance the history token first, producing a strong resync instead
        # of the established-listener ``clear_ui`` whose quiesce this scenario
        # is specifically meant to exercise.  Coordinator G1 carries the same
        # transport-open gate.
        if not _poll_until(
            lambda: cdp.evaluate("(window.__streamOpen || 0) >= 1"),
            10,
            0.05,
        ):
            raise AssertionError("rewind-window: SSE stream never opened")
        # Hold every /history 3s so the clear_ui refetch keeps the quiesce
        # armed long enough to click the second rewind mid-rebuild.
        node.delay_history(3000)
        # Click #1 — the REAL rewind button on the SECOND user row: POSTs,
        # server emits clear_ui, the refetch is now held.
        if not cdp.evaluate("window.__clickRewind(1)"):
            raise AssertionError("rewind-window: second-row rewind button missing")
        # Wait for the clear_ui refetch to arm the quiesce (observable edge).
        if not _poll_until(lambda: cdp.evaluate("window.__pane._replayQueue != null"), 5, 0.05):
            raise AssertionError("rewind-window: clear_ui never armed the quiesce")
        # Click #2 — the rewind button on the FIRST user row WHILE the quiesce
        # is armed: the #890 gate must return before POSTing.
        if not cdp.evaluate("window.__clickRewind(0)"):
            raise AssertionError("rewind-window: first-row rewind button missing")
        # The gate leaves no positive edge (a POST that never happens), so
        # confirm the NON-occurrence over a bounded window: a failed gate's
        # /rewind is NOT delayed and would land within ~200ms.
        _poll_until(lambda: node.rewind_requests != 1, 1.5, 0.05)
        posts = node.rewind_requests
        # Release the hold and let the single in-flight rewind settle to one
        # user row (2nd-of-3 row rewound 2 turns => one user turn remains).
        node.delay_history(0)
        _poll_until(
            lambda: (
                (not cdp.evaluate("window.__pane._replayQueue != null"))
                and cdp.evaluate(_ROWS_JS) == 1
            ),
            8,
        )
        print(f"  rewind-window rewind_requests={posts}")
        cdp.evaluate(f"window.__verifyRewindWindow({posts})")
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_rewind_failed_window(chrome: str) -> str:
    """Scenario E3 — the FAILED clear_ui refetch aftermath (#890).  Clones
    run_rewind_window's three-turn seeding, but the rewind's clear_ui refetch
    is forced to 500 (node.fail_history) instead of held open.  The
    _historyStale LATCH (set at clear_ui, cleared ONLY by a successful
    replayHistory render) must keep the row affordances gated over the
    stale-but-real transcript AFTER the failed fetch releases the transient
    _replayQueue quiesce — the exact aftermath where pre-latch (quiesce-only)
    code reopened the gate and let a second rewind over-rewind.  The bounded 2s
    retry then heals the transcript and reopens the gate for a fresh,
    legitimate rewind.  Backend proofs: rewind_requests 1 -> (gated) 1 -> 2,
    history_fail_remaining == 0, history_requests >= 2."""
    node, ws_id = _seed_three_completed_turns("browser-rewind-failed-window")
    profile = Path(_scratch()) / "chrome-rewind-failed-window"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=rewind-failed-window"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # Wait for the initial /history to paint all three user rows.  This
        # load MUST succeed, so arm the forced failure only AFTERWARDS.
        if not _poll_until(
            lambda: cdp.evaluate(_ROWS_JS) == 3,
            20,
            0.2,
        ):
            raise AssertionError("rewind-failed-window: three user rows never rendered")
        # Arm ONE forced /history 500: the NEXT /history — the rewind's
        # clear_ui refetch — fails.  The initial load already succeeded, so the
        # failure lands on the refetch, not first paint.
        node.fail_history(1)
        # Click #1 — the REAL rewind on the SECOND user row: POSTs (the
        # authoritative rewind commits server-side), the server emits clear_ui,
        # and its refetch 500s.  The stale transcript survives (#890
        # guard-before-wipe) and the _historyStale latch stays SET.
        if not cdp.evaluate("window.__clickRewind(1)"):
            raise AssertionError("rewind-failed-window: second-row rewind button missing")
        if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
            raise AssertionError("rewind-failed-window: first rewind never POSTed")
        if not _poll_until(lambda: node.history_fail_remaining == 0, 15):
            raise AssertionError("rewind-failed-window: forced /history failure never fired")
        # Backend proof the failure actually happened (never scripted absence).
        assert node.history_fail_remaining == 0, "rewind-failed-window: fail budget not consumed"
        # Hold the bounded retry's /history open.  The retry is a fixed 2s
        # timer; delaying its fetch (the next /history to ARRIVE) defers the
        # latch's only clear site — replayHistory on a SUCCESSFUL render — to
        # ~failure+5s, a wide, CDP-speed-independent window for the gated click
        # below.  Armed AFTER the failure (so the FIRST refetch still fails
        # FAST, keeping detection prompt) and ~1.8s BEFORE the retry fires.
        # This is the determinism shape the E3 spec invites; a bare 500ms bound
        # would also hold (the click lands ~300ms after the failure, well
        # before the 2s retry), but the delay removes the race entirely — the
        # latch provably cannot clear until we release, so the closed-phase
        # checks below never overlap the heal.
        node.delay_history(3000)
        # CLOSED-PHASE — the failed-fetch aftermath.  Wait for the failed
        # refetch to fully settle: it releases the transient quiesce
        # (_replayQueue -> null) while the _historyStale latch SURVIVES.  This
        # is the crux the scenario exists for — a pre-latch gate keyed on the
        # quiesce would now be OPEN; only the latch holds it.
        if not _poll_until(
            lambda: cdp.evaluate(
                "window.__pane._replayQueue == null && window.__pane._historyStale === true"
            ),
            5,
            0.05,
        ):
            raise AssertionError(
                "rewind-failed-window: failed fetch did not settle to latch-held/quiesce-released"
            )
        # The stale transcript is intact — the failed fetch wiped nothing.
        stale_rows = cdp.evaluate(_ROWS_JS)
        if stale_rows != 3:
            raise AssertionError(
                f"rewind-failed-window: failed fetch did not preserve the transcript "
                f"(user rows={stale_rows}, expected 3)"
            )
        # Click #2 — rewind on the FIRST user row while the latch is set: the
        # #890 gate (busy || _historyStale) must return before POSTing.
        if not cdp.evaluate("window.__clickRewind(0)"):
            raise AssertionError("rewind-failed-window: first-row rewind button missing")
        # The gate leaves no positive edge (a POST that never happens), so
        # confirm the NON-occurrence over a bounded window: a failed gate's
        # /rewind is NOT delayed and would land within ~200ms.  This is the
        # assertion that regresses to 2 on pre-latch code (with posts2).
        _poll_until(lambda: node.rewind_requests != 1, 1.5, 0.05)
        closed_posts = node.rewind_requests
        # HEAL-PHASE — the bounded retry fires at ~2s (pane idle/turn-free), its
        # held /history completes (~failure+5s) and rebuilds the rewound
        # transcript: index 1 of 3 user rows rewinds 2 turns => ONE user row
        # (same arithmetic as run_rewind_window).  Poll to a deadline rather
        # than sleeping the 2s timer + 3s hold.
        healed = _poll_until(
            lambda: cdp.evaluate(_ROWS_JS) == 1,
            10,
            0.2,
        )
        # The retry re-fetched: init load (1) + failed refetch (2) + retry (3).
        # ">= 2" is the spec floor (matching the fail-refetch sibling); the
        # 3-user-rows -> 1-user-row DOM transition above is the load-bearing
        # proof the retry RENDERED — a stale transcript can only shrink via a
        # successful /history render.  Load-bearing counter — do not let
        # history_requests drop back to write-only.
        assert node.history_requests >= 2, (
            f"rewind-failed-window: retry did not re-fetch (history_requests={node.history_requests})"
        )
        # Release the hold now that the heal landed — the reopen's own clear_ui
        # refetch must not be delayed.
        node.delay_history(0)
        # REOPEN-PHASE — the healing render cleared the latch, reopening the
        # gate.  A rewind on the remaining user row is now legitimate and must
        # land with a FRESH count (rewind_requests -> 2).  On a heal failure the
        # verdict is already lost; skip the click and stamp the observed counts.
        if healed:
            if not cdp.evaluate("window.__clickRewind(0)"):
                raise AssertionError("rewind-failed-window: healed-row rewind button missing")
            _poll_until(lambda: node.rewind_requests == 2, 8, 0.05)
        posts = node.rewind_requests
        print(f"  rewind-failed-window closed_posts={closed_posts} healed={healed} posts={posts}")
        cdp.evaluate(
            f"window.__verifyRewindFail({posts}, {closed_posts}, {'true' if healed else 'false'})"
        )
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_stale_backstop(chrome: str) -> str:
    """Scenario E4 — the ``_historyStale`` latch's TRANSPORT-FREE idle-edge
    backstop (#890, the round-5 critical).  The DOUBLE-failure sibling of E3:
    a rewind's clear_ui refetch AND its one bounded 2s retry are BOTH forced
    to 500 (``node.fail_history(2)``), so the latch cannot self-heal and
    rewind/edit stay gated over the stale-but-real transcript.  The recovery
    server then publishes one real ``state_change:idle`` through the loaded
    UI. That is the settle edge the backstop consumes, without admitting a
    live ``user_turn`` or starting unrelated model work that would change the
    row and transcript counters in this transport-isolation probe. The
    backstop remains a quiesced, same-token
    REST ``_refetchHistory``, deliberately NOT ``_loadHistoryThenConnect``.
    With the fault budget exhausted it heals the rewound transcript.

    THE r5 PROOF (both counted at the fault layer): ``events_requests`` is
    UNCHANGED across the whole heal (``sse0`` — zero new EventSource
    connections; the storm regression opens one per reconnect) and
    ``history_requests`` grew by exactly ONE (the backstop's single fetch).
    Backend proofs:
    ``rewind_requests`` 1 -> (gated) 1 -> 2, ``history_fail_remaining == 0``.
    Every poll is deadline-bounded so a regressed looping backstop stamps a
    clean FAILED, never a hang."""
    node, ws_id = _seed_three_completed_turns("browser-stale-backstop")
    profile = Path(_scratch()) / "chrome-stale-backstop"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=stale-backstop"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # Wait for the initial /history to paint all three user rows.  This
        # load MUST succeed, so arm the forced failures only AFTERWARDS.
        if not _poll_until(
            lambda: cdp.evaluate(_ROWS_JS) == 3,
            20,
            0.2,
        ):
            raise AssertionError("stale-backstop: three user rows never rendered")
        # Arm TWO forced /history 500s: the rewind's clear_ui refetch AND its
        # one bounded 2s retry both fail, so the latch cannot self-heal and
        # ONLY the organic idle-edge backstop can clear it.
        node.fail_history(2)
        # Click #1 — the REAL rewind on the SECOND user row: POSTs (the
        # authoritative rewind commits server-side to ONE user turn — index 1
        # of 3 rewinds 2), the server emits clear_ui, and its refetch 500s
        # (fault 2 -> 1).
        if not cdp.evaluate("window.__clickRewind(1)"):
            raise AssertionError("stale-backstop: second-row rewind button missing")
        if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
            raise AssertionError("stale-backstop: first rewind never POSTed")
        # Both the clear_ui refetch AND the 2s retry must fire and fail (fault
        # 2 -> 1 -> 0): history_fail_remaining == 0 proves both consumed.  The
        # retry fires ~2s after the first failure (pane idle, turn-free), so a
        # 20s deadline covers it comfortably.
        if not _poll_until(lambda: node.history_fail_remaining == 0, 20):
            raise AssertionError(
                "stale-backstop: the two forced /history failures never both fired"
            )
        # Backend proof the failures actually happened (never scripted absence).
        assert node.history_fail_remaining == 0, "stale-backstop: fail budget not consumed"
        # Aftermath: the failed refetches released the transient quiesce
        # (_replayQueue -> null) while the _historyStale latch SURVIVES — the
        # backstop's precondition (and the idle-edge guard is !_replayQueue).
        if not _poll_until(
            lambda: cdp.evaluate(
                "window.__pane._replayQueue == null && window.__pane._historyStale === true"
            ),
            5,
            0.05,
        ):
            raise AssertionError(
                "stale-backstop: aftermath did not settle to latch-held/quiesce-released"
            )
        # The stale transcript is intact — the failed fetches wiped nothing.
        stale_rows = cdp.evaluate(_ROWS_JS)
        if stale_rows != 3:
            raise AssertionError(
                f"stale-backstop: failed fetches did not preserve the transcript "
                f"(user rows={stale_rows}, expected 3)"
            )
        # Click #2 — rewind on the FIRST user row while the latch is set: the
        # #890 gate (busy || _historyStale) must return before POSTing.  The
        # non-occurrence is confirmed over a bounded window (a broken gate's
        # /rewind is not delayed and would land within ~200ms).
        if not cdp.evaluate("window.__clickRewind(0)"):
            raise AssertionError("stale-backstop: first-row rewind button missing")
        _poll_until(lambda: node.rewind_requests != 1, 1.5, 0.05)
        gated_posts = node.rewind_requests  # must still be 1 (latch gated it)
        # Baselines captured immediately before the idle edge: the heal adds
        # exactly ZERO SSE opens and exactly ONE /history fetch relative here.
        events_baseline = node.events_requests
        history_baseline = node.history_requests
        # Publish the same per-workstream idle envelope a real turn settle
        # emits, without accepting/projecting another user turn or starting
        # unrelated model work.
        node.emit_idle_edge(ws_id)
        # HEAL: idle edge -> quiesced REST refetch (fault exhausted) succeeds
        # -> replayHistory rebuilds the rewound transcript to ONE user row and
        # clears the latch.  The 3-user-rows -> 1-user-row transition is
        # the load-bearing proof the backstop RENDERED — a stale transcript can
        # only change via a successful /history render.  Deadline-bounded so a
        # regressed looping backstop times out to a clean FAILED, never a hang.
        healed = _poll_until(
            lambda: cdp.evaluate(_ROWS_JS + " === 1 && window.__pane._historyStale === false"),
            20,
            0.2,
        )
        # THE r5 DELTAS, captured BEFORE the reopen click's own clear_ui
        # refetch so the arithmetic is exactly the backstop's:
        #  - events_delta MUST be 0: the backstop is a REST _refetchHistory, so
        #    it opens ZERO EventSource connections (a reload backstop's
        #    connectSSE would bump events_requests by one per reconnect — the
        #    round-5 storm).  The EventSource opened at initial load is already
        #    folded into the baseline.
        #  - history_delta MUST be 1: the only /history in the edge+heal
        #    window is the backstop fetch.
        events_delta = node.events_requests - events_baseline
        history_delta = node.history_requests - history_baseline
        # REOPEN: the healing render cleared the latch, so a rewind on a
        # remaining user row is legitimate and lands (rewind_requests -> 2).
        # On a heal failure the verdict is already lost; skip the click and
        # stamp the observed counts.
        if healed:
            if not cdp.evaluate("window.__clickRewind(0)"):
                raise AssertionError("stale-backstop: healed-row rewind button missing")
            _poll_until(lambda: node.rewind_requests == 2, 8, 0.05)
        posts = node.rewind_requests
        print(
            f"  stale-backstop gated_posts={gated_posts} healed={healed} "
            f"events_delta={events_delta} history_delta={history_delta} posts={posts}"
        )
        cdp.evaluate(
            "window.__verifyStaleBackstop("
            f"{'true' if healed else 'false'}, "
            f"{events_delta}, {history_delta}, {gated_posts}, {posts})"
        )
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_hidden_retry(chrome: str) -> str:
    """Scenario E5 — the clear_ui retry's stream-liveness fire guard (#900,
    the interactive mirror of coord's G5).  ``disconnectSSE`` deliberately
    leaves the 2s retry ARMED — transport-only redials keep the pending heal
    intent — so a close-on-hide landing inside the arm window lets the retry
    fire with the transport down.  A seedless ``_refetchHistory`` then
    renders /history's as-of-now truth while ``_lastEventId`` stays frozen at
    the last delivered id, and the show-edge reconnect presents that frozen
    cursor: the server answers replay_ok and the slice repaints every turn
    the hidden render just committed (only ``system_turn`` and compaction
    markers carry id dedup — content and tool rows do not, and the render has
    just reset the streaming refs and the announce map).

    THE DETECTOR is a NON-OCCURRENCE, counted at the fault layer:
    ``history_requests`` must be UNCHANGED across the whole hidden window.
    Remove the transport clause from the fire guard and the hidden fetch lands,
    stamping hidden1 — the scenario's negative control.

    SCOPE, stated precisely (do not overclaim it): a hide nulls ``evtSource``
    outright, and ``connectSSE`` early-returns while ``document.hidden``, so
    this scenario can only ever exercise the guard's PRESENCE term
    (``this.evtSource &&``).  It structurally cannot produce the non-null,
    not-OPEN source the ``readyState === OPEN`` term exists for — that state
    needs a native redial in progress, which no fault primitive here produces
    deterministically.  The readyState half is therefore covered by REASONING
    plus coord parity, not by this scenario; its correctness twin IS covered,
    by E6/E8 through ``_refetchHistory``'s render-time gate.  Same true of
    coord's G5.

    A replay_ok reconnect carries NO synthetic ``state_change`` (only
    fresh/truncated replays do), so the latch stays closed across ``__show``:
    that is the accepted liveness-lag residual, and no timer may shortcut it.
    The recovery server therefore publishes one real ``state_change:idle``
    after show; this is the same backstop trigger without admitting a live
    user turn or starting unrelated model work. Exactly ONE new SSE
    open remains across show + heal (the show edge's own; the heal adds zero)."""

    node, ws_id = _seed_three_completed_turns("browser-hidden-retry")
    profile = Path(_scratch()) / "chrome-hidden-retry"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=hidden-retry"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # First paint MUST succeed — arm the forced failure only afterwards.
        if not _poll_until(lambda: cdp.evaluate(_ROWS_JS) == 3, 20, 0.2):
            raise AssertionError("hidden-retry: three user rows never rendered")
        # The stream must be OPEN before the hide, or close-on-hide has
        # nothing to tear down and the scenario proves nothing.
        if not _poll_until(lambda: node.events_requests >= 1, 10, 0.05):
            raise AssertionError("hidden-retry: SSE stream never opened")
        node.fail_history(1)
        if not cdp.evaluate("window.__clickRewind(1)"):
            raise AssertionError("hidden-retry: second-row rewind button missing")
        if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
            raise AssertionError("hidden-retry: rewind never POSTed")
        if not _poll_until(lambda: node.history_fail_remaining == 0, 15):
            raise AssertionError("hidden-retry: forced /history failure never fired")
        # Hide IMMEDIATELY — well inside the 2s arm window (the poll above
        # settles ~0.3s after the failure).  close-on-hide tears the
        # transport down and deliberately leaves the retry armed.
        cdp.evaluate("window.__hide && window.__hide()")
        if not _poll_until(lambda: cdp.evaluate("window.__pane.evtSource === null"), 5, 0.05):
            raise AssertionError("hidden-retry: close-on-hide never dropped the transport")
        hidden_baseline = node.history_requests
        # NON-occurrence window: the retry fires at STALE_RETRY_BASE_MS plus up
        # to STALE_RETRY_JITTER_MS, so the window must outlast floor+ceiling.
        # Without the guard this poll returns True (the hidden fetch lands) and
        # hidden_delta stamps 1.
        # Window = the 2000 ms floor + the jitter ceiling + slack.  The
        # retry's delay is `2000 + rand*STALE_RETRY_JITTER_MS` (#900), so a
        # window sized on the floor alone would close BEFORE a
        # top-of-range firing and report hidden0 for the wrong reason —
        # the detector would go vacuous and its negative control would
        # silently stop working.  Raise this with the constant.
        _poll_until(lambda: node.history_requests != hidden_baseline, 4.5, 0.1)
        hidden_delta = node.history_requests - hidden_baseline
        # The latch must still be SET — a skipped retry heals nothing, which
        # is exactly why the backstop still owns the repair below.
        latch_held = cdp.evaluate("window.__pane._historyStale === true")
        # Show: the reconnect presents the frozen cursor (replay_ok, nothing
        # lost) and carries no synthetic state_change, so the latch survives
        # it.  A server-origin idle edge then drives the settle the backstop
        # needs without admitting another user row.
        events_before_show = node.events_requests
        cdp.evaluate("window.__show && window.__show()")
        if not _poll_until(lambda: node.events_requests == events_before_show + 1, 10, 0.05):
            raise AssertionError("hidden-retry: show-edge reconnect never arrived")
        node.emit_idle_edge(ws_id)
        healed = _poll_until(
            lambda: cdp.evaluate(_ROWS_JS + " === 1 && window.__pane._historyStale === false"),
            20,
            0.2,
        )
        show_sse = node.events_requests - events_before_show
        if healed:
            if not cdp.evaluate("window.__clickRewind(0)"):
                raise AssertionError("hidden-retry: healed-row rewind button missing")
            _poll_until(lambda: node.rewind_requests == 2, 8, 0.05)
        posts = node.rewind_requests
        print(
            f"  hidden-retry hidden_delta={hidden_delta} latch_held={latch_held} "
            f"healed={healed} show_sse={show_sse} posts={posts}"
        )
        if not latch_held:
            raise AssertionError("hidden-retry: latch cleared without a render")
        cdp.evaluate(
            f"window.__verifyHiddenRetry({hidden_delta}, "
            f"{'true' if healed else 'false'}, {show_sse}, {posts})"
        )
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_await_window_gate(chrome: str) -> str:
    """Scenario E6 — the seedless render's cursor-safety gate (#900), the
    AWAIT-window half E5 cannot reach: E5's retry never fetches, so only a
    fetch that STARTS on a live stream and resolves onto a dead one exercises
    the render-time check.

    The retry fires with the transport OPEN (its fire guard passes), and its
    /history is held at the fault layer while a close-on-hide tears the
    stream down underneath it.  Rendering that payload would commit
    /history's as-of-now truth with ``_lastEventId`` frozen BELOW the rows
    painted — the same frozen-cursor double-render E5 guards at fire time,
    reached through a window no fire-time check can see.  The gate declines
    the render and takes the failed-fetch path instead: no wipe, quiesce
    released, latch intact for the backstop.

    DETECTOR: the stale transcript must survive the resolved fetch — THREE
    user rows still standing (the pre-rewind truth) with the latch still SET.
    Without the gate the render lands and stamps rows1-latch0, because a
    successful replayHistory is the latch's only clear site.

    NON-VACUITY is the load-bearing part here, and ``history_requests`` cannot
    carry it: that counter bumps on ARRIVAL, before the hold and before any
    status is chosen, so "the gate declined a good payload" and "there was no
    good payload" would stamp identical observables (no wipe either way, latch
    held either way).  ``history_ok`` — incremented only when the PRODUCTION
    route answers 200 — is what proves a renderable payload actually existed,
    and it closes the production-side-failure hole an injected-fail budget
    cannot see."""
    from tests._sse_recovery_server import final_text_script

    # The heal-driving send needs its OWN scripted turn: an exhausted script
    # queue still settles (into the error arm, which the backstop also
    # consumes), so relying on it would let this scenario pass for a reason
    # it does not name.
    node, ws_id = _seed_three_completed_turns(
        "browser-await-window-gate",
        extra_scripts=(final_text_script(BACKSTOP_SENTINEL),),
    )
    profile = Path(_scratch()) / "chrome-await-window-gate"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=await-window-gate"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        if not _poll_until(lambda: cdp.evaluate(_ROWS_JS) == 3, 20, 0.2):
            raise AssertionError("await-window-gate: three user rows never rendered")
        if not _poll_until(lambda: node.events_requests >= 1, 10, 0.05):
            raise AssertionError("await-window-gate: SSE stream never opened")
        # The rewind's own clear_ui refetch fails, arming the 2s retry with
        # the latch set and the transcript stale-but-real.
        node.fail_history(1)
        if not cdp.evaluate("window.__clickRewind(1)"):
            raise AssertionError("await-window-gate: second-row rewind button missing")
        if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
            raise AssertionError("await-window-gate: rewind never POSTed")
        if not _poll_until(lambda: node.history_fail_remaining == 0, 15):
            raise AssertionError("await-window-gate: forced /history failure never fired")
        # Hold the RETRY's fetch open.  history_requests counts on ARRIVAL,
        # before the hold sleeps, so the bump below is the in-flight edge.
        retry_baseline = node.history_requests
        ok_baseline = node.history_ok
        node.delay_history(3000)
        # The tab stays VISIBLE here — the retry must pass its fire guard,
        # or this scenario degenerates into E5 and proves nothing.
        if not _poll_until(lambda: node.history_requests == retry_baseline + 1, 6, 0.05):
            raise AssertionError("await-window-gate: the retry never fetched on the live stream")
        # Kill the transport mid-await.  The payload is already committed
        # server-side; only the RENDER decision is still open.
        cdp.evaluate("window.__hide && window.__hide()")
        if not _poll_until(lambda: cdp.evaluate("window.__pane.evtSource === null"), 5, 0.05):
            raise AssertionError("await-window-gate: close-on-hide never dropped the transport")
        # Clear the knob for any LATER arrival; the already-held fetch is
        # sleeping on the duration it captured at arrival and serves that
        # out regardless — which is why the poll below must outlast it.
        node.delay_history(0)
        if not _poll_until(lambda: cdp.evaluate("window.__pane._replayQueue === null"), 12, 0.1):
            raise AssertionError("await-window-gate: the quiesce never released")
        # The payload must have been GOOD — otherwise a declined render and a
        # failed fetch are indistinguishable (see the docstring).
        if not _poll_until(lambda: node.history_ok >= ok_baseline + 1, 12, 0.1):
            raise AssertionError(
                "await-window-gate: the held /history never answered 200 "
                f"(history_ok={node.history_ok}, baseline={ok_baseline}) — "
                "a declined render cannot be distinguished from a failed fetch"
            )
        rows = cdp.evaluate(_ROWS_JS)
        latch_held = cdp.evaluate("window.__pane._historyStale === true")
        # The heal still belongs to the backstop: show, then drive an organic
        # settle with a plain send and watch the rewound truth land.
        events_before_show = node.events_requests
        cdp.evaluate("window.__show && window.__show()")
        if not _poll_until(lambda: node.events_requests == events_before_show + 1, 10, 0.05):
            raise AssertionError("await-window-gate: show-edge reconnect never arrived")
        _send_in_page(cdp, "fourth turn")
        healed = _poll_until(
            lambda: cdp.evaluate(
                _ROWS_JS
                + " === 2 && window.__pane._historyStale === false"
                + " && (window.__pane.messagesEl.textContent||'').includes("
                + json.dumps(BACKSTOP_SENTINEL)
                + ")"
            ),
            20,
            0.2,
        )
        print(f"  await-window-gate rows={rows} latch_held={latch_held} healed={healed}")
        cdp.evaluate(
            f"window.__verifyAwaitWindowGate({rows}, "
            f"{'true' if latch_held else 'false'}, {'true' if healed else 'false'})"
        )
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_reconnect_in_await(chrome: str) -> str:
    """Scenario E8 — the stream-generation term (#900 r2), and the first
    detector in this harness that can observe a DOUBLE RENDER at all.

    Every other scenario counts ``.msg.user`` rows; an assistant/tool slice
    rendered twice leaves that count unchanged, so none of them can see the
    artefact this whole campaign prevents. E8 counts a sentinel's OCCURRENCES
    in the transcript text
    instead, which is structure-agnostic across duplicate assistant bubbles and
    duplicate tool blocks.

    The window: the clear_ui retry fires with the transport OPEN, its /history
    is held at the fault layer, and inside that await the transport DROPS and
    RE-ESTABLISHES.  ``readyState`` reads OPEN afterwards exactly as it did
    before, so neither the presence term nor the readyState term can tell the
    two apart — but the redial re-presented the frozen ``_lastEventId``, the
    server answered ``replay_ok`` with the turn committed during the gap, and
    the replay quiesce BUFFERED that slice.  Rendering then commits the turn,
    and ``_endReplayQuiesce`` repaints it on top: two copies.

    With the generation term the render is declined, the stale-but-real
    transcript survives, and the flushed replay paints the new turn exactly
    ONCE.  Strip the term and the same run stamps ``dupes2``.

    The redial is a REAL disconnect+connect, deliberately not ``__hide()``:
    a hide leaves ``evtSource`` null, which the presence term already decides,
    so a hide-based control would pass for the wrong reason."""
    from tests._sse_recovery_server import final_text_script

    node, ws_id = _seed_three_completed_turns(
        "browser-reconnect-in-await",
        extra_scripts=(final_text_script(DUPLICATE_SENTINEL),),
    )
    profile = Path(_scratch()) / "chrome-reconnect-in-await"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=reconnect-in-await"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        if not _poll_until(lambda: cdp.evaluate(_ROWS_JS) == 3, 20, 0.2):
            raise AssertionError("reconnect-in-await: three user rows never rendered")
        if not _poll_until(lambda: node.events_requests >= 1, 10, 0.05):
            raise AssertionError("reconnect-in-await: SSE stream never opened")
        # Arm the latch: the rewind's own clear_ui refetch fails, leaving the
        # stale transcript up and the bounded retry armed.
        node.fail_history(1)
        if not cdp.evaluate("window.__clickRewind(1)"):
            raise AssertionError("reconnect-in-await: second-row rewind button missing")
        if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
            raise AssertionError("reconnect-in-await: rewind never POSTed")
        if not _poll_until(lambda: node.history_fail_remaining == 0, 15):
            raise AssertionError("reconnect-in-await: forced /history failure never fired")
        # Hold the RETRY's fetch wide open — everything below happens inside
        # its await.  history_requests counts on ARRIVAL, so the bump is the
        # in-flight edge.
        retry_baseline = node.history_requests
        ok_baseline = node.history_ok
        node.delay_history(6000)
        if not _poll_until(lambda: node.history_requests == retry_baseline + 1, 8, 0.05):
            raise AssertionError("reconnect-in-await: the retry never fetched on the live stream")
        # (1) Drop the transport.  The cursor freezes here.
        cdp.evaluate("window.__pane.disconnectSSE()")
        if not _poll_until(lambda: cdp.evaluate("window.__pane.evtSource === null"), 5, 0.05):
            raise AssertionError("reconnect-in-await: the transport never dropped")
        # (2) Commit a turn while the stream is DOWN, so the server holds
        #     events past the frozen cursor with no way to have delivered them.
        # Runner-side send + the real turn-complete barrier: the page's
        # stream is down, so driving this from the browser would prove
        # nothing extra and only add a timing race.
        node.send(ws_id, "gap turn")
        node.wait_turn(ws_id)
        # (3) Re-establish, still inside the await.  This is the whole point:
        #     readyState goes back to OPEN, so only the generation moved.
        cdp.evaluate("window.__redial()")
        if not _poll_until(
            lambda: cdp.evaluate(
                "!!window.__pane.evtSource && "
                "window.__pane.evtSource.readyState === EventSource.OPEN"
            ),
            10,
            0.05,
        ):
            raise AssertionError("reconnect-in-await: the redial never reached OPEN")
        # NON-VACUITY, the leg this scenario turns on: the held fetch must
        # still be OUTSTANDING right now.  If it already resolved — a slow box
        # can push the disconnect/send/wait_turn/redial sequence past the hold
        # — the payload landed while evtSource was still null, the PRESENCE
        # term declined it, and a green verdict would never have touched the
        # generation term at all.
        if node.history_ok != ok_baseline:
            raise AssertionError(
                "reconnect-in-await: the held /history resolved BEFORE the "
                f"redial (history_ok={node.history_ok}, baseline={ok_baseline}) "
                "— the generation term was never exercised; raise the hold"
            )
        # Release the knob for later arrivals; the held fetch serves out its
        # own 6000 ms regardless, which is what the polls below outlast.
        node.delay_history(0)
        if not _poll_until(lambda: node.history_ok >= ok_baseline + 1, 15, 0.1):
            raise AssertionError(
                "reconnect-in-await: the held /history never answered 200 — "
                "a declined render cannot be distinguished from a failed fetch"
            )
        # Settle to the CONVERGED end state before counting.  The declined
        # render leaves the latch set, its flushed settle edge fires the
        # backstop, and the backstop's own fetch — stable generation, live
        # stream — lands the rewound-plus-gap transcript (2 user rows, latch
        # cleared).  Counting before that would race the heal.
        healed = _poll_until(
            lambda: cdp.evaluate(_ROWS_JS + " === 2 && window.__pane._historyStale === false"),
            25,
            0.2,
        )
        dupes = cdp.evaluate(f"window.__countSentinel({json.dumps(DUPLICATE_SENTINEL)})")
        print(f"  reconnect-in-await dupes={dupes} healed={healed}")
        cdp.evaluate(f"window.__verifyReconnectInAwait({dupes}, {'true' if healed else 'false'})")
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_destroy_invalidation(chrome: str) -> str:
    """Scenario E7 — destroy()'s load-token bump (#900).  The ONLY interactive
    scenario that mounts through ``createInteractivePane``: every other one
    drives the ``Pane`` class directly, so the factory closure that owns
    ``destroy()`` had no browser coverage at all — and destroy() is where the
    #900 backport's largest hole lived.

    ``destroy()`` bumped no load token, so a ``/history`` still in flight at
    teardown kept passing every post-await gate.  The worst leg is
    ``_loadHistoryThenConnect``'s ``.finally``: it reopened an ``EventSource``
    on the DETACHED pane and re-registered the document-level
    ``visibilitychange`` listener ``_removeVisibilityHandler`` had just
    dropped — and that stream's ``onerror`` re-armed the host recover beat
    indefinitely, because it gives up only on ``dead``, which ``destroy()``
    never sets.  A closed tab kept a node connection and a 5s reconnect beat
    for the life of the page.

    The runner holds the FIRST ``/history`` open at the fault layer, destroys
    the controller mid-flight, and lets the load resolve into the void.
    DETECTOR: ``events_requests`` must still be 0 — the load resolved without
    reconnecting — with the pane detached and ``_visHandler`` null behind it.
    Remove the bump and the ``.finally`` fires: sse1, and a non-null handler
    still bound to the document."""
    node, ws_id = _seed_three_completed_turns("browser-destroy-invalidation")
    profile = Path(_scratch()) / "chrome-destroy-invalidation"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        # Hold the FIRST /history — the one connect() dispatches — so the
        # teardown lands with the load genuinely outstanding.
        node.delay_history(4000)
        ok_baseline = node.history_ok
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=destroy-invalidation"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # history_requests counts on ARRIVAL, before the hold sleeps.
        if not _poll_until(lambda: node.history_requests >= 1, 20, 0.05):
            raise AssertionError("destroy-invalidation: the first /history never arrived")
        # Non-vacuity: nothing may have connected yet, or the .finally under
        # test has already run and the scenario proves nothing.
        if node.events_requests != 0:
            raise AssertionError(
                f"destroy-invalidation: stream opened before teardown "
                f"(events_requests={node.events_requests})"
            )
        if not cdp.evaluate("!!window.__ctl"):
            raise AssertionError("destroy-invalidation: page did not mount through the factory")
        cdp.evaluate("window.__ctl.destroy()")
        if not _poll_until(lambda: cdp.evaluate("!window.__pane.el.parentNode"), 5, 0.05):
            raise AssertionError("destroy-invalidation: destroy() never detached the pane")
        # Hygiene only: this cannot release the in-flight hold (the fault
        # layer captured the duration at arrival), and no further /history
        # is expected here.  The non-occurrence poll below therefore has to
        # outlast the REMAINING hold — raising delay_history above without
        # raising that deadline would make this detector vacuous.
        node.delay_history(0)
        _poll_until(lambda: node.events_requests != 0, 8, 0.1)
        # sse_opens == 0 only means "nothing connected in 8s".  Prove the held
        # load actually SETTLED inside that window, or the .finally under test
        # never ran and the non-occurrence is measuring nothing.
        if node.history_ok < ok_baseline + 1:
            raise AssertionError(
                "destroy-invalidation: the held /history never resolved inside "
                f"the observation window (history_ok={node.history_ok}) — the "
                ".finally under test never ran"
            )
        sse_opens = node.events_requests
        vis_null = cdp.evaluate("window.__pane._visHandler === null")
        print(f"  destroy-invalidation sse_opens={sse_opens} vis_null={vis_null}")
        cdp.evaluate(f"window.__verifyDestroyInvalidation({sse_opens})")
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_coord_rewind_window(chrome: str) -> str:
    """Scenario G1 — the coordinator row affordance gate (``busy ||
    historyStale``, #894, the coord port of E2).  Three completed turns =>
    three user rows; a REAL rewind click on the second row POSTs and its
    clear_ui refetch is held open by node.delay_history; a second REAL rewind
    click (first row) mid-hold must return before POSTing.  Coord has no
    quiesce to observe (its latch is closure-private), so the in-flight edge
    is the fault layer's ``history_requests`` bump — counted on ARRIVAL,
    before the hold sleeps — and the latch is set synchronously BEFORE that
    fetch dispatches, so the bump proves the gate is closed.  Backend proof:
    node.rewind_requests == 1 (only the first click reached the server)."""
    node, ws_id = _seed_three_completed_turns("browser-coord-rewind-window")
    profile = Path(_scratch()) / "chrome-coord-rewind-window"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/coord-recovery?ws_id={ws_id}&scenario=coord-rewind-window"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # Wait for the initial /history to paint all three user rows.
        if not _poll_until(lambda: cdp.evaluate(_COORD_ROWS_JS) == 3, 20, 0.2):
            raise AssertionError("coord-rewind-window: three user rows never rendered")
        # Rows paint from init's ``await refetchHistory(true)`` and the pane
        # dials SSE only AFTERWARDS — a rewind clicked in that gap emits
        # clear_ui into a channel nobody joined (a cursorless connect takes
        # the fresh no-replay branch), the latch never sets, and the
        # scenario false-fails.  Gate on the transport wrapper's counter,
        # like the coord page's coord-restart send gate.
        if not _poll_until(lambda: cdp.evaluate("window.__esOpens") >= 1, 10, 0.05):
            raise AssertionError("coord-rewind-window: SSE stream never opened")
        # Relative baseline (never assume how many /history the boot ran).
        hist_baseline = node.history_requests
        # Hold every /history 3s so the clear_ui refetch keeps the latch's
        # only clear site — the success render — from running while the
        # second click lands.
        node.delay_history(3000)
        # Click #1 — the REAL rewind button on the SECOND user row: POSTs,
        # server emits clear_ui, the refetch is now held.
        if not cdp.evaluate("window.__clickCoordRewind(1)"):
            raise AssertionError("coord-rewind-window: second-row rewind button missing")
        # The held refetch ARRIVED (counter bumps before the delay sleep) —
        # the clear_ui handler set historyStale synchronously before
        # dispatching this fetch, so from here the gate is provably closed.
        if not _poll_until(lambda: node.history_requests == hist_baseline + 1, 5, 0.05):
            raise AssertionError("coord-rewind-window: clear_ui refetch never arrived")
        # Click #2 — the rewind button on the FIRST user row WHILE the
        # refetch is held: the #894 gate must return before POSTing.
        if not cdp.evaluate("window.__clickCoordRewind(0)"):
            raise AssertionError("coord-rewind-window: first-row rewind button missing")
        # The gate leaves no positive edge (a POST that never happens), so
        # confirm the NON-occurrence over a bounded window: a failed gate's
        # /rewind is NOT delayed and would land within ~200ms.
        _poll_until(lambda: node.rewind_requests != 1, 1.5, 0.05)
        posts = node.rewind_requests
        # Release the hold and let the single in-flight rewind settle to one
        # user row (2nd-of-3 row rewound 2 turns => one user turn remains).
        node.delay_history(0)
        _poll_until(lambda: cdp.evaluate(_COORD_ROWS_JS) == 1, 8)
        print(f"  coord-rewind-window rewind_requests={posts}")
        cdp.evaluate(f"window.__verifyCoordRewindWindow({posts})")
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_coord_rewind_failed_window(chrome: str) -> str:
    """Scenario G2 — the coordinator FAILED clear_ui refetch aftermath
    (#894, the coord port of E3).  The rewind's clear_ui refetch is forced to
    500; the historyStale LATCH (set at clear_ui, cleared ONLY by a
    successful refetchHistory render) must keep the row affordances gated
    over the stale-but-real transcript after the failed fetch — the exact
    aftermath where a refetch-in-flight flag would reopen the gate and let a
    second rewind over-rewind.  The bounded 2s retry then heals and reopens.

    Coord's latch is closure-private, so the closed phase is proven by
    observables: rewind #1 POSTed + the fail budget consumed => the clear_ui
    handler ran (the latch was set synchronously before its fetch), and the
    gated click's POST NON-occurrence is the gate assertion itself.  Backend
    proofs: rewind_requests 1 -> (gated) 1 -> 2, history_fail_remaining == 0,
    history_requests >= baseline + 2 (failed refetch + the retry's fetch)."""
    node, ws_id = _seed_three_completed_turns("browser-coord-rewind-failed-window")
    profile = Path(_scratch()) / "chrome-coord-rewind-failed-window"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/coord-recovery?ws_id={ws_id}&scenario=coord-rewind-failed-window"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # Wait for the initial /history to paint all three user rows.  This
        # load MUST succeed, so arm the forced failure only AFTERWARDS.
        if not _poll_until(lambda: cdp.evaluate(_COORD_ROWS_JS) == 3, 20, 0.2):
            raise AssertionError("coord-rewind-failed-window: three user rows never rendered")
        # SSE-open gate — see run_coord_rewind_window: a pre-connect rewind
        # drops its clear_ui and false-fails the scenario.
        if not _poll_until(lambda: cdp.evaluate("window.__esOpens") >= 1, 10, 0.05):
            raise AssertionError("coord-rewind-failed-window: SSE stream never opened")
        hist_baseline = node.history_requests
        # Arm ONE forced /history 500: the NEXT /history — the rewind's
        # clear_ui refetch — fails.
        node.fail_history(1)
        # Click #1 — the REAL rewind on the SECOND user row: POSTs (the
        # authoritative rewind commits server-side), the server emits
        # clear_ui, and its refetch 500s.  The stale transcript survives
        # (#882 guard-before-wipe) and the historyStale latch stays SET.
        if not cdp.evaluate("window.__clickCoordRewind(1)"):
            raise AssertionError("coord-rewind-failed-window: second-row rewind button missing")
        if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
            raise AssertionError("coord-rewind-failed-window: first rewind never POSTed")
        if not _poll_until(lambda: node.history_fail_remaining == 0, 15):
            raise AssertionError("coord-rewind-failed-window: forced /history failure never fired")
        # Backend proof the failure actually happened (never scripted absence).
        assert node.history_fail_remaining == 0, (
            "coord-rewind-failed-window: fail budget not consumed"
        )
        # Hold the bounded retry's /history open: the retry is a fixed 2s
        # timer, so delaying its fetch defers the latch's only clear site to
        # ~failure+5s — a wide, CDP-speed-independent window for the gated
        # click below.  Armed AFTER the failure (the first refetch fails
        # fast) and ~1.8s BEFORE the retry fires.
        node.delay_history(3000)
        # CLOSED-PHASE — the stale transcript is intact (the failed fetch
        # wiped nothing: guard-before-wipe), and the latch survived the
        # failed exit (its clear sits below ``if (!hist) return``).
        stale_rows = cdp.evaluate(_COORD_ROWS_JS)
        if stale_rows != 3:
            raise AssertionError(
                f"coord-rewind-failed-window: failed fetch did not preserve the "
                f"transcript (user rows={stale_rows}, expected 3)"
            )
        # Click #2 — rewind on the FIRST user row while the latch is set:
        # the #894 gate (busy || historyStale) must return before POSTing.
        if not cdp.evaluate("window.__clickCoordRewind(0)"):
            raise AssertionError("coord-rewind-failed-window: first-row rewind button missing")
        # Non-occurrence over a bounded window — the assertion that
        # regresses to closed2 on pre-latch code.
        _poll_until(lambda: node.rewind_requests != 1, 1.5, 0.05)
        closed_posts = node.rewind_requests
        # HEAL-PHASE — the bounded retry fires at ~2s (pane idle,
        # turn-free), its held /history completes (~failure+5s) and rebuilds
        # the rewound transcript: index 1 of 3 user rows rewinds 2 turns =>
        # ONE user row.  Poll to a deadline rather than sleeping.
        healed = _poll_until(lambda: cdp.evaluate(_COORD_ROWS_JS) == 1, 10, 0.2)
        # The retry re-fetched: failed refetch + the retry's success = two
        # more than the paint baseline.  The 3->1 DOM transition above is
        # the load-bearing proof the retry RENDERED.  Load-bearing counter —
        # do not let history_requests drop back to write-only.
        assert node.history_requests >= hist_baseline + 2, (
            f"coord-rewind-failed-window: retry did not re-fetch "
            f"(history_requests={node.history_requests}, baseline={hist_baseline})"
        )
        # Release the hold — the reopen's own clear_ui refetch must not be
        # delayed.
        node.delay_history(0)
        # REOPEN-PHASE — the healing render cleared the latch.  A rewind on
        # the remaining user row must land with a FRESH count
        # (rewind_requests -> 2).  On a heal failure the verdict is already
        # lost; skip the click and stamp the observed counts.
        if healed:
            if not cdp.evaluate("window.__clickCoordRewind(0)"):
                raise AssertionError("coord-rewind-failed-window: healed-row rewind button missing")
            _poll_until(lambda: node.rewind_requests == 2, 8, 0.05)
        posts = node.rewind_requests
        print(
            f"  coord-rewind-failed-window closed_posts={closed_posts} "
            f"healed={healed} posts={posts}"
        )
        cdp.evaluate(
            f"window.__verifyCoordRewindFail({posts}, {closed_posts}, "
            f"{'true' if healed else 'false'})"
        )
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def _coord_stick_latch(cdp: CDP, node: Any, tag: str) -> None:
    """The shared G3/G4 prologue: paint three rows, gate on the SSE open,
    then stick the staleness latch — fail_history(2) exhausts the rewind's
    clear_ui refetch AND its one bounded 2s retry, so only an organic
    idle-edge heal can clear it.  Extracted so G4's premise (latch stuck
    exactly as in G3) is enforced by construction, the same rationale
    _seed_three_completed_turns documents for the E family.

    RULED (r10): G2/G5 deliberately keep their single-failure prologues
    inline rather than adopting this helper — their baseline captures
    and phase timings interleave INTO the prologue steps (G2 snapshots
    history_requests before the click; G5 hides the instant the fail
    budget drains), so a parameterized version would need a flag per
    divergence and obscure the choreography it exists to clarify.  The
    helper serves the two double-failure scenarios whose premise must
    match exactly."""
    if not _poll_until(lambda: cdp.evaluate(_COORD_ROWS_JS) == 3, 20, 0.2):
        raise AssertionError(f"{tag}: three user rows never rendered")
    if not _poll_until(lambda: cdp.evaluate("window.__esOpens") >= 1, 10, 0.05):
        raise AssertionError(f"{tag}: SSE stream never opened")
    node.fail_history(2)
    if not cdp.evaluate("window.__clickCoordRewind(1)"):
        raise AssertionError(f"{tag}: second-row rewind button missing")
    if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
        raise AssertionError(f"{tag}: first rewind never POSTed")
    if not _poll_until(lambda: node.history_fail_remaining == 0, 20):
        raise AssertionError(f"{tag}: the two forced /history failures never both fired")
    assert node.history_fail_remaining == 0, f"{tag}: fail budget not consumed"
    stale_rows = cdp.evaluate(_COORD_ROWS_JS)
    if stale_rows != 3:
        raise AssertionError(
            f"{tag}: failed fetches did not preserve the transcript "
            f"(user rows={stale_rows}, expected 3)"
        )


def run_coord_stale_backstop(chrome: str) -> str:
    """Scenario G3 — the coordinator ``historyStale`` latch's TRANSPORT-FREE
    idle-edge backstop (#894, the coord port of E4).  The DOUBLE-failure
    sibling of G2: the rewind's clear_ui refetch AND its one bounded 2s retry
    are BOTH forced to 500 (``node.fail_history(2)``), so the latch cannot
    self-heal and rewind/edit stay gated over the stale-but-real transcript.
    The recovery server publishes one real ``state_change:idle`` through the
    loaded UI, which fires the backstop without admitting a live user turn or
    starting unrelated model work: a plain seedless REST
    ``refetchHistory`` — deliberately NOT
    ``loadHistoryThenReconnect`` (a reconnecting heal draws the server's
    synthetic ``state_change:idle`` back into its own trigger: a
    zero-backoff reconnect/refetch storm).

    THE STORM PROOF (both counted at the fault layer): ``events_requests``
    is UNCHANGED across the whole heal (``sse0`` — zero new EventSource
    connections) and ``history_requests`` grew by exactly ONE (the
    backstop's single fetch).
    Coord's latch is closure-private, so the latch-cleared proof is the
    reopen POST (rewind_requests -> 2), not a field read.  Every poll is
    deadline-bounded so a regressed looping backstop stamps a clean FAILED,
    never a hang."""
    node, ws_id = _seed_three_completed_turns("browser-coord-stale-backstop")
    profile = Path(_scratch()) / "chrome-coord-stale-backstop"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/coord-recovery?ws_id={ws_id}&scenario=coord-stale-backstop"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # Wait for the initial /history to paint all three user rows.  This
        # load MUST succeed, so arm the forced failures only AFTERWARDS.
        _coord_stick_latch(cdp, node, "coord-stale-backstop")
        # Click #2 — rewind on the FIRST user row while the latch is set:
        # gated, POST non-occurrence confirmed over a bounded window.
        if not cdp.evaluate("window.__clickCoordRewind(0)"):
            raise AssertionError("coord-stale-backstop: first-row rewind button missing")
        _poll_until(lambda: node.rewind_requests != 1, 1.5, 0.05)
        gated_posts = node.rewind_requests  # must still be 1 (latch gated it)
        # Baselines captured immediately before the idle edge: the heal adds
        # exactly ZERO SSE opens and exactly ONE /history fetch from here.
        events_baseline = node.events_requests
        history_baseline = node.history_requests
        node.emit_idle_edge(ws_id)
        # HEAL: idle edge -> plain REST refetch (fault exhausted) succeeds ->
        # the render rebuilds the rewound transcript to ONE user row.  The
        # 3->1 transition is the load-bearing proof the
        # backstop RENDERED; the latch-cleared proof is the reopen POST
        # below (the latch itself is closure-private).
        healed = _poll_until(
            lambda: cdp.evaluate(_COORD_ROWS_JS + " === 1"),
            20,
            0.2,
        )
        # THE STORM DELTAS, captured BEFORE the reopen click's own clear_ui
        # refetch so the arithmetic is exactly the backstop's:
        #  - events_delta MUST be 0: the backstop is a REST refetchHistory,
        #    ZERO EventSource connections (a reload backstop's connectSSE
        #    bumps events_requests per reconnect — the storm).
        #  - history_delta MUST be 1: the only /history in the edge+heal
        #    window is the backstop's fetch.
        events_delta = node.events_requests - events_baseline
        history_delta = node.history_requests - history_baseline
        # REOPEN: the healing render cleared the latch, so a rewind on a
        # remaining user row is legitimate and lands (rewind_requests -> 2).
        # On a heal failure the verdict is already lost; skip the click and
        # stamp the observed counts.
        if healed:
            if not cdp.evaluate("window.__clickCoordRewind(0)"):
                raise AssertionError("coord-stale-backstop: healed-row rewind button missing")
            _poll_until(lambda: node.rewind_requests == 2, 8, 0.05)
        posts = node.rewind_requests
        print(
            f"  coord-stale-backstop gated_posts={gated_posts} healed={healed} "
            f"events_delta={events_delta} history_delta={history_delta} posts={posts}"
        )
        cdp.evaluate(
            "window.__verifyCoordStaleBackstop("
            f"{'true' if healed else 'false'}, "
            f"{events_delta}, {history_delta}, {gated_posts}, {posts})"
        )
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_coord_heal_midturn(chrome: str) -> str:
    """Scenario G4 — the coordinator's seedless render-time tool gate.

    G3's double-failure prologue leaves the latch stuck.  A server-origin
    idle edge starts the seedless backstop fetch and ``delay_history`` holds
    it.  While it is in flight the recovery server publishes a real
    ``tool_pending`` envelope through ``SessionUIBase.on_agent_step``.  The
    coordinator's event-owned ``liveToolCalls`` set is therefore non-empty
    when the good history response resolves, so the render must be declined:
    the stale transcript and the live tool shell both remain continuously
    visible. This avoids using ``/send`` as a trigger; a send would project a
    live ``user_turn`` and start unrelated model activity while this precise
    render window is being measured.

    The matching ``tool_result`` retires the live-call entry and a second idle
    edge re-fires the backstop.  That fetch may render and heals to the single
    post-rewind user row.  Exact discriminators: the first successful payload
    completed while rows3 + the live shell survived, zero SSE opens, and
    exactly two history fetches (declined + healing).  Removing the tool gate
    renders the first payload, wipes the shell, clears the latch, and leaves
    only one history fetch."""
    probe_call_id = "recovery-render-gate-probe"
    probe_selector = ".conv-batch--running .conv-row[data-call-id='" + probe_call_id + "']"
    node, ws_id = _seed_three_completed_turns("browser-coord-heal-midturn")
    profile = Path(_scratch()) / "chrome-coord-heal-midturn"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/coord-recovery?ws_id={ws_id}&scenario=coord-heal-midturn"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        _coord_stick_latch(cdp, node, "coord-heal-midturn")
        events_baseline = node.events_requests
        history_baseline = node.history_requests
        history_ok_baseline = node.history_ok
        # Hold the first backstop fetch long enough to publish and visibly
        # confirm the live tool phase before its response resolves.
        node.delay_history(1500)
        node.emit_idle_edge(ws_id)
        if not _poll_until(lambda: node.history_requests == history_baseline + 1, 20, 0.05):
            raise AssertionError("coord-heal-midturn: backstop fetch never arrived")
        node.emit_tool_pending(ws_id, probe_call_id)
        if not _poll_until(
            lambda: cdp.evaluate(
                _COORD_ROWS_JS
                + " === 3 && !!document.querySelector("
                + json.dumps(probe_selector)
                + ")"
            ),
            10,
            0.05,
        ):
            raise AssertionError("coord-heal-midturn: live tool phase never rendered")
        # A 200 response proves the held request resolved with a renderable
        # payload.  Observe for another bounded window so the browser has had
        # ample time to process it; any wipe flips this predicate immediately.
        if not _poll_until(lambda: node.history_ok >= history_ok_baseline + 1, 10, 0.05):
            raise AssertionError("coord-heal-midturn: held /history never resolved successfully")
        gate_broke = _poll_until(
            lambda: (
                not cdp.evaluate(
                    _COORD_ROWS_JS
                    + " === 3 && !!document.querySelector("
                    + json.dumps(probe_selector)
                    + ")"
                )
            ),
            1.0,
            0.05,
        )
        midturn_survived = not gate_broke

        # Retire the event-owned live-call entry, then publish the settle edge
        # that is now allowed to consume and render authoritative history.
        node.delay_history(0)
        node.emit_tool_result(ws_id, probe_call_id)
        node.emit_idle_edge(ws_id)
        healed = _poll_until(
            lambda: cdp.evaluate(
                _COORD_ROWS_JS
                + " === 1 && !document.querySelector("
                + json.dumps(".conv-row[data-call-id='" + probe_call_id + "']")
                + ")"
            ),
            20,
            0.2,
        )
        events_delta = node.events_requests - events_baseline
        history_delta = node.history_requests - history_baseline
        # REOPEN: the heal cleared the latch; a fresh rewind lands.
        if healed:
            if not cdp.evaluate("window.__clickCoordRewind(0)"):
                raise AssertionError("coord-heal-midturn: healed-row rewind button missing")
            _poll_until(lambda: node.rewind_requests == 2, 8, 0.05)
        posts = node.rewind_requests
        print(
            f"  coord-heal-midturn midturn_survived={midturn_survived} healed={healed} "
            f"events_delta={events_delta} history_delta={history_delta} posts={posts}"
        )
        cdp.evaluate(
            "window.__verifyCoordHealMidturn("
            f"{'true' if healed else 'false'}, "
            f"{'true' if midturn_survived else 'false'}, "
            f"{events_delta}, {history_delta}, {posts})"
        )
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_coord_hidden_retry(chrome: str) -> str:
    """Scenario G5 — the retry's stream-liveness fire guard (#894 r4).  The
    2s retry deliberately survives close-on-hide (transport redials keep
    heal intent), so it can FIRE while the tab is hidden and the transport
    is down.  A seedless fetch then would render the pane past the frozen
    ``lastEventId``; the show-edge reconnect replays from that frozen
    cursor and double-renders every turn the hidden render already painted.
    The ``evtSource`` fire-guard term skips the hidden firing instead
    (hiddenDelta 0); the show edge only restores the transport
    (replay_ok carries no synthetic state_change), and the recovery server
    publishes a real idle state edge to fire the TRANSPORT-FREE backstop on
    the live stream without admitting another user row.

    A replay_ok reconnect (frozen cursor, nothing lost) carries no
    synthetic state_change — only fresh/truncated replays do — so the
    latch stays closed after __show until the next settle edge — exactly the
    accepted-residual ruling (no timer may shortcut the lag).  The test-server
    pulse supplies that edge through the real UI event path.

    Proofs: history_requests UNCHANGED across the hidden retry window (the
    non-occurrence detector that regresses to hidden1 without the guard);
    exactly ONE new SSE open across show + heal (the user-driven reconnect
    — the heal itself adds zero); the healed render carries the single
    post-rewind user row; the reopen click lands."""

    node, ws_id = _seed_three_completed_turns("browser-coord-hidden-retry")
    profile = Path(_scratch()) / "chrome-coord-hidden-retry"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/coord-recovery?ws_id={ws_id}&scenario=coord-hidden-retry"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        if not _poll_until(lambda: cdp.evaluate(_COORD_ROWS_JS) == 3, 20, 0.2):
            raise AssertionError("coord-hidden-retry: three user rows never rendered")
        # SSE-open gate — see run_coord_rewind_window.
        if not _poll_until(lambda: cdp.evaluate("window.__esOpens") >= 1, 10, 0.05):
            raise AssertionError("coord-hidden-retry: SSE stream never opened")
        node.fail_history(1)
        if not cdp.evaluate("window.__clickCoordRewind(1)"):
            raise AssertionError("coord-hidden-retry: second-row rewind button missing")
        if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
            raise AssertionError("coord-hidden-retry: first rewind never POSTed")
        if not _poll_until(lambda: node.history_fail_remaining == 0, 15):
            raise AssertionError("coord-hidden-retry: forced /history failure never fired")
        # Hide IMMEDIATELY — well inside the 2s arm window (the poll above
        # settles ~0.3s after the failure).  close-on-hide tears the
        # transport down but deliberately leaves the retry timer armed.
        cdp.evaluate("window.__hide && window.__hide()")
        hidden_baseline = node.history_requests
        # NON-occurrence window: the retry fires at STALE_RETRY_BASE_MS plus up
        # to STALE_RETRY_JITTER_MS, so the window must outlast floor+ceiling.
        # Without the evtSource guard this poll returns True
        # (the hidden fetch lands) and hiddenDelta stamps 1.
        # Window = the 2000 ms floor + the jitter ceiling + slack.  The
        # retry's delay is `2000 + rand*STALE_RETRY_JITTER_MS` (#900), so a
        # window sized on the floor alone would close BEFORE a
        # top-of-range firing and report hidden0 for the wrong reason —
        # the detector would go vacuous and its negative control would
        # silently stop working.  Raise this with the constant.
        _poll_until(lambda: node.history_requests != hidden_baseline, 4.5, 0.1)
        hidden_delta = node.history_requests - hidden_baseline
        # Show: the reconnect presents the frozen cursor and replays
        # replay_ok (nothing lost), which carries NO synthetic
        # state_change (only fresh/truncated replays do — the
        # latch-closed lag is the accepted residual).  Wait for the reconnect
        # itself, then publish an idle edge through the loaded UI.  The heal
        # renders the single post-rewind user row.
        events_before_show = node.events_requests
        cdp.evaluate("window.__show && window.__show()")
        if not _poll_until(lambda: node.events_requests == events_before_show + 1, 10, 0.05):
            raise AssertionError("coord-hidden-retry: show-edge reconnect never arrived")
        node.emit_idle_edge(ws_id)
        healed = _poll_until(
            lambda: cdp.evaluate(_COORD_ROWS_JS + " === 1"),
            20,
            0.2,
        )
        show_sse = node.events_requests - events_before_show
        if healed:
            if not cdp.evaluate("window.__clickCoordRewind(0)"):
                raise AssertionError("coord-hidden-retry: healed-row rewind button missing")
            _poll_until(lambda: node.rewind_requests == 2, 8, 0.05)
        posts = node.rewind_requests
        print(
            f"  coord-hidden-retry hidden_delta={hidden_delta} healed={healed} "
            f"show_sse={show_sse} posts={posts}"
        )
        cdp.evaluate(
            f"window.__verifyCoordHiddenRetry({hidden_delta}, "
            f"{'true' if healed else 'false'}, {show_sse}, {posts})"
        )
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_coord_orphan_rewind(chrome: str) -> str:
    """Scenario G6 — the poisoned-pane detector (#894 r6/r7).  A node
    killed MID-TOOL leaves the committed tool_calls turn genuinely
    unresulted in storage (r7-verified: only a GRACEFUL close synthesizes
    the "Cancelled by user" result via session.cancel(); boot-time
    rehydration synthesizes nothing — so ``stop(hard=True)`` models the
    real SIGKILL/OOM crash).  The recovery render paints that orphan as
    a ``.conv-batch--running`` placeholder no result will ever strip.
    The r5 DOM-probed gate read the residue as "live" and skipped every
    subsequent SEEDLESS render for the life of the page: rewinds
    committed server-side but never rendered, the latch stuck, and
    rewind/edit went permanently dead.  The event-driven live-call set
    is EMPTY for an orphan (nothing announced it on the live stream
    since the reconnect), so the rewind's clear_ui render must proceed
    THROUGH the residue: the orphan batch is wiped, the rewound truth
    paints (the server keeps the user message and removes the unresulted
    assistant turn — rewind-for-retry), and the latch clears.

    This is the client hardening's behavioral detector: the runner
    asserts the residue IS present after recovery (the poisoned-pane
    precondition manifested), then that the seedless rewind flow works
    end to end regardless (posts 1, history_requests grew, one user row
    and no residue in the end state).  DOM-probe gate code fails the
    heal poll with the residue still standing — the orphan bit is the
    discriminator."""
    from tests._sse_recovery_server import final_text_script, parallel_bash_script

    port = _free_port()
    node = _boot_node(port=port)
    # ~200s of pacing: the hard-killed node's session thread keeps running
    # this bash in-process and persists its result at natural completion —
    # it must outlive the scenario's WORST-CASE deadline sum (join 20s +
    # boot 10s + orphan 30s + rewind 5s + heal 15s + verdict 15s ≈ 95s,
    # plus setup) or the "unresulted orphan" silently resolves
    # mid-scenario and the detector degrades to a false READY.
    paced = parallel_bash_script({"g6": "for i in $(seq 1 4000); do echo g6-$i; sleep 0.05; done"})
    ws_id = node.create_workstream(paced, final_text_script("g6-done"), name="browser-coord-orphan")
    profile = Path(_scratch()) / "chrome-coord-orphan-rewind"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/coord-recovery?ws_id={ws_id}&scenario=coord-orphan-rewind"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # First poll on this page (no seeded rows to wait for), so the
        # evaluate can race the page load and return None — coalesce.
        if not _poll_until(lambda: (cdp.evaluate("window.__esOpens") or 0) >= 1, 15, 0.05):
            raise AssertionError("coord-orphan-rewind: SSE stream never opened")
        # Drive the paced turn and kill the node while its bash streams —
        # the tool rows on screen prove the batch was mid-flight.
        _send_in_page(cdp, "run a turn")
        if not _poll_until(
            lambda: (
                cdp.evaluate(
                    "document.getElementById('coord-messages')"
                    ".querySelectorAll('.conv-row[data-call-id]').length"
                )
                >= 1
            ),
            15,
            0.2,
        ):
            raise AssertionError("coord-orphan-rewind: tool rows never painted")
        node.stop(hard=True)
        node = _boot_node(port=port)
        node.open_workstream(ws_id)
        # A hard-crashed reborn node answers the page's stale-high cursor
        # with a SILENT fresh stream — no replay_truncated (the honest-
        # truncation signal rides state a graceful stop persists; a crash
        # never writes it — pre-existing server hole, tracked separately).
        # The realistic operator recovery is a page RELOAD: init's seeded
        # /history render paints the orphan deterministically.
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # The pane's reconnect machinery (native retry -> truncated resync
        # -> seeded render) repaints the interrupted turn.  With a HARD
        # kill the tool call is genuinely unresulted, so the recovery
        # render shows the ORPHAN: one user row, a --running placeholder
        # batch no result will ever strip — the poisoned-pane
        # precondition, now manifested for real.
        if not _poll_until(
            lambda: (
                cdp.evaluate(_COORD_ROWS_JS) == 1
                and cdp.evaluate(
                    "document.getElementById('coord-messages')"
                    ".querySelector('.conv-batch--running') !== null"
                )
            ),
            30,
            0.3,
        ):
            raise AssertionError(
                "coord-orphan-rewind: recovery never painted the orphan "
                "--running residue (hard kill did not leave the tool call "
                "unresulted, or the reconnect failed)"
            )
        hist_baseline = node.history_requests
        # Rewind the sole user row — the full seedless flow must work
        # after a kill+reboot recovery (clear_ui render lands, latch
        # clears).
        if not cdp.evaluate("window.__clickCoordRewind(0)"):
            raise AssertionError("coord-orphan-rewind: rewind button missing")
        if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
            raise AssertionError("coord-orphan-rewind: rewind never POSTed")
        # Rewound truth: the server removes the unresulted assistant turn
        # and KEEPS the user message — success is one user row, residue
        # gone (a skipped render leaves the residue standing instead).
        healed = _poll_until(
            lambda: (
                cdp.evaluate(_COORD_ROWS_JS) == 1
                and not cdp.evaluate(
                    "document.getElementById('coord-messages')"
                    ".querySelector('.conv-batch--running') !== null"
                )
            ),
            15,
            0.2,
        )
        hist_delta = node.history_requests - hist_baseline
        posts = node.rewind_requests
        print(f"  coord-orphan-rewind healed={healed} hist_delta={hist_delta} posts={posts}")
        cdp.evaluate(f"window.__verifyCoordOrphanRewind({posts}, {hist_delta})")
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_coord_joined_flight(chrome: str) -> str:
    """Scenario G7 — the joined-flight window (#894 r8).  The #884
    /history single-flight coalesces concurrent requests per
    (ws_id, limit); before the r8 server fix a clear_ui refetch
    dispatched AFTER a rewind could JOIN a flight whose load_messages
    ran BEFORE the truncation committed — the joined pre-rewind payload
    painted as fresh truth (the client's dispatch stamp is current; the
    staleness is the flight's transaction point) and cleared the latch:
    the original over-rewind window, resurrected through the server
    seam.  The fix folds ChatSession._history_generation (bumped in
    _persist_truncation, the shared rewind/retry chokepoint) into the
    flight key, so post-truncation dispatches can never join
    pre-truncation flights.

    Choreography: page A paints three rows; ``delay_load`` then holds
    every reconstruction open INSIDE ``load_messages`` (the flight
    layer — ``delay_history`` sleeps in the fault layer, before the
    route, where flights never overlap); a background authenticated GET
    ("viewer B" — a raw request enters ``load_messages`` identically,
    without a second browser's cost) parks a pre-rewind flight; A
    rewinds mid-hold — its clear_ui refetch must MISS B's held flight
    (``load_calls`` grows by TWO: a joined request never enters
    ``load_messages`` — the e2e twin of the unit test's proof) and
    render the POST-rewind single row once the holds release.  Pre-fix
    stamps loads1 (joined) with three stale rows painted as fresh
    truth."""
    node, ws_id = _seed_three_completed_turns("browser-coord-joined-flight")
    profile_a = Path(_scratch()) / "chrome-coord-joined-a"
    proc_a, cdp_port_a = _launch_chrome(chrome, profile_a)
    cdp_a: CDP | None = None
    try:
        cdp_a = CDP(_page_ws_url(cdp_port_a))
        url = f"{node.base_url}/coord-recovery?ws_id={ws_id}&scenario=coord-joined-flight"
        _set_cookie_and_navigate(cdp_a, node.base_url, node.token, url)
        if not _poll_until(lambda: cdp_a.evaluate(_COORD_ROWS_JS) == 3, 20, 0.2):
            raise AssertionError("coord-joined-flight: A's three user rows never rendered")
        if not _poll_until(lambda: (cdp_a.evaluate("window.__esOpens") or 0) >= 1, 10, 0.05):
            raise AssertionError("coord-joined-flight: A's SSE stream never opened")
        load_baseline = node.load_calls
        # Hold every reconstruction open INSIDE load_messages (the flight
        # layer — delay_history sleeps in the fault layer, before the
        # route, where flights never overlap), then park the pre-rewind
        # flight under the hold.  The parked "viewer B" is a plain
        # authenticated GET on a background thread: a raw request enters
        # load_messages identically, and a second headless browser added
        # ~300 MB + seconds of launch for no additional proof.
        node.delay_load(3000)

        def _b_get() -> None:
            req = urllib.request.Request(
                f"{node.base_url}/v1/api/workstreams/{ws_id}/history",
                headers={"Cookie": f"turnstone_auth_server={node.token}"},
            )
            with contextlib.suppress(Exception):
                urllib.request.urlopen(req, timeout=30).read()

        b_thread = threading.Thread(target=_b_get, daemon=True)
        b_thread.start()
        if not _poll_until(lambda: node.load_calls == load_baseline + 1, 15, 0.05):
            raise AssertionError("coord-joined-flight: B's flight never entered load_messages")
        # A rewinds mid-hold: second row -> 2 turns -> server keeps one
        # user turn.  Its clear_ui refetch must MISS B's pre-rewind
        # flight (fresh arrival = baseline + 2, the join-miss proof).
        if not cdp_a.evaluate("window.__clickCoordRewind(1)"):
            raise AssertionError("coord-joined-flight: second-row rewind button missing")
        if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
            raise AssertionError("coord-joined-flight: rewind never POSTed")
        # The MISS proof at the flight layer: A's post-rewind dispatch
        # must enter load_messages itself (a JOINED request never does) —
        # the e2e twin of the unit test's load_calls == 2.
        joined_miss = _poll_until(lambda: node.load_calls == load_baseline + 2, 8, 0.05)
        # The holds release (~3s); A must render the POST-rewind truth
        # (pre-fix, the joined pre-rewind payload paints THREE rows).
        healed = _poll_until(lambda: cdp_a.evaluate(_COORD_ROWS_JS) == 1, 12, 0.2)
        load_delta = node.load_calls - load_baseline
        posts = node.rewind_requests
        print(
            f"  coord-joined-flight joined_miss={joined_miss} healed={healed} "
            f"load_delta={load_delta} posts={posts}"
        )
        cdp_a.evaluate(f"window.__verifyCoordJoinedFlight({posts}, {load_delta})")
        return _poll_title(cdp_a, 15)
    finally:
        if cdp_a is not None:
            cdp_a.close()
        _kill(proc_a)
        node.stop()


def run_handoff_repair_budget(chrome: str, *, coordinator: bool = False) -> str:
    """Strong repair is bounded and fail-closed; a rendered tokenless 200 downgrades.

    Four FAILED (500) history responses prove the bounded automatic budget:
    both real browser clients make exactly four attempts without opening an
    EventSource, park on the persistent manual prompt, and make one attempt
    per Retry click. A rendered-but-tokenless 200 is the server's deliberate
    cold storage-only read (or a pre-handoff server) and must DOWNGRADE to
    the tokenless bootstrap — one cursorless EventSource, prompt gone — never
    burn budget against a healthy response. A later resync answered by the
    real (tokened) server reconnects with proof. A final held attempt is
    destroyed before it settles and must not resurrect the stream.
    """

    tag = "COORDHANDOFF" if coordinator else "HANDOFF"
    scenario = "coord-handoff-repair-budget" if coordinator else "handoff-repair-budget"
    node = _boot_node()
    ws_id = node.create_workstream(name=f"browser-{scenario}")
    profile = Path(_scratch()) / f"chrome-{scenario}"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        route = "coord-recovery" if coordinator else "recovery"
        url = f"{node.base_url}/{route}?ws_id={ws_id}&scenario={scenario}"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        open_expr = "window.__esOpens || 0" if coordinator else "window.__streamOpen || 0"
        if not _poll_until(lambda: cdp.evaluate(open_expr) >= 1, 15, 0.1):
            raise AssertionError(f"{scenario}: initial EventSource never opened")

        history0 = node.history_requests
        events0 = node.events_requests
        node.fail_history(4)
        node.emit_history_resync(ws_id)
        prompt_expr = "!!document.querySelector('.history-handoff-repair')"
        if not _poll_until(
            lambda: node.history_requests == history0 + 4 and cdp.evaluate(prompt_expr),
            25,
            0.1,
        ):
            raise AssertionError(
                f"{scenario}: repair did not park after four failed attempts "
                f"(history={node.history_requests - history0}, events={node.events_requests - events0})"
            )
        # No hidden fifth attempt and no unverified EventSource.
        time.sleep(3)
        if node.history_requests != history0 + 4 or node.events_requests != events0:
            raise AssertionError(
                f"{scenario}: automatic budget failed closed "
                f"(history={node.history_requests - history0}, events={node.events_requests - events0})"
            )

        # One manual FAILED response: exactly one request and no auto burst.
        node.fail_history(1)
        if not cdp.evaluate("document.querySelector('.history-handoff-retry').click(); true"):
            raise AssertionError(f"{scenario}: manual retry button missing")
        if not _poll_until(lambda: node.history_requests == history0 + 5, 8, 0.05):
            raise AssertionError(f"{scenario}: manual retry did not issue one request")
        time.sleep(3)
        if node.history_requests != history0 + 5 or node.events_requests != events0:
            raise AssertionError(f"{scenario}: manual failure re-armed automatic work")

        # A rendered tokenless 200 downgrades: latch cleared, prompt gone, one
        # CURSORLESS EventSource (no history_token). The server's tokenless
        # bootstrap then converges the pane (clear_ui -> one more /history).
        node.tokenless_history(1)
        cdp.evaluate("document.querySelector('.history-handoff-retry').click()")
        if not _poll_until(
            lambda: (
                node.history_requests >= history0 + 6
                and node.events_requests == events0 + 1
                and not cdp.evaluate(prompt_expr)
            ),
            12,
            0.1,
        ):
            raise AssertionError(
                f"{scenario}: rendered tokenless 200 did not downgrade to bootstrap "
                f"(history={node.history_requests - history0}, events={node.events_requests - events0})"
            )
        tokenless_stream = (
            cdp.evaluate("!((window.__lastEventSourceUrl || '').includes('history_token='))")
            if coordinator
            else cdp.evaluate(
                "!!window.__pane.evtSource && "
                "!window.__pane.evtSource.url.includes('history_token=')"
            )
        )
        if not tokenless_stream:
            raise AssertionError(f"{scenario}: downgraded stream claimed a handoff token")

        # Let the bootstrap convergence (clear_ui refetch) settle before the
        # next phase snapshots its counters.
        def _history_settled() -> bool:
            snapshot = node.history_requests
            time.sleep(1.0)
            return node.history_requests == snapshot

        if not _poll_until(_history_settled, 15, 0.1):
            raise AssertionError(f"{scenario}: bootstrap convergence never settled")

        # A later resync answered by the REAL server reconnects with proof and
        # never re-parks.
        settled_history = node.history_requests
        settled_events = node.events_requests
        node.emit_history_resync(ws_id)
        if not _poll_until(
            lambda: (
                node.history_requests >= settled_history + 1
                and node.events_requests == settled_events + 1
                and not cdp.evaluate(prompt_expr)
            ),
            12,
            0.1,
        ):
            raise AssertionError(f"{scenario}: proven repair did not reconnect once")
        tokened = (
            cdp.evaluate("(window.__lastEventSourceUrl || '').includes('history_token=')")
            if coordinator
            else cdp.evaluate(
                "!!window.__pane.evtSource && "
                "window.__pane.evtSource.url.includes('history_token=')"
            )
        )
        if not tokened:
            raise AssertionError(f"{scenario}: proven repair reopened without proof")

        # Terminal teardown during a held attempt: abort/settle immediately;
        # when the server-side hold expires, no async tail may reopen.
        node.delay_history(4000)
        held0 = node.history_requests
        opens_before_destroy = node.events_requests
        node.emit_history_resync(ws_id)
        if not _poll_until(lambda: node.history_requests == held0 + 1, 5, 0.05):
            raise AssertionError(f"{scenario}: held teardown attempt never started")
        history_before_destroy = node.history_requests
        if coordinator:
            cdp.evaluate("window.__pane.destroy()")
        else:
            cdp.evaluate("window.__ctl.destroy()")
        time.sleep(5)
        node.delay_history(0)
        if (
            node.events_requests != opens_before_destroy
            or node.history_requests != history_before_destroy
        ):
            raise AssertionError(
                f"{scenario}: teardown resurrected repair work "
                f"(history={node.history_requests - history_before_destroy}, "
                f"events={node.events_requests - opens_before_destroy})"
            )

        return f"RECOVERY-READY-{tag}-auto4-manual1-downgrade1-proof1-teardown0"
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_user_turn_two_pane(chrome: str) -> str:
    """One accepted USER reaches two upgraded panes with no REST/redial fan-out."""

    from tests._sse_recovery_server import final_text_script

    node = _boot_node()
    ws_id = node.create_workstream(
        final_text_script("projection acknowledged"),
        name="browser-user-turn-two-pane",
    )
    profile = Path(_scratch()) / "chrome-user-turn-two-pane"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    message = "one shared projected prompt"
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=user-turn-two-pane"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        opened = _poll_until(
            lambda: (
                (cdp.evaluate("window.__streamOpen || 0") or 0) == 1
                and (cdp.evaluate("window.__peerStreamOpen || 0") or 0) == 1
            ),
            20,
            0.1,
        )
        if not opened:
            diagnostics = cdp.evaluate(
                "({originOpens:window.__streamOpen||0,"
                "peerOpens:window.__peerStreamOpen||0,"
                "originUrl:window.__pane&&window.__pane.evtSource&&window.__pane.evtSource.url,"
                "peerUrl:window.__peerPane&&window.__peerPane.evtSource&&"
                "window.__peerPane.evtSource.url,title:document.title})"
            )
            raise AssertionError(
                "user-turn-two-pane: both initial streams did not open once "
                f"(browser={diagnostics!r}, history={node.history_requests}, "
                f"events={node.events_requests})"
            )

        capability_urls = cdp.evaluate(
            "({origin: window.__pane.evtSource.url, peer: window.__peerPane.evtSource.url})"
        )
        if not all("user_turn=1" in capability_urls[name] for name in ("origin", "peer")):
            raise AssertionError(f"user-turn-two-pane: capability missing from {capability_urls!r}")

        # Count reducer deliveries themselves as well as DOM rows. This catches
        # a duplicate event that happened to be hidden by DOM/event-id dedup.
        cdp.evaluate(
            "window.__originUserEvents=0; window.__peerUserEvents=0; "
            "window.__originRepairEvents=0; window.__peerRepairEvents=0; "
            "const oh=window.__pane.handleEvent.bind(window.__pane); "
            "window.__pane.handleEvent=function(e){"
            "if(e&&e.type==='user_turn')window.__originUserEvents++;"
            "if(e&&e.type==='replay_truncated')window.__originRepairEvents++;"
            "return oh(e);}; "
            "const ph=window.__peerPane.handleEvent.bind(window.__peerPane); "
            "window.__peerPane.handleEvent=function(e){"
            "if(e&&e.type==='user_turn')window.__peerUserEvents++;"
            "if(e&&e.type==='replay_truncated')window.__peerRepairEvents++;"
            "return ph(e);}; true"
        )
        history0 = node.history_requests
        events0 = node.events_requests
        cdp.evaluate(f"window.__sendProjectedUserTurn({json.dumps(message)})")

        rows_expr = (
            "window.__pane.messagesEl.querySelectorAll('.msg.user').length===1 && "
            "window.__peerPane.messagesEl.querySelectorAll('.msg.user').length===1"
        )
        if not _poll_until(lambda: bool(cdp.evaluate(rows_expr)), 12, 0.05):
            raise AssertionError("user-turn-two-pane: canonical rows did not render once")
        node.wait_turn(ws_id, timeout=30)
        time.sleep(0.5)

        state = cdp.evaluate(
            "(()=>{const row=(p)=>p.messagesEl.querySelector('.msg.user');"
            "const text=(r)=>r&&r.querySelector('.msg-user-text')&&"
            "r.querySelector('.msg-user-text').textContent;"
            "const o=row(window.__pane),p=row(window.__peerPane);"
            "return {originRows:window.__pane.messagesEl.querySelectorAll('.msg.user').length,"
            "peerRows:window.__peerPane.messagesEl.querySelectorAll('.msg.user').length,"
            "originText:text(o),peerText:text(p),"
            "originId:o&&o.dataset.eventId,peerId:p&&p.dataset.eventId,"
            "originEvents:window.__originUserEvents,peerEvents:window.__peerUserEvents,"
            "originRepair:window.__originRepairEvents,peerRepair:window.__peerRepairEvents,"
            "originOpens:window.__streamOpen,peerOpens:window.__peerStreamOpen};})()"
        )
        if state != {
            "originRows": 1,
            "peerRows": 1,
            "originText": message,
            "peerText": message,
            "originId": state.get("originId"),
            "peerId": state.get("peerId"),
            "originEvents": 1,
            "peerEvents": 1,
            "originRepair": 0,
            "peerRepair": 0,
            "originOpens": 1,
            "peerOpens": 1,
        }:
            raise AssertionError(f"user-turn-two-pane: unexpected projection state {state!r}")
        if not state["originId"] or state["originId"] != state["peerId"]:
            raise AssertionError(f"user-turn-two-pane: canonical ids diverged {state!r}")
        if node.history_requests != history0 or node.events_requests != events0:
            raise AssertionError(
                "user-turn-two-pane: normal send caused REST/redial fan-out "
                f"(history={node.history_requests - history0}, events={node.events_requests - events0})"
            )
        return "RECOVERY-READY-USERTURN-panes2-rows1-events1-history0-redial0"
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_tool_turn_two_pane(chrome: str) -> str:
    """Two accepted TOOL rows reach two panes without REST/redial fan-out."""

    from tests._sse_recovery_server import bash_toolcall_script, final_text_script

    call_id = "browser-reused-tool-id"
    first_sentinel = "TOOL_ONE_SENTINEL"
    second_sentinel = "TOOL_TWO_SENTINEL"
    node = _boot_node()
    ws_id = node.create_workstream(
        bash_toolcall_script(call_id, f"printf {first_sentinel}"),
        bash_toolcall_script(call_id, f"printf {second_sentinel}"),
        final_text_script("tool projection acknowledged"),
        name="browser-tool-turn-two-pane",
    )
    profile = Path(_scratch()) / "chrome-tool-turn-two-pane"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=tool-turn-two-pane"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        opened = _poll_until(
            lambda: (
                (cdp.evaluate("window.__streamOpen || 0") or 0) == 1
                and (cdp.evaluate("window.__peerStreamOpen || 0") or 0) == 1
            ),
            20,
            0.1,
        )
        if not opened:
            diagnostics = cdp.evaluate(
                "({originOpens:window.__streamOpen||0,"
                "peerOpens:window.__peerStreamOpen||0,"
                "originUrl:window.__pane&&window.__pane.evtSource&&window.__pane.evtSource.url,"
                "peerUrl:window.__peerPane&&window.__peerPane.evtSource&&"
                "window.__peerPane.evtSource.url,title:document.title})"
            )
            raise AssertionError(
                "tool-turn-two-pane: both initial streams did not open once "
                f"(browser={diagnostics!r}, history={node.history_requests}, "
                f"events={node.events_requests})"
            )

        capability_urls = cdp.evaluate(
            "({origin: window.__pane.evtSource.url, peer: window.__peerPane.evtSource.url})"
        )
        for name in ("origin", "peer"):
            if "tool_turn=1" not in capability_urls[name]:
                raise AssertionError(
                    f"tool-turn-two-pane: capability missing from {capability_urls!r}"
                )

        # Count reducer deliveries separately from DOM convergence. Each tool
        # emits a preliminary receipt and one accepted replacement; only the
        # accepted frames are the durable projection under test.
        cdp.evaluate(
            "window.__originAcceptedToolEvents=[]; window.__peerAcceptedToolEvents=[]; "
            "window.__originRepairEvents=0; window.__peerRepairEvents=0; "
            "const oh=window.__pane.handleEvent.bind(window.__pane); "
            "window.__pane.handleEvent=function(e){"
            "if(e&&e.type==='tool_result'&&e.accepted===true)"
            "window.__originAcceptedToolEvents.push({id:e.call_id,name:e.name,output:e.output});"
            "if(e&&e.type==='replay_truncated')window.__originRepairEvents++;"
            "return oh(e);}; "
            "const ph=window.__peerPane.handleEvent.bind(window.__peerPane); "
            "window.__peerPane.handleEvent=function(e){"
            "if(e&&e.type==='tool_result'&&e.accepted===true)"
            "window.__peerAcceptedToolEvents.push({id:e.call_id,name:e.name,output:e.output});"
            "if(e&&e.type==='replay_truncated')window.__peerRepairEvents++;"
            "return ph(e);}; true"
        )
        history0 = node.history_requests
        events0 = node.events_requests
        cdp.evaluate("window.__sendProjectedUserTurn('run two reused-id tools')")
        node.wait_turn(ws_id, timeout=45)

        ready_expr = (
            "window.__originAcceptedToolEvents.length===2 && "
            "window.__peerAcceptedToolEvents.length===2 && "
            f"window.__pane.messagesEl.querySelectorAll('.conv-row[data-call-id=\"{call_id}\"]').length===2 && "
            f"window.__peerPane.messagesEl.querySelectorAll('.conv-row[data-call-id=\"{call_id}\"]').length===2"
        )
        if not _poll_until(lambda: bool(cdp.evaluate(ready_expr)), 15, 0.05):
            raise AssertionError("tool-turn-two-pane: canonical rows did not converge twice")
        time.sleep(0.5)

        state = cdp.evaluate(
            "(()=>{const project=(p)=>Array.from(p.messagesEl.querySelectorAll('.conv-batch'))"
            f".filter((b)=>b.querySelector('.conv-row[data-call-id=\"{call_id}\"]'))"
            ".map((b)=>({outputs:Array.from(b.querySelectorAll('.tool-output'))"
            ".map((o)=>o.textContent)}));"
            "return {origin:project(window.__pane),peer:project(window.__peerPane),"
            "originEvents:window.__originAcceptedToolEvents,"
            "peerEvents:window.__peerAcceptedToolEvents,"
            "originRepair:window.__originRepairEvents,peerRepair:window.__peerRepairEvents,"
            "originOpens:window.__streamOpen,peerOpens:window.__peerStreamOpen};})()"
        )
        for pane_name in ("origin", "peer"):
            batches = state[pane_name]
            if len(batches) != 2:
                raise AssertionError(f"tool-turn-two-pane: {pane_name} batches={batches!r}")
            rendered = ["\n".join(batch["outputs"]) for batch in batches]
            if not all(
                expected in rendered[index] and other not in rendered[index]
                for index, (expected, other) in enumerate(
                    ((first_sentinel, second_sentinel), (second_sentinel, first_sentinel))
                )
            ):
                raise AssertionError(
                    f"tool-turn-two-pane: {pane_name} occurrence outputs crossed {rendered!r}"
                )
            events = state[f"{pane_name}Events"]
            if [event["id"] for event in events] != [call_id, call_id]:
                raise AssertionError(
                    f"tool-turn-two-pane: {pane_name} event ids diverged {events!r}"
                )
            event_outputs = [event["output"] for event in events]
            if not all(
                expected in event_outputs[index] and other not in event_outputs[index]
                for index, (expected, other) in enumerate(
                    ((first_sentinel, second_sentinel), (second_sentinel, first_sentinel))
                )
            ):
                raise AssertionError(
                    f"tool-turn-two-pane: {pane_name} accepted outputs crossed {events!r}"
                )
        if state["originRepair"] != 0 or state["peerRepair"] != 0:
            raise AssertionError(f"tool-turn-two-pane: unexpected repair {state!r}")
        if state["originOpens"] != 1 or state["peerOpens"] != 1:
            raise AssertionError(f"tool-turn-two-pane: stream reopened {state!r}")
        if node.history_requests != history0 or node.events_requests != events0:
            raise AssertionError(
                "tool-turn-two-pane: normal tool rows caused REST/redial fan-out "
                f"(history={node.history_requests - history0}, "
                f"events={node.events_requests - events0})"
            )
        return "RECOVERY-READY-TOOLTURN-panes2-rows2-events2-history0-redial0"
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def _wait_state(node: Any, ws_id: str, state: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if node.ws_state(ws_id) == state:
            return
        time.sleep(0.1)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(8)
        except subprocess.TimeoutExpired:
            proc.kill()


def keep_open(port: int) -> None:
    """Boot the node + storm ws and serve the page for manual inspection."""
    node = _boot_node(port=port)
    ws_id = node.create_workstream(*_storm_scripts(), name="manual-storm")
    print(f"node: {node.base_url}")
    print(f"cookie: turnstone_auth_server={node.token}")
    print(f"page:  {node.base_url}/recovery?ws_id={ws_id}&scenario=storm&rows=4")
    print("set the cookie for this origin, then open the page. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        node.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--scenario",
        choices=[
            "storm",
            "restart",
            "coord-restart",
            "fail-refetch",
            "stale-ref-reload",
            "rewind-window",
            "rewind-failed-window",
            "stale-backstop",
            "hidden-retry",
            "await-window-gate",
            "destroy-invalidation",
            "reconnect-in-await",
            "coord-rewind-window",
            "coord-rewind-failed-window",
            "coord-stale-backstop",
            "coord-heal-midturn",
            "coord-hidden-retry",
            "coord-orphan-rewind",
            "coord-joined-flight",
            "handoff-repair-budget",
            "coord-handoff-repair-budget",
            "user-turn-two-pane",
            "tool-turn-two-pane",
            "roster-restart",
            "roster-restart-native",
            "both",
            "all",
        ],
        default="all",
        help="'both' = A+B (legacy alias); 'all' runs every scenario",
    )
    ap.add_argument("--keep-open", type=int, metavar="PORT", help="serve the storm page, no CDP")
    args = ap.parse_args()

    if args.keep_open:
        keep_open(args.keep_open)
        return

    chrome = _find_chrome()
    if chrome is None:
        print("recovery_e2e: no chrome/chromium on PATH — see the module docstring runbook")
        raise SystemExit(2)

    failures = 0
    if args.scenario in ("storm", "both", "all"):
        verdict = run_storm(chrome)
        print(f"scenario A (storm):   {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("restart", "both", "all"):
        verdict = run_restart(chrome)
        print(f"scenario B (restart): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("coord-restart", "all"):
        verdict = run_coord_restart(chrome)
        print(f"scenario C (coord):   {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("fail-refetch", "all"):
        verdict = run_fail_refetch(chrome)
        print(f"scenario D (failref): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("stale-ref-reload", "all"):
        verdict = run_stale_ref(chrome)
        print(f"scenario E1 (staleref): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("rewind-window", "all"):
        verdict = run_rewind_window(chrome)
        print(f"scenario E2 (rewindwin): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("rewind-failed-window", "all"):
        verdict = run_rewind_failed_window(chrome)
        print(f"scenario E3 (rewindfail): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("stale-backstop", "all"):
        verdict = run_stale_backstop(chrome)
        print(f"scenario E4 (stalebackstop): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("hidden-retry", "all"):
        verdict = run_hidden_retry(chrome)
        print(f"scenario E5 (hiddenretry): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("await-window-gate", "all"):
        verdict = run_await_window_gate(chrome)
        print(f"scenario E6 (awaitgate): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("destroy-invalidation", "all"):
        verdict = run_destroy_invalidation(chrome)
        print(f"scenario E7 (destroyinval): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("reconnect-in-await", "all"):
        verdict = run_reconnect_in_await(chrome)
        print(f"scenario E8 (reconnectawait): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("coord-rewind-window", "all"):
        verdict = run_coord_rewind_window(chrome)
        print(f"scenario G1 (coord-rewindwin): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("coord-rewind-failed-window", "all"):
        verdict = run_coord_rewind_failed_window(chrome)
        print(f"scenario G2 (coord-rewindfail): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("coord-stale-backstop", "all"):
        verdict = run_coord_stale_backstop(chrome)
        print(f"scenario G3 (coord-stalebackstop): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("coord-heal-midturn", "all"):
        verdict = run_coord_heal_midturn(chrome)
        print(f"scenario G4 (coord-healmidturn): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("coord-hidden-retry", "all"):
        verdict = run_coord_hidden_retry(chrome)
        print(f"scenario G5 (coord-hiddenretry): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("coord-orphan-rewind", "all"):
        verdict = run_coord_orphan_rewind(chrome)
        print(f"scenario G6 (coord-orphanrewind): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("coord-joined-flight", "all"):
        verdict = run_coord_joined_flight(chrome)
        print(f"scenario G7 (coord-joinedflight): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("handoff-repair-budget", "all"):
        verdict = run_handoff_repair_budget(chrome)
        print(f"scenario H1 (handoff-budget): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("coord-handoff-repair-budget", "all"):
        verdict = run_handoff_repair_budget(chrome, coordinator=True)
        print(f"scenario H2 (coord-handoff-budget): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("user-turn-two-pane", "all"):
        verdict = run_user_turn_two_pane(chrome)
        print(f"scenario I1 (user-turn-two-pane): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("tool-turn-two-pane", "all"):
        verdict = run_tool_turn_two_pane(chrome)
        print(f"scenario I2 (tool-turn-two-pane): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("roster-restart", "all"):
        verdict = run_roster_restart(chrome)
        print(f"scenario F1 (roster-manual): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    if args.scenario in ("roster-restart-native", "all"):
        verdict = run_roster_restart_native(chrome)
        print(f"scenario F2 (roster-native): {verdict}")
        failures += 0 if verdict.startswith("RECOVERY-READY") else 1
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
