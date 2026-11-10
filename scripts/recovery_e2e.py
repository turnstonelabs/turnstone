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
    python3 scripts/recovery_e2e.py --scenario slow-history      # E9 (#905)
    python3 scripts/recovery_e2e.py --scenario retry-running-batch # E10
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
    python3 scripts/recovery_e2e.py --scenario es-contract  # the EventSource contract, real browser
    python3 scripts/recovery_e2e.py --scenario both   # A+B only (legacy)
    python3 scripts/recovery_e2e.py --keep-open 8971  # serve the storm page
                                                      # for manual inspection
    python3 scripts/recovery_e2e.py --control hidden-retry  # one negative control
    python3 scripts/recovery_e2e.py --controls        # every negative control
    python3 scripts/recovery_e2e.py --check-controls  # control needles only, no browser

NEGATIVE CONTROLS (``CONTROLS``): each runs one scenario against a mutated copy
of a served client module, held in memory and served by the fault layer, and
passes only when the scenario stamps the failing verdict recorded for it.  The
contract is at the "Negative controls" section near the end of this file.

THE PANE ADAPTER: each pane page carries one delimited adapter block
(``window.__adapter``).  Every read of pane state beyond the public DOM and
controller, by page code or by a runner-side expression, goes through it, and
stream facts (opens, ``replay_truncated`` frames) are counted there per pane at
the transport rather than by wrapping the pane's event dispatch.

The #890 guard-before-wipe family (against the interactive pane; the
coordinator ports are the G family below):

Scenario D (fail-refetch): clones B, but arms one forced /history 500
(``RecoveryServer.fail_history``) just before the show edge so the first
truncated resync FAILS.  Asserts the failed fetch is a DOM/ref no-op — the
pre-restart rows stay, no empty-state, sentinel un-healed — with the backend
proof ``history_fail_remaining == 0``; then the connect-chokepoint retry's
second /history heals it.  Stamps ``RECOVERY-READY-FAILFETCH-stale1-healed1``.

Scenario E1 (stale-ref-reload): turn 1's events stop reaching the pane right
after its first content (as a mid-content drop would), leaving a live
assistant-bubble ref; a ``history_resync`` then runs the handoff repair, a
same-ws UNARMED reload, which must reset it so the next turn's text builds a
FRESH bubble instead of concatenating into the stale one.  Stamps
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

Scenario E9 (slow-history): a /history slower than the first deadline (#905).
Every /history is held 20 s; a rewind's clear_ui refetch expires at 15 s and
the one stale retry, with the deadline doubled to 30 s, outlasts the hold and
renders the rewound transcript.  Stamps ``RECOVERY-READY-SLOWHIST-rows1-latch0-hist2``.

Scenario E10 (retry-running-batch): an ordinary Retry whose regenerated turn
starts a tool batch before the clear_ui refetch's /history answers, mid-batch,
with a cursor.  The refetch is promoted to a seeded render: the trimmed history
renders and the stream reopens at the cursor, whose replay shows the batch once.
Stamps ``RECOVERY-READY-RETRYBATCH-replaced0-latch0-batch1-cursor1-busy1-reopen1``.

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
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

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
# Scenario E10's replaced reply: the text Retry must remove from the transcript.
REPLACED_SENTINEL = "REPLACED-c9d1"
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

# Runner-side reads of pane state go through the page's adapter block (``window.__adapter``),
# never a pane field.  _OPENS_JS and _OPEN_JS work on both pane pages (the coordinator page's
# accessors ignore the pane argument: one pane per page); the opens reads answer 0 on a page whose
# module has not registered its pane yet (an unregistered pane makes the accessor throw).
# _PEER_OPENS_JS needs the two-pane page.  _STALE_JS, _QUIESCED_JS and _RELEASED_JS are
# interactive-only: the coordinator keeps that state closure-private, and its block throws for
# them.
_OPENS_JS = "(window.__adapter && window.__pane ? window.__adapter.opens(window.__pane) : 0)"
_OPEN_JS = "window.__adapter.transportOpen(window.__pane)"
_PEER_OPENS_JS = (
    "(window.__adapter && window.__peerPane ? window.__adapter.opens(window.__peerPane) : 0)"
)
_STALE_JS = "window.__adapter.historyStale(window.__pane)"
_QUIESCED_JS = "window.__adapter.isQuiesced(window.__pane)"
_RELEASED_JS = "window.__adapter.transportReleased(window.__pane)"

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
    <script>
      // ==== PANE ADAPTER (interactive page) ==================================
      // The ONLY code on this page, or in the runner's evaluated expressions,
      // that reads Pane internals.  Scenario code calls window.__adapter; when
      // the pane's stream/recovery internals change, this block changes and
      // nothing else should have to.  Stream state comes from the pane's sync
      // core snapshot (pane.sync.inspect()); render state from a few pane
      // fields.  Public pane surface (messagesEl, el, busy, inputEl,
      // sendMessage, wsId) and the factory controller's connect()/destroy()
      // stay direct.
      //
      // Stream facts are counted HERE, per pane, at the transport: never by
      // wrapping the pane's event dispatch, because transport events
      // (replay_truncated, stream_overflow, history_resync) need not reach the
      // pane's reducer.  The attribution lives here too: on the two-pane page
      // both panes dial byte-identical URLs, so a page-level counter cannot
      // tell them apart.  Each EventSource the page constructs is credited to
      // the registered pane holding it (this classic script runs before the
      // module, so every pane connect constructs the wrapper).
      (function () {
        const RealES = window.EventSource;
        const panes = [];
        const streams = [];
        const openHooks = new Map();

        function ownerOf(rec) {
          if (!rec.pane) {
            for (const p of panes) {
              if (p.sync && p.sync.inspect().source === rec.es) {
                rec.pane = p;
                break;
              }
            }
          }
          return rec.pane;
        }
        function total(pane, key) {
          let n = 0;
          for (const rec of streams) if (ownerOf(rec) === pane) n += rec[key];
          return n;
        }
        function AttributedES(url, opts) {
          const es = new RealES(url, opts);
          const rec = { es: es, pane: null, opens: 0, truncated: 0 };
          streams.push(rec);
          // The pane stores the source as soon as construction returns.
          queueMicrotask(function () {
            ownerOf(rec);
          });
          es.addEventListener("open", function () {
            rec.opens += 1;
            const pane = ownerOf(rec);
            const opens = pane ? total(pane, "opens") : 0;
            (openHooks.get(pane) || []).forEach(function (fn) {
              fn(opens);
            });
          });
          es.addEventListener("message", function (e) {
            ownerOf(rec);
            try {
              const d = JSON.parse(e.data);
              if (d && d.type === "replay_truncated") rec.truncated += 1;
            } catch (_) {}
          });
          return es;
        }
        AttributedES.prototype = RealES.prototype;
        AttributedES.CONNECTING = RealES.CONNECTING;
        AttributedES.OPEN = RealES.OPEN;
        AttributedES.CLOSED = RealES.CLOSED;
        window.EventSource = AttributedES;

        // Every accessor that reads pane state throws when the field behind it
        // is missing, so a renamed internal fails the run loudly instead of
        // reading as "not set" and letting a detector pass untested.
        function need(pane, name, accessor) {
          if (!pane || typeof pane !== "object" || !(name in pane)) {
            throw new Error(
              "adapter." + accessor + ": pane has no " + name +
                " (pane internals changed; update the adapter block)",
            );
          }
          return pane[name];
        }
        function needMethod(pane, name, accessor) {
          const method = need(pane, name, accessor);
          if (typeof method !== "function") {
            throw new Error("adapter." + accessor + ": pane." + name + " is not a method");
          }
          return method;
        }
        function snapshot(pane, accessor) {
          const sync = need(pane, "sync", accessor);
          if (!sync || typeof sync.inspect !== "function") {
            throw new Error(
              "adapter." + accessor + ": pane.sync has no inspect()" +
                " (pane internals changed; update the adapter block)",
            );
          }
          return sync.inspect();
        }
        function needRegistered(pane, accessor) {
          if (panes.indexOf(pane) === -1) {
            throw new Error("adapter." + accessor + ": pane was never registered");
          }
        }

        window.__adapter = {
          // Credit this pane's streams to it; every pane on the page registers.
          // Attribution reads the source in the pane's sync snapshot, so
          // register checks it.
          register(pane) {
            snapshot(pane, "register");
            panes.push(pane);
            return pane;
          },
          // First paint of a bare-class pane: history, then the stream.
          start(pane) {
            need(pane, "sync", "start").start();
          },
          // Intercept CONTENT dispatch: fn(evt, dispatch) runs for every event
          // the pane's reducer receives (live or flushed) and calls
          // dispatch(evt) to deliver one.
          wrapDispatch(pane, fn) {
            const dispatch = needMethod(pane, "handleEvent", "wrapDispatch").bind(pane);
            pane.handleEvent = function (evt) {
              return fn(evt, dispatch);
            };
          },
          // fn(opens) after every stream open credited to this pane.
          onOpen(pane, fn) {
            needRegistered(pane, "onOpen");
            if (!openHooks.has(pane)) openHooks.set(pane, []);
            openHooks.get(pane).push(fn);
          },
          // Stream opens credited to the pane, native reconnects included.
          opens(pane) {
            needRegistered(pane, "opens");
            return total(pane, "opens");
          },
          // replay_truncated frames received on the pane's streams, counted at
          // arrival (before any queue flush, and even if a queue is dropped).
          truncatedSeen(pane) {
            needRegistered(pane, "truncatedSeen");
            return total(pane, "truncated");
          },
          // URL of the source the pane holds now; null when it holds none.
          sourceUrl(pane) {
            const source = snapshot(pane, "sourceUrl").source;
            return source ? source.url : null;
          },
          // The pane holds a source and it is OPEN.  CONNECTING (a native
          // reconnect pending) is false.
          transportOpen(pane) {
            const source = snapshot(pane, "transportOpen").source;
            return !!source && source.readyState === RealES.OPEN;
          },
          // The pane holds no source, or holds one that is CLOSED.  CONNECTING
          // is false here too, so transportOpen and transportDown are NOT
          // complements: a reconnecting source is neither.
          transportDown(pane) {
            const source = snapshot(pane, "transportDown").source;
            return !source || source.readyState === RealES.CLOSED;
          },
          // The pane holds no source at all: close-on-hide and a disconnect
          // release it.  A held source in any state, CLOSED included, is false.
          transportReleased(pane) {
            return snapshot(pane, "transportReleased").source === null;
          },
          // The transcript-staleness latch is set: rewind and edit stay gated
          // until a successful history render clears it.
          historyStale(pane) {
            return snapshot(pane, "historyStale").historyStale === true;
          },
          // A seedless refetch is in flight and live events queue behind it.
          isQuiesced(pane) {
            return snapshot(pane, "isQuiesced").quiesced === true;
          },
          // The pane's document-level visibilitychange listener is registered.
          visibilityHooked(pane) {
            return snapshot(pane, "visibilityHooked").listening === true;
          },
          // The reply bubble's body ref is set.  The field exists only once the
          // pane has painted a bubble; E1 asks right after one.
          liveBubble(pane) {
            return need(pane, "currentAssistantBodyEl", "liveBubble") != null;
          },
          // The pane holds a context reading for this task-agent call id.
          agentContextHeld(pane, callId) {
            return need(pane, "_agentContexts", "agentContextHeld").has(callId);
          },
          // The tool row the pane resolves for this call id, or null.
          toolRow(pane, callId) {
            return needMethod(pane, "_toolRow", "toolRow").call(pane, callId);
          },
        };
      })();
      // ==== END PANE ADAPTER =================================================
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
      // Every pane on this page is registered with the adapter (block above).
      const adapter = window.__adapter;
      adapter.register(pane);
      window.__pane = pane;
      let peerPane = null;
      if (
        scenario === "user-turn-two-pane" ||
        scenario === "tool-turn-two-pane"
      ) {
        peerPane = new InteractivePane(wsId, { base: "" });
        peerPane.wsId = wsId;
        adapter.register(peerPane);
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
        let delayedReusedParents = [];
        window.__reuseDelayArmed = false;
        window.__reuseContextBeforeParent = false;
        window.__reuseRelinked = false;
        window.__armReusedParentDelay = function () {
          delayedReusedParents = [];
          window.__reuseDelayArmed = true;
        };
        adapter.wrapDispatch(pane, function (evt, dispatch) {
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
          const result = dispatch(evt);
          if (
            window.__reuseDelayArmed &&
            evt &&
            evt.type === "agent_context" &&
            evt.parent_call_id === "task1" &&
            delayedReusedParents.length
          ) {
            window.__reuseContextBeforeParent = adapter.agentContextHeld(
              pane,
              "task1",
            );
            window.__reuseDelayArmed = false;
            const delayed = delayedReusedParents;
            delayedReusedParents = [];
            delayed.forEach((parentEvt) => dispatch(parentEvt));
            const row = adapter.toolRow(pane, "task1");
            window.__reuseRelinked = !!(
              row &&
              row.querySelector(
                '.conv-agent[data-state="running"] .conv-agent-context:not([hidden])',
              )
            );
          }
          return result;
        });
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
        // The pane's SSE is open (its open event fired), so the listener is
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

      // Drive /send once the stream is live.
      adapter.onOpen(pane, function (opens) {
        if (scenario === "storm" && !agentResume) sendOnce("run the storm");
        else if (
          (scenario === "restart" ||
            scenario === "fail-refetch" ||
            scenario === "stale-ref-reload") &&
          opens === 1
        )
          sendOnce("run a turn");
        // rewind-window drives its turns SERVER-side before navigation, when
        // no page listener exists; the initial /history paints those rows, so
        // the page never auto-sends there.
      });

      // stale-ref-reload: from the instant the first assistant `content`
      // paints, hold every later event back from the pane until the runner
      // releases the hold (once the stream that carried them is gone).
      function installContentHold() {
        window.__holdStarted = 0;
        window.__holding = false;
        adapter.wrapDispatch(pane, function (ev, dispatch) {
          if (window.__holding) return undefined;
          const r = dispatch(ev);
          // The fix's regression trap needs a NON-null streaming ref
          // surviving into an unarmed same-ws reload.  The scripted provider
          // emits `content` atomically and the segment's stream_end frame
          // (which nulls those refs) follows with no pollable gap, so the
          // hold must RIDE the content event: once it has been applied (the
          // live bubble now set), nothing after it reaches the pane, stream_end
          // included — leaving exactly the stale ref a mid-content drop would.
          if (!window.__holdStarted && ev && ev.type === "content") {
            window.__holdStarted = 1;
            window.__holding = true;
            // Non-vacuity guard: record that the hold genuinely left a
            // live streaming ref (the stale-ref precondition).  If the
            // segment's stream_end had already nulled it, the reload reset
            // is a no-op and a green verdict would be meaningless — so
            // __verifyStaleRef fails loudly when this is false.
            window.__staleRefWasSet = adapter.liveBubble(pane);
          }
          return r;
        });
      }

      // First paint the REAL way: /history then connect SSE.  Under the
      // factory that load is owned by connect() (which also seeds the
      // empty state), and going through it is the point of E7 — the
      // in-flight load destroy() must invalidate is the one connect()
      // starts.
      if (ctl) ctl.connect();
      else {
        adapter.start(pane);
        if (peerPane) adapter.start(peerPane);
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
          const trunc = adapter.truncatedSeen(pane);
          const ok =
            c.topLevel >= 1 &&
            idle &&
            !disc &&
            healed &&
            trunc >= 1;
          document.title = ok
            ? "RECOVERY-READY-RESTART-rows" + c.topLevel + "-trunc" + trunc
            : "RECOVERY-FAILED-RESTART-rows" + c.topLevel +
              "-busy" + (pane.busy ? 1 : 0) + "-disc" + (disc ? 1 : 0) +
              "-healed" + (healed ? 1 : 0) + "-trunc" + trunc;
        };
      } else if (scenario === "fail-refetch") {
        // Scenario D — a FAILED truncated-resync /history (the runner arms
        // node.fail_history(1) just before the show edge).  PHASE 1: the
        // failed fetch is a DOM/ref no-op (#890 guard-before-wipe) — the
        // pre-restart rows survive, no empty-state is appended, and the
        // hidden-window sentinel is NOT healed yet.  PHASE 2: the connect
        // chokepoint's retry redraws replay_truncated and the SECOND
        // /history succeeds, healing the gap.
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
            adapter.truncatedSeen(pane) >= 1;
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
          // On failure the phase-1 capture's own fields ride along (p1rows,
          // p1empty), so a stale0 says which part of the preserve broke.
          document.title = ok
            ? "RECOVERY-READY-FAILFETCH-stale1-healed1"
            : "RECOVERY-FAILED-FAILFETCH-stale" +
              (stale.ok ? 1 : 0) +
              "-p1rows" +
              (stale.rows || 0) +
              "-p1empty" +
              (stale.emptyState ? 1 : 0) +
              "-healed" +
              (healed ? 1 : 0) +
              "-rows" +
              rows +
              "-busy" +
              (pane.busy ? 1 : 0) +
              "-trunc" +
              adapter.truncatedSeen(pane);
        };
      } else if (scenario === "stale-ref-reload") {
        // Scenario E1 — regression trap for the #890 streaming-ref reset.
        // installContentHold's hold left a stale live bubble; the runner then
        // emits history_resync (whose handoff repair is an unarmed same-ws
        // reload) and drives turn 2.  Phase 1 (post reload, pre turn-2)
        // captures the FIRST assistant bubble element + its text;
        // __verifyStaleRef proves turn 2's sentinel landed in a DIFFERENT
        // (fresh) bubble and the captured bubble is byte-for-byte unchanged.
        // Pre-fix the unarmed reload kept the stale ref and turn 2
        // concatenated into the old bubble.
        installContentHold();
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
          const visNull = !adapter.visibilityHooked(pane);
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
      // ==== PANE ADAPTER (coordinator page) ==================================
      // The ONLY code on this page, or in the runner's evaluated expressions,
      // that observes the coordinator pane beyond its public chrome and its
      // returned controller.  The pane's handleEvent, cursor and latch are
      // closure-private and its chrome deliberately builds NO header/SSE
      // indicator, so today every fact here is read off the wire by wrapping
      // EventSource BEFORE any module loads (classic script = runs before the
      // deferred module set, so the pane's connectSSE always constructs the
      // wrapper).  One pane per page, so the counts are that pane's; the
      // accessors take the pane anyway, for one call shape across pages:
      //   opens(pane)         — stream opens (drives the send; a listener is
      //                         registered before /send so no events are
      //                         missed);
      //   truncatedSeen(pane) — replay_truncated frames (the envelope);
      //   idFrames(pane)      — id-bearing frames, i.e. exactly the frames
      //                         that advance the pane's reconnect cursor (same
      //                         ``!= null && !== ""`` guard as the pane) — the
      //                         hide fires only after this proves a live
      //                         mid-turn cursor;
      //   sourceUrl(pane)     — URL of the most recently constructed stream;
      //   transportOpen(pane) — the most recently constructed stream is OPEN
      //                         (CONNECTING is false);
      //   transportDown(pane) — no stream constructed yet, or the most recent
      //                         one is CLOSED (CONNECTING is false here too, so
      //                         the two are NOT complements).
      // The pane's latch, quiesce and source field are not observable from
      // outside, so historyStale, isQuiesced, transportReleased and
      // visibilityHooked throw here rather than answer.
      (function () {
        const RealES = window.EventSource;
        let opens = 0;
        let truncated = 0;
        let idFrames = 0;
        let lastUrl = null;
        let lastSource = null;
        function unobservable(accessor) {
          return function () {
            throw new Error(
              "adapter." + accessor + ": not observable on the coordinator page " +
                "(the pane keeps this state closure-private)",
            );
          };
        }
        function CountingES(url, opts) {
          lastUrl = String(url);
          const es = new RealES(url, opts);
          lastSource = es;
          es.addEventListener("open", function () {
            opens += 1;
          });
          es.addEventListener("message", function (e) {
            if (e.lastEventId != null && e.lastEventId !== "") {
              idFrames += 1;
            }
            try {
              const d = JSON.parse(e.data);
              if (d && d.type === "replay_truncated") truncated += 1;
            } catch (_) {}
          });
          return es;
        }
        CountingES.prototype = RealES.prototype;
        CountingES.CONNECTING = RealES.CONNECTING;
        CountingES.OPEN = RealES.OPEN;
        CountingES.CLOSED = RealES.CLOSED;
        window.EventSource = CountingES;

        window.__adapter = {
          opens(_pane) {
            return opens;
          },
          truncatedSeen(_pane) {
            return truncated;
          },
          idFrames(_pane) {
            return idFrames;
          },
          sourceUrl(_pane) {
            return lastUrl;
          },
          transportOpen(_pane) {
            return !!lastSource && lastSource.readyState === RealES.OPEN;
          },
          transportDown(_pane) {
            return !lastSource || lastSource.readyState === RealES.CLOSED;
          },
          historyStale: unobservable("historyStale"),
          isQuiesced: unobservable("isQuiesced"),
          transportReleased: unobservable("transportReleased"),
          visibilityHooked: unobservable("visibilityHooked"),
        };
      })();
      // ==== END PANE ADAPTER =================================================
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

      // Drive /send once the stream has OPENED at the transport (the
      // adapter's opens — the pane's listener is registered by then, so no
      // events are missed).
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
          if (window.__adapter.opens(pane) >= 1) {
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
        // stream must have re-opened after the show (two opens), the
        // status bar must not be stuck dim (.ws-sb-disconnected removed by
        // the post-recovery onopen), and the tool rows must be intact.
        const messages = document.getElementById("coord-messages");
        const rows = messages
          ? messages.querySelectorAll(".conv-row[data-call-id]").length
          : 0;
        const reopened = window.__adapter.opens(pane) >= 2;
        const trunc = window.__adapter.truncatedSeen(pane);
        const disc =
          document.querySelector("#coord-status-bar.ws-sb-disconnected") !== null;
        const healed =
          healedSentinel !== "" &&
          ((messages && messages.textContent) || "").includes(healedSentinel);
        const ok = rows >= 1 && reopened && !disc && healed && trunc >= 1;
        document.title = ok
          ? "RECOVERY-READY-COORD-rows" + rows + "-trunc" + trunc
          : "RECOVERY-FAILED-COORD-rows" + rows +
            "-reopened" + (reopened ? 1 : 0) +
            "-disc" + (disc ? 1 : 0) + "-healed" + (healed ? 1 : 0) +
            "-trunc" + trunc;
      };
    </script>
  </body>
</html>
"""

# ---------------------------------------------------------------------------
# The EventSource contract page — served same-origin by the node at
# /es-contract.  It runs tests._js_harness_helpers.EVENT_SOURCE_CONTRACT, the
# same script the node tests run against the fake EventSource, against the
# browser's real one.  The driver acts through the fault layer's controllable
# streams (tests._sse_recovery_server.ContractStreams): every control is a
# fetch that returns once the server has written to the stream socket, and
# ``settle`` then gives the browser a fixed beat to process it (everything is on
# loopback).  The contract text is inserted when the page is served, never
# copied here.
# ---------------------------------------------------------------------------

ES_CONTRACT_PAGE_HTML = r"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>es contract</title>
  </head>
  <body>
    <script>
/*EVENT_SOURCE_CONTRACT*/
    </script>
    <script>
      (function () {
        const SETTLE_MS = 300;
        async function control(name, action, frame) {
          const r = await fetch(
            "/es-contract/control/" + encodeURIComponent(name) + "/" + action,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(frame || {}),
            },
          );
          if (!r.ok) {
            throw new Error("contract control " + action + " " + name + ": HTTP " + r.status);
          }
          return r.json();
        }
        const driver = {
          url: (name) => "/es-contract/stream/" + encodeURIComponent(name),
          open: (name) => control(name, "open"),
          send: (name, frame) => control(name, "send", frame),
          drop: (name) => control(name, "drop"),
          fail: (name) => control(name, "fail"),
          lastEventIdHeader: async (name) => {
            const r = await fetch(
              "/es-contract/control/" + encodeURIComponent(name) + "/last-event-id",
            );
            return (await r.json()).value;
          },
          settle: () => new Promise((resolve) => setTimeout(resolve, SETTLE_MS)),
        };
        checkEventSourceContract(driver).then(
          (checked) => {
            window.__esContractChecked = checked;
            document.title = "RECOVERY-READY-ESCONTRACT-" + checked.join("-");
          },
          (err) => {
            document.title = "RECOVERY-FAILED-ESCONTRACT: " + ((err && err.message) || String(err));
          },
        );
      })();
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
    if _CONTROL_RUN is not None:
        # A control must load the mutated module, never a copy the reused
        # profile cached from an earlier run (the /coord-static mount sends no
        # Cache-Control, so the browser may reuse it without asking).
        shutil.rmtree(profile, ignore_errors=True)
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


def _es_contract_route() -> Any:
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    async def es_contract_page(_request: Any) -> HTMLResponse:
        from tests._js_harness_helpers import EVENT_SOURCE_CONTRACT

        return HTMLResponse(
            ES_CONTRACT_PAGE_HTML.replace("/*EVENT_SOURCE_CONTRACT*/", EVENT_SOURCE_CONTRACT)
        )

    return Route("/es-contract", es_contract_page)


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
    # ``_CONTROL_RUN``: every node a control's scenario boots, restarts
    # included, serves the control's mutated module in place of the file.
    reset_storage()
    init_storage("sqlite", path=os.path.join(_scratch(), "recovery_e2e.db"), run_migrations=True)
    run = _CONTROL_RUN
    node = RecoveryServer(
        extra_routes=[_page_route(), _es_contract_route(), *_coord_routes()],
        port=port,
        sock=sock,
        served_overrides=run.overrides if run is not None else None,
    )
    if run is not None:
        run.nodes.append(node)
    return node


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
        # (the adapter's idFrames; exactly the frames that advance the pane's
        # reconnect cursor).  The turn must also still be RUNNING server-side, or the
        # frozen cursor could sit at/above the committed counter and the
        # reconnect would take the lossless replay_ok path (trunc0).  The
        # paced bash runs >=2s, so this lands mid-turn.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            frames = cdp.evaluate(
                "(window.__adapter ? window.__adapter.idFrames(window.__pane) : 0)"
            )
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
    """Scenario E1 — regression test for the #890 streaming-ref reset.  Turn
    1's events stop reaching the pane right after its first content, leaving a
    live assistant bubble ref; a same-ws UNARMED reload (the handoff repair a
    ``history_resync`` starts) must reset it, so the NEXT turn's text builds a
    FRESH bubble instead of concatenating into the stale one.  No node restart:
    the page's hook holds every event after turn 1's content back from the
    pane, the runner emits ``history_resync`` with the repair's first /history
    forced to fail, releases the hold once that stream is gone, then drives
    turn 2 and proves the sentinel landed in a different bubble with the first
    bubble unchanged."""
    node = _boot_node()
    # Turn 1 carries assistant CONTENT (the bubble that must go stale) AND a
    # paced bash, so the turn is genuinely mid-flight when the transport drops
    # (unlike run_restart's pure-bash turn, which sets no content ref — the
    # concatenation bug is specifically about a content bubble).  The content
    # event starts the page's content hold; the bash + closing text then
    # complete server-side while it holds.
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
        # The page auto-sends turn 1; its content event trips the hold (every
        # later event of the turn stays away from the pane, mid-segment).
        # Poll the flag rather than guess a sleep.
        if not _poll_until(lambda: cdp.evaluate("window.__holdStarted === 1"), 20):
            raise AssertionError("stale-ref: mid-content hold never fired")
        # Turn 1 completes server-side while its events are held back.
        node.wait_turn(ws_id, timeout=30)
        # Arm one /history failure, then start the handoff repair: a same-ws,
        # UNARMED reload (no truncation cursor was ever recorded) that closes
        # the stream first.
        node.fail_history(1)
        node.emit_history_resync(ws_id)
        # The repair's first attempt fails; the next one renders and
        # reconnects with proof.  The held stream is gone by now, so release
        # the hold for the reconnect's events.
        if not _poll_until(lambda: node.history_fail_remaining == 0, 10, 0.05):
            raise AssertionError("stale-ref: forced /history failure never fired")
        cdp.evaluate("window.__holding = false")
        # The reconnect must be open before turn 2 (its listener catches the
        # content), and the repair's reload must have reset the stale ref.
        _poll_until(lambda: cdp.evaluate(_OPENS_JS + " >= 2"), 10)
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
            lambda: cdp.evaluate(_OPENS_JS + " >= 1"),
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
        if not _poll_until(lambda: cdp.evaluate(_QUIESCED_JS), 5, 0.05):
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
            lambda: (not cdp.evaluate(_QUIESCED_JS)) and cdp.evaluate(_ROWS_JS) == 1,
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
            lambda: cdp.evaluate("!" + _QUIESCED_JS + " && " + _STALE_JS),
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
            lambda: cdp.evaluate("!" + _QUIESCED_JS + " && " + _STALE_JS),
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
            lambda: cdp.evaluate(_ROWS_JS + " === 1 && !" + _STALE_JS),
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
        if not _poll_until(lambda: cdp.evaluate(_RELEASED_JS), 5, 0.05):
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
        latch_held = cdp.evaluate(_STALE_JS)
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
            lambda: cdp.evaluate(_ROWS_JS + " === 1 && !" + _STALE_JS),
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
        if not _poll_until(lambda: cdp.evaluate(_RELEASED_JS), 5, 0.05):
            raise AssertionError("await-window-gate: close-on-hide never dropped the transport")
        # Clear the knob for any LATER arrival; the already-held fetch is
        # sleeping on the duration it captured at arrival and serves that
        # out regardless — which is why the poll below must outlast it.
        node.delay_history(0)
        if not _poll_until(lambda: cdp.evaluate("!" + _QUIESCED_JS), 12, 0.1):
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
        latch_held = cdp.evaluate(_STALE_JS)
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
                + " === 2 && !"
                + _STALE_JS
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

    The drop and the redial are the page's close-on-hide and show edge.  A
    fetch that resolved while the tab was hidden would meet no transport,
    which the presence term already decides; what makes this the generation
    term's case is that the show edge has reopened the stream BEFORE the held
    fetch resolves, which the non-vacuity check below asserts.  With this
    choreography, stripping the generation term still stamps ``dupes2``."""
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
        # (1) Drop the transport with a close-on-hide.  The cursor freezes here.
        cdp.evaluate("window.__hide()")
        if not _poll_until(lambda: cdp.evaluate(_RELEASED_JS), 5, 0.05):
            raise AssertionError("reconnect-in-await: the transport never dropped")
        # (2) Commit a turn while the stream is DOWN, so the server holds
        #     events past the frozen cursor with no way to have delivered them.
        # Runner-side send + the real turn-complete barrier: the page's
        # stream is down, so driving this from the browser would prove
        # nothing extra and only add a timing race.
        node.send(ws_id, "gap turn")
        node.wait_turn(ws_id)
        # (3) Re-establish with the show edge, still inside the await.  This
        #     is the whole point: readyState goes back to OPEN, so only the
        #     generation moved.
        cdp.evaluate("window.__show()")
        if not _poll_until(lambda: cdp.evaluate(_OPEN_JS), 10, 0.05):
            raise AssertionError("reconnect-in-await: the show edge never reached OPEN")
        # NON-VACUITY, the leg this scenario turns on: the held fetch must
        # still be OUTSTANDING right now.  If it already resolved — a slow box
        # can push the hide/send/wait_turn/show sequence past the hold — the
        # payload landed while the transport was still down, the PRESENCE
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
            lambda: cdp.evaluate(_ROWS_JS + " === 2 && !" + _STALE_JS),
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
    reconnecting — with the pane detached and no visibility handler registered
    behind it.  Remove the bump and the ``.finally`` fires: sse1, and a handler
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
        vis_null = cdp.evaluate("!window.__adapter.visibilityHooked(window.__pane)")
        print(f"  destroy-invalidation sse_opens={sse_opens} vis_null={vis_null}")
        cdp.evaluate(f"window.__verifyDestroyInvalidation({sse_opens})")
        return _poll_title(cdp, 15)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_slow_history(chrome: str) -> str:
    """Scenario E9 — a /history slower than the first deadline still renders (#905).

    The server finishes a reconstruction even when the client aborts, so a retry bounded by the
    same 15 s only repeats the server's work and never renders.  The core doubles the bound after
    each expiry in a row (15, 30, 60, then 120 s) and resets it on any payload.

    Three completed turns => three user rows; every /history is then held 20 s at the fault
    layer.  A REAL rewind click on the second row POSTs, and its clear_ui refetch expires at 15 s:
    the latch is set and the three rows stand.  The one stale retry, about 2 s later on the open,
    idle stream, runs with a 30 s bound and outlasts the hold: the rewound transcript renders.

    DETECTOR: one user row with the latch clear, after exactly two /history requests from the
    click on.  Without the growth both attempts expire at 15 s and nothing else retries: three
    rows with the latch set (``rows3-latch1-hist2``)."""
    node, ws_id = _seed_three_completed_turns("browser-slow-history")
    profile = Path(_scratch()) / "chrome-slow-history"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=slow-history"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        if not _poll_until(lambda: cdp.evaluate(_ROWS_JS) == 3, 20, 0.2):
            raise AssertionError("slow-history: three user rows never rendered")
        # The same handoff gap E2 waits out: a click before the stream is open draws a strong
        # resync instead of the clear_ui refetch this scenario is about.
        if not _poll_until(lambda: cdp.evaluate(_OPENS_JS + " >= 1"), 10, 0.05):
            raise AssertionError("slow-history: SSE stream never opened")
        baseline = node.history_requests
        node.delay_history(20000)
        if not cdp.evaluate("window.__clickRewind(1)"):
            raise AssertionError("slow-history: second-row rewind button missing")
        if not _poll_until(lambda: node.rewind_requests == 1, 5, 0.05):
            raise AssertionError("slow-history: rewind never POSTed")
        if not _poll_until(lambda: cdp.evaluate(_QUIESCED_JS), 5, 0.05):
            raise AssertionError("slow-history: clear_ui never started its refetch")
        # 15 s to the first expiry, 2-2.5 s to the retry, 20 s of hold, plus margin.
        _poll_until(
            lambda: cdp.evaluate(_ROWS_JS + " === 1 && !" + _STALE_JS),
            50,
            0.25,
        )
        rows = cdp.evaluate(_ROWS_JS)
        stale = 1 if cdp.evaluate(_STALE_JS) else 0
        requests = node.history_requests - baseline
        node.delay_history(0)
        tag = f"SLOWHIST-rows{rows}-latch{stale}-hist{requests}"
        print(f"  slow-history rows={rows} latch={stale} history_requests={requests}")
        ok = rows == 1 and stale == 0 and requests == 2
        return f"RECOVERY-READY-{tag}" if ok else f"RECOVERY-FAILED-{tag}"
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


# Records, per /history response the page receives, whether it carried a cursor (1), did not (0)
# or had no readable JSON body (-1).  Installed by the runner after first paint; the pane's fetch
# reads the global at call time.
_RECORD_HISTORY_CURSORS_JS = r"""
(() => {
  const real = window.fetch;
  window.__historyCursors = [];
  window.fetch = async (...args) => {
    const response = await real(...args);
    if (String(args[0]).split("?")[0].endsWith("/history")) {
      response.clone().json().then(
        (data) => window.__historyCursors.push(data && data.cursor != null ? 1 : 0),
        () => window.__historyCursors.push(-1),
      );
    }
    return response;
  };
  return true;
})()
"""


def run_retry_running_batch(chrome: str) -> str:
    """Scenario E10 — an ordinary Retry whose regenerated turn starts a tool batch before the
    clear_ui refetch's /history answers.

    One completed turn is seeded before navigation; its reply is the one Retry replaces.  A REAL
    Retry click POSTs /retry: the server truncates, publishes clear_ui and re-dispatches the
    turn, a paced bash.  Every /history is held 1 s at the fault layer, so the refetch is answered
    mid-batch, where /history trims the executing tool call and returns a cursor.  The core
    promotes that refetch to a seeded render: the trimmed history renders (the replaced reply
    goes), the queue is dropped, and the stream is rebuilt at the cursor, whose replay delivers
    the batch.

    DETECTOR, read while the batch still runs (busy1), without waiting for the turn to end: the
    replaced reply is gone, the latch is clear, the batch shows once and the stream reopened once
    (reopen1).  NON-VACUITY: the page recorded a cursor on the refetch's response (cursor1).
    Removing the promotion leaves a seedless render that looks right here, because Retry's
    clear_ui precedes every frame of the regenerated turn, so its queue holds them all; only the
    reopen term sees it (``reopen0``).  The case where a seedless render loses rows (a bootstrap
    clear_ui mid-batch) is executed in the core suite."""
    from tests._sse_recovery_server import final_text_script, parallel_bash_script

    node = _boot_node()
    paced = parallel_bash_script({"r0": "for i in $(seq 1 60); do echo r-$i; sleep 0.1; done"})
    ws_id = node.create_workstream(
        final_text_script(REPLACED_SENTINEL),
        paced,
        final_text_script("regenerated turn closed"),
        name="browser-retry-running-batch",
    )
    node.send(ws_id, "list the files")
    node.wait_turn(ws_id)
    profile = Path(_scratch()) / "chrome-retry-running-batch"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    replaced_js = (
        "(window.__pane.messagesEl.textContent || '').includes("
        + json.dumps(REPLACED_SENTINEL)
        + ")"
    )
    batches_js = "window.__pane.messagesEl.querySelectorAll('.conv-batch').length"
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=retry-running-batch"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        if not _poll_until(
            lambda: cdp.evaluate(_ROWS_JS) == 1 and cdp.evaluate(replaced_js), 20, 0.2
        ):
            raise AssertionError("retry-running-batch: the seeded turn never rendered")
        if not _poll_until(lambda: cdp.evaluate(_OPENS_JS + " >= 1"), 10, 0.05):
            raise AssertionError("retry-running-batch: SSE stream never opened")
        if not cdp.evaluate(_RECORD_HISTORY_CURSORS_JS):
            raise AssertionError("retry-running-batch: could not record /history cursors")
        opens_before = cdp.evaluate(_OPENS_JS)
        node.delay_history(1000)
        clicked = cdp.evaluate(
            "(() => { const b = window.__pane.messagesEl.querySelector('.msg-retry-btn');"
            " if (!b) return false; b.click(); return true; })()"
        )
        if not clicked:
            raise AssertionError("retry-running-batch: the last reply offers no Retry")
        if not _poll_until(lambda: cdp.evaluate(_QUIESCED_JS), 5, 0.05):
            raise AssertionError("retry-running-batch: clear_ui never started its refetch")
        if not _poll_until(lambda: cdp.evaluate("window.__historyCursors.length >= 1"), 8, 0.05):
            raise AssertionError("retry-running-batch: the refetch never answered")
        node.delay_history(0)
        _poll_until(lambda: not cdp.evaluate(_QUIESCED_JS), 5, 0.05)
        _poll_until(lambda: cdp.evaluate(_OPEN_JS), 5, 0.05)
        _poll_until(lambda: cdp.evaluate(batches_js) >= 1, 3, 0.05)
        replaced = 1 if cdp.evaluate(replaced_js) else 0
        stale = 1 if cdp.evaluate(_STALE_JS) else 0
        batches = cdp.evaluate(batches_js)
        cursor = cdp.evaluate("window.__historyCursors[0]")
        busy = 1 if cdp.evaluate("window.__pane.busy") else 0
        reopen = cdp.evaluate(_OPENS_JS) - opens_before
        node.wait_turn(ws_id, timeout=30)
        tag = (
            f"RETRYBATCH-replaced{replaced}-latch{stale}-batch{batches}-cursor{cursor}"
            f"-busy{busy}-reopen{reopen}"
        )
        print(f"  retry-running-batch {tag}")
        ok = (
            replaced == 0
            and stale == 0
            and batches == 1
            and cursor == 1
            and busy == 1
            and reopen == 1
        )
        return f"RECOVERY-READY-{tag}" if ok else f"RECOVERY-FAILED-{tag}"
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
        if not _poll_until(lambda: cdp.evaluate(_OPENS_JS) >= 1, 10, 0.05):
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
        if not _poll_until(lambda: cdp.evaluate(_OPENS_JS) >= 1, 10, 0.05):
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
    if not _poll_until(lambda: cdp.evaluate(_OPENS_JS) >= 1, 10, 0.05):
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
        if not _poll_until(lambda: cdp.evaluate(_OPENS_JS) >= 1, 10, 0.05):
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
        if not _poll_until(lambda: (cdp.evaluate(_OPENS_JS) or 0) >= 1, 15, 0.05):
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
        if not _poll_until(lambda: (cdp_a.evaluate(_OPENS_JS) or 0) >= 1, 10, 0.05):
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
        if not _poll_until(lambda: cdp.evaluate(_OPENS_JS) >= 1, 15, 0.1):
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
                f"(history={node.history_requests - history0}, "
                f"events={node.events_requests - events0}, "
                f"prompt={1 if cdp.evaluate(prompt_expr) else 0})"
            )
        tokenless_stream = (
            cdp.evaluate(
                "!((window.__adapter.sourceUrl(window.__pane) || '').includes('history_token='))"
            )
            if coordinator
            else cdp.evaluate(
                "!!window.__adapter.sourceUrl(window.__pane) && "
                "!window.__adapter.sourceUrl(window.__pane).includes('history_token=')"
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
            cdp.evaluate(
                "(window.__adapter.sourceUrl(window.__pane) || '').includes('history_token=')"
            )
            if coordinator
            else cdp.evaluate(
                "!!window.__adapter.sourceUrl(window.__pane) && "
                "window.__adapter.sourceUrl(window.__pane).includes('history_token=')"
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
                (cdp.evaluate(_OPENS_JS) or 0) == 1 and (cdp.evaluate(_PEER_OPENS_JS) or 0) == 1
            ),
            20,
            0.1,
        )
        if not opened:
            diagnostics = cdp.evaluate(
                "({originOpens:window.__adapter.opens(window.__pane),"
                "peerOpens:window.__adapter.opens(window.__peerPane),"
                "originUrl:window.__adapter.sourceUrl(window.__pane),"
                "peerUrl:window.__adapter.sourceUrl(window.__peerPane),title:document.title})"
            )
            raise AssertionError(
                "user-turn-two-pane: both initial streams did not open once "
                f"(browser={diagnostics!r}, history={node.history_requests}, "
                f"events={node.events_requests})"
            )

        capability_urls = cdp.evaluate(
            "({origin: window.__adapter.sourceUrl(window.__pane),"
            " peer: window.__adapter.sourceUrl(window.__peerPane)})"
        )
        if not all("user_turn=1" in capability_urls[name] for name in ("origin", "peer")):
            raise AssertionError(f"user-turn-two-pane: capability missing from {capability_urls!r}")

        # Count reducer deliveries themselves as well as DOM rows. This catches
        # a duplicate event that happened to be hidden by DOM/event-id dedup.
        # Repair frames are transport facts: the adapter counts them per pane,
        # and the baselines start the count here.
        cdp.evaluate(
            "(function(){const a=window.__adapter;"
            "window.__originUserEvents=0; window.__peerUserEvents=0;"
            "window.__originRepairBase=a.truncatedSeen(window.__pane);"
            "window.__peerRepairBase=a.truncatedSeen(window.__peerPane);"
            "a.wrapDispatch(window.__pane,function(e,dispatch){"
            "if(e&&e.type==='user_turn')window.__originUserEvents++;"
            "return dispatch(e);});"
            "a.wrapDispatch(window.__peerPane,function(e,dispatch){"
            "if(e&&e.type==='user_turn')window.__peerUserEvents++;"
            "return dispatch(e);});})(); true"
        )
        history0 = node.history_requests
        events0 = node.events_requests
        cdp.evaluate(f"window.__sendProjectedUserTurn({json.dumps(message)})")

        rows_expr = (
            "window.__pane.messagesEl.querySelectorAll('.msg.user').length===1 && "
            "window.__peerPane.messagesEl.querySelectorAll('.msg.user').length===1"
        )
        if not _poll_until(lambda: bool(cdp.evaluate(rows_expr)), 12, 0.05):
            counts = cdp.evaluate(
                "({origin: window.__pane.messagesEl.querySelectorAll('.msg.user').length,"
                " peer: window.__peerPane.messagesEl.querySelectorAll('.msg.user').length})"
            )
            raise AssertionError(
                "user-turn-two-pane: canonical rows did not render once "
                f"(origin={counts['origin']}, peer={counts['peer']})"
            )
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
            "originRepair:window.__adapter.truncatedSeen(window.__pane)"
            "-window.__originRepairBase,"
            "peerRepair:window.__adapter.truncatedSeen(window.__peerPane)"
            "-window.__peerRepairBase,"
            "originOpens:window.__adapter.opens(window.__pane),"
            "peerOpens:window.__adapter.opens(window.__peerPane)};})()"
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
                (cdp.evaluate(_OPENS_JS) or 0) == 1 and (cdp.evaluate(_PEER_OPENS_JS) or 0) == 1
            ),
            20,
            0.1,
        )
        if not opened:
            diagnostics = cdp.evaluate(
                "({originOpens:window.__adapter.opens(window.__pane),"
                "peerOpens:window.__adapter.opens(window.__peerPane),"
                "originUrl:window.__adapter.sourceUrl(window.__pane),"
                "peerUrl:window.__adapter.sourceUrl(window.__peerPane),title:document.title})"
            )
            raise AssertionError(
                "tool-turn-two-pane: both initial streams did not open once "
                f"(browser={diagnostics!r}, history={node.history_requests}, "
                f"events={node.events_requests})"
            )

        capability_urls = cdp.evaluate(
            "({origin: window.__adapter.sourceUrl(window.__pane),"
            " peer: window.__adapter.sourceUrl(window.__peerPane)})"
        )
        for name in ("origin", "peer"):
            if "tool_turn=1" not in capability_urls[name]:
                raise AssertionError(
                    f"tool-turn-two-pane: capability missing from {capability_urls!r}"
                )

        # Count reducer deliveries separately from DOM convergence. Each tool
        # emits a preliminary receipt and one accepted replacement; only the
        # accepted frames are the durable projection under test.  Repair
        # frames are transport facts: the adapter counts them per pane, and the
        # baselines start the count here.
        cdp.evaluate(
            "(function(){const a=window.__adapter;"
            "window.__originAcceptedToolEvents=[]; window.__peerAcceptedToolEvents=[];"
            "window.__originRepairBase=a.truncatedSeen(window.__pane);"
            "window.__peerRepairBase=a.truncatedSeen(window.__peerPane);"
            "a.wrapDispatch(window.__pane,function(e,dispatch){"
            "if(e&&e.type==='tool_result'&&e.accepted===true)"
            "window.__originAcceptedToolEvents.push({id:e.call_id,name:e.name,output:e.output});"
            "return dispatch(e);});"
            "a.wrapDispatch(window.__peerPane,function(e,dispatch){"
            "if(e&&e.type==='tool_result'&&e.accepted===true)"
            "window.__peerAcceptedToolEvents.push({id:e.call_id,name:e.name,output:e.output});"
            "return dispatch(e);});})(); true"
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
            "originRepair:window.__adapter.truncatedSeen(window.__pane)"
            "-window.__originRepairBase,"
            "peerRepair:window.__adapter.truncatedSeen(window.__peerPane)"
            "-window.__peerRepairBase,"
            "originOpens:window.__adapter.opens(window.__pane),"
            "peerOpens:window.__adapter.opens(window.__peerPane)};})()"
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


def run_es_contract(chrome: str) -> str:
    """The shared EventSource contract, run against the browser's real EventSource.

    The fake EventSource in tests/_js_harness_helpers.py claims to model the browser, and the
    node tests check it against EVENT_SOURCE_CONTRACT.  This runs the same contract function in
    Chrome, driven through the fault layer's controllable streams, so the fake and the browser
    are held to one script.  Stamps ``RECOVERY-READY-ESCONTRACT-<rules checked>``, or
    ``RECOVERY-FAILED-ESCONTRACT: <the broken rule and what the browser did>``."""
    node = _boot_node()
    profile = Path(_scratch()) / "chrome-es-contract"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        _set_cookie_and_navigate(cdp, node.base_url, node.token, f"{node.base_url}/es-contract")
        return _poll_title(cdp, 120)
    finally:
        if cdp is not None:
            cdp.close()
        _kill(proc)
        node.stop()


def run_coord_handoff_repair_budget(chrome: str) -> str:
    return run_handoff_repair_budget(chrome, coordinator=True)


@dataclass(frozen=True)
class Scenario:
    """One harness scenario.

    ``label`` is printed verbatim before the verdict.  ``uncontrolled`` is None
    when ``CONTROLS`` holds a negative control for the scenario; otherwise it
    says why none exists, and a READY verdict prints as ``READY (no control)``.
    """

    name: str
    sid: str
    label: str
    run: Callable[[str], str]
    uncontrolled: str | None = None


# E1: its recorded control (74eedff1: drop the unarmed reload's streaming-ref
# reset) stays READY.  The only unarmed reload left is the handoff repair, and
# the repair never reconnects without a completed render: a failed attempt
# keeps the stream closed and retries.  That render resets the same refs twice
# over (the sync core's reset just before renderHistory, and replayHistory's
# own), so removing any one of the three resets stays READY.
_E1_UNCONTROLLED = (
    "every reconnect after an unarmed reload (the handoff repair) follows a completed "
    "render, which resets the reply refs itself; no single removal fails it"
)
# G7: its recorded control (b85f7929: the /history flight key without the
# truncation generation) stays READY.  load_messages runs under the session's
# history visibility lock and the rewind takes the same lock, so with viewer B's
# flight parked inside load_messages the rewind waits it out (a rewind POST
# measured 3.00 s behind a 3 s hold) and A's refetch never meets B's flight.
_G7_UNCONTROLLED = (
    "the rewind serializes behind B's parked flight (history visibility lock), so "
    "A's refetch never overlaps it and the flight key is never tested"
)
# es-contract checks the browser's EventSource against the shared contract; the
# contract's own negative cases are its control.
_ES_CONTRACT_UNCONTROLLED = "self-checking: the contract's own cases are its control"

# Every scenario, in run order.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("storm", "A", "scenario A (storm):  ", run_storm),
    Scenario("restart", "B", "scenario B (restart):", run_restart),
    Scenario("coord-restart", "C", "scenario C (coord):  ", run_coord_restart),
    Scenario("fail-refetch", "D", "scenario D (failref):", run_fail_refetch),
    Scenario(
        "stale-ref-reload",
        "E1",
        "scenario E1 (staleref):",
        run_stale_ref,
        uncontrolled=_E1_UNCONTROLLED,
    ),
    Scenario("rewind-window", "E2", "scenario E2 (rewindwin):", run_rewind_window),
    Scenario("rewind-failed-window", "E3", "scenario E3 (rewindfail):", run_rewind_failed_window),
    Scenario("stale-backstop", "E4", "scenario E4 (stalebackstop):", run_stale_backstop),
    Scenario("hidden-retry", "E5", "scenario E5 (hiddenretry):", run_hidden_retry),
    Scenario("await-window-gate", "E6", "scenario E6 (awaitgate):", run_await_window_gate),
    Scenario("destroy-invalidation", "E7", "scenario E7 (destroyinval):", run_destroy_invalidation),
    Scenario("reconnect-in-await", "E8", "scenario E8 (reconnectawait):", run_reconnect_in_await),
    Scenario("slow-history", "E9", "scenario E9 (slowhistory):", run_slow_history),
    Scenario("retry-running-batch", "E10", "scenario E10 (retrybatch):", run_retry_running_batch),
    Scenario(
        "coord-rewind-window", "G1", "scenario G1 (coord-rewindwin):", run_coord_rewind_window
    ),
    Scenario(
        "coord-rewind-failed-window",
        "G2",
        "scenario G2 (coord-rewindfail):",
        run_coord_rewind_failed_window,
    ),
    Scenario(
        "coord-stale-backstop",
        "G3",
        "scenario G3 (coord-stalebackstop):",
        run_coord_stale_backstop,
    ),
    Scenario(
        "coord-heal-midturn", "G4", "scenario G4 (coord-healmidturn):", run_coord_heal_midturn
    ),
    Scenario(
        "coord-hidden-retry", "G5", "scenario G5 (coord-hiddenretry):", run_coord_hidden_retry
    ),
    Scenario(
        "coord-orphan-rewind",
        "G6",
        "scenario G6 (coord-orphanrewind):",
        run_coord_orphan_rewind,
    ),
    Scenario(
        "coord-joined-flight",
        "G7",
        "scenario G7 (coord-joinedflight):",
        run_coord_joined_flight,
        uncontrolled=_G7_UNCONTROLLED,
    ),
    Scenario(
        "handoff-repair-budget", "H1", "scenario H1 (handoff-budget):", run_handoff_repair_budget
    ),
    Scenario(
        "coord-handoff-repair-budget",
        "H2",
        "scenario H2 (coord-handoff-budget):",
        run_coord_handoff_repair_budget,
    ),
    Scenario(
        "user-turn-two-pane", "I1", "scenario I1 (user-turn-two-pane):", run_user_turn_two_pane
    ),
    Scenario(
        "tool-turn-two-pane", "I2", "scenario I2 (tool-turn-two-pane):", run_tool_turn_two_pane
    ),
    Scenario("roster-restart", "F1", "scenario F1 (roster-manual):", run_roster_restart),
    Scenario(
        "roster-restart-native", "F2", "scenario F2 (roster-native):", run_roster_restart_native
    ),
    Scenario(
        "es-contract",
        "ES",
        "scenario ES (es-contract):",
        run_es_contract,
        uncontrolled=_ES_CONTRACT_UNCONTROLLED,
    ),
)

# ---------------------------------------------------------------------------
# Negative controls
#
# A control proves a scenario can still fail: it runs the scenario against a mutated copy of one
# served client module and PASSES only when the scenario stamps the failing verdict recorded here.
# READY is a control failure, and so is any other failure: a module that no longer parses, a
# precondition that trips first, or a stamp that moved.  The mutated module is held in memory and
# served by the fault layer (``RecoveryServer``'s served overrides); nothing is written to the
# tree.  A needle that no longer occurs exactly ``count`` times is an ERROR, never a silent pass on
# the unmutated file, and so is a mutated module the browser never requested.
#
# The expected verdict is the part that carries over when the code under a control is rewritten;
# the needle is the part that moves with the code.
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent

# Served URL prefix -> the package directory the node (or this harness's
# /coord-static mount) serves it from.
_SERVED_ROOTS = {
    "/shared/": _REPO / "turnstone" / "shared_static",
    "/coord-static/": _REPO / "turnstone" / "console" / "static" / "coordinator",
    "/static/": _REPO / "turnstone" / "ui" / "static",
}


@dataclass(frozen=True)
class SourceEdit:
    """Replace ``needle`` (exactly ``count`` occurrences) in one served file."""

    served: str
    needle: str
    replacement: str
    count: int = 1


@dataclass(frozen=True)
class Control:
    """One scenario's negative control.

    ``removes`` says in one line what the mutation takes out of the client; it
    is printed on the control's result line, so a FAIL names the removal.
    ``expected`` is a regular expression the scenario's verdict must match in
    full; a scenario that reports failure by raising produces the verdict
    ``EXCEPTION <type>: <message>``.  ``origin`` records where the control came
    from: ``history <commit>`` (recorded in that commit's message as a hand
    mutation), ``docstring`` (stated in the scenario's own docstring), or
    ``added with the controls table`` (none was recorded before).
    """

    scenario: str
    removes: str
    expected: str
    origin: str
    edit: SourceEdit


@dataclass
class _ControlRun:
    """The control being served, and every node its scenario booted."""

    overrides: dict[str, bytes]
    nodes: list[Any] = field(default_factory=list)


_CONTROL_RUN: _ControlRun | None = None


_I = "/shared/interactive.js"
_WS = "/shared/workstream_sync.js"
_CO = "/coord-static/coordinator.js"

# One control per scenario that has an honest one; a scenario without one says why in its
# ``Scenario.uncontrolled``.  Each ``expected`` is the stamp the control was observed to produce,
# narrowed to the shape only its own removal produces; ``rows\d+`` marks a row count the verdict
# reads right after the reopen click's POST, before that rewind's render lands (incidental, not
# the detector).

CONTROLS: tuple[Control, ...] = (
    Control(
        scenario="storm",
        removes="sub-agent child steps are never routed into their task card",
        expected=r"RECOVERY-FAILED-STORM-top6-agent1-nested0-escaped2-.*",
        origin="added with the controls table",
        edit=SourceEdit(
            _I,
            "    const parentId = items[0] && items[0].parent_call_id;\n",
            '    const parentId = "";\n',
        ),
    ),
    Control(
        scenario="restart",
        removes="the stream cursor is never captured from the MessageEvent",
        expected=r"RECOVERY-FAILED-RESTART-rows1-busy0-disc0-healed1-trunc0",
        origin="added with the controls table",
        edit=SourceEdit(_WS, "      this.#cursor = id;\n", ""),
    ),
    Control(
        scenario="coord-restart",
        removes="the coordinator never captures the stream cursor from the MessageEvent",
        expected=r"RECOVERY-FAILED-COORD-rows1-reopened1-disc0-healed1-trunc0",
        origin="added with the controls table",
        edit=SourceEdit(_CO, "        lastEventId = event.lastEventId;\n", ""),
    ),
    Control(
        scenario="fail-refetch",
        removes="the response status check, so a failed /history body reaches the render",
        # trunc1 with healed1 is this removal's shape: the failed body renders and clears the gap
        # record, so the heal comes from a fresh cursorless connect with no second
        # replay_truncated (the intact retry path always draws one).  The phase-1 fields depend
        # on whether the capture lands before or after that heal.
        expected=(
            r"RECOVERY-FAILED-FAILFETCH-stale0-p1rows\d+-p1empty[01]-healed1-rows1-busy0-trunc1"
        ),
        origin="added with the controls table",
        edit=SourceEdit(
            _WS,
            "      if (!response || !response.ok) return null;\n",
            "      if (!response) return null;\n",
        ),
    ),
    Control(
        scenario="rewind-window",
        removes="the staleness latch from the rewind gate",
        expected=r"RECOVERY-FAILED-REWINDWIN-posts2-rows0",
        origin="history 74eedff1",
        edit=SourceEdit(
            _I,
            "  _rewindToMessage(msgEl) {\n    if (this.busy || this.sync.historyStale) return;\n",
            "  _rewindToMessage(msgEl) {\n    if (this.busy) return;\n",
        ),
    ),
    Control(
        scenario="rewind-failed-window",
        removes="the staleness latch from the rewind gate",
        expected=r"RECOVERY-FAILED-REWINDFAIL-closed2-heal0-posts2-rows0",
        origin="history 74eedff1",
        edit=SourceEdit(
            _I,
            "  _rewindToMessage(msgEl) {\n    if (this.busy || this.sync.historyStale) return;\n",
            "  _rewindToMessage(msgEl) {\n    if (this.busy) return;\n",
        ),
    ),
    Control(
        scenario="stale-backstop",
        removes="the transport-free backstop, in favour of a reload that reconnects",
        expected=r"RECOVERY-FAILED-STALEBACKSTOP-heal1-sse1-hist1-gated1-posts2-rows\d+",
        origin="docstring",
        edit=SourceEdit(_WS, "      this.#deferStaleBackstop();\n", "      this.#reload();\n"),
    ),
    Control(
        scenario="hidden-retry",
        removes="the transport clause (presence and OPEN) from the retry's fire guard",
        expected=r"RECOVERY-FAILED-HIDDENRETRY-hidden1-heal1-sse1-posts2-rows\d+",
        origin="history 8effe656, 4007fab8",
        edit=SourceEdit(
            _WS,
            "      if (gen !== this.#generation || !this.#historyStale || this.#queue || "
            "!this.#sourceOpen()) {\n",
            "      if (gen !== this.#generation || !this.#historyStale || this.#queue) {\n",
        ),
    ),
    Control(
        scenario="await-window-gate",
        removes="the stream-openness terms from the seedless render gate",
        expected=r"RECOVERY-FAILED-AWAITGATE-rows1-latch0-heal1",
        origin="docstring",
        edit=SourceEdit(_WS, "        open: this.#sourceOpen(),\n", "        open: true,\n"),
    ),
    Control(
        scenario="destroy-invalidation",
        removes="destroy()'s teardown of its sync core (the generation bump, aborts and listener)",
        expected=r"RECOVERY-FAILED-DESTROYINVAL-sse1-detached1-vis0",
        origin="history 539b91d3",
        edit=SourceEdit(_I, "      pane.sync.destroy();\n", ""),
    ),
    Control(
        scenario="reconnect-in-await",
        removes="the stream-generation term from the seedless render gate",
        expected=r"RECOVERY-FAILED-RECONNECTAWAIT-dupes2-healed1",
        origin="history 7ed5d90a",
        edit=SourceEdit(
            _WS,
            "        sameConnection: this.#epoch === epoch,\n",
            "        sameConnection: true,\n",
        ),
    ),
    Control(
        scenario="slow-history",
        removes="the /history deadline's growth after an expiry",
        expected=r"RECOVERY-FAILED-SLOWHIST-rows3-latch1-hist2",
        origin="docstring",
        edit=SourceEdit(
            _WS,
            "        this.#historyDeadlineMs = Math.min(this.#historyDeadlineMs * 2, "
            "HISTORY_DEADLINE_MAX_MS);\n",
            "",
        ),
    ),
    Control(
        scenario="retry-running-batch",
        removes="the promotion of a cursor-bearing refetch, leaving a seedless render",
        expected=r"RECOVERY-FAILED-RETRYBATCH-replaced0-latch0-batch1-cursor1-busy1-reopen0",
        origin="docstring",
        edit=SourceEdit(
            _WS,
            "      if (data.cursor != null) {\n"
            "        this.#promote(data);\n"
            '        return "rendered";\n'
            "      }\n",
            "",
        ),
    ),
    Control(
        scenario="coord-rewind-window",
        removes="the staleness latch from the coordinator's rewind gate",
        expected=r"RECOVERY-FAILED-COORDREWINDWIN-posts2-rows0",
        origin="history f58fcd1b",
        edit=SourceEdit(
            _CO,
            "    // the heal paths that reopen the affordances.\n"
            "    if (busy || historyStale) return;\n",
            "    // the heal paths that reopen the affordances.\n    if (busy) return;\n",
        ),
    ),
    Control(
        scenario="coord-rewind-failed-window",
        removes="the staleness latch from the coordinator's rewind gate",
        expected=r"RECOVERY-FAILED-COORDREWINDFAIL-closed2-heal0-posts2-rows0",
        origin="history f58fcd1b",
        edit=SourceEdit(
            _CO,
            "    // the heal paths that reopen the affordances.\n"
            "    if (busy || historyStale) return;\n",
            "    // the heal paths that reopen the affordances.\n    if (busy) return;\n",
        ),
    ),
    Control(
        scenario="coord-stale-backstop",
        removes="the coordinator's transport-free backstop, in favour of a reconnecting reload",
        expected=r"RECOVERY-FAILED-COORDSTALEBACKSTOP-heal1-sse1-hist1-gated1-posts2-rows\d+",
        origin="history f58fcd1b",
        edit=SourceEdit(
            _CO,
            "            // and nothing here re-arms the retry, keeping it bounded.\n"
            "            refetchHistory();\n",
            "            // and nothing here re-arms the retry, keeping it bounded.\n"
            "            loadHistoryThenReconnect();\n",
        ),
    ),
    Control(
        scenario="coord-heal-midturn",
        removes="the live tool-call term from the coordinator's seedless render gate",
        expected=r"RECOVERY-FAILED-COORDHEALMIDTURN-heal1-mid0-sse0-hist1-posts2-rows\d+",
        origin="history 54631d31",
        edit=SourceEdit(_CO, "        liveToolCalls.size > 0 ||\n", ""),
    ),
    Control(
        scenario="coord-hidden-retry",
        removes="the transport clause (presence and OPEN) from the coordinator retry's fire guard",
        expected=r"RECOVERY-FAILED-COORDHIDDENRETRY-hidden1-heal1-sse1-posts2-rows\d+",
        origin="history 54631d31, 4007fab8",
        edit=SourceEdit(
            _CO,
            "                    visHandler &&\n"
            "                    evtSource &&\n"
            "                    evtSource.readyState === EventSource.OPEN\n"
            "                  ) {\n",
            "                    visHandler\n                  ) {\n",
        ),
    ),
    Control(
        scenario="coord-orphan-rewind",
        removes="the event-owned live-call term, restoring the DOM probe for running batches",
        expected=r"RECOVERY-FAILED-COORDORPHANREWIND-posts1-hist2-rows1-orphan1",
        origin="docstring",
        edit=SourceEdit(
            _CO,
            "        liveToolCalls.size > 0 ||\n",
            '        messagesEl.querySelector(".conv-batch--running") !== null ||\n',
        ),
    ),
    Control(
        scenario="handoff-repair-budget",
        removes="the rendered outcome a completed history load reports",
        expected=(
            r"EXCEPTION AssertionError: handoff-repair-budget: rendered tokenless 200 did"
            r" not downgrade to bootstrap \(history=6, events=0, prompt=1\)"
        ),
        origin="added with the controls table",
        edit=SourceEdit(
            _WS,
            '    if (!first && !hadGap && data.cursor != null) this.#emit("missed");\n'
            '    return "rendered";\n',
            '    if (!first && !hadGap && data.cursor != null) this.#emit("missed");\n',
        ),
    ),
    Control(
        scenario="coord-handoff-repair-budget",
        removes="the rendered outcome the coordinator's completed history load reports",
        expected=(
            r"EXCEPTION AssertionError: coord-handoff-repair-budget: rendered tokenless 200"
            r" did not downgrade to bootstrap \(history=6, events=0, prompt=1\)"
        ),
        origin="added with the controls table",
        edit=SourceEdit(_CO, '    return "rendered";\n', ""),
    ),
    Control(
        scenario="user-turn-two-pane",
        removes="the accepted user_turn's removal of the origin's optimistic bubble",
        expected=(
            r"EXCEPTION AssertionError: user-turn-two-pane: canonical rows did not render once"
            r" \(origin=2, peer=1\)"
        ),
        origin="added with the controls table",
        edit=SourceEdit(
            "/shared/composer_queue.js",
            "      evt.client_send_ids,\n      true,\n    );\n",
            "      evt.client_send_ids,\n      false,\n    );\n",
        ),
    ),
    Control(
        scenario="tool-turn-two-pane",
        removes="indexing of a newly painted batch's rows under their (reused) call ids",
        expected=(
            r"EXCEPTION AssertionError: tool-turn-two-pane: origin occurrence outputs crossed"
            r" \['TOOL_TWO_SENTINEL', ''\]"
        ),
        origin="added with the controls table",
        edit=SourceEdit(
            _I,
            "      // never the old turn's still-visible DOM.\n"
            "      indexLatestToolRow(\n"
            "        this._toolRowIndex,\n"
            "        this._toolResultNodes,\n"
            "        callId,\n"
            "        row,\n"
            "      );\n",
            "      // never the old turn's still-visible DOM.\n",
        ),
    ),
    Control(
        scenario="roster-restart",
        removes="the dashboard's ?last_event_id= presentation on a manual global reconnect",
        expected=r"RECOVERY-FAILED-ROSTER-trunc0-cursor0-ghost0-keeper1",
        origin="added with the controls table",
        edit=SourceEdit(
            "/static/app.js",
            '    globalUrl += "?last_event_id=" + encodeURIComponent(globalLastEventId);\n',
            "",
        ),
    ),
    Control(
        scenario="roster-restart-native",
        removes="the dashboard's cursor clear on an in-stream node_snapshot",
        expected=r"RECOVERY-FAILED-ROSTERNATIVE-dead-cursor-represented-cursor1",
        origin="history 5386d598",
        edit=SourceEdit(
            "/static/app.js",
            "      globalLastEventId = null;\n"
            "      applyRosterSnapshot(data.workstreams || [], { evict: true });\n",
            "      applyRosterSnapshot(data.workstreams || [], { evict: true });\n",
        ),
    ),
)


def _served_file(served: str) -> Path:
    """The file on disk the node serves at ``served``."""
    for prefix, root in _SERVED_ROOTS.items():
        if served.startswith(prefix):
            path = root / served[len(prefix) :]
            if not path.is_file():
                raise ValueError(f"{served}: no file at {path}")
            return path
    raise ValueError(f"{served}: no known static mount serves this path")


def _control_overrides(control: Control) -> dict[str, bytes]:
    """The mutated bodies a control serves; raises ValueError on a stale needle."""
    from tests._sse_recovery_server import mutated_source

    edit = control.edit
    body = mutated_source(
        _served_file(edit.served), edit.needle, edit.replacement, count=edit.count
    )
    return {edit.served: body}


def run_control(chrome: str, control: Control) -> tuple[str, str]:
    """Run one control; return ``(outcome, verdict)`` with outcome PASS, FAIL or ERROR."""
    global _CONTROL_RUN
    try:
        overrides = _control_overrides(control)
    except ValueError as exc:
        return "ERROR", f"stale needle: {exc}"
    runner = {sc.name: sc.run for sc in SCENARIOS}[control.scenario]
    run = _ControlRun(overrides=overrides)
    _CONTROL_RUN = run
    try:
        verdict = runner(chrome)
    except Exception as exc:
        # Several scenarios report a failed precondition or detector by
        # raising.  Every outcome is classified below; anything but the
        # recorded verdict fails the control.
        verdict = f"EXCEPTION {type(exc).__name__}: {exc}"
    finally:
        _CONTROL_RUN = None
    unserved = [path for path in overrides if not any(n.override_hits.get(path) for n in run.nodes)]
    if unserved:
        return "ERROR", f"mutated module never served ({', '.join(unserved)}); verdict {verdict}"
    if verdict.startswith("RECOVERY-READY"):
        return "FAIL", verdict
    if re.fullmatch(control.expected, verdict) is None:
        return "FAIL", verdict
    return "PASS", verdict


def check_controls() -> int:
    """Check every control's needle against the tree, and that every scenario has
    exactly one of a control or a stated reason for none; return the error count."""
    errors = 0
    for control in CONTROLS:
        try:
            _control_overrides(control)
            print(f"control {control.scenario}: needle OK")
        except ValueError as exc:
            errors += 1
            print(f"control {control.scenario}: ERROR stale needle: {exc}")
    controlled = [control.scenario for control in CONTROLS]
    names = [sc.name for sc in SCENARIOS]
    for name in sorted(set(controlled) - set(names)):
        errors += 1
        print(f"control {name}: ERROR no such scenario")
    for name in sorted({n for n in controlled if controlled.count(n) > 1}):
        errors += 1
        print(f"control {name}: ERROR more than one control")
    for sc in SCENARIOS:
        if (sc.name in controlled) == (sc.uncontrolled is not None):
            errors += 1
            print(f"scenario {sc.name}: ERROR needs exactly one of a control or a reason for none")
    print(f"control needles: {len(CONTROLS) - errors}/{len(CONTROLS)} OK")
    return errors


def run_controls(chrome: str, controls: list[Control]) -> int:
    """Run ``controls`` in order; print one line each and a tally. Return the
    number that did not PASS."""
    short = {sc.name: sc.sid for sc in SCENARIOS}
    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    for control in controls:
        started = time.monotonic()
        outcome, verdict = run_control(chrome, control)
        counts[outcome] += 1
        elapsed = time.monotonic() - started
        print(
            f"control {short[control.scenario]} ({control.scenario}): {outcome} "
            f"[{elapsed:.0f}s] {verdict} | removed: {control.removes}"
        )
    print(
        f"controls: {counts['PASS']}/{len(controls)} PASS "
        f"({counts['FAIL']} FAIL, {counts['ERROR']} ERROR)"
    )
    return counts["FAIL"] + counts["ERROR"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--scenario",
        choices=[sc.name for sc in SCENARIOS] + ["both", "all"],
        default="all",
        help="'both' = A+B (legacy alias); 'all' runs every scenario",
    )
    ap.add_argument("--keep-open", type=int, metavar="PORT", help="serve the storm page, no CDP")
    ap.add_argument(
        "--control",
        choices=[control.scenario for control in CONTROLS],
        metavar="SCENARIO",
        help="run SCENARIO's negative control: passes only on its recorded failing verdict",
    )
    ap.add_argument("--controls", action="store_true", help="run every negative control")
    ap.add_argument(
        "--check-controls",
        action="store_true",
        help="check every control's needle against the tree, without a browser",
    )
    args = ap.parse_args()

    if args.keep_open:
        keep_open(args.keep_open)
        return
    if args.check_controls:
        raise SystemExit(1 if check_controls() else 0)

    chrome = _find_chrome()
    if chrome is None:
        print("recovery_e2e: no chrome/chromium on PATH — see the module docstring runbook")
        raise SystemExit(2)

    if args.control or args.controls:
        selected = [c for c in CONTROLS if args.controls or c.scenario == args.control]
        raise SystemExit(1 if run_controls(chrome, selected) else 0)

    failures = 0
    ran = 0
    uncontrolled_ready: list[str] = []
    for sc in SCENARIOS:
        if args.scenario not in (sc.name, "all") and not (
            args.scenario == "both" and sc.name in ("storm", "restart")
        ):
            continue
        verdict = sc.run(chrome)
        ran += 1
        ready = verdict.startswith("RECOVERY-READY")
        failures += 0 if ready else 1
        note = ""
        if ready and sc.uncontrolled is not None:
            uncontrolled_ready.append(sc.sid)
            note = f"  [READY (no control): {sc.uncontrolled}]"
        print(f"{sc.label} {verdict}{note}")
    ready_total = ran - failures
    print(
        f"recovery_e2e: {ready_total}/{ran} READY "
        f"({ready_total - len(uncontrolled_ready)} controlled; READY (no control): "
        f"{', '.join(uncontrolled_ready) or 'none'})"
    )
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
