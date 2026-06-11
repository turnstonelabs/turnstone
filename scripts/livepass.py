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

Rebuild after ANY markup change: the dialog blocks are embedded at build
time. Assets are symlinked, so CSS/JS edits are live on refresh.
"""

from __future__ import annotations

import argparse
import re
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
                supports_streaming: true, supports_vision: true,
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("/tmp/livepass"))
    ap.add_argument("--serve", type=int, metavar="PORT")
    args = ap.parse_args()
    build(args.out)
    if args.serve:
        import functools
        import http.server

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(args.out))
        print(f"serving {args.out} on http://localhost:{args.serve}/ — Ctrl+C stops")
        http.server.ThreadingHTTPServer(("127.0.0.1", args.serve), handler).serve_forever()


if __name__ == "__main__":
    main()
