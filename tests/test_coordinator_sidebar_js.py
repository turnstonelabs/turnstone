"""coordinator.js: the sidebar's tasks and children panes when their loads fail."""

from __future__ import annotations

import re
from pathlib import Path

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_COORD = (
    Path(__file__).resolve().parents[1]
    / "turnstone"
    / "console"
    / "static"
    / "coordinator"
    / "coordinator.js"
)

_SIGNATURES = (
    "  function emptyPaneText(load, emptyText, what) {",
    "  function renderTasks() {",
    "  async function loadTasks() {",
    "  function _renderChildrenNow() {",
    "  async function loadChildren({ replace = false } = {}) {",
)
# The panes' load-state declarations, sliced too, so their initial values
# are the page's own.
_LOAD_STATE = re.compile(r"^  let (?:tasksLoad|childrenLoad) = .*;$", re.MULTILINE)

# The panes' collaborators, stubbed; the functions under test are the
# real ones, sliced out of coordinator.js.
_STUBS = """
import assert from "node:assert/strict";

const wsId = "c".repeat(32);
let respond;
const getJSON = () => respond();
const failed = () => Promise.reject(new Error("HTTP 503"));
const answer = (body) => () => Promise.resolve(body);
console.warn = () => {};

const document = { createElement: () => ({ className: "", textContent: "" }) };
function pane() {
  return {
    shown: [],
    replaceChildren() {
      this.shown = [];
    },
    appendChild(el) {
      this.shown.push(el.textContent);
    },
    setAttribute() {},
  };
}

let tasksState = { version: 1, tasks: [] };
const tasksEl = pane();
const tasksCountEl = { textContent: "" };
const renderTaskRow = (t) => ({ textContent: t.title });

const childrenTreeEl = pane();
const childrenState = new Map();
const childrenLastSeen = new Map();
const visibleChildIds = new Set();
const _captureRowFocusKey = () => null;
const _restoreRowFocus = () => {};
const _getChildObserver = () => null;
const _refreshChildrenCount = () => {};
const renderChildRow = (r) => ({ textContent: r.name });
const renderChildren = () => _renderChildrenNow();
const scheduleLiveFetch = () => {};
"""


def _run(body: str) -> None:
    source = _COORD.read_text()
    load_state = _LOAD_STATE.findall(source)
    assert len(load_state) == 2, load_state
    functions = [extract_braced(source, signature) for signature in _SIGNATURES]
    sliced = "\n".join(load_state + functions)
    result = run_node_source(_STUBS + sliced + "\n" + body)
    assert result.returncode == 0, result.stderr


def test_a_pane_whose_loads_all_failed_says_so() -> None:
    _run(
        """
respond = failed;
await loadTasks();
await loadChildren();
assert.deepEqual(tasksEl.shown, ["tasks couldn't be loaded"]);
assert.deepEqual(childrenTreeEl.shown, ["children couldn't be loaded"]);
// A later load that succeeds replaces the message.
respond = answer({ version: 1, tasks: [], items: [] });
await loadTasks();
await loadChildren();
assert.deepEqual(tasksEl.shown, ["no tasks yet"]);
assert.deepEqual(childrenTreeEl.shown, ["no children spawned yet"]);
"""
    )


def test_a_failed_refresh_keeps_what_the_last_load_showed() -> None:
    _run(
        """
respond = answer({
  version: 1,
  tasks: [{ title: "plan" }],
  items: [{ ws_id: "a".repeat(32), name: "worker" }],
});
await loadTasks();
await loadChildren();
respond = failed;
await loadTasks();
await loadChildren();
assert.deepEqual(tasksEl.shown, ["plan"]);
assert.deepEqual(childrenTreeEl.shown, ["worker"]);
"""
    )


def test_an_empty_pane_that_loaded_stays_empty_through_a_failed_refresh() -> None:
    _run(
        """
respond = answer({ version: 1, tasks: [], items: [] });
await loadTasks();
await loadChildren();
respond = failed;
await loadTasks();
await loadChildren();
assert.deepEqual(tasksEl.shown, ["no tasks yet"]);
assert.deepEqual(childrenTreeEl.shown, ["no children spawned yet"]);
"""
    )


def test_live_children_after_a_failed_load_are_marked_as_part_of_the_list() -> None:
    _run(
        """
respond = failed;
await loadChildren();
// A live child event adds a row the failed load never listed.
childrenState.set("b".repeat(32), { ws_id: "b".repeat(32), name: "worker" });
renderChildren();
assert.deepEqual(childrenTreeEl.shown, [
  "children couldn't be loaded; showing live updates only",
  "worker",
]);
// A later load that succeeds lists them all, unmarked.
respond = answer({ items: [{ ws_id: "a".repeat(32), name: "planner" }] });
await loadChildren();
assert.deepEqual(childrenTreeEl.shown, ["planner", "worker"]);
"""
    )


def test_a_children_render_before_the_first_load_reads_as_empty() -> None:
    """A child event can render the tree while its first load is still in
    flight; that render must not claim the load failed."""
    _run(
        """
renderChildren();
assert.deepEqual(childrenTreeEl.shown, ["no children spawned yet"]);
"""
    )
