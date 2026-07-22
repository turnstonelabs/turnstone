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
    python3 scripts/recovery_e2e.py --scenario both   # A+B only (legacy)
    python3 scripts/recovery_e2e.py --keep-open 8971  # serve the storm page
                                                      # for manual inspection

The #890 guard-before-wipe family (all against the interactive pane, no
coordinator):

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
gated by ``busy || _replayQueue`` and never reach the server.  Backend proof
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
rows survive).  A plain send — sends are deliberately NOT latch-gated — runs
a fourth scripted ``final_text`` turn whose ORGANIC turn-settle idle edge
fires the backstop: a quiesced, same-token REST ``_refetchHistory``
(deliberately NOT ``_loadHistoryThenConnect`` — the old reload backstop drew
the server's synthetic ``state_change:idle`` on its fresh reconnect and
re-triggered itself, a zero-backoff reconnect/refetch storm against a
recovering node).  With the fault budget now exhausted the refetch succeeds
and rebuilds the rewound (ONE user turn, index 1 of 3 rewinds 2) + sent (a
second user turn) transcript to TWO user rows, clearing the latch, so a
fresh rewind on a remaining row lands (``rewind_requests`` -> 2).  THE r5
PROOF, both counted at the fault layer: ``events_requests`` is UNCHANGED
across the whole heal episode (``sse0`` — zero new EventSource connections;
the storm would have opened one per reconnect) and ``history_requests`` grew
by exactly ONE (the backstop's single fetch — the plain fourth turn emits no
clear_ui).  All polls are deadline-bounded so a regressed looping backstop
stamps a clean FAILED, never a hang.  Stamps
``RECOVERY-READY-STALEBACKSTOP-heal1-sse0``.

Scenario A (storm): the page connects, POSTs ``/send`` on stream-open (so
the listener is registered first), the node runs a 4-parallel-bash
``seq 1 500`` storm plus a task_agent whose sub-tools are chatty bashes;
the page asserts the final DOM has the expected top-level tool rows, the
task_agent card nests its sub-tool rows (NO child escaped to the top
level), and the composer settles idle. Stamps ``RECOVERY-READY-STORM-<n>``.

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

# Fourth-turn sentinel for scenario E4 (stale-backstop): the scripted
# final_text of the plain send that drives the idle-edge backstop.  Same
# collision-proof discipline — the E4 transcript is pure final_text turns
# ("one"/"two"/"three" + the seed messages), so this hex-suffixed token
# proves the sent turn reached the healed /history render, distinct from
# everything else on screen.
BACKSTOP_SENTINEL = "BACKSTOP-b2e4"

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
      import { InteractivePane } from "/shared/interactive.js";

      const q = new URLSearchParams(location.search);
      const wsId = q.get("ws_id");
      const scenario = q.get("scenario") || "storm";
      const expectRows = parseInt(q.get("rows") || "4", 10);
      // Healed-gap sentinel, threaded from the runner (HEALED_SENTINEL)
      // so the injected turn text and this check cannot drift apart.
      const healedSentinel = q.get("healed") || "";
      // Turn-2 sentinel for stale-ref-reload (SECOND_SENTINEL), same
      // single-source discipline via the ?second= param.
      const secondSentinel = q.get("second") || "";

      // REAL pane against THIS origin (base=""): real authFetch (cookie) and
      // real EventSource. The default host provides all SSE seams.
      const pane = new InteractivePane(wsId, { base: "" });
      document.getElementById("mount").appendChild(pane.el);
      pane.wsId = wsId;
      window.__pane = pane;

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
        return { topLevel, escapedChildren, agentCard: !!agentCard, nested };
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

      // Drive /send once the stream is live (wrap the default host hook).
      const origOpen = pane._host.onStreamOpen.bind(pane._host);
      pane._host.onStreamOpen = function (p) {
        origOpen(p);
        window.__streamOpen = (window.__streamOpen || 0) + 1;
        if (scenario === "storm") sendOnce("run the storm");
        else if (
          (scenario === "restart" ||
            scenario === "fail-refetch" ||
            scenario === "stale-ref-reload") &&
          window.__streamOpen === 1
        )
          sendOnce("run a turn");
        // rewind-window drives its turns SERVER-side before navigation (a
        // /send never emits a live user row — only /history replay does), so
        // the page never auto-sends there.
      };

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

      // First paint the REAL way: /history then connect SSE.
      pane._loadHistoryThenConnect(wsId);

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
          if (c.topLevel >= expectRows && c.agentCard && c.nested >= 2 && idle) {
            document.title = c.escapedChildren
              ? "RECOVERY-FAILED-escaped-" + c.escapedChildren
              : "RECOVERY-READY-STORM-" + c.topLevel + "-nested-" + c.nested;
            return;
          }
          if (Date.now() > deadline) {
            document.title =
              "RECOVERY-FAILED-STORM-top" + c.topLevel + "-agent" + (c.agentCard ? 1 : 0) +
              "-nested" + c.nested + "-escaped" + c.escapedChildren + "-busy" + (pane.busy ? 1 : 0);
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
        // transcript.  A plain send (deliberately NOT latch-gated) runs a
        // fresh turn whose ORGANIC turn-settle idle edge fires the backstop —
        // a quiesced, same-token REST _refetchHistory, NOT
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
          // heal1 = the backstop's quiesced REST refetch rebuilt the rewound
          //   (ONE user turn) + sent (a second) transcript => TWO user rows,
          //   latch cleared.  sse0 = it touched the transport ZERO times
          //   (sseDelta 0 — the storm regression opens one EventSource per
          //   reconnect).  histDelta 1 = the backstop's single fetch (the
          //   plain fourth turn emits no clear_ui).  gatedPosts 1 = the row-0
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
      // dropped (see buildCoordChrome's comment).
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
      const sendPoll = setInterval(() => {
        if (window.__esOpens >= 1) {
          clearInterval(sendPoll);
          sendOnce("run a turn");
        }
      }, 100);

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


def _boot_node(port: int = 0) -> Any:
    from tests._sse_recovery_server import RecoveryServer
    from turnstone.core.storage import init_storage, reset_storage

    # The page route must bypass auth on first load (the cookie is set by the
    # runner via CDP BEFORE navigation), so make it public by prefixing under
    # a public path is unavailable here; instead the runner sets the cookie so
    # /recovery passes the middleware. init storage per boot (shared singleton).
    reset_storage()
    init_storage("sqlite", path=os.path.join(_scratch(), "recovery_e2e.db"), run_migrations=True)
    return RecoveryServer(extra_routes=[_page_route(), *_coord_routes()], port=port)


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
    """A parallel bash storm PLUS a task_agent whose sub-tools are chatty
    bashes (so the browser proves both fix-3 batching AND sub-agent nesting
    with no escaped children)."""
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
            {"id": "s_a", "name": "bash", "arguments": json.dumps({"command": ": a; seq 1 200"})},
            {"id": "s_b", "name": "bash", "arguments": json.dumps({"command": ": b; seq 1 200"})},
        ],
        finish_reason="tool_calls",
    )
    # Turn 1: the 4-bash storm; turn 2: a task_agent with 2 chatty sub-bashes.
    return (
        storm,
        final_text_script("storm done"),
        task,
        sub,
        final_text_script("sub done"),
        final_text_script("all done"),
    )


def run_storm(chrome: str) -> str:
    node = _boot_node()
    ws_id = node.create_workstream(*_storm_scripts(), name="browser-storm")
    profile = Path(_scratch()) / "chrome-storm"
    proc, cdp_port = _launch_chrome(chrome, profile)
    cdp: CDP | None = None
    try:
        cdp = CDP(_page_ws_url(cdp_port))
        # The page POSTs the STORM turn on stream-open; the runner sends the
        # task_agent follow-up once the first turn settles so both land.
        url = f"{node.base_url}/recovery?ws_id={ws_id}&scenario=storm&rows=4"
        _set_cookie_and_navigate(cdp, node.base_url, node.token, url)
        # After the storm turn, trigger the task_agent turn via REST so the
        # page renders the nested sub-agent card.
        _wait_state(node, ws_id, "idle", 40)
        node.send(ws_id, "spawn the sub agent")
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
    turn mid-flight (E1/E4).  A raw POST emits no live user row, so the sent
    turn appears only via the next /history render."""
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
    the SAME transcript.  Each of the three ``node.send`` calls consumes one
    scripted turn; a /send never emits a live user row (only /history replay
    does), so the initial page-load /history render is what paints all three
    user rows.

    ``extra_scripts`` are appended to the scripted client AFTER the three
    seeding scripts and left UNSENT: E4 queues a fourth ``final_text`` turn
    there for the later backstop-driving send (its positional script stays in
    sync because the three seeding sends consume exactly the three seeding
    scripts)."""
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
            lambda: (
                cdp.evaluate("window.__pane.messagesEl.querySelectorAll('.msg.user').length") == 3
            ),
            20,
            0.2,
        ):
            raise AssertionError("rewind-window: three user rows never rendered")
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
                and cdp.evaluate("window.__pane.messagesEl.querySelectorAll('.msg.user').length")
                == 1
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
            lambda: (
                cdp.evaluate("window.__pane.messagesEl.querySelectorAll('.msg.user').length") == 3
            ),
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
        stale_rows = cdp.evaluate("window.__pane.messagesEl.querySelectorAll('.msg.user').length")
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
            lambda: (
                cdp.evaluate("window.__pane.messagesEl.querySelectorAll('.msg.user').length") == 1
            ),
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
    rewind/edit stay gated over the stale-but-real transcript.  A plain send
    — sends are deliberately NOT latch-gated (``sendMessage`` gates only on
    ``busy``; the raw ``authFetch`` here bypasses even that) — runs the fourth
    scripted ``final_text`` turn whose ORGANIC turn-settle idle edge fires the
    backstop: a quiesced, same-token REST ``_refetchHistory``, deliberately
    NOT ``_loadHistoryThenConnect`` (the old reload backstop drew the server's
    synthetic ``state_change:idle`` on its fresh reconnect and re-triggered
    itself — a zero-backoff reconnect/refetch storm).  With the fault budget
    exhausted the refetch succeeds and heals the rewound + sent transcript.

    THE r5 PROOF (both counted at the fault layer): ``events_requests`` is
    UNCHANGED across the whole heal (``sse0`` — zero new EventSource
    connections; the storm regression opens one per reconnect) and
    ``history_requests`` grew by exactly ONE (the backstop's single fetch —
    the plain fourth turn emits no clear_ui).  Backend proofs:
    ``rewind_requests`` 1 -> (gated) 1 -> 2, ``history_fail_remaining == 0``.
    Every poll is deadline-bounded so a regressed looping backstop stamps a
    clean FAILED, never a hang."""
    from tests._sse_recovery_server import final_text_script

    # Seed three turns and queue a FOURTH final_text (the sentinel-bearing
    # turn the backstop-driving send below runs) — the shared helper keeps the
    # seeding byte-identical to E2/E3 so the rewind arithmetic matches.
    node, ws_id = _seed_three_completed_turns(
        "browser-stale-backstop",
        extra_scripts=(final_text_script(BACKSTOP_SENTINEL),),
    )
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
            lambda: (
                cdp.evaluate("window.__pane.messagesEl.querySelectorAll('.msg.user').length") == 3
            ),
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
        stale_rows = cdp.evaluate("window.__pane.messagesEl.querySelectorAll('.msg.user').length")
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
        # Baselines captured the instant BEFORE the send: the heal must add
        # exactly ZERO SSE opens and exactly ONE /history fetch relative here.
        events_baseline = node.events_requests
        history_baseline = node.history_requests
        # Drive a plain send via in-page authFetch — sends are NOT latch-gated
        # (see docstring), so this reaches the server, runs the fourth scripted
        # turn (BACKSTOP_SENTINEL), and its ORGANIC turn-settle idle edge fires
        # the backstop.
        _send_in_page(cdp, "fourth turn")
        # HEAL: the fourth turn settles -> idle edge -> quiesced REST refetch
        # (fault exhausted) succeeds -> replayHistory rebuilds the rewound (ONE
        # user turn) + sent (a second) transcript to TWO user rows, SENTINEL
        # present, latch cleared.  The 3-user-rows -> 2-user-rows transition is
        # the load-bearing proof the backstop RENDERED — a stale transcript can
        # only change via a successful /history render.  Deadline-bounded so a
        # regressed looping backstop times out to a clean FAILED, never a hang.
        healed = _poll_until(
            lambda: cdp.evaluate(
                "window.__pane.messagesEl.querySelectorAll('.msg.user').length === 2 "
                "&& window.__pane._historyStale === false "
                "&& (window.__pane.messagesEl.textContent||'').includes("
                + json.dumps(BACKSTOP_SENTINEL)
                + ")"
            ),
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
        #  - history_delta MUST be 1: the plain fourth turn emits no clear_ui,
        #    so the ONLY /history in the send+heal window is the backstop fetch.
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
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
