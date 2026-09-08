"""Pane-manager lifecycle coverage for the tab dismiss controls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._js_harness_helpers import FAKE_DOM, node_skip, run_node_source

_PANE_JS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/pane.js"


def _run_panes(scenario: str) -> None:
    source = (
        FAKE_DOM
        + f"\nconst {{ PaneManager, ShellPane }} = await import({json.dumps(_PANE_JS.as_uri())});\n"
        + r"""
const assert = (value, message) => { if (!value) throw new Error(message); };
Object.defineProperties(FakeElement.prototype, {
  style: {get() { return this._style ??= {}; }},
  parentElement: {get() { return this.parentNode; }},
  firstChild: {get() { return this.children[0] || null; }},
  nextSibling: {get() {
    const siblings = this.parentNode?.children || [];
    return siblings[siblings.indexOf(this) + 1] || null;
  }},
});
FakeElement.prototype.insertBefore = function (child, anchor) {
  if (child.isConnected && child.contains(document.activeElement))
    document.activeElement = null;
  child.remove();
  const index = anchor ? this.children.indexOf(anchor) : this.children.length;
  assert(index >= 0, "insertBefore anchor must belong to its parent");
  this.children.splice(index, 0, child);
  child.parentNode = this;
  child._setConnected(this.isConnected);
};
FakeElement.prototype.removeChild = function (child) { child.remove(); };
let keyboardFocus = true;
const emit = (target, type, props = {}, bubbles = true) => {
  let stopped = false;
  const event = {target, type, preventDefault() {}, stopPropagation() { stopped = true; }, ...props};
  for (let node = target; node; node = bubbles ? node.parentNode : null) {
    for (const {fn} of node._listeners.get(type) || []) fn(event);
    if (stopped) break;
  }
};
FakeElement.prototype.matches = function (selector) {
  return selector.split(",").some(part => {
    part = part.trim();
    if (part === ":focus-visible") return document.activeElement === this && keyboardFocus;
    if (part === ":hover") return false;
    return this._matches(part);
  });
};
FakeElement.prototype.focus = function () {
  const previous = document.activeElement;
  if (previous === this) return;
  document.activeElement = this;
  if (previous) emit(previous, "blur", {}, false);
  emit(this, "focus", {}, false);
  emit(this, "focusin");
};
const pressKey = (key) => {
  keyboardFocus = true;
  emit(document.activeElement, "keydown", {key});
};
ResizeObserver.prototype.unobserve = function (target) { this.targets.delete(target); };
globalThis.sessionStorage = localStorage;
const tabs = document.createElement("div");
const panes = document.createElement("div");
panes.clientWidth = 1400;
panes.clientHeight = 900;
document.documentElement.append(tabs, panes);
const pm = new PaneManager({tabbarEl: tabs, panesEl: panes});
const activated = [];
const closed = [];
for (const type of ["dashboard", "a", "b", "preview"]) {
  pm.registerType(type, () => {
    const pane = new ShellPane({type, title: type, closable: type !== "dashboard",
      ephemeral: type === "preview"});
    pane.onActivate = () => activated.push(type);
    pane.onClose = () => closed.push(type);
    return pane;
  });
}
const dismiss = (pane) => pane.tabEl.parentElement.querySelector(".tab-dismiss");
const dashboard = pm.openPane("dashboard");
const a = pm.openPane("a");
const b = pm.openPane("b");
const overflowTabs = () => {
  tabs.clientWidth = 200;
  tabs.clientLeft = 0;
  tabs.scrollLeft = 0;
  tabs.scrollTop = 17;
  panes.scrollTop = 91;
  tabs.getBoundingClientRect = () => ({left: 20});
  const geometry = new Map([[dashboard, [0, 110]], [a, [114, 140]], [b, [258, 150]]]);
  for (const [pane] of geometry) {
    pane._tabGroup.getBoundingClientRect = () => {
      const [offset, width] = geometry.get(pane);
      const left = 20 + offset - tabs.scrollLeft;
      return {left, width, right: left + width};
    };
  }
  return geometry;
};
"""
        + scenario
    )
    proc = run_node_source(source)
    assert proc.returncode == 0, f"pane harness failed:\n{proc.stderr}\n{proc.stdout}"


@node_skip
def test_tab_dismiss_hides_without_activating_or_destroying_the_pane() -> None:
    _run_panes(r"""
assert(!dismiss(a).hidden && !dismiss(b).hidden, "closable tabs always offer dismissal");
assert(dismiss(dashboard).hidden, "background Dashboard cannot be dismissed");
assert(pm.splitFocused("right", a.id).ok, "split must succeed");
assert(dismiss(a).textContent === "−" && dismiss(b).textContent === "−", "split hides cells");
const group = b.tabEl.parentElement;
assert(group.children[0] === dismiss(b) && group.children[1] === b.tabEl, "dismiss leads select");
assert(!b.tabEl.contains(dismiss(b)), "buttons must be siblings, never nested");
assert(!panes.querySelector(".tab-dismiss"), "pane content must contain no dismiss overlay");
const previousActivations = activated.length;
dismiss(b).focus();
dismiss(b).click();
assert(b.el.hidden && b.el.isConnected && b.tabEl.isConnected, "hide must retain pane and tab");
assert(!closed.length && activated.length === previousActivations, "hide must not activate target");
assert(!dismiss(b).hidden && dismiss(a).textContent === "✕", "background and survivor offer close");
assert(document.activeElement === b.tabEl, "hiding must return focus to its tab");
assert(b.tabEl.parentElement === group, "hiding must preserve the tab group");
pm.activate(b.id);
assert(!dismiss(b).hidden && !dismiss(a).hidden, "tab switching retains both close controls");
pm.setTabTitle(b.id, "Renamed pane");
assert(dismiss(b).getAttribute("aria-label").includes("Renamed pane"), "label follows title");
pm._restoreLayout({type: "split", dir: "row", ratio: 0.5,
  children: [{type: "leaf", paneId: a.id}, {type: "leaf", paneId: b.id}]});
assert(!dismiss(a).hidden && dismiss(b).textContent === "−", "restore refreshes dismiss modes");
pm.unsplit();
assert(dismiss(a).textContent === "✕" && dismiss(b).textContent === "✕", "unsplit offers close on both tabs");
""")


@node_skip
@pytest.mark.parametrize("split_remains", [False, True])
@pytest.mark.parametrize("hide_active", [False, True])
def test_hidden_tab_can_close_without_reactivation(split_remains: bool, hide_active: bool) -> None:
    _run_panes(
        f"const splitRemains = {json.dumps(split_remains)}, hideActive = {json.dumps(hide_active)};\n"
        + r"""
assert(pm.splitFocused("right", a.id).ok, "split must succeed");
if (splitRemains)
  assert(pm.splitFocused("down", dashboard.id).ok, "third cell must fit");
pm.activate(hideActive ? b.id : a.id);
assert(dismiss(b).textContent === "−", "visible split pane offers hide");
dismiss(b).focus();
dismiss(b).click();
assert(b.el.hidden && b.tabEl.isConnected && !closed.length, "hide retains the background tab");
assert(pm.isSplit() === splitRemains, "hiding changes only the intended split cell");
assert(!dismiss(b).hidden, "hidden tab must immediately offer dismissal");
assert(dismiss(b).textContent === "✕", "hidden tab must switch from hide to close");
assert(dismiss(b).classList.contains("tab-dismiss--close"), "hidden tab uses destructive styling");
assert(dismiss(b).title === "Close pane", "tooltip must describe close");
assert(dismiss(b).getAttribute("aria-label") === "Close pane: b", "accessible name must describe close");
const activeBeforeClose = pm.getActive().type;
const activationsBeforeClose = activated.length;
dismiss(b).focus();
assert(!b.el.classList.contains("tab-dismiss-target"), "hidden tab must not highlight a pane");
dismiss(b).click();
assert(closed.join() === "b", "the second dismiss destroys only the hidden pane");
assert(!b.el.isConnected && !b.tabEl.isConnected, "close removes the hidden pane and its tab");
assert(pm.getActive().type === activeBeforeClose && activated.length === activationsBeforeClose,
  "closing a hidden tab must not reactivate it or disturb the active pane");
assert(!a.el.hidden && a.el.isConnected, "surviving pane content stays connected and visible");
assert(document.activeElement === a.tabEl, "close returns keyboard focus to the active tab");
"""
    )


@node_skip
def test_tab_dismiss_closes_preview_and_respects_dashboard() -> None:
    _run_panes(r"""
const preview = pm.openPaneBeside("preview");
assert(dismiss(preview).textContent === "✕", "split preview must advertise close");
assert(dismiss(preview).classList.contains("tab-dismiss--close"), "preview has destructive styling");
assert(dismiss(preview).title === "Close pane", "preview tooltip describes close");
assert(dismiss(preview).getAttribute("aria-label") === "Close pane: preview", "preview has close name");
const group = preview.tabEl.parentElement;
dismiss(preview).focus();
dismiss(preview).click();
assert(closed.join() === "preview", "preview dismiss must destroy its pane");
assert(!preview.el.isConnected && !group.isConnected, "close removes content and entire tab group");
assert(document.activeElement === b.tabEl, "close returns focus to the surviving tab");
assert(!pm.isSplit() && dismiss(b).textContent === "✕", "preview close must refresh survivor");
assert(pm.splitFocused("right", dashboard.id).ok, "Dashboard can appear in a split");
assert(!dismiss(dashboard).hidden && dismiss(dashboard).textContent === "−", "Dashboard can hide");
dismiss(dashboard).click();
assert(dashboard.el.isConnected && dashboard.el.hidden, "Dashboard hide must retain its tab");
pm.activate(dashboard.id);
assert(dismiss(dashboard).hidden, "single Dashboard must not offer close");
""")


@node_skip
def test_tab_menu_button_is_separate_and_preserves_selection_and_focus() -> None:
    _run_panes(r"""
document.body = document.documentElement;
document._listeners = new Map();
document.addEventListener = FakeElement.prototype.addEventListener;
document.removeEventListener = FakeElement.prototype.removeEventListener;
FakeElement.prototype.getBoundingClientRect = () => ({left: 20, right: 60, top: 0, bottom: 28,
  width: 40, height: 28});
window.innerWidth = 1400;
window.innerHeight = 900;
let invoked = false;
pm.registerType("menu", () => {
  const pane = new ShellPane({type: "menu", title: "Menu pane"});
  pane.tabMenu = () => [{label: "Inspect", action: () => { invoked = true; }},
    {label: "Second", action: () => {}}];
  return pane;
});
const pane = pm.openPane("menu");
pm.activate(a.id);
const button = pane._tabGroup.querySelector(".tab-caret");
assert(button.tagName === "BUTTON" && button.type === "button", "menu has a real button");
assert(button.parentElement === pane.tabEl.parentElement && !pane.tabEl.contains(button),
  "menu and selection controls are siblings");
assert(button.getAttribute("aria-haspopup") === "menu", "menu button announces its popup");
assert(!button.hasAttribute("aria-hidden"), "menu button must be exposed to assistive technology");
assert(!tabs.querySelector('[role="tablist"]').contains(button), "menu stays outside the tablist");
pm.setTabTitle(pane.id, "Renamed menu");
assert(button.getAttribute("aria-label") === "Pane actions: Renamed menu", "menu name follows title");
const activationsBefore = activated.length;
button.focus();
emit(button, "click");
assert(button.getAttribute("aria-expanded") === "true", "click opens the menu");
assert(document.body.querySelector('[role="menu"]').getAttribute("aria-label") ===
  "Renamed menu actions", "menu belongs to the requested background tab");
button.focus();
emit(button, "click");
assert(button.getAttribute("aria-expanded") === "false", "second click closes the menu");
pressKey("ArrowDown");
assert(document.activeElement === document.body.querySelector('.tab-menu-item'),
  "ArrowDown opens at the first menu item");
emit(document, "keydown", {key: "Escape"});
assert(document.activeElement === button && !document.body.querySelector('[role="menu"]'),
  "Escape returns focus to the menu button");
pane.tabEl.focus();
emit(pane.tabEl, "keydown", {key: "F10", shiftKey: true});
emit(document, "keydown", {key: "Escape"});
assert(document.activeElement === pane.tabEl, "Shift+F10 returns focus to the tab label");
emit(pane.tabEl, "contextmenu");
document.body.querySelector('.tab-menu-item').click();
assert(invoked && document.activeElement === pane.tabEl, "menu action returns focus to its opener");
button.focus();
emit(button, "click");
await new Promise(resolve => setTimeout(resolve, 0));
emit(document, "mousedown", {target: a.tabEl});
assert(button.getAttribute("aria-expanded") === "false", "outside click clears menu state");
assert(pm.getActive().type === "a" && activated.length === activationsBefore,
  "menu interactions must not activate the background pane");
""")


@node_skip
def test_tab_reconciliation_preserves_focus_order_and_glyphs() -> None:
    _run_panes(r"""
b.tabEl.focus();
pm.activate(b.id);
assert(document.activeElement === b.tabEl, "refresh must not detach the focused tab");
const preview = pm.openPane("preview");
assert(document.activeElement === b.tabEl, "opening a pane must not detach existing tabs");
assert(tabs.querySelectorAll(".tab").map(el => el.dataset.paneId).join() ===
  "dashboard,a,b,preview", "groups retain tab order");
pm.setTabGlyph(b.id, document.createElement("span"));
assert(b.tabEl.firstChild.classList.contains("tab-glyph"), "state glyph stays in select button");
assert(dismiss(b).parentElement.children[0] === dismiss(b), "glyph cannot precede dismiss slot");
dismiss(preview).focus();
pressKey("ArrowLeft");
assert(document.activeElement === b.tabEl, "arrows from dismiss must rove between tabs");
assert(pm.getActive().type === preview.type, "arrow navigation must not activate a pane");
""")


@node_skip
def test_active_tab_visibility_tracks_activation_and_geometry_changes() -> None:
    _run_panes(r"""
const geometry = overflowTabs();
pm.activate(b.id);
assert(tabs.scrollLeft === 208, "activation reveals the entire rightmost tab");
tabs.scrollLeft = 0;
triggerResize(tabs);
assert(tabs.scrollLeft === 208, "strip resizing reveals the active tab");
geometry.set(b, [258, 190]);
triggerResize(b._tabGroup);
assert(tabs.scrollLeft === 248, "a growing title remains visible beside its control");
geometry.set(a, [114, 180]);
geometry.set(b, [298, 190]);
triggerResize(a._tabGroup);
assert(tabs.scrollLeft === 288, "a preceding tab's growth cannot clip the active tab");
assert(tabs.scrollTop === 17 && panes.scrollTop === 91, "revealing tabs only scrolls horizontally");
const group = a._tabGroup;
pm.close(a.id);
assert(!resizeObservers.some(observer => observer.targets.has(group)), "closed tabs must be unobserved");
""")


@node_skip
def test_pointer_focus_does_not_scroll_tab_before_click() -> None:
    _run_panes(r"""
overflowTabs();
pm.activate(b.id);
const scroll = tabs.scrollLeft;
keyboardFocus = false;
a.tabEl.focus({preventScroll: true});
assert(tabs.scrollLeft === scroll, "pointer focus must not move the tab before click lands");
assert(pm.getActive().type === "b", "pointer focus alone must not activate the tab");
a.tabEl.click();
assert(pm.getActive().type === "a", "the subsequent click activates the intended tab");
assert(tabs.scrollLeft === 114, "activation reveals the tab after the click lands");
""")


@node_skip
def test_keyboard_focus_stays_visible_when_strip_or_tabs_resize() -> None:
    _run_panes(r"""
const geometry = overflowTabs();
pm.activate(b.id);
b.tabEl.focus({preventScroll: true});
pressKey("Home");
assert(document.activeElement === dashboard.tabEl && tabs.scrollLeft === 0,
  "Home must reveal the unselected first tab through the focus listener");
tabs.clientWidth = 180;
triggerResize(tabs);
assert(tabs.scrollLeft === 0, "resizing must keep keyboard focus visible before the active tab");
pressKey("ArrowRight");
assert(document.activeElement === a.tabEl && tabs.scrollLeft === 74,
  "arrow navigation reveals the whole focused tab");
geometry.set(a, [114, 170]);
geometry.set(b, [288, 150]);
triggerResize(a._tabGroup);
assert(tabs.scrollLeft === 104, "title growth must keep the focused tab visible");
triggerResize(b._tabGroup);
assert(tabs.scrollLeft === 104, "other title changes must not scroll away from keyboard focus");
geometry.set(a, [114, 250]);
triggerResize(a._tabGroup);
assert(tabs.scrollLeft === 114, "oversized tabs retain their leading control and title start");
assert(pm.getActive().type === "b", "keyboard focus must not activate its pane");
assert(tabs.scrollTop === 17 && panes.scrollTop === 91, "focus reveal only scrolls horizontally");
keyboardFocus = false;
triggerResize(tabs);
assert(tabs.scrollLeft === 258, "without keyboard focus, resizing reveals the active tab");
""")


@node_skip
def test_tablist_owns_only_selection_controls_and_tracks_open_and_close() -> None:
    _run_panes(r"""
const list = tabs.querySelector('[role="tablist"]');
assert(list && list.getAttribute("aria-label") === "Open panes", "tabs have a named tablist");
assert(!tabs.hasAttribute("role"), "the scroller must not own dismiss buttons as tablist children");
const owned = () => list.getAttribute("aria-owns").split(" ");
assert(owned().join() === [dashboard, a, b].map(pane => pane.tabEl.id).join(),
  "the tablist must own selection controls in tab order");
for (const pane of [dashboard, a, b]) {
  assert(!list.contains(dismiss(pane)), "dismiss controls must live outside the tablist");
  assert(!owned().includes(dismiss(pane).id), "dismiss controls must not be owned by the tablist");
  assert(pane.tabEl.getAttribute("role") === "tab", "owned controls retain tab semantics");
}
const preview = pm.openPane("preview");
assert(owned().at(-1) === preview.tabEl.id, "opening a pane adds its tab to the tablist");
pm.close(a.id);
pm.close(preview.id);
assert(owned().join() === [dashboard, b].map(pane => pane.tabEl.id).join(),
  "closing panes removes their ownership references and retains survivor order");
""")
