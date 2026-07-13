#!/usr/bin/env python3
"""Build the livepass harnesses — render real hatch dialogs/shelves headlessly.

The livepass is how converted modal surfaces get verified without booting a
server: a minimal page that symlinks the REAL stylesheets and scripts, embeds
the REAL markup (extracted fresh from the index files at build time), stubs
``window.authFetch`` with canned fixtures, and drives surfaces via ``?open=``
query params — including click-driving submits so dead buttons can't hide
(the model-Save bug class).

Usage:
    python3 scripts/livepass.py                # build into /tmp/livepass/
    python3 scripts/livepass.py --out DIR      # build elsewhere
    python3 scripts/livepass.py --serve 8950   # build + serve (Ctrl+C stops)

Then screenshot states (file:// blocks ES modules — always serve over http;
the reduced-motion flag is REQUIRED, entrance animations race the capture):

    google-chrome --headless --disable-gpu --hide-scrollbars \\
      --force-prefers-reduced-motion --window-size=1440,900 \\
      --virtual-time-budget=9000 --screenshot=out.png \\
      "http://localhost:8950/ui/livepass.html?open=new-ws&theme=light"

UI harness (?open=): new-ws · new-ws-fork · edit-title · delete-ws ·
  revoke-mcp · ws-delete · ws-delete-results   (+ &theme=light, &busy=1)
Console harness (?open=): schedule-create · schedule-edit · model-create ·
  model-edit · model-save (drives a Save click; document.title becomes
  PUT-OK-<n> on success) · policy · confirm · token
  Plus &tall=1 (90-row users panel — the .admin-content scroll state; the
  synthetic rows wrap to two lines, so judge overflow geometry, not row
  cadence) · &scrolled=1 lands mid-list, &scrolled=bottom shows the 24px
  scroll tail · &focuslast=1 focuses the last shelf-body control (the
  displaced-dock regression probe: only .sh-body may scroll; head/foot stay
  pinned).  All combinable with ?open=.  The console page wraps the fragment
  in the REAL L-shell chain — pane-pinned height, interior scroller — so
  scroll/dock geometry matches production; keep it that way.  Body-level
  dialogs (confirm/install/coord-delete) are injected as riders; a driven
  ?open= that ends with no open dialog stamps OPEN-FAILED-<state> into the
  title instead of passing silently.
  Governance surfaces (roles/HR/OGP/memory/skill) need fixtures that are not
  canned yet — add a fixture + driver branch below when you need one.
Shell harness (?split=): right (default) · down · three · none — boots the
  REAL shell.js + pane.js split-view engine over stubbed seams (two demo
  conversational panes; ?split=three adds the Dashboard cell).  + &theme=light.
  document.title stamps SPLIT-READY-<visible cells> on success and
  SPLIT-FAILED-<reason> when a driven split was denied — judge the focused
  cell's top accent bar, the separators, and the .shown tab marker.
Attachments harness (/attachments/livepass.html): the composer attachment
  chips + the sent-message attachment pills, both driven through the REAL
  code paths — createAttachmentController.rehydrate() builds the chips and
  Pane.addUserMessage() builds the pills, so the preview nodes (image/pdf
  thumbnail, <audio> player, lazy text snippet) render exactly as in
  production.  Committed fixtures cover every kind plus a long filename;
  thumbnails + the audio clip are served by an in-process fixture route
  (--serve only), the text snippet flows through the stubbed authFetch.
  + &theme=light.  document.title stamps ATTACH-READY-<chips>-<pills> on
  success, ATTACH-FAILED-c<n>-p<n> when a surface came up empty.  Judge the
  thumbnail crop/size, the native audio-control fit at the constrained
  height, the snippet contrast, and how a long filename behaves at the
  340px chip cap.
Task-agent harness (/taskagent/livepass.html): the task_agent card — a task
  agent's sub-tool steps nested under its conversation row, driven through the
  REAL InteractivePane.handleEvent (parent tool_pending/tool_info -> child
  tool_pending/tool_result/tool_output_chunk/approve_request -> task_agent
  tool_result) so the SSE->card routing (_routeAgentItems / _ensureAgentCard,
  and appendToolOutput finding the nested row by call_id) is exercised, not
  just the leaf builders.  Query flags: &theme=light; &collapsed=1 (all-auto,
  no approval -> the natural collapse-by-default state); &parallel=1 (card in a
  2-tool batch, for the rail-bleed rules); &recall=1 (the RECALL path —
  replayHistory rebuilding the card from a /history `agent_steps` overlay, i.e.
  a reload while the ws is in memory); &expand=1 (open every card so a shot
  shows the nested steps); &race=1 (child steps emitted BEFORE the task_agent
  row paints — the parallel-pool ordering window; the orphan buffer must nest
  them rather than let them escape to top-level); &orphan=1 (child steps whose
  task_agent row NEVER paints — the safety valve must escape them to visible
  top-level rows after the grace window, stamping TASKAGENT-ORPHANS-ESCAPED-<n>,
  not leave them buffered/invisible).  document.title stamps
  TASKAGENT-READY-<steps> on
  success, TASKAGENT-FAILED-... / TASKAGENT-ERROR when routing breaks, so a
  broken card can't screenshot green.

Perf harness (/perf/livepass.html): long-session performance baseline for the
  interactive pane — mounts the REAL InteractivePane at real scroll geometry
  (fixed-height mount, production CSS chain) and drives production-shaped
  events through pane.handleEvent/replayHistory with rAF yields, measuring:
  replayHistory wall time at N messages, live event-storm cost per turn on top
  of that transcript (reasoning/content deltas + tool batches + task_agent
  cards), tool_output_chunk throughput, busy/idle churn, heap + node count +
  _agentCards size across repeated replay cycles (leak probe), and longtask
  counts.  Query params: ?n= (history size) &turns= &chunks= &cycles= &idle=
  &post=1 (POST the JSON report to /perf/report — the --perf runner captures
  it).  Results land in <pre id="perf-json"> and document.title stamps
  PERF-READY-<n> / PERF-FAILED-<phase>.  MEASUREMENT RULES: never run with
  --virtual-time-budget (it corrupts performance.now) and never pass
  --force-prefers-reduced-motion (it disables the animations whose cost we
  measure); the --perf runner passes --js-flags=--expose-gc and
  --enable-precise-memory-info so heap numbers are stable and real.

    python3 scripts/livepass.py --perf                  # 300 and 3000 msgs
    python3 scripts/livepass.py --perf --perf-n 5000    # match the field run

Rebuild after ANY markup change: the dialog blocks are embedded at build
time. Assets are symlinked, so CSS/JS edits are live on refresh.
"""

from __future__ import annotations

import argparse
import http.server
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_INDEX = ROOT / "turnstone/ui/static/index.html"
CONSOLE_INDEX = ROOT / "turnstone/console/static/index.html"


def extract_dialogs(index: Path, only_id: str | None = None) -> list[str]:
    """Every <dialog class="hatch ..."> block, verbatim from the tree."""
    html = index.read_text(encoding="utf-8")
    blocks = []
    for m in re.finditer(r"[ \t]*<dialog\s[^>]*class=\"[^\"]*\bhatch\b[^\"]*\"", html):
        end = html.index("</dialog>", m.start()) + len("</dialog>")
        block = html[m.start() : end]
        if only_id and f'id="{only_id}"' not in block:
            continue
        blocks.append(block)
    if not blocks:
        raise SystemExit(f"no dialog.hatch blocks found in {index}")
    return blocks


def extract_admin_fragment() -> str:
    """The console admin pane — the hatch-host all shelves live inside."""
    html = CONSOLE_INDEX.read_text(encoding="utf-8")
    start = html.index('<div id="admin-layout"')
    end = html.index("<!-- /admin-layout -->") + len("<!-- /admin-layout -->")
    return html[start:end]


def inject(template: str, marker: str, payload: str) -> str:
    begin = template.index(f"<!-- {marker}:BEGIN -->") + len(f"<!-- {marker}:BEGIN -->")
    end = template.index(f"<!-- {marker}:END -->")
    return template[:begin] + "\n" + payload + "\n" + template[end:]


def symlink(link: Path, target: Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


# --------------------------------------------------------------------------
# UI harness — the standalone app's dialog tier.  Drives the REAL cards.js
# controller for the batch surfaces so the production code path renders.
# --------------------------------------------------------------------------
UI_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>ui livepass</title>
    <link rel="stylesheet" href="shared/base.css" />
    <link rel="stylesheet" href="shared/ui-base.css" />
    <link rel="stylesheet" href="shared/chat.css" />
    <link rel="stylesheet" href="shared/conversation.css" />
    <link rel="stylesheet" href="shared/cards.css" />
    <link rel="stylesheet" href="static/style.css" />
    <link rel="stylesheet" href="shared/shell.css" />
    <link rel="stylesheet" href="shared/interactive.css" />
    <link rel="stylesheet" href="shared/hatch.css" />
  </head>
  <body>
    <!-- DIALOGS:BEGIN -->
    <!-- DIALOGS:END -->
    <div id="toast" role="status" aria-live="polite"></div>
    <script>
      window.authFetch = function (url) {
        // One canned failure so the results view shows the mixed state.
        var fail = url && url.indexOf("c3d4e5f6a1b2") !== -1;
        return Promise.resolve({
          ok: !fail,
          status: fail ? 409 : 200,
          headers: { get: function () { return "application/json"; } },
          json: function () { return Promise.resolve({}); },
          text: function () {
            return Promise.resolve(
              fail ? '{"error": "workstream is still running"}' : "",
            );
          },
        });
      };
      window.showToast = function (msg) { console.log("toast:", msg); };
    </script>
    <script type="module">
      import { openDialog, setBusy } from "./shared/hatch.js";
      const q = new URLSearchParams(location.search);
      if (q.get("theme") === "light")
        document.documentElement.dataset.theme = "light";
      const open = q.get("open") || "";
      function fill(id, text) {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
      }
      if (open === "new-ws" || open === "new-ws-fork") {
        const dlg = document.getElementById("new-ws-dialog");
        const canned = {
          "new-ws-model": ["sonnet-4-6", "gpt-5-2", "qwen3-32b"],
          "new-ws-judge-model": ["sonnet-4-6", "qwen3-32b"],
          "new-ws-skill": ["code-review (default)", "deep-research"],
        };
        for (const id in canned) {
          const s = document.getElementById(id);
          for (const n of canned[id]) {
            const o = document.createElement("option");
            o.value = n;
            o.textContent = n;
            s.appendChild(o);
          }
        }
        if (open === "new-ws-fork") {
          fill("new-ws-title", "Fork workstream");
          fill("new-ws-tag", "WS-FORK");
          document.getElementById("new-ws-submit").textContent = "Fork";
          const skillLabel = document.querySelector('label[for="new-ws-skill"]');
          if (skillLabel) skillLabel.hidden = true;
          document.getElementById("new-ws-skill").hidden = true;
          document.getElementById("new-ws-attach-row").hidden = true;
        }
        openDialog(dlg);
      } else if (open === "edit-title") {
        document.getElementById("edit-title-input").value =
          "lshell renovation pass 3";
        openDialog(document.getElementById("edit-title-dialog"));
      } else if (open === "delete-ws") {
        fill(
          "delete-ws-message",
          'Delete "lshell renovation pass 3"? This cannot be undone.',
        );
        openDialog(document.getElementById("delete-ws-dialog"));
      } else if (open === "revoke-mcp") {
        fill(
          "revoke-mcp-message",
          "Revoke the connection to github? Tools that need this server will require re-consent.",
        );
        openDialog(document.getElementById("revoke-mcp-dialog"));
      } else if (open === "ws-delete" || open === "ws-delete-results") {
        // Drive the REAL shared controller so the dialog renders through
        // the production code path (cards.js confirmSelection/confirm).
        const mod = await import("./shared/cards.js");
        const c = mod.createSavedCardsController({
          idPrefix: "ws-delete",
          buttonId: "ws-delete-btn",
          noun: "workstream",
          activateLabel: (s) => "Resume: " + (s.title || s.ws_id),
          render: () => {},
          buildDeleteRequest: (wsId) => ({
            url: "/v1/api/workstreams/" + encodeURIComponent(wsId) + "/delete",
            options: { method: "POST" },
          }),
        });
        c.setItems([
          { ws_id: "a1b2c3d4e5f6", title: "lshell renovation pass 3" },
          { ws_id: "b2c3d4e5f6a1", title: "canonical trajectory spike" },
          {
            ws_id: "c3d4e5f6a1b2",
            title:
              "a very long workstream title that should wrap " +
              "rather than punch out of the dialog box entirely",
          },
        ]);
        c.toggleAll();
        c.confirmSelection();
        if (open === "ws-delete-results") c.confirm();
      }
      if (q.get("busy")) {
        const d = document.querySelector("dialog[open]");
        if (d) setBusy(d, true);
      }
    </script>
  </body>
</html>
"""

# --------------------------------------------------------------------------
# Console harness — the admin pane fragment hosts the shelves (token-created
# included); dialog-tier markup outside the fragment (confirm/install/
# coord-delete) is injected via the RIDERS marker in build().
# model-save click-drives the submit: document.title flips to PUT-OK-<n>.
# --------------------------------------------------------------------------
CONSOLE_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>console livepass</title>
    <link rel="stylesheet" href="shared/base.css" />
    <link rel="stylesheet" href="shared/ui-base.css" />
    <link rel="stylesheet" href="console-static/style.css" />
    <link rel="stylesheet" href="shared/shell.css" />
    <link rel="stylesheet" href="shared/hatch.css" />
  </head>
  <body>
    <!-- The REAL L-shell chain (shell.js buildShell + pane.js DOM, verbatim
         class names) so the harness inherits production scroll geometry:
         .pane-body > #view-admin > .admin-layout height-pin the hatch-host
         and .admin-content is the pane's interior scroller.  Never replace
         this with bespoke height overrides — the clipped-pane / displaced-
         shelf regressions were invisible to the harness precisely because
         it used to pin #admin-layout with its own CSS. -->
    <div class="app">
      <aside class="rail" id="shell-rail">
        <div class="rail-brand">
          <button class="brand-home" type="button">
            <div class="brand-mark"></div>
            <span class="brand-name">turnstone</span>
            <span class="brand-sub">console</span>
          </button>
        </div>
      </aside>
      <main class="content">
        <div class="tabbar"></div>
        <div class="panes">
          <section class="pane">
            <!-- no .pane-head: PaneManager._mount builds section.pane >
                 div.pane-body only -->
            <div class="pane-body">
              <div id="view-admin">
                <!-- FRAGMENT:BEGIN -->
                <!-- FRAGMENT:END -->
              </div>
            </div>
          </section>
        </div>
      </main>
    </div>
    <!-- Body-level dialog tier (confirm / install / coord-delete): their
         markup sits OUTSIDE #admin-layout in index.html, so the fragment
         extraction misses them — build() injects every hatch dialog the
         fragment does not already contain. -->
    <!-- RIDERS:BEGIN -->
    <!-- RIDERS:END -->
    <div id="toast" role="status" aria-live="polite"></div>
    <script>
      (function () {
        function reply(data) {
          return Promise.resolve({
            ok: true,
            status: 200,
            headers: { get: function () { return "application/json"; } },
            json: function () { return Promise.resolve(data); },
            text: function () { return Promise.resolve(JSON.stringify(data)); },
          });
        }
        var SCHED = {
          task_id: "t1", name: "nightly-digest", description: "Morning digest",
          schedule_type: "cron", cron_expr: "0 6 * * 1,3,5", at_time: "",
          target_mode: "auto", model: "fable-5", skill: "daily-digest",
          initial_message: "Summarize overnight cluster activity.",
          auto_approve: false, enabled: true,
          notify_targets: [{ channel_type: "discord", channel_id: "8675309" }],
          next_run: "2026-06-10T06:00:00",
        };
        var MODEL = {
          definition_id: "def1", alias: "fable-5", model: "claude-fable-5",
          provider: "anthropic", base_url: "", context_window: 200000,
          capabilities: JSON.stringify({ supports_vision: true }),
          enabled: true, temperature: null, max_tokens: null,
          reasoning_effort: null, surface_persisted_reasoning: true,
          replay_reasoning_to_model: false,
        };
        window.__putCount = 0;
        window.authFetch = function (url, opts) {
          var method = (opts && opts.method) || "GET";
          if (method === "PUT" && url.indexOf("/model-definitions/def1") >= 0) {
            window.__putCount++;
            document.title = "PUT-OK-" + window.__putCount;
            return reply({ ok: true });
          }
          if (url.indexOf("/schedules/preview") >= 0)
            return reply({
              valid: true, error: "",
              next: [
                "2026-06-10T06:00:00+00:00",
                "2026-06-12T06:00:00+00:00",
                "2026-06-15T06:00:00+00:00",
              ],
            });
          if (url.indexOf("/schedules/t1") >= 0) return reply(SCHED);
          if (url.indexOf("/schedules") >= 0) return reply({ schedules: [SCHED] });
          if (url.indexOf("/model-capabilities/known") >= 0)
            return reply({ models: ["claude-fable-5", "claude-opus-4-8"] });
          if (url.indexOf("/model-capabilities?") >= 0)
            return reply({
              known: true,
              capabilities: {
                context_window: 200000, supports_tools: true,
                supports_vision: true,
                supports_web_search: true, supports_temperature: true,
                supports_effort: true,
              },
            });
          if (url.indexOf("/model-definitions/def1") >= 0) return reply(MODEL);
          if (url.indexOf("/model-definitions") >= 0) return reply({ models: [] });
          if (url.indexOf("/api/models") >= 0)
            return reply({ models: [
              { alias: "fable-5", model: "claude-fable-5" },
              { alias: "gpt-5.2", model: "gpt-5.2" },
            ] });
          if (url.indexOf("/skills") >= 0)
            return reply({ skills: [{ name: "daily-digest" }, { name: "ops-runbook" }] });
          if (url.indexOf("/policies") >= 0)
            return reply({ policies: [
              { policy_id: "p1", name: "deny-rm", tool_pattern: "bash*rm*",
                action: "deny", priority: 900, enabled: true },
              { policy_id: "p2", name: "default-ask", tool_pattern: "*",
                action: "ask", priority: 0, enabled: true },
            ] });
          return reply({});
        };
        window.showToast = function (m) {
          console.log("toast:", m);
          var t = document.getElementById("toast");
          t.textContent = m;
          t.classList.add("show");
        };
      })();
    </script>
    <script type="module" src="shared/utils.js"></script>
    <script type="module" src="shared/hatch.js"></script>
    <script src="console-static/admin.js"></script>
    <script src="console-static/governance.js"></script>
    <script>
      window.addEventListener("load", function () {
        var q = new URLSearchParams(location.search);
        if (q.get("theme") === "light")
          document.documentElement.dataset.theme = "light";
        var open = q.get("open") || "";
        // ?tall=1 — the scroll state: one panel visible with enough rows to
        // overflow the pane, so a screenshot shows .admin-content scrolling
        // (and a shelf staying docked above it).  Mirrors switchAdminTab's
        // one-panel-visible invariant without booting the tab loaders.
        if (q.get("tall")) {
          var panels = document.querySelectorAll(".admin-panel");
          for (var i = 0; i < panels.length; i++)
            panels[i].style.display =
              panels[i].id === "admin-users" ? "" : "none";
          // No fallback: a fragment rename must fail loudly, not misplace rows.
          var rowHost = document.querySelector("#admin-users [role=list]");
          rowHost.textContent = ""; // drop the static "Loading users…" stub
          for (var r = 0; r < 90; r++) {
            var row = document.createElement("div");
            row.className = "admin-row"; // real row chrome — geometry tracks production
            row.textContent =
              "user-" + String(r).padStart(3, "0") + " \\u00b7 synthetic row";
            rowHost.appendChild(row);
          }
          var content = document.getElementById("admin-content");
          if (content && q.get("scrolled"))
            content.scrollTop =
              q.get("scrolled") === "bottom"
                ? content.scrollHeight // the 24px scroll-tail state
                : content.scrollHeight / 2; // land mid-list
        }
        setTimeout(function () {
          if (open === "schedule-create") showCreateScheduleModal();
          else if (open === "schedule-edit") showEditScheduleModal("t1");
          else if (open === "model-create") showCreateModelModal();
          else if (open === "model-edit" || open === "model-save")
            showEditModelModal("def1");
          else if (open === "policy") {
            window._govPolicies && _govPolicies.length === 0 &&
              loadGovPolicies && loadGovPolicies();
            showCreatePolicyModal();
          } else if (open === "confirm")
            showConfirmModal(
              "Delete schedule",
              "Delete nightly-digest? Its run history is removed with it. This cannot be undone.",
              "Delete",
              function () {},
            );
          else if (open === "token")
            showTokenCreatedModal(
              "tsk_9f2e41c7a8b35d60e1f4a2b89c7d3e5f6a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d",
            );
          if (open === "model-save")
            setTimeout(function () {
              document.getElementById("model-create-submit").click();
            }, 900);
          if (q.get("busy"))
            setTimeout(function () {
              var d = document.querySelector("dialog[open]");
              if (d) window.TurnstoneHatch.setBusy(d, true);
            }, 400);
          // A driven state that ends with nothing open must fail LOUDLY in
          // the screenshot pipeline, not render a quietly dialog-less page.
          setTimeout(function () {
            var top = document.querySelector("dialog[open]");
            if (open && !top) document.title = "OPEN-FAILED-" + open;
            // &focuslast=1 — the displaced-dock regression probe: focus the
            // last form control in the shelf BODY (the visually-hidden
            // toggle/radio inputs live there).  Only .sh-body may scroll;
            // the head/foot strips must stay pinned in the screenshot.
            if (top && q.get("focuslast")) {
              var els = top.querySelectorAll(
                ".sh-body input, .sh-body select, .sh-body textarea",
              );
              if (els.length) els[els.length - 1].focus();
            }
          }, 600);
        }, 150);
      });
    </script>
  </body>
</html>
"""


# --------------------------------------------------------------------------
# Shell harness — the SPLIT-VIEW surface.  Unlike the ui/console pages (which
# embed extracted markup), this one boots the REAL shell.js + pane.js over
# stubbed classic seams and drives the split engine via ?split=.  Two demo
# conversational panes give the cells plausible content; the Dashboard pane
# (registered by the shell itself) fills the third cell in ?split=three.
# Loud-failure rule: the title stamps SPLIT-READY-<cells> only when the built
# state matches the request — a denied/failed split stamps SPLIT-FAILED-<why>.
# --------------------------------------------------------------------------
SHELL_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>shell livepass</title>
    <link rel="stylesheet" href="shared/base.css" />
    <link rel="stylesheet" href="shared/ui-base.css" />
    <link rel="stylesheet" href="shared/chat.css" />
    <link rel="stylesheet" href="shared/conversation.css" />
    <link rel="stylesheet" href="shared/cards.css" />
    <link rel="stylesheet" href="static/style.css" />
    <link rel="stylesheet" href="shared/shell.css" />
    <link rel="stylesheet" href="shared/interactive.css" />
  </head>
  <body>
    <div id="header"><div id="status-bar"></div><button id="theme-toggle">☾</button></div>
    <div id="breadcrumb"></div>
    <div id="main" style="padding: 18px">
      <h2 style="margin: 0 0 8px">Dashboard</h2>
      <p style="color: var(--ink-3)">
        Launcher + workstreams table live here (livepass stub).
      </p>
    </div>
    <div id="view-admin" style="display: none"></div>
    <script>
      window.TURNSTONE_SHELL_CAPS = { cluster: false, brandSub: "console" };
      window.TS_APP = {
        boot() {},
        getClusterState() { return { nodes: {} }; },
        onRender() {},
      };
      window.TS_ADMIN = {};
      var q = new URLSearchParams(location.search);
      if (q.get("theme") === "light")
        document.documentElement.dataset.theme = "light";
    </script>
    <script type="module" src="shared/shell.js"></script>
    <script type="module">
      const q = new URLSearchParams(location.search);
      for (let i = 0; i < 100 && !window.TS_SHELL; i++)
        await new Promise((r) => setTimeout(r, 20));
      if (!window.TS_SHELL) {
        document.title = "SPLIT-FAILED-no-shell";
      } else {
        sessionStorage.clear();
        const pm = window.TS_SHELL.panes;
        const { ShellPane } = await import("./shared/pane.js");
        const mkConv = (type, title, lines) => {
          pm.registerType(type, () => {
            const p = new ShellPane({ type, title });
            p.tabMenu = () => [
              { label: "Close pane", action: () => pm.close(p.id) },
            ];
            p.onMount = function () {
              const wrap = document.createElement("div");
              wrap.style.cssText =
                "flex:1;min-height:0;padding:16px;display:flex;flex-direction:column;gap:10px;overflow:auto;";
              for (const [role, text] of lines) {
                const d = document.createElement("div");
                d.className = "msg " + role;
                d.textContent = text;
                wrap.append(d);
              }
              // Edge-touching opaque chrome — the strip that occluded the
              // focus ring before the ::after overlay; keeps the bug class
              // visible in every future pass.
              const sb = document.createElement("div");
              sb.className = "ws-status-bar";
              sb.textContent = "17,418 / 393,216 (4.4%) · max 9 tools";
              this.bodyEl.append(wrap, sb);
            };
            return p;
          });
        };
        mkConv("repro", "repro-flaky-suite", [
          ["user", "Track down the flaky retry in the channel gateway tests."],
          [
            "assistant",
            "Three suspects so far — the debounce window in mcp_client, the " +
              "circuit-breaker reset, and the socket-mode reconnect. Bisecting now.",
          ],
          [
            "assistant",
            "Found it: the breaker reset races the stream pre-close. Patch incoming.",
          ],
        ]);
        mkConv("relnotes", "draft-1.6.2-notes", [
          ["user", "Draft the 1.6.2 patch notes from the merged PR list."],
          [
            "assistant",
            "Pulling #657–#662. Consent badge, orphan verb, MCP task hygiene, " +
              "the anthropic-compatible lane, and the mcp<2 cap.",
          ],
        ]);
        pm.openPane("repro");
        pm.openPane("relnotes");
        const want = q.get("split") || "right";
        let failed = null;
        if (want !== "none") {
          const r1 = pm.splitFocused("right");
          if (!r1.ok) failed = r1.reason;
          if (!failed && (want === "three" || want === "down")) {
            const r2 = pm.splitFocused("down");
            if (!r2.ok) failed = r2.reason;
          }
        }
        const cells = document.querySelectorAll(
          ".panes > section.pane:not([hidden])",
        ).length;
        document.title = failed
          ? "SPLIT-FAILED-" + failed
          : "SPLIT-READY-" + cells;
      }
    </script>
  </body>
</html>
"""


# --------------------------------------------------------------------------
# Attachments harness — the composer attachment chips + the sent-message
# attachment pills.  Both are driven through the REAL code paths so the preview
# nodes render exactly as production builds them: createAttachmentController's
# rehydrate() renders the chips (renderChip -> _applyPreview ->
# buildAttachmentPreview), and Pane.addUserMessage() renders the pills (which
# call the same window.buildAttachmentPreview).  The page frame is harness-only
# chrome and not under review; the chips row and the pill row are.
# --------------------------------------------------------------------------
ATTACH_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>attachments livepass</title>
    <link rel="stylesheet" href="shared/base.css" />
    <link rel="stylesheet" href="shared/ui-base.css" />
    <link rel="stylesheet" href="shared/interactive.css" />
    <link rel="stylesheet" href="shared/chat.css" />
    <style>
      /* Harness-only framing (NOT under review) — gives the two real surfaces
         a plausible page context at a realistic pane width. */
      body {
        padding: 24px; margin: 0; display: flex; flex-direction: column;
        gap: 28px; background: var(--bg); color: var(--fg);
        font-family: var(--font-sans, system-ui, sans-serif);
      }
      .demo-label {
        font: 11px var(--font-mono, monospace); color: var(--fg-dim);
        text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 8px;
      }
      .demo-frame {
        width: 560px; max-width: 100%; border: 1px solid var(--border);
        border-radius: var(--radius-sm); overflow: hidden;
        background: var(--bg-surface);
      }
      .messages { padding: 16px; }
      /* Static composer chrome for context; the chips row is built by the
         REAL createAttachmentController. */
      .demo-textarea {
        width: 100%; min-height: 44px; resize: none; background: var(--bg-elevated);
        color: var(--fg); border: 1px solid var(--border);
        border-radius: var(--radius-sm); padding: 8px; font: inherit;
      }
    </style>
  </head>
  <body>
    <div>
      <div class="demo-label">composer — attachment chips (real createAttachmentController)</div>
      <div class="demo-frame">
        <div class="composer">
          <div class="composer-chips" id="chips" role="list" aria-label="Attachments"></div>
          <div class="composer-row">
            <textarea class="demo-textarea" placeholder="Message…"></textarea>
          </div>
        </div>
      </div>
    </div>
    <div>
      <div class="demo-label">conversation — sent-message attachment pills (real Pane.addUserMessage)</div>
      <div class="demo-frame">
        <div class="messages" id="messages"></div>
      </div>
    </div>
    <div id="toast" role="status" aria-live="polite"></div>
    <script>
      // Committed-attachment fixtures (no `uploading`, real ids) — one of every
      // kind plus a deliberately long filename to probe chip truncation/wrap.
      window.__ATTACH = [
        { attachment_id: "att-image", kind: "image",
          filename: "observatory-dome.jpg", size_bytes: 184320 },
        { attachment_id: "att-pdf", kind: "pdf",
          filename: "q2-cluster-report.pdf", size_bytes: 529408 },
        { attachment_id: "att-audio", kind: "audio",
          filename: "standup-2026-06-15.m4a", size_bytes: 2202009 },
        { attachment_id: "att-text", kind: "text",
          filename: "release-notes-1.7.0a2.md", size_bytes: 4317 },
        { attachment_id: "att-longname", kind: "text",
          filename: "a-deliberately-long-attachment-filename-that-truncates.md",
          size_bytes: 8214 },
      ];
      window.__SNIPPET =
        "# Release notes \\u2014 1.7.0a2\\n\\nN-sample consensus voting lands behind " +
        "a flag; reranker-primary retrieval replaces RRF as the default; " +
        "coordinator memory is now keyed per user.";
      // image/pdf thumbnails + the audio clip load via element .src and are
      // served by the livepass fixture route; the text snippet is the only
      // preview that flows through authFetch, so the stub answers .../content.
      // Held under a private name: auth.js's legacy window bridge
      // (Object.assign(window, {authFetch})) runs at module-import time and
      // would clobber a plain window.authFetch — the module reinstates it
      // below, after the imports have evaluated.
      window.__attachFetch = function (url) {
        var path = (url || "").split("?")[0];
        function reply(ok, body, asText) {
          return Promise.resolve({
            ok: ok, status: ok ? 200 : 404,
            json: function () { return Promise.resolve(body || {}); },
            text: function () {
              return Promise.resolve(asText != null ? asText : "");
            },
          });
        }
        if (/\\/attachments$/.test(path))
          return reply(true, { attachments: window.__ATTACH });
        // Any text /content gets the snippet (audio /content is served as
        // bytes by the fixture route, never through authFetch).
        if (path.indexOf("/content") !== -1)
          return reply(true, {}, window.__SNIPPET);
        return reply(true, {});
      };
      window.toast = { error: function (m) { console.log("toast:", m); } };
    </script>
    <script type="module">
      import { createAttachmentController } from "./shared/composer_attachments.js";
      import { InteractivePane } from "./shared/interactive.js";
      const q = new URLSearchParams(location.search);
      if (q.get("theme") === "light")
        document.documentElement.dataset.theme = "light";

      // Reinstate the fixture fetch now the imports (and auth.js's window
      // bridge) have run — the pills' buildAttachmentPreview reads
      // window.authFetch directly for the text snippet.
      window.authFetch = window.__attachFetch;

      // composer chips — drive the REAL controller.  Pass the stub explicitly
      // so the chip path never depends on window.authFetch timing.
      const ctl = createAttachmentController({
        chipsEl: document.getElementById("chips"),
        getWsId: () => "demo-ws",
        authFetch: window.__attachFetch,
      });
      await ctl.rehydrate();

      // message pills — drive the REAL Pane.addUserMessage; stub only the
      // host seams (scroll/empty-state/action-row) that need a mounted pane.
      const pane = new InteractivePane("demo-ws");
      pane.messagesEl = document.getElementById("messages");
      pane.removeEmptyState = () => {};
      pane._addUserMsgActions = () => {};
      pane.scrollToBottom = () => {};
      pane.addUserMessage(
        "Please review the attached report, the dome photo, the standup " +
          "recording, and the release notes.",
        window.__ATTACH.slice(0, 4),
      );

      // Loud failure — a broken harness must not screenshot green.
      setTimeout(function () {
        const chips = document.querySelectorAll("#chips .composer-chip").length;
        const pills = document.querySelectorAll(
          "#messages .msg-user-attach-pill",
        ).length;
        document.title =
          chips && pills
            ? "ATTACH-READY-" + chips + "-" + pills
            : "ATTACH-FAILED-c" + chips + "-p" + pills;
      }, 800);
    </script>
  </body>
</html>
"""


# --------------------------------------------------------------------------
# Task-agent harness — the task_agent card: a task agent's sub-tool steps
# nested under its conversation row.  Driven through the REAL
# InteractivePane.handleEvent so the SSE->card ROUTING (_routeAgentItems /
# _ensureAgentCard, plus appendToolOutput finding the nested row by call_id)
# is exercised, not just the leaf builders.  The page frame is harness-only
# chrome; the .conv-batch / task_agent card is what's under review.
# --------------------------------------------------------------------------
TASKAGENT_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>task_agent livepass</title>
    <link rel="stylesheet" href="shared/base.css" />
    <link rel="stylesheet" href="shared/ui-base.css" />
    <link rel="stylesheet" href="shared/chat.css" />
    <link rel="stylesheet" href="shared/conversation.css" />
    <link rel="stylesheet" href="shared/cards.css" />
    <link rel="stylesheet" href="shared/interactive.css" />
    <style>
      /* Harness-only framing (NOT under review) — a plausible pane context. */
      body {
        padding: 24px; margin: 0; background: var(--bg); color: var(--ink);
        font-family: var(--font-sans, system-ui, sans-serif);
      }
      .demo-frame { max-width: 720px; margin: 0 auto; }
      .demo-label {
        font: 11px var(--font-mono, monospace); color: var(--ink-3);
        text-transform: uppercase; letter-spacing: 0.08em; margin: 0 0 8px;
      }
    </style>
  </head>
  <body>
    <div class="demo-frame">
      <div class="demo-label">conversation — task_agent card (real InteractivePane.handleEvent)</div>
      <div class="messages" id="messages"></div>
    </div>
    <script>
      // interactive.js reads window.toast / window.authFetch; the static render
      // never POSTs, so no-op stubs are enough.
      window.toast = { error: function (m) { console.log("toast:", m); } };
      window.authFetch = function () {
        return Promise.resolve({
          ok: true,
          json: function () { return Promise.resolve({}); },
          text: function () { return Promise.resolve(""); },
        });
      };
    </script>
    <script type="module">
      import { InteractivePane } from "./shared/interactive.js";
      const q = new URLSearchParams(location.search);
      if (q.get("theme") === "light")
        document.documentElement.dataset.theme = "light";

      const messages = document.getElementById("messages");
      try {
        // Drive the REAL pane; stub only the host seams a mounted pane provides.
        const pane = new InteractivePane("demo-ws");
        pane.messagesEl = messages;
        pane.inputEl = document.createElement("textarea");
        pane.sendBtn = document.createElement("button");
        pane.isNearBottom = () => false;
        pane.scrollToBottom = () => {};
        pane.removeEmptyState = () => {};
        pane.removeThinkingIndicator = () => {};
        pane.setBusy = () => {};
        const ev = (e) => pane.handleEvent(e);

        // ?recall=1: exercise the RECALL path — replayHistory rebuilding the
        // card from the /history `agent_steps` overlay (a reload / reopen while
        // the ws is still in memory), as opposed to the live SSE path below.
        const recall = q.get("recall") === "1";
        if (recall) {
          pane.replayHistory([
            { role: "user", content: "Find all call sites of resolve_alias and summarize them" },
            { role: "assistant", tool_calls: [{
              name: "task_agent", id: "task1",
              arguments: JSON.stringify({ prompt: "Find call sites of resolve_alias" }),
              agent_steps: [
                { id: "task1::c1", name: "search", arguments: JSON.stringify({ query: "resolve_alias" }), output: "12 matches across 4 files", is_error: false },
                { id: "task1::c2", name: "read_file", arguments: JSON.stringify({ path: "core/registry.py" }), output: "4.1 KB read", is_error: false },
                { id: "task1::c3", name: "bash", arguments: JSON.stringify({ command: "pytest -k registry" }), output: "12 passed in 1.2s", is_error: false },
                { id: "task1::c4", name: "notify", arguments: JSON.stringify({ channel: "#eng", message: "post summary" }), output: "posted to #eng", is_error: false },
              ],
            }] },
            { role: "tool", tool_call_id: "task1", content: "resolve_alias has 4 call sites (registry.py:120, session.py:12200, model_registry.py:88, eval.py:54); all pass a validated alias before use." },
          ]);
        } else if (q.get("race") === "1") {
          // ?race=1: reproduce the parallel-pool ordering window — each
          // sub-tool's tool_pending is emitted exactly once (as in production)
          // but AHEAD of the task_agent row paint, as happens when a pooled
          // sub-agent's SSE event is handled before its parent row commits.
          // The orphan buffer must hold them and nest them when the parent row
          // lands; pre-fix they escaped to top-level rows and the card came up
          // short (steps < 4 -> TASKAGENT-FAILED), so this can't screenshot
          // green without the fix.
          const raceTask = {
            call_id: "task1", func_name: "task_agent",
            header: 'task_agent: "Find all call sites of resolve_alias and summarize them"',
            needs_approval: false,
          };
          const childPending = (cid, fn, header) =>
            ev({ type: "tool_pending", items: [{ call_id: cid, parent_call_id: "task1", func_name: fn, header: header, needs_approval: false }] });
          // a) Orphan child pendings arrive first — no parent row yet.
          childPending("task1::c1", "search", 'search: "resolve_alias"');
          childPending("task1::c2", "read_file", "read_file: core/registry.py");
          childPending("task1::c3", "bash", "pytest -k registry");
          childPending("task1::c4", "notify", "notify: post summary to #eng");
          // b) Parent task_agent row paints (pending -> resolved): must flush the
          //    buffered orphans into the card AND survive the upgrade rebuild.
          ev({ type: "tool_pending", items: [raceTask] });
          ev({ type: "tool_info", items: [Object.assign({ auto_approved: false }, raceTask)] });
          // c) Results + a streamed chunk follow, nesting into the flushed rows.
          ev({ type: "tool_result", call_id: "task1::c1", parent_call_id: "task1", name: "search", output: "12 matches across 4 files" });
          ev({ type: "tool_result", call_id: "task1::c2", parent_call_id: "task1", name: "read_file", output: "4.1 KB read" });
          ev({ type: "tool_output_chunk", call_id: "task1::c3", parent_call_id: "task1", chunk: "collected 12 items ... " });
          ev({ type: "tool_result", call_id: "task1::c3", parent_call_id: "task1", name: "bash", output: "12 passed in 1.2s" });
          ev({ type: "tool_result", call_id: "task1::c4", parent_call_id: "task1", name: "notify", output: "posted to #eng" });
          ev({ type: "tool_result", call_id: "task1", name: "task_agent", output: "resolve_alias has 4 call sites (registry.py:120, session.py:12200, model_registry.py:88, eval.py:54); all pass a validated alias before use." });
        } else if (q.get("orphan") === "1") {
          // ?orphan=1: the SAFETY VALVE — child steps whose task_agent row
          // NEVER paints (an id-correlation mismatch, or an agent aborted
          // before its row painted).  They must not vanish: after the grace
          // window the buffer escapes them to visible top-level rows (the
          // pre-buffer behaviour) rather than holding them forever.  The parent
          // task_agent row is deliberately never emitted here.
          const orphanPending = (cid, fn, header) =>
            ev({ type: "tool_pending", items: [{ call_id: cid, parent_call_id: "task1", func_name: fn, header: header, needs_approval: false }] });
          orphanPending("task1::c1", "search", 'search: "resolve_alias"');
          orphanPending("task1::c2", "read_file", "read_file: core/registry.py");
          orphanPending("task1::c3", "bash", "pytest -k registry");
        } else {

        // 1. Parent paints the task_agent call (a top-level tool row).
        const taskItem = {
          call_id: "task1", func_name: "task_agent",
          header: 'task_agent: "Find all call sites of resolve_alias and summarize them"',
          needs_approval: false,
        };
        // ?parallel=1 puts the task_agent in a 2-tool parallel batch so the
        // nested-step rail-bleed fix can be verified against the rail rules.
        const parentItems = q.get("parallel") === "1"
          ? [taskItem, { call_id: "sib1", func_name: "bash", header: "git status", needs_approval: false }]
          : [taskItem];
        ev({ type: "tool_pending", items: parentItems });
        ev({ type: "tool_info", items: parentItems.map((it) => Object.assign({ auto_approved: false }, it)) });
        if (parentItems.length > 1)
          ev({ type: "tool_result", call_id: "sib1", name: "bash", output: "clean" });

        // 2. Sub-agent steps tagged parent_call_id="task1" — exercises routing.
        function stepRow(cid, fn, header, result) {
          ev({ type: "tool_pending", items: [{ call_id: cid, parent_call_id: "task1", func_name: fn, header: header, needs_approval: false }] });
          if (result != null)
            ev({ type: "tool_result", call_id: cid, parent_call_id: "task1", name: fn, output: result });
        }
        stepRow("task1::c1", "search", 'search: "resolve_alias"', "12 matches across 4 files");
        stepRow("task1::c2", "read_file", "read_file: core/registry.py", "4.1 KB read");
        ev({ type: "tool_pending", items: [{ call_id: "task1::c3", parent_call_id: "task1", func_name: "bash", header: "pytest -k registry", needs_approval: false }] });
        ev({ type: "tool_output_chunk", call_id: "task1::c3", parent_call_id: "task1", chunk: "collected 12 items ... " });
        ev({ type: "tool_result", call_id: "task1::c3", parent_call_id: "task1", name: "bash", output: "12 passed in 1.2s" });
        // 4th step.  Default: a nested sub-tool approval (notify is not
        // auto-approved) — the pane must auto-expand the collapse-by-default
        // card so the blocking prompt is visible.  ?collapsed=1: a plain
        // completed step instead, so nothing forces the card open and the
        // screenshot shows the natural collapsed state (the common case).
        if (q.get("collapsed") === "1") {
          stepRow("task1::c4", "notify", "notify: post summary to #eng", "posted to #eng");
        } else {
          ev({ type: "approve_request", judge_pending: false, items: [{ call_id: "task1::c4", parent_call_id: "task1", func_name: "notify", header: "notify: post summary to #eng", needs_approval: true }] });
        }

        // 3. The task agent's own synthesis, rendered below the card.
        ev({ type: "tool_result", call_id: "task1", name: "task_agent", output: "resolve_alias has 4 call sites (registry.py:120, session.py:12200, model_registry.py:88, eval.py:54); all pass a validated alias before use." });
        }

        // ?expand=1: open every card so a screenshot shows the nested steps
        // (cards collapse by default; recall has no approval to auto-expand).
        if (q.get("expand") === "1") {
          document.querySelectorAll(".conv-agent").forEach(function (c) {
            c.dataset.collapsed = "false";
            const t = c.querySelector(".conv-agent-toggle");
            if (t) t.setAttribute("aria-expanded", "true");
          });
        }

        // Loud failure — broken routing must not screenshot green.
        const orphanMode = q.get("orphan") === "1";
        setTimeout(function () {
          if (orphanMode) {
            // The parent never painted; after the grace window the buffered
            // steps must have ESCAPED to visible top-level rows, not vanished.
            const escaped = document.querySelectorAll('.conv-batch .conv-row[data-call-id^="task1::"]').length;
            const leaked = document.querySelector('.conv-row[data-call-id="task1"] .conv-agent');
            document.title = escaped >= 3 && !leaked
              ? "TASKAGENT-ORPHANS-ESCAPED-" + escaped
              : "TASKAGENT-FAILED-escaped" + escaped + "-card" + (leaked ? 1 : 0);
            return;
          }
          const row = document.querySelector('.conv-row[data-call-id="task1"]');
          const card = row && row.querySelector(".conv-agent");
          const steps = card ? card.querySelectorAll(".conv-agent-body .conv-row").length : 0;
          const hasResult = !!(row && /call sites/.test(row.textContent || ""));
          document.title = card && steps >= 4 && hasResult
            ? "TASKAGENT-READY-" + steps
            : "TASKAGENT-FAILED-card" + (card ? 1 : 0) + "-steps" + steps + "-result" + (hasResult ? 1 : 0);
        }, orphanMode ? 900 : 300);
      } catch (e) {
        messages.textContent = "HARNESS ERROR: " + e.message + "\\n" + (e.stack || "");
        document.title = "TASKAGENT-ERROR";
      }
    </script>
  </body>
</html>
"""


# --------------------------------------------------------------------------
# Perf harness — long-session performance baseline for the interactive pane.
# Mounts the REAL InteractivePane (production DOM via _createDOM, production
# CSS chain) in a fixed-height mount so .pane-messages has REAL scroll
# geometry — the forced-layout costs under measurement (isNearBottom /
# scrollToBottom / chunk-append scroll pins) only exist against live layout,
# which is why nothing here stubs scroll/geometry the way the task-agent
# harness does.  All timing is real time (see MEASUREMENT RULES in the module
# docstring).  Workload is deterministic (seeded LCG) so runs are comparable.
# --------------------------------------------------------------------------
PERF_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>perf livepass</title>
    <link rel="stylesheet" href="shared/base.css" />
    <link rel="stylesheet" href="shared/ui-base.css" />
    <link rel="stylesheet" href="shared/chat.css" />
    <link rel="stylesheet" href="shared/conversation.css" />
    <link rel="stylesheet" href="shared/cards.css" />
    <link rel="stylesheet" href="static/style.css" />
    <link rel="stylesheet" href="shared/interactive.css" />
    <style>
      /* Harness-only framing (NOT under review): a fixed-height mount so the
         pane's .pane-messages scroller has real production geometry. */
      body { margin: 0; background: var(--bg); color: var(--fg); }
      #mount { height: 720px; width: 920px; display: flex; overflow: hidden; }
      #mount > .pane { flex: 1; display: flex; flex-direction: column; min-height: 0; }
      #perf-json { font: 11px monospace; white-space: pre-wrap; padding: 12px; }
    </style>
  </head>
  <body>
    <div id="mount"></div>
    <pre id="perf-json">running…</pre>
    <script>
      window.toast = { error: function (m) { console.log("toast:", m); } };
      // Collect every uncaught error/rejection into the report — a perf run
      // that silently swallowed a pipeline exception must not read as clean.
      window.__perfErrors = [];
      window.onerror = function (msg, src, line) {
        window.__perfErrors.push(String(msg) + " @ " + (src || "?") + ":" + (line || 0));
      };
      window.addEventListener("unhandledrejection", function (e) {
        window.__perfErrors.push("unhandledrejection: " + String(e && e.reason));
      });
      window.__perfFetch = function () {
        return Promise.resolve({
          ok: true, status: 200,
          json: function () { return Promise.resolve({}); },
          text: function () { return Promise.resolve(""); },
        });
      };
      window.authFetch = window.__perfFetch;
    </script>
    <script type="module">
      import { InteractivePane } from "./shared/interactive.js";
      // auth.js's legacy window bridge clobbers window.authFetch at module
      // import time — reinstate the stub now imports have evaluated (same
      // dance as the attachments harness).
      window.authFetch = window.__perfFetch;

      const q = new URLSearchParams(location.search);
      const N = parseInt(q.get("n") || "1000", 10);
      const TURNS = parseInt(q.get("turns") || "20", 10);
      const CHUNKS = parseInt(q.get("chunks") || "300", 10);
      const CYCLES = parseInt(q.get("cycles") || "3", 10);
      const IDLE = parseInt(q.get("idle") || "20", 10);

      // Long-task accounting across every phase (>50ms main-thread blocks).
      const lt = { count: 0, total_ms: 0, max_ms: 0 };
      try {
        new PerformanceObserver(function (list) {
          list.getEntries().forEach(function (e) {
            lt.count += 1;
            lt.total_ms += Math.round(e.duration);
            lt.max_ms = Math.max(lt.max_ms, Math.round(e.duration));
          });
        }).observe({ type: "longtask", buffered: true });
      } catch (e) { /* unsupported — longtasks stay zeroed */ }

      // Deterministic workload (seeded LCG) so runs are comparable.
      let _seed = 42;
      function rnd() {
        _seed = (_seed * 1664525 + 1013904223) >>> 0;
        return _seed / 4294967296;
      }
      const WORDS = ("the retry loop grinds the dungeon server while the " +
        "judge weighs verdicts and the coordinator shuffles children across " +
        "nodes tokens accumulate compaction folds turns storage keeps the " +
        "canon and the rail repaints").split(" ");
      function sentence(w) {
        const parts = [];
        for (let i = 0; i < w; i++) parts.push(WORDS[(rnd() * WORDS.length) | 0]);
        return parts.join(" ");
      }
      // Realistic assistant markdown: prose + list + fenced code (varying
      // content so the hljs cache behaves as in production) + inline code.
      function mdBody(i) {
        return (
          "Turn " + i + ": " + sentence(18) + ".\\n\\n" +
          "- " + sentence(6) + "\\n- " + sentence(7) + "\\n\\n" +
          "```python\\n" +
          "def step_" + i + "(depth):\\n" +
          "    total = " + ((rnd() * 1000) | 0) + "\\n" +
          "    for k in range(depth):\\n" +
          "        total += k * " + (1 + ((rnd() * 9) | 0)) + "\\n" +
          "    return total\\n" +
          "```\\n\\n" +
          sentence(14) + " `inline_" + i + "` " + sentence(8) + "."
        );
      }
      // History in the canonical projected wire shape replayHistory consumes
      // (user / assistant content / assistant tool_calls / tool result), with
      // periodic reasoning bubbles and task_agent cards (agent_steps overlay).
      function buildHistory(n) {
        const msgs = [];
        let i = 0;
        while (msgs.length < n) {
          i += 1;
          msgs.push({ role: "user", content: "Request " + i + ": " + sentence(10) + "?" });
          if (msgs.length >= n) break;
          if (i % 10 === 0) {
            msgs.push({ role: "assistant", reasoning: sentence(40) + ".", content: mdBody(i) });
          } else {
            msgs.push({ role: "assistant", content: mdBody(i) });
          }
          if (msgs.length >= n) break;
          const callId = "h" + i;
          if (i % 8 === 0) {
            msgs.push({ role: "assistant", tool_calls: [{
              name: "task_agent", id: callId,
              arguments: JSON.stringify({ prompt: "subtask " + i }),
              agent_steps: [
                { id: callId + "::c1", name: "search",
                  arguments: JSON.stringify({ query: "q" + i }),
                  output: sentence(8), is_error: false },
                { id: callId + "::c2", name: "read_file",
                  arguments: JSON.stringify({ path: "core/f" + i + ".py" }),
                  output: sentence(6), is_error: false },
                { id: callId + "::c3", name: "bash",
                  arguments: JSON.stringify({ command: "pytest -k t" + i }),
                  output: sentence(7), is_error: false },
              ],
            }] });
          } else {
            msgs.push({ role: "assistant", tool_calls: [{
              name: "bash", id: callId,
              arguments: JSON.stringify({ command: "grep -rn pattern_" + i + " src/" }),
            }] });
          }
          if (msgs.length >= n) break;
          msgs.push({ role: "tool", tool_call_id: callId,
            content: "output " + i + ":\\n" + sentence(20) });
        }
        return msgs;
      }

      const tick = () => new Promise((r) => requestAnimationFrame(r));
      // One live turn, production event mix: thinking indicator, reasoning
      // deltas, content deltas (yield every few so streamingRender's internal
      // rAF actually applies frames, as in a real token stream), stream_end,
      // an auto-approved bash batch with streamed chunks, every 5th turn a
      // task_agent card with routed children, then the idle edge.
      async function stormTurn(pane, i) {
        pane.handleEvent({ type: "state_change", state: "running" });
        pane.handleEvent({ type: "thinking_start" });
        const reason = sentence(50);
        let d = 0;
        for (let k = 0; k < reason.length; k += 20) {
          pane.handleEvent({ type: "reasoning", text: reason.slice(k, k + 20) });
          d += 1;
          if (d % 4 === 3) await tick();
        }
        const body = mdBody(100000 + i);
        d = 0;
        for (let k = 0; k < body.length; k += 22) {
          pane.handleEvent({ type: "content", text: body.slice(k, k + 22) });
          d += 1;
          if (d % 6 === 5) await tick();
        }
        pane.handleEvent({ type: "stream_end" });
        const callId = "s" + i;
        const item = { call_id: callId, func_name: "bash",
          header: "bash: run step " + i, needs_approval: false };
        pane.handleEvent({ type: "tool_pending", items: [item] });
        pane.handleEvent({ type: "tool_info",
          items: [Object.assign({ auto_approved: true }, item)] });
        for (let k = 0; k < 24; k++) {
          pane.handleEvent({ type: "tool_output_chunk", call_id: callId,
            chunk: "line " + k + ": " + sentence(5) + "\\n" });
          if (k % 6 === 5) await tick();
        }
        pane.handleEvent({ type: "tool_result", call_id: callId, name: "bash",
          output: "done " + i + "\\n" + sentence(12) });
        if (i % 5 === 4) {
          const tid = "sa" + i;
          const titem = { call_id: tid, func_name: "task_agent",
            header: 'task_agent: "subtask ' + i + '"', needs_approval: false };
          pane.handleEvent({ type: "tool_pending", items: [titem] });
          pane.handleEvent({ type: "tool_info",
            items: [Object.assign({ auto_approved: true }, titem)] });
          for (let c = 1; c <= 3; c++) {
            const cid = tid + "::c" + c;
            pane.handleEvent({ type: "tool_pending", items: [{
              call_id: cid, parent_call_id: tid, func_name: "search",
              header: "search: q" + c, needs_approval: false }] });
            pane.handleEvent({ type: "tool_result", call_id: cid,
              parent_call_id: tid, name: "search", output: sentence(6) });
          }
          pane.handleEvent({ type: "tool_result", call_id: tid,
            name: "task_agent", output: sentence(15) });
          await tick();
        }
        pane.handleEvent({ type: "state_change", state: "idle" });
        await tick();
      }

      function heapBytes() {
        // --js-flags=--expose-gc makes this a real floor, not GC noise.
        if (typeof window.gc === "function") {
          try { window.gc(); window.gc(); } catch (e) { /* noop */ }
        }
        return (performance.memory && performance.memory.usedJSHeapSize) || null;
      }

      const report = {
        n: N, turns: TURNS, chunks: CHUNKS, cycles: CYCLES, idle: IDLE,
        // Echoed run token — the runner validates it so a straggler POST
        // from a killed prior attempt can't be misattributed to this run.
        run: q.get("run") || "",
        errors: window.__perfErrors,
      };
      let phase = "mount";
      try {
        const pane = new InteractivePane("perf-ws");
        document.getElementById("mount").appendChild(pane.el);
        const msgs = buildHistory(N);
        report.heap_start = heapBytes();

        phase = "replay";
        let t0 = performance.now();
        pane.replayHistory(msgs);
        report.replay_ms = Math.round(performance.now() - t0);
        await tick();
        report.nodes_after_replay = pane.messagesEl.querySelectorAll("*").length;

        phase = "storm";
        t0 = performance.now();
        for (let i = 0; i < TURNS; i++) await stormTurn(pane, i);
        report.storm_ms = Math.round(performance.now() - t0);
        report.storm_ms_per_turn = Math.round(report.storm_ms / TURNS);

        phase = "chunkstorm";
        const ccItem = { call_id: "cc1", func_name: "bash",
          header: "bash: tail -f build.log", needs_approval: false };
        pane.handleEvent({ type: "tool_pending", items: [ccItem] });
        pane.handleEvent({ type: "tool_info",
          items: [Object.assign({ auto_approved: true }, ccItem)] });
        t0 = performance.now();
        for (let k = 0; k < CHUNKS; k++) {
          pane.handleEvent({ type: "tool_output_chunk", call_id: "cc1",
            chunk: "log line " + k + "\\n" });
          if (k % 6 === 5) await tick();
        }
        report.chunk_ms = Math.round(performance.now() - t0);
        pane.handleEvent({ type: "tool_result", call_id: "cc1", name: "bash",
          output: "tail done" });

        phase = "idlechurn";
        t0 = performance.now();
        for (let k = 0; k < IDLE; k++) {
          pane.handleEvent({ type: "state_change", state: "running" });
          pane.handleEvent({ type: "state_change", state: "idle" });
          if (k % 4 === 3) await tick();
        }
        report.idle_ms = Math.round(performance.now() - t0);

        // Leak probe: repeated full replays of the SAME history should
        // converge to a flat heap/node/agent-card profile; monotonic growth
        // here is retained-detached-DOM (the _agentCards class of bug).
        phase = "replaycycles";
        report.cycle_stats = [];
        for (let c = 0; c < CYCLES; c++) {
          t0 = performance.now();
          pane.replayHistory(msgs);
          const ms = Math.round(performance.now() - t0);
          await tick();
          report.cycle_stats.push({
            replay_ms: ms,
            heap: heapBytes(),
            nodes: pane.messagesEl.querySelectorAll("*").length,
            agent_cards: pane._agentCards ? pane._agentCards.size : 0,
          });
        }
        report.heap_end = heapBytes();
        report.longtasks = lt;
        document.title = "PERF-READY-" + N;
      } catch (e) {
        window.__perfErrors.push(
          "phase " + phase + ": " + (e && e.message ? e.message : String(e)),
        );
        report.failed_phase = phase;
        report.longtasks = lt;
        document.title = "PERF-FAILED-" + phase;
      }
      document.getElementById("perf-json").textContent =
        JSON.stringify(report, null, 2);
      if (q.get("post")) {
        try {
          await fetch("/perf/report", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(report),
          });
        } catch (e) { /* runner captures the timeout instead */ }
      }
    </script>
  </body>
</html>
"""


# Fixture media for the attachments harness.  image/pdf thumbnails and the
# audio clip load via element .src (NOT authFetch), so the --serve dev server
# answers those paths directly with representative bytes: a photo-like image,
# a document-page-like image for the PDF thumbnail, and a short WAV so the
# native <audio> control chrome renders against the constrained CSS height.
_FIXTURE_CACHE: dict[str, bytes] = {}


def _png_photo() -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (320, 320), (24, 27, 31))
    d = ImageDraw.Draw(img)
    for y in range(320):  # warm vertical wash so object-fit crop is legible
        t = y / 320
        d.line([(0, y), (320, y)], fill=(int(20 + t * 60), int(18 + t * 40), int(26 + t * 70)))
    d.ellipse([180, 36, 300, 156], fill=(229, 160, 66))  # amber "sun"
    d.polygon([(0, 320), (130, 170), (250, 320)], fill=(38, 66, 58))  # hill
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _png_page() -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (320, 414), (250, 250, 248))  # paper white, A4-ish
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 320, 9], fill=(140, 94, 27))  # header rule
    y = 30
    for i, w in enumerate([260, 240, 280, 200, 250, 230, 270, 180, 255, 210, 240]):
        shade = (40, 44, 54) if i == 0 else (150, 154, 164)
        d.rectangle([28, y, 28 + w, y + 10], fill=shade)
        y += 30
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _wav() -> bytes:
    import math
    import struct
    import wave
    from io import BytesIO

    buf = BytesIO()
    frames = b"".join(
        struct.pack("<h", int(2600 * math.sin(2 * math.pi * 440 * i / 8000))) for i in range(8000)
    )
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(frames)
    return buf.getvalue()


def _fixture_for(path: str) -> tuple[bytes, str] | None:
    """Map a media request path to (bytes, content-type), or None to fall through."""
    if path.endswith("/thumbnail"):
        key = "page" if "att-pdf" in path else "photo"
        if key not in _FIXTURE_CACHE:
            _FIXTURE_CACHE[key] = _png_page() if key == "page" else _png_photo()
        return _FIXTURE_CACHE[key], "image/png"
    if path.endswith("/content") and "att-audio" in path:
        if "wav" not in _FIXTURE_CACHE:
            _FIXTURE_CACHE["wav"] = _wav()
        return _FIXTURE_CACHE["wav"], "audio/wav"
    return None


def build(out: Path) -> None:
    ui = out / "ui"
    con = out / "console"
    ui.mkdir(parents=True, exist_ok=True)
    con.mkdir(parents=True, exist_ok=True)

    symlink(ui / "shared", ROOT / "turnstone/shared_static")
    symlink(ui / "static", ROOT / "turnstone/ui/static")
    blocks = extract_dialogs(UI_INDEX)
    # the coordinator batch dialog shares the cards.js builder — ride along
    blocks += extract_dialogs(CONSOLE_INDEX, only_id="coord-delete-dialog")
    (ui / "livepass.html").write_text(
        inject(UI_TEMPLATE, "DIALOGS", "\n".join(blocks)), encoding="utf-8"
    )
    print(f"{ui}/livepass.html — {len(blocks)} dialogs")

    symlink(con / "shared", ROOT / "turnstone/shared_static")
    symlink(con / "console-static", ROOT / "turnstone/console/static")
    frag = extract_admin_fragment()
    # Dialog-tier markup living OUTSIDE #admin-layout (confirm, install,
    # coord-delete) would otherwise be silently absent — and ?open=confirm
    # would screenshot a dialog-less page while the gate stayed green.
    riders = [b for b in extract_dialogs(CONSOLE_INDEX) if b not in frag]
    page = inject(CONSOLE_TEMPLATE, "FRAGMENT", frag)
    page = inject(page, "RIDERS", "\n".join(riders))
    (con / "livepass.html").write_text(page, encoding="utf-8")
    print(f"{con}/livepass.html — admin fragment + {len(riders)} rider dialogs")

    sh = out / "shell"
    sh.mkdir(parents=True, exist_ok=True)
    symlink(sh / "shared", ROOT / "turnstone/shared_static")
    symlink(sh / "static", ROOT / "turnstone/console/static")
    (sh / "livepass.html").write_text(SHELL_TEMPLATE, encoding="utf-8")
    print(f"{sh}/livepass.html — split-view shell surface")

    att = out / "attachments"
    att.mkdir(parents=True, exist_ok=True)
    symlink(att / "shared", ROOT / "turnstone/shared_static")
    (att / "livepass.html").write_text(ATTACH_TEMPLATE, encoding="utf-8")
    print(f"{att}/livepass.html — composer chips + message attachment pills")

    ta = out / "taskagent"
    ta.mkdir(parents=True, exist_ok=True)
    symlink(ta / "shared", ROOT / "turnstone/shared_static")
    (ta / "livepass.html").write_text(TASKAGENT_TEMPLATE, encoding="utf-8")
    print(f"{ta}/livepass.html — task_agent card (real Pane.handleEvent routing)")

    pf = out / "perf"
    pf.mkdir(parents=True, exist_ok=True)
    symlink(pf / "shared", ROOT / "turnstone/shared_static")
    symlink(pf / "static", ROOT / "turnstone/ui/static")
    (pf / "livepass.html").write_text(PERF_TEMPLATE, encoding="utf-8")
    print(f"{pf}/livepass.html — long-session perf baseline (real InteractivePane)")


class _PerfStore:
    """Rendezvous for the perf page's POSTed JSON report."""

    def __init__(self) -> None:
        import threading

        self.event = threading.Event()
        self.data: dict[str, object] | None = None


class _HarnessHandler(http.server.SimpleHTTPRequestHandler):
    """Static file server + attachment media fixtures + perf-report sink.

    The attachments harness loads thumbnails + the audio clip via element
    .src; serve those from generated fixtures, fall through to static for
    everything else.  The perf harness POSTs its JSON report to /perf/report
    when driven with ?post=1 — the --perf runner blocks on ``perf_store``.
    """

    perf_store: _PerfStore | None = None
    quiet = False

    def do_GET(self) -> None:  # noqa: N802 (stdlib casing)
        blob = _fixture_for(self.path.split("?")[0])
        if blob is None:
            super().do_GET()
            return
        data, ctype = blob
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 (stdlib casing)
        store = type(self).perf_store
        if self.path.split("?")[0] != "/perf/report" or store is None:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            store.data = json.loads(body)
        except ValueError:
            store.data = {"errors": ["runner: unparseable report body"]}
        store.event.set()
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (stdlib signature)
        if not type(self).quiet:
            super().log_message(format, *args)


def _find_chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _await_report(
    store: _PerfStore, proc: subprocess.Popen[bytes], run_token: str, timeout: float
) -> dict[str, object] | None:
    """Wait for THIS attempt's report: validated by run token, bailing early
    when Chrome exits without reporting (the sandbox-startup-failure case —
    waiting the full timeout there cost minutes before the --no-sandbox
    fallback could even start).  A straggler POST from a previous attempt
    (its handler thread can complete after the next attempt cleared the
    store) carries the wrong token and is discarded instead of being
    misattributed to this run."""
    deadline = time.monotonic() + timeout
    proc_exited_at: float | None = None
    while time.monotonic() < deadline:
        if store.event.wait(0.5):
            data = store.data
            store.event.clear()
            store.data = None
            if isinstance(data, dict) and data.get("run") == run_token:
                return data
            continue  # stale straggler from a prior attempt — keep waiting
        if proc.poll() is not None:
            now = time.monotonic()
            if proc_exited_at is None:
                proc_exited_at = now  # grace: an in-flight POST may still land
            elif now - proc_exited_at > 3.0:
                return None  # exited without reporting — try the next attempt
    return None


def _perf_run_one(
    chrome: str, out: Path, port: int, store: _PerfStore, n: int, turns: int, timeout: float
) -> dict[str, object] | None:
    """One headless-Chrome perf pass; returns the page's report or None."""
    base_flags = [
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1440,900",
        "--no-first-run",
        "--disable-extensions",
        # Throttled timers/rAF in a backgrounded renderer would corrupt the
        # measurement — pin the renderer foreground-scheduled.
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        # Stable, real heap numbers (heapBytes() calls window.gc() first).
        "--js-flags=--expose-gc",
        "--enable-precise-memory-info",
    ]
    for attempt, extra in enumerate(
        ([], ["--no-sandbox"])  # sandboxed first, container fallback second
    ):
        run_token = f"n{n}-a{attempt}-{uuid.uuid4().hex[:8]}"
        url = (
            f"http://127.0.0.1:{port}/perf/livepass.html?n={n}&turns={turns}&post=1&run={run_token}"
        )
        store.event.clear()
        store.data = None
        profile = out / f".chrome-perf-{n}"
        proc = subprocess.Popen(
            [chrome, *base_flags, *extra, f"--user-data-dir={profile}", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            report = _await_report(store, proc, run_token, timeout)
            if report is not None:
                return report
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
    return None


def run_perf(out: Path, sizes: list[int], turns: int, timeout: float) -> bool:
    """Build, serve, and run the perf page once per history size; print a table."""
    import functools
    import threading

    chrome = _find_chrome()
    if chrome is None:
        print("perf: no chrome/chromium binary found on PATH")
        return False
    store = _PerfStore()
    _HarnessHandler.perf_store = store
    _HarnessHandler.quiet = True
    handler = functools.partial(_HarnessHandler, directory=str(out))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reports: dict[int, dict[str, object]] = {}
    try:
        for n in sizes:
            print(f"perf: n={n} turns={turns} … ", end="", flush=True)
            report = _perf_run_one(chrome, out, port, store, n, turns, timeout)
            if report is None:
                print("FAILED (no report — timeout or chrome startup failure)")
                continue
            failed = report.get("failed_phase")
            errors = report.get("errors") or []
            status = f"failed in {failed}" if failed else "ok"
            print(f"{status} ({len(errors) if isinstance(errors, list) else '?'} page errors)")
            reports[n] = report
            (out / f"perf-report-n{n}.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
    finally:
        server.shutdown()
        _HarnessHandler.perf_store = None
        _HarnessHandler.quiet = False
    if not reports:
        return False
    _print_perf_table(reports)
    print(f"\nraw reports: {out}/perf-report-n*.json")
    return True


def _print_perf_table(reports: dict[int, dict[str, object]]) -> None:
    sizes = sorted(reports)

    def cell(n: int, key: str) -> str:
        value = reports[n].get(key)
        return "—" if value is None else str(value)

    def mb(value: object) -> str:
        return f"{value / 1048576:.1f}MB" if isinstance(value, (int, float)) else "—"

    rows: list[tuple[str, list[str]]] = [
        ("replay_ms (full history build)", [cell(n, "replay_ms") for n in sizes]),
        ("nodes after replay", [cell(n, "nodes_after_replay") for n in sizes]),
        ("storm ms/turn (live mix)", [cell(n, "storm_ms_per_turn") for n in sizes]),
        ("chunk_ms (output chunks)", [cell(n, "chunk_ms") for n in sizes]),
        ("idle_ms (busy/idle churn)", [cell(n, "idle_ms") for n in sizes]),
        ("heap start → end", []),
        ("longtasks count/max_ms", []),
        ("replay cycles ms", []),
        ("agent_cards after cycles", []),
    ]
    for n in sizes:
        rep = reports[n]
        rows[5][1].append(f"{mb(rep.get('heap_start'))} → {mb(rep.get('heap_end'))}")
        lt = rep.get("longtasks")
        rows[6][1].append(f"{lt.get('count')}/{lt.get('max_ms')}" if isinstance(lt, dict) else "—")
        cycles = rep.get("cycle_stats")
        if isinstance(cycles, list) and cycles:
            rows[7][1].append(",".join(str(c.get("replay_ms", "?")) for c in cycles))
            rows[8][1].append(str(cycles[-1].get("agent_cards", "?")))
        else:
            rows[7][1].append("—")
            rows[8][1].append("—")

    label_w = max(len(label) for label, _ in rows)
    col_w = max(14, *(len(f"n={n}") for n in sizes))
    header = " " * label_w + "  " + "  ".join(f"n={n}".rjust(col_w) for n in sizes)
    print("\n" + header)
    for label, cells in rows:
        print(label.ljust(label_w) + "  " + "  ".join(c.rjust(col_w) for c in cells))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("/tmp/livepass"))
    ap.add_argument("--serve", type=int, metavar="PORT")
    ap.add_argument("--perf", action="store_true", help="run the perf baseline and exit")
    ap.add_argument(
        "--perf-n",
        default="300,3000",
        help="comma-separated history sizes for --perf (default: 300,3000)",
    )
    ap.add_argument("--perf-turns", type=int, default=20)
    ap.add_argument("--perf-timeout", type=float, default=420.0)
    args = ap.parse_args()
    build(args.out)
    if args.perf:
        sizes = [int(s) for s in str(args.perf_n).split(",") if s.strip()]
        raise SystemExit(0 if run_perf(args.out, sizes, args.perf_turns, args.perf_timeout) else 1)
    if args.serve:
        import functools

        handler = functools.partial(_HarnessHandler, directory=str(args.out))
        print(f"serving {args.out} on http://localhost:{args.serve}/ — Ctrl+C stops")
        http.server.ThreadingHTTPServer(("127.0.0.1", args.serve), handler).serve_forever()


if __name__ == "__main__":
    main()
