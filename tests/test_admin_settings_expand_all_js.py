"""admin.js: the expand all / collapse all button on the Settings and Node Metadata tabs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._js_harness_helpers import (
    extract_braced,
    node_skip,
    run_node_source,
    strip_js_comments,
)

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static"
_ADMIN = _STATIC / "admin.js"

_CONTAINERS = ["admin-settings-content", "admin-node-metadata-content"]

_SIGNATURES = [
    "function _toggleSettingsSection(headerEl) {",
    "function _setSettingsSectionCollapsed(section, collapsed) {",
    "function _expandAllContainer(btn) {",
    "function _anySectionCollapsed(sections) {",
    "function _syncExpandAll(btn, container) {",
    "function toggleAllSections(btn) {",
    "function _initExpandAllButtons() {",
]

# Sections are the shape both tabs render: a node with data-collapsed and one
# header child carrying aria-expanded.  The fake MutationObserver delivers a
# mutation only when the observe() options cover it (childList on the
# container's children, attributes on a descendant only with subtree and the
# attribute in the filter), so options too narrow to see a render or a toggle
# fail these tests.  It delivers synchronously where a browser queues it.
_HARNESS = """
const deliver = (container, mutation) => {
  for (const {fn, options} of container.observers) {
    if (mutation.nested && !options.subtree) continue;
    if (mutation.type === "childList" && !options.childList) continue;
    if (mutation.type === "attributes") {
      // attributeFilter alone turns attribute watching on, as in the DOM.
      if (!options.attributes && !options.attributeFilter) continue;
      const filter = options.attributeFilter;
      if (filter && !filter.includes(mutation.attributeName)) continue;
    }
    fn([mutation]);
  }
};
class Node {
  constructor(container) { this.container = container; this.attributes = {}; }
  setAttribute(name, value) { this.attributes[name] = String(value); this.changed(name); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  removeAttribute(name) { delete this.attributes[name]; this.changed(name); }
  changed(name) {
    deliver(this.container, {type: "attributes", attributeName: name, nested: true});
  }
}
const section = (container, collapsed) => {
  const el = new Node(container);
  el.header = new Node(container);
  el.header.parentElement = el;
  el.header.attributes["aria-expanded"] = collapsed ? "false" : "true";
  if (collapsed) el.attributes["data-collapsed"] = "";
  el.querySelector = (selector) => {
    assert.equal(selector, ".settings-section-header");
    return el.header;
  };
  return el;
};
class Container {
  constructor() { this.sections = []; this.observers = []; }
  querySelectorAll(selector) {
    assert.equal(selector, ".settings-section");
    return this.sections;
  }
  // A render, or a loading or error message (no sections), replaces the children.
  render(collapsed) {
    this.sections = collapsed.map((c) => section(this, c));
    deliver(this, {type: "childList", nested: false});
  }
}
const containers = {
  "admin-settings-content": new Container(),
  "admin-node-metadata-content": new Container(),
};
const button = (controls) => ({
  disabled: false,
  textContent: "stale",
  getAttribute: (name) => (name === "aria-controls" ? controls : null),
});
const buttons = Object.keys(containers).map(button);
// Closing the admin pane takes the containers out of the document.
let detached = false;
globalThis.document = {
  getElementById: (id) => (detached ? null : containers[id] ?? null),
  querySelectorAll: (selector) => {
    assert.equal(selector, ".settings-expand-all");
    return buttons;
  },
};
globalThis.MutationObserver = class {
  constructor(fn) { this.fn = fn; }
  observe(target, options) {
    assert.ok(target, "observe() needs the container");
    target.observers.push({fn: this.fn, options});
  }
};
_initExpandAllButtons();
const state = (btn) => {
  const sections = containers[btn.getAttribute("aria-controls")].sections;
  return {
    label: btn.textContent,
    disabled: btn.disabled,
    collapsed: sections.map((s) => s.hasAttribute("data-collapsed")),
    ariaExpanded: sections.map((s) => s.header.getAttribute("aria-expanded")),
  };
};
"""


def _run(body: str, container: str = "admin-settings-content") -> None:
    source = _ADMIN.read_text()
    sliced = "\n".join(extract_braced(source, signature) for signature in _SIGNATURES)
    pick = (
        f"const tab = containers[{container!r}];\n"
        f"const btn = buttons.find((b) => b.getAttribute('aria-controls') === {container!r});\n"
    )
    result = run_node_source(
        "import assert from 'node:assert/strict';\n" + sliced + "\n" + _HARNESS + pick + body
    )
    assert result.returncode == 0, result.stderr


def test_buttons_start_disabled_until_sections_render() -> None:
    _run(
        """
for (const b of buttons) {
  assert.deepEqual(state(b), {label: "expand all", disabled: true, collapsed: [], ariaExpanded: []});
  toggleAllSections(b);
  assert.deepEqual(state(b), {label: "expand all", disabled: true, collapsed: [], ariaExpanded: []});
}
"""
    )


@pytest.mark.parametrize("container", _CONTAINERS)
def test_expand_all_opens_every_section_then_collapse_all_closes_them(container: str) -> None:
    _run(
        """
tab.render([true, true, true]);
assert.deepEqual(state(btn), {
  label: "expand all", disabled: false,
  collapsed: [true, true, true], ariaExpanded: ["false", "false", "false"],
});
toggleAllSections(btn);
assert.deepEqual(state(btn), {
  label: "collapse all", disabled: false,
  collapsed: [false, false, false], ariaExpanded: ["true", "true", "true"],
});
toggleAllSections(btn);
assert.deepEqual(state(btn), {
  label: "expand all", disabled: false,
  collapsed: [true, true, true], ariaExpanded: ["false", "false", "false"],
});
""",
        container,
    )


def test_expand_all_leaves_an_already_open_section_open() -> None:
    _run(
        """
tab.render([false, true]);
assert.equal(btn.textContent, "expand all");
toggleAllSections(btn);
assert.deepEqual(state(btn).collapsed, [false, false]);
assert.equal(btn.textContent, "collapse all");
"""
    )


@pytest.mark.parametrize("container", _CONTAINERS)
def test_toggling_one_section_keeps_the_label_in_step(container: str) -> None:
    _run(
        """
tab.render([false, true]);
_toggleSettingsSection(tab.sections[1].header);
assert.deepEqual(state(btn), {
  label: "collapse all", disabled: false,
  collapsed: [false, false], ariaExpanded: ["true", "true"],
});
_toggleSettingsSection(tab.sections[0].header);
assert.deepEqual(state(btn), {
  label: "expand all", disabled: false,
  collapsed: [true, false], ariaExpanded: ["false", "true"],
});
""",
        container,
    )


@pytest.mark.parametrize("container", _CONTAINERS)
def test_a_rerender_or_failed_load_resets_the_button(container: str) -> None:
    _run(
        """
tab.render([true]);
toggleAllSections(btn);
assert.deepEqual([btn.textContent, btn.disabled], ["collapse all", false]);
tab.render([true, true]);
assert.deepEqual([btn.textContent, btn.disabled], ["expand all", false]);
tab.render([]);
assert.deepEqual([btn.textContent, btn.disabled], ["expand all", true]);
""",
        container,
    )


def test_the_observer_watches_data_collapsed_itself() -> None:
    _run(
        """
tab.render([true]);
tab.sections[0].removeAttribute("data-collapsed");
assert.equal(btn.textContent, "collapse all");
"""
    )


def test_a_load_finishing_after_the_pane_closes_still_syncs() -> None:
    _run(
        """
tab.render([false]);
assert.equal(btn.textContent, "collapse all");
detached = true;
tab.render([true]);
assert.deepEqual([btn.textContent, btn.disabled], ["expand all", false]);
"""
    )


def test_each_button_controls_only_its_own_tab() -> None:
    _run(
        """
const nodes = containers["admin-node-metadata-content"];
const nodesBtn = buttons.find((b) => b.getAttribute("aria-controls") === "admin-node-metadata-content");
nodes.render([true]);
tab.render([true, true]);
toggleAllSections(btn);
assert.deepEqual(state(btn).collapsed, [false, false]);
assert.deepEqual(state(nodesBtn), {
  label: "expand all", disabled: false, collapsed: [true], ariaExpanded: ["false"],
});
"""
    )


@pytest.mark.parametrize("container", _CONTAINERS)
def test_the_toolbar_button_is_wired_to_its_container(container: str) -> None:
    html = (_STATIC / "index.html").read_text()
    assert f'id="{container}"' in html
    match = re.search(rf'<button[^>]*\baria-controls="{container}"[^>]*>', html)
    assert match, f"no button controls {container}"
    tag = match.group(0)
    assert 'class="settings-expand-all"' in tag
    assert 'onclick="toggleAllSections(this)"' in tag
    assert re.search(r"\sdisabled\s*>$", tag), "the button must start disabled"


def test_admin_js_wires_the_buttons_at_load() -> None:
    source = strip_js_comments(_ADMIN.read_text())
    assert re.search(r"^_initExpandAllButtons\(\);$", source, re.M)
