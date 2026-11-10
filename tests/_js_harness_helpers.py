"""Shared helpers for the Python-driven node harnesses that evaluate the
``shared_static`` ES modules with script semantics."""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


FAKE_DOM = r"""
class FakeElement {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.parentNode = null;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.className = "";
    this.textContent = "";
    this.title = "";
    this.type = "";
    this.paused = true;
    this.ended = false;
    this.scrollHeight = 0;
    this.scrollTop = 0;
    this.clientHeight = 0;
    this._connected = false;
    this._listeners = new Map();
    this.classList = {
      contains: (name) => this.className.split(/\s+/).filter(Boolean).includes(name),
      add: (...names) => {
        const set = new Set(this.className.split(/\s+/).filter(Boolean));
        names.forEach((name) => set.add(name));
        this.className = Array.from(set).join(" ");
      },
      remove: (...names) => {
        const drop = new Set(names);
        this.className = this.className
          .split(/\s+/)
          .filter((name) => name && !drop.has(name))
          .join(" ");
      },
      toggle: (name, force) => {
        const next = force == null ? !this.classList.contains(name) : !!force;
        if (next) this.classList.add(name);
        else this.classList.remove(name);
        return next;
      },
    };
  }
  get isConnected() { return this._connected; }
  _setConnected(value) {
    this._connected = value;
    this.children.forEach((child) => child._setConnected(value));
  }
  appendChild(child) {
    if (child.parentNode) child.remove();
    this.children.push(child);
    child.parentNode = this;
    child._setConnected(this._connected);
    return child;
  }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  replaceChildren(...children) {
    [...this.children].forEach((child) => child.remove());
    this.append(...children);
  }
  remove() {
    if (!this.parentNode) return;
    const i = this.parentNode.children.indexOf(this);
    if (i >= 0) this.parentNode.children.splice(i, 1);
    this.parentNode = null;
    this._setConnected(false);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, fn, options) {
    // An aborted signal removes the listener, as a browser does.
    const signal = options && typeof options === 'object' ? options.signal : null;
    if (signal && signal.aborted) return;
    if (!this._listeners.has(name)) this._listeners.set(name, []);
    this._listeners.get(name).push({ fn, once: !!(options && options.once) });
    if (signal) {
      signal.addEventListener('abort', () => this.removeEventListener(name, fn), { once: true });
    }
  }
  removeEventListener(name, fn) {
    const rows = this._listeners.get(name) || [];
    this._listeners.set(name, rows.filter((row) => row.fn !== fn));
  }
  dispatch(name) {
    const rows = [...(this._listeners.get(name) || [])];
    for (const row of rows) {
      row.fn({ target: this });
      if (row.once) this.removeEventListener(name, row.fn);
    }
  }
  click() { this.dispatch("click"); }
  focus() { document.activeElement = this; }
  contains(node) {
    for (let current = node; current; current = current.parentNode) {
      if (current === this) return true;
    }
    return false;
  }
  _matches(selector) {
    selector = selector.trim();
    const attr = selector.match(/^(?:\.([\w-]+))?\[([\w-]+)="([^"]*)"\]$/);
    if (attr) {
      if (attr[1] && !this.classList.contains(attr[1])) return false;
      const name = attr[2];
      const value = name.startsWith("data-")
        ? this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())]
        : this.getAttribute(name);
      return String(value) === attr[3];
    }
    if (selector.startsWith(".")) {
      return selector.slice(1).split(".").every((name) => this.classList.contains(name));
    }
    return this.tagName.toLowerCase() === selector.toLowerCase();
  }
  closest(selector) {
    for (let current = this; current; current = current.parentNode) {
      if (current._matches(selector)) return current;
    }
    return null;
  }
  querySelectorAll(selector) {
    const selectors = selector.split(",").map((part) => part.trim());
    const found = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (selectors.some((part) => child._matches(part))) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getClientRects() { return this._visible === false ? [] : [{}]; }
}

const html = new FakeElement("html");
html._setConnected(true);
globalThis.document = {
  documentElement: html,
  activeElement: null,
  createElement: (tag) => new FakeElement(tag),
};
const storage = new Map();
let failGet = false;
let failSet = false;
globalThis.localStorage = {
  getItem: (key) => {
    if (failGet) throw new Error("get blocked");
    return storage.has(key) ? storage.get(key) : null;
  },
  setItem: (key, value) => {
    if (failSet) throw new Error("set blocked");
    storage.set(key, String(value));
  },
};
const windowListeners = new Map();
globalThis.window = {
  addEventListener: (name, fn) => windowListeners.set(name, fn),
};
globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
const resizeObservers = [];
globalThis.ResizeObserver = class {
  constructor(fn) {
    this.fn = fn;
    this.targets = new Set();
    this.disconnected = false;
    resizeObservers.push(this);
  }
  observe(target) { this.targets.add(target); }
  disconnect() { this.disconnected = true; this.targets.clear(); }
};
globalThis.triggerResize = (target) => {
  for (const observer of resizeObservers) {
    if (!observer.disconnected && observer.targets.has(target)) observer.fn([]);
  }
};
"""

# The browser around a whole pane, past FAKE_DOM: the document and window calls
# the panes make at construction, and the node methods their renderers use.
# Opt-in, after FAKE_DOM, for suites that run InteractivePane, the coordinator
# pane or the conversation builders.
PANE_DOM = r"""
const assert = (condition, message) => { if (!condition) throw Error(message); };
const create = document.createElement;
document.createElement = tag => { const el = create(tag); el.style = {}; return el; };
document.body = html;
document.addEventListener = document.removeEventListener = () => {};
document.querySelector = selector => html.querySelector(selector);
document.getElementById = id => html.querySelector('#' + id);
window.matchMedia = () => ({matches: false, addEventListener() {}});
window.removeEventListener = () => {};
window.setTimeout = globalThis.setTimeout = () => 0;
window.clearTimeout = globalThis.clearTimeout = () => {};
globalThis.setInterval = () => 0;
globalThis.clearInterval = globalThis.cancelAnimationFrame = () => {};
globalThis.BroadcastChannel = class {postMessage() {} addEventListener() {} close() {}};
globalThis.CSS = {escape: value => value};
const permissions = new Map([['turnstone_scopes', 'read,write']]);
globalThis.sessionStorage = {getItem: key => permissions.get(key) || null};
const matches = FakeElement.prototype._matches;
FakeElement.prototype._matches = function(selector) {
  return selector.startsWith('#')
    ? (this.id || this.getAttribute('id')) === selector.slice(1)
    : matches.call(this, selector);
};
FakeElement.prototype.after = function(node) {
  const parent = this.parentNode;
  if (!parent || node === this) return;
  if (node.parentNode) node.remove();
  parent.children.splice(parent.children.indexOf(this) + 1, 0, node);
  node.parentNode = parent;
  node._setConnected(parent.isConnected);
};
FakeElement.prototype.insertAdjacentElement = function(where, node) {
  if (where !== 'afterend') throw Error('unexpected insertAdjacentElement ' + where);
  this.after(node);
  return node;
};
FakeElement.prototype.insertBefore = function(node, before) {
  if (!before) return this.appendChild(node);
  if (node === before) return node;
  if (node.parentNode) node.remove();
  this.children.splice(this.children.indexOf(before), 0, node);
  node.parentNode = this;
  node._setConnected(this.isConnected);
  return node;
};
Object.defineProperty(FakeElement.prototype, 'nextElementSibling', {get() {
  return this.parentNode
    ? this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null : null;
}});
Object.defineProperty(FakeElement.prototype, 'nextSibling', {get() {
  return this.nextElementSibling;
}});
Object.defineProperty(FakeElement.prototype, 'firstElementChild', {get() {return this.children[0] || null;}});
Object.defineProperty(FakeElement.prototype, 'lastElementChild', {get() {return this.children.at(-1) || null;}});
Object.defineProperty(FakeElement.prototype, 'innerHTML', {
  get() {return this._html ?? this.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');},
  set(value) {this.replaceChildren(); this._html = value;},
});
const appendNodes = FakeElement.prototype.append;
FakeElement.prototype.append = function(...nodes) {
  appendNodes.apply(this, nodes.map(node => typeof node === 'string' ? document.createTextNode(node) : node));
};
FakeElement.prototype.dispatchEvent = function(event) {
  for (let cur = this; cur; cur = event.bubbles ? cur.parentNode : null) {
    for (const entry of [...(cur._listeners.get(event.type) || [])]) entry.fn(event);
  }
  return true;
};
document.createDocumentFragment = () => document.createElement('fragment');
document.createTextNode = text => {
  const node = document.createElement('text'); node.textContent = String(text); return node;
};
// Renderers insert DOM nodes; prose HTML parsing is outside these fixtures.
globalThis.DOMParser = class {parseFromString(text) {
  return {body: {childNodes: [document.createTextNode(text)]}};
}};
"""

# Selectors past FAKE_DOM's, opt-in after PANE_DOM: comma lists, compound
# tag/class/attribute selectors (``a[href]``, ``[role="button"]``) and
# descendant combinators, so code calling ``closest("button, a[href]")`` or
# ``querySelector(".a .b")`` matches as a browser would.
FULL_SELECTORS = r"""
{
  const baseMatches = FakeElement.prototype._matches;
  FakeElement.prototype._matches = function(selector) {
    selector = selector.trim();
    if (selector.includes(',')) return selector.split(',').some(part => this._matches(part));
    const m = selector.match(/^([a-zA-Z][\w-]*)?((?:\.[\w-]+)*)((?:\[[\w-]+(?:="[^"]*")?\])*)$/);
    if (!m || (!m[1] && !m[2] && !m[3])) return baseMatches.call(this, selector);
    const [, tag, classes, attrs] = m;
    if (tag && this.tagName.toLowerCase() !== tag.toLowerCase()) return false;
    for (const name of classes.split('.').filter(Boolean)) {
      if (!this.classList.contains(name)) return false;
    }
    for (const attr of attrs.match(/\[[^\]]+\]/g) || []) {
      const [, name, expected] = attr.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
      const value = name.startsWith('data-')
        ? this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())]
        : this.getAttribute(name) ?? (name === 'href' ? this.href : undefined);
      if (expected === undefined ? value == null : String(value) !== expected) return false;
    }
    return true;
  };
  const baseQuery = FakeElement.prototype.querySelectorAll;
  FakeElement.prototype.querySelectorAll = function(selector) {
    const found = [];
    for (const part of selector.split(',').map(s => s.trim())) {
      let scope = [this];
      for (const step of part.split(/\s+/).filter(s => s !== '>')) {
        const next = [];
        for (const node of scope) {
          for (const el of baseQuery.call(node, step)) if (!next.includes(el)) next.push(el);
        }
        scope = next;
      }
      for (const el of scope) if (!found.includes(el)) found.push(el);
    }
    return found;
  };
}
"""

# Animation frames run when the test flushes them, after the code that asked,
# as a browser runs them, instead of FAKE_DOM's immediate call.  Opt-in.
QUEUED_FRAMES = r"""
const frames = [];
globalThis.requestAnimationFrame = fn => { frames.push(fn); return frames.length; };
const flushFrames = () => { while (frames.length) frames.shift()(); };
"""

# Focus as a browser keeps it, opt-in: removing the focused node (or a node
# holding it) hands focus to the body, and parentElement is the parent node.
BROWSER_FOCUS = r"""
Object.defineProperty(FakeElement.prototype, 'parentElement', {get() {
  return this.parentNode && this.parentNode.tagName ? this.parentNode : null;
}});
const removeNode = FakeElement.prototype.remove;
FakeElement.prototype.remove = function() {
  const active = document.activeElement;
  const held = !!active && this.contains(active);
  removeNode.call(this);
  if (held) document.activeElement = document.body;
};
"""

# Node removal as pane teardown does it, opt-in after PANE_DOM: ``removeChild``
# detaches a child and returns it, and throws for a node that is not a child,
# as a browser does.
REMOVE_CHILD = r"""
FakeElement.prototype.removeChild = function(child) {
  if (!child || child.parentNode !== this) {
    throw new DOMException('removeChild: the node is not a child of this node', 'NotFoundError');
  }
  child.remove();
  return child;
};
"""

# A browser EventSource the test drives, opt-in.  Needs nothing else; with
# FAKE_DOM loaded it is also installed as ``window.EventSource``.  Errors thrown
# by a listener are collected in ``reportedErrors`` (shared with FAKE_CLOCK and
# FAKE_VISIBILITY) instead of reaching the control that fired the event, as a
# browser reports them instead of throwing them at the network.
FAKE_EVENT_SOURCE = r"""
{
  const reported = (globalThis.reportedErrors = globalThis.reportedErrors || []);
  const NUL = String.fromCharCode(0);
  const STATES = ['CONNECTING', 'OPEN', 'CLOSED'];
  class FakeEventSource {
    static instances = [];
    static baseURL = 'http://example.com/';
    #url;
    #withCredentials;
    #readyState = 0;
    #lastEventId = '';
    // type -> entries in registration order: {fn, removed}.  An on<type> handler
    // holds one entry from the moment it is set to a non-null value until it is
    // set back to null; the entry calls whatever the handler is at dispatch time.
    #listeners = new Map();
    #handlers = {open: null, message: null, error: null};
    #handlerEntries = {};
    constructor(url, init) {
      if (arguments.length === 0) throw new TypeError('EventSource: a URL is required');
      this.#url = new URL(String(url), FakeEventSource.baseURL).href;
      this.#withCredentials = !!(init && init.withCredentials);
      // Test-side record, not part of the browser interface.
      this.init = init;
      this.attempts = [{lastEventIdHeader: null}];
      this.closeCalled = false;
      FakeEventSource.instances.push(this);
    }
    get url() { return this.#url; }
    get withCredentials() { return this.#withCredentials; }
    get readyState() { return this.#readyState; }
    get onopen() { return this.#handlers.open; }
    set onopen(value) { this.#setHandler('open', value); }
    get onmessage() { return this.#handlers.message; }
    set onmessage(value) { this.#setHandler('message', value); }
    get onerror() { return this.#handlers.error; }
    set onerror(value) { this.#setHandler('error', value); }
    #setHandler(type, value) {
      const callable = typeof value === 'function' || (value && typeof value === 'object');
      const next = callable ? value : null;
      this.#handlers[type] = next;
      const entry = this.#handlerEntries[type];
      if (next === null && entry) {
        delete this.#handlerEntries[type];
        this.#removeEntry(type, entry);
      } else if (next !== null && !entry) {
        const added = {fn: event => {
          const handler = this.#handlers[type];
          if (typeof handler === 'function') handler.call(this, event);
        }, removed: false};
        this.#handlerEntries[type] = added;
        this.#entries(type).push(added);
      }
    }
    #entries(type) {
      if (!this.#listeners.has(type)) this.#listeners.set(type, []);
      return this.#listeners.get(type);
    }
    #removeEntry(type, entry) {
      entry.removed = true;
      this.#listeners.set(type, this.#entries(type).filter(row => row !== entry));
    }
    close() {
      this.closeCalled = true;
      this.#readyState = 2;
    }
    addEventListener(type, fn) {
      if (!fn) return;
      const rows = this.#entries(type);
      if (!rows.some(row => row.fn === fn)) rows.push({fn, removed: false});
    }
    removeEventListener(type, fn) {
      const entry = this.#entries(type).find(row => row.fn === fn);
      if (entry) this.#removeEntry(type, entry);
    }
    // DOM "inner invoke": walk a copy of the list taken at dispatch, so a
    // listener added during the dispatch waits for the next event, and skip an
    // entry removed by an earlier listener in this same dispatch.
    #fire(event) {
      event.target = event.currentTarget = this;
      for (const entry of [...this.#entries(event.type)]) {
        if (entry.removed) continue;
        const fn = entry.fn;
        try {
          if (typeof fn === 'function') fn.call(this, event);
          else fn.handleEvent(event);
        } catch (err) {
          reported.push({where: 'EventSource ' + event.type + ' listener', err});
          console.error('uncaught in EventSource ' + event.type + ' listener', err);
        }
      }
    }
    #require(state, control) {
      if (this.#readyState === state) return;
      throw new Error('FakeEventSource.' + control + '(): readyState is ' +
        STATES[this.#readyState] + ', needs ' + STATES[state]);
    }
    // The pending connection attempt succeeded: announce the connection.
    open() {
      if (this.#readyState === 2) return false;
      this.#require(0, 'open');
      this.#readyState = 1;
      this.#fire({type: 'open'});
      return true;
    }
    // One frame, as the decoded data field (a non-string is JSON-encoded), with
    // an optional id field and event type.
    deliver(data, {id, event} = {}) {
      if (this.#readyState === 2) return false;
      this.#require(1, 'deliver');
      if (id !== undefined && !String(id).includes(NUL)) this.#lastEventId = String(id);
      const text = typeof data === 'string' ? data : JSON.stringify(data);
      if (text === '') return false;
      this.#fire({type: event || 'message', data: text, lastEventId: this.#lastEventId,
        origin: new URL(this.#url).origin});
      return true;
    }
    // The connection dropped (or a retry hit a network error): the browser
    // reestablishes on this same object.
    drop() {
      if (this.#readyState === 2) return false;
      this.#readyState = 0;
      this.attempts.push({lastEventIdHeader: this.#lastEventId === '' ? null : this.#lastEventId});
      this.#fire({type: 'error'});
      return true;
    }
    // The pending connection attempt got a fatal response: fail the connection.
    fail() {
      if (this.#readyState === 2) return false;
      this.#require(0, 'fail');
      this.#readyState = 2;
      this.#fire({type: 'error'});
      return true;
    }
    static latest() { return FakeEventSource.instances.at(-1) || null; }
    static live() { return FakeEventSource.instances.filter(es => es.readyState !== 2); }
  }
  for (const [value, name] of STATES.entries()) {
    Object.defineProperty(FakeEventSource, name, {value, enumerable: true});
    Object.defineProperty(FakeEventSource.prototype, name, {value, enumerable: true});
  }
  globalThis.FakeEventSource = globalThis.EventSource = FakeEventSource;
  if (globalThis.window) globalThis.window.EventSource = FakeEventSource;
}
"""
"""The browser EventSource behaviors FAKE_EVENT_SOURCE models, each with the HTML
or DOM spec rule it comes from (WHATWG HTML, "Server-sent events" and "Event
handlers"; WHATWG DOM, "Dispatching events").  The rule ids are the ones
EVENT_SOURCE_CONTRACT checks.  A real browser must pass the same checks; the
``es-contract`` scenario in scripts/recovery_e2e.py is meant to run them there.

ES1  ``CONNECTING`` 0, ``OPEN`` 1, ``CLOSED`` 2 exist on the interface object and
     on instances (EventSource IDL constants).
ES2  A new source is CONNECTING; ``url`` is the constructor URL resolved against
     the document base, so it is absolute; ``withCredentials`` mirrors the init
     dictionary and defaults to false (constructor steps).  ``url``,
     ``withCredentials`` and ``readyState`` are read-only accessors.
ES3  The source object has no ``lastEventId`` member: the id is exposed only on
     each MessageEvent (the EventSource IDL has no such attribute).
ES4  When the connection is announced, readyState becomes OPEN and ``open``
     fires, unless the source is CLOSED by then ("announce the connection").
ES5  ``MessageEvent.lastEventId`` is the source's last event ID string: "" until
     an ``id:`` field arrives; an ``id:`` sets it; frames without one leave it
     unchanged, so they carry the previous id ("the buffer does not get reset");
     an explicit empty ``id:`` sets it back to ""; an id containing NUL is
     ignored (event stream interpretation, the "id" field; "dispatch the event"
     step 1).
ES6  A frame with an empty data buffer dispatches no event, but its id still
     sets the last event ID string ("dispatch the event" steps 1-2).
ES7  A frame with an ``event:`` type is dispatched under that type: listeners
     added for the type receive it and ``onmessage`` does not ("dispatch the
     event" step 6).
ES8  When an open connection drops, readyState becomes CONNECTING and ``error``
     fires (handlers see CONNECTING); the same object reconnects, sending
     ``Last-Event-ID`` with the last event ID string when it is not empty, and
     fires ``open`` again when the new connection is announced ("reestablish
     the connection" steps 1 and 5).
ES9  A fatal response to a connection attempt (not 200, wrong MIME type) sets
     readyState CLOSED and fires ``error``; no reconnect follows, on a first
     connection or a reconnect alike ("fail the connection").
ES10 ``close()`` sets readyState CLOSED synchronously and fires no event; no
     ``open``, ``message`` or ``error`` fires afterwards, since each queued task
     returns early on CLOSED (the close() method; the announce, dispatch and
     reestablish tasks).
ES11 ``close()`` while reconnecting cancels the reconnect: no ``open`` follows
     ("reestablish the connection" step 5 returns unless CONNECTING).
ES12 An ``on<type>`` handler takes its place among the type's listeners when it
     is first set to a non-null value: listeners added before it run first; a
     handler replaced by another function keeps that place; a handler set to
     null loses it, and set again it joins at the end (HTML "Event handlers":
     activating and deactivating an event handler).
ES13 A listener removed by an earlier listener during a dispatch does not run
     for that event, and a listener added during the dispatch waits for the next
     event (DOM "inner invoke": the listener list is cloned before the walk and
     an entry whose removed flag is set is skipped).

Test-side controls (not part of the browser interface):

- ``FakeEventSource.instances`` lists every source in creation order;
  ``latest()`` and ``live()`` (not CLOSED) are shortcuts.
- ``es.open()`` announces the pending connection attempt (first or reconnect).
- ``es.deliver(data, {id, event})`` delivers one frame on an OPEN source; ``data``
  is the decoded data field, not wire text (a non-string is JSON-encoded).
- ``es.drop()`` drops the connection (ES8); a later ``es.open()`` is the reconnect.
- ``es.fail()`` refuses the pending attempt (ES9); an OPEN source must
  ``drop()`` first, as an open stream cannot receive a second response.
- ``es.attempts`` records each connection attempt's ``Last-Event-ID`` header
  (``null`` when not sent); ``es.closeCalled`` records a ``close()`` call by the
  client, as opposed to CLOSED reached through ``fail()``; ``es.init`` is the
  constructor's second argument.
- Every control fires its event synchronously, as if the browser's queued task
  ran at once, and is a no-op returning false on a CLOSED source (ES10).  A
  control used out of order (``deliver`` before ``open``) throws: that is a
  test bug, not browser behavior.

Not modelled: wire parsing (``retry:``, comments, multi-line data, BOM), the
reconnection delay (the test decides when the reconnect succeeds), a browser
that gives up after repeated network errors, and CORS.
"""

# The ES-rule checks above as one self-contained script: it defines
# ``checkEventSourceContract(driver)``, which builds sources with the global
# ``EventSource`` and asks the driver to make the server act.  In node the
# driver uses FakeEventSource's own controls; the ``es-contract`` scenario in
# scripts/recovery_e2e.py is meant to run the same function against a real
# EventSource with a server-backed driver.  The driver interface (every method
# may return a promise; unchanged by ES12 and ES13, which use only ``send``):
#
#   url(name)                 URL of a stream the driver controls, root-relative
#   open(name)                accept the pending connection attempt
#   send(name, frame)         write one frame {data, id?, event?}; data "" is an
#                             id-only frame
#   drop(name)                end the response; the browser retries
#   fail(name)                answer the pending attempt with a fatal status
#   lastEventIdHeader(name)   Last-Event-ID of the latest attempt, or null
#   settle()                  wait until the browser has processed the above
#
# Driver calls on a source the client closed must not throw.  The function
# resolves to the list of rule ids it checked and throws on the first broken one.
EVENT_SOURCE_CONTRACT = r"""
async function checkEventSourceContract(driver) {
  const checked = [];
  const check = (condition, rule, detail) => {
    if (!condition) throw new Error('EventSource contract ' + rule + ' broken: ' + detail);
    if (!checked.includes(rule)) checked.push(rule);
  };
  const record = source => {
    const log = [];
    for (const type of ['open', 'message', 'error']) {
      source['on' + type] = event => log.push({type: event.type, state: source.readyState,
        data: event.data, lastEventId: event.lastEventId, target: event.target});
    }
    return log;
  };
  const types = log => log.map(entry => entry.type).join(',');
  const step = async promise => { await promise; await driver.settle(); };

  check(EventSource.CONNECTING === 0 && EventSource.OPEN === 1 && EventSource.CLOSED === 2,
    'ES1', 'constants on the interface');
  const urlA = driver.url('a');
  const a = new EventSource(urlA);
  const logA = record(a);
  check(a.CONNECTING === 0 && a.OPEN === 1 && a.CLOSED === 2, 'ES1', 'constants on an instance');
  check(a.readyState === EventSource.CONNECTING, 'ES2', 'a new source is ' + a.readyState);
  check(/^[a-z]+:[/][/]/.test(a.url) && a.url.endsWith(urlA), 'ES2', 'url is ' + a.url);
  check(a.withCredentials === false, 'ES2', 'withCredentials defaults to false');
  const b = new EventSource(driver.url('b'), {withCredentials: true});
  check(b.withCredentials === true, 'ES2', 'withCredentials mirrors the init dictionary');
  b.close();
  check(!('lastEventId' in a), 'ES3', 'the source object exposes lastEventId');

  await step(driver.open('a'));
  check(a.readyState === EventSource.OPEN && types(logA) === 'open', 'ES4',
    'after the announce: state ' + a.readyState + ', events ' + types(logA));
  check(logA[0].state === EventSource.OPEN && logA[0].target === a, 'ES4',
    'open fires on the source with readyState OPEN');

  const messages = () => logA.filter(entry => entry.type === 'message');
  const last = () => messages().at(-1) || {};
  await step(driver.send('a', {data: 'first'}));
  check(last().data === 'first' && last().lastEventId === '', 'ES5',
    'lastEventId before any id is ' + JSON.stringify(last().lastEventId));
  await step(driver.send('a', {data: 'second', id: '7'}));
  check(last().lastEventId === '7', 'ES5', 'an id field sets lastEventId');
  await step(driver.send('a', {data: 'third'}));
  check(last().data === 'third' && last().lastEventId === '7', 'ES5',
    'a frame without an id carries ' + JSON.stringify(last().lastEventId));
  const before = messages().length;
  await step(driver.send('a', {data: '', id: '8'}));
  check(messages().length === before, 'ES6', 'an empty data buffer dispatched an event');
  await step(driver.send('a', {data: 'fourth'}));
  check(last().data === 'fourth' && last().lastEventId === '8', 'ES6',
    'the id of an empty frame still applies, got ' + JSON.stringify(last().lastEventId));
  const named = [];
  a.addEventListener('custom', event => named.push(event));
  await step(driver.send('a', {data: 'named', event: 'custom'}));
  check(named.length === 1 && named[0].data === 'named' && named[0].lastEventId === '8', 'ES7',
    'a typed frame reaches its listeners');
  check(last().data === 'fourth', 'ES7', 'a typed frame reached onmessage');
  check(!('lastEventId' in a), 'ES3', 'the source object exposes lastEventId after ids');

  const logHandler = a.onmessage;
  check(typeof logHandler === 'function', 'ES12', 'onmessage reads back the handler set on it');
  const order = [];
  const earlier = () => order.push('earlier');
  const later = () => order.push('later');
  a.onmessage = null;
  a.addEventListener('message', earlier);
  a.onmessage = () => order.push('handler');
  a.addEventListener('message', later);
  await step(driver.send('a', {data: 'order-1'}));
  check(order.join() === 'earlier,handler,later', 'ES12',
    'the handler runs in the place it was first set: ' + order.join());
  order.length = 0;
  a.onmessage = () => order.push('replaced');
  await step(driver.send('a', {data: 'order-2'}));
  check(order.join() === 'earlier,replaced,later', 'ES12',
    'a replaced handler keeps its place: ' + order.join());
  order.length = 0;
  a.onmessage = null;
  a.onmessage = () => order.push('reset');
  await step(driver.send('a', {data: 'order-3'}));
  check(order.join() === 'earlier,later,reset', 'ES12',
    'a handler set again after null joins at the end: ' + order.join());
  a.removeEventListener('message', earlier);
  a.removeEventListener('message', later);
  order.length = 0;
  const removed = () => order.push('removed');
  const added = () => order.push('added');
  const remover = () => {
    order.push('remover');
    a.removeEventListener('message', removed);
    a.addEventListener('message', added);
  };
  a.addEventListener('message', remover);
  a.addEventListener('message', removed);
  await step(driver.send('a', {data: 'order-4'}));
  check(order.join() === 'reset,remover', 'ES13',
    'listeners removed or added during a dispatch: ' + order.join());
  order.length = 0;
  a.removeEventListener('message', remover);
  await step(driver.send('a', {data: 'order-5'}));
  check(order.join() === 'reset,added', 'ES13',
    'a listener added during a dispatch runs on the next event: ' + order.join());
  a.removeEventListener('message', added);
  a.onmessage = logHandler;

  await step(driver.drop('a'));
  check(logA.at(-1).type === 'error' && logA.at(-1).state === EventSource.CONNECTING, 'ES8',
    'a drop fires error with readyState ' + logA.at(-1).state);
  check(a.readyState === EventSource.CONNECTING, 'ES8', 'a dropped source is ' + a.readyState);
  await step(driver.open('a'));
  check(a.readyState === EventSource.OPEN && types(logA).endsWith('error,open'), 'ES8',
    'the reconnect re-announces on the same object: ' + types(logA));
  check((await driver.lastEventIdHeader('a')) === '8', 'ES8', 'the reconnect sent Last-Event-ID');
  await step(driver.send('a', {data: 'after-reconnect'}));
  check(last().lastEventId === '8', 'ES5', 'the id persists across a reconnect');
  await step(driver.send('a', {data: 'reset', id: ''}));
  check(last().lastEventId === '', 'ES5', 'an explicit empty id resets lastEventId');

  const settled = logA.length;
  a.close();
  check(a.readyState === EventSource.CLOSED && logA.length === settled, 'ES10',
    'close() is synchronous and fires nothing');
  await step(driver.send('a', {data: 'late', id: '9'}));
  await step(driver.drop('a'));
  check(logA.length === settled && a.readyState === EventSource.CLOSED, 'ES10',
    'events reached a closed source: ' + types(logA.slice(settled)));

  const c = new EventSource(driver.url('c'));
  const logC = record(c);
  await step(driver.open('c'));
  await step(driver.drop('c'));
  await step(driver.fail('c'));
  check(types(logC) === 'open,error,error' &&
    logC.map(entry => entry.state).join(',') === '1,0,2', 'ES9',
    'a refused reconnect: ' + JSON.stringify(logC.map(entry => [entry.type, entry.state])));
  await step(driver.settle());
  check(c.readyState === EventSource.CLOSED && logC.length === 3, 'ES9', 'a failed source retried');
  const d = new EventSource(driver.url('d'));
  const logD = record(d);
  await step(driver.fail('d'));
  check(types(logD) === 'error' && logD[0].state === EventSource.CLOSED, 'ES9',
    'a refused first connection: ' + types(logD));

  const e = new EventSource(driver.url('e'));
  const logE = record(e);
  await step(driver.open('e'));
  await step(driver.drop('e'));
  e.close();
  await step(driver.open('e'));
  check(types(logE) === 'open,error' && e.readyState === EventSource.CLOSED, 'ES11',
    'a close while reconnecting still reopened: ' + types(logE));
  return checked;
}
"""

# A virtual clock, opt-in, after PANE_DOM (whose no-op timers it replaces):
# setTimeout / setInterval / clear*, Date.now and performance.now read it.
# Timer ids start at 1.  ``await clock.advance(ms)`` fires every timer due
# within ``ms`` in due order (ties in arming order), including timers armed by
# the callbacks it runs, and drains microtasks between firings; a callback that
# throws is collected in ``reportedErrors``.  ``await flush()`` drains
# microtasks without moving time.  ``pinRandom(value)`` fixes Math.random, which
# every jittered delay reads.  Animation frames replace FAKE_DOM's immediate
# call: a frame callback runs at the next flush, after the code that asked has
# stored its handle (as in a browser, where frames run after the current task),
# and each flush runs at most three frame rounds, so a callback that keeps
# requesting frames cannot spin.  Timers follow the HTML nesting clamp: a timer
# armed from a timer callback nested more than five deep waits at least 4 ms (a
# callback that re-arms a 0 ms timeout therefore moves virtual time); a timer
# that still fires a thousand times without time moving (re-armed from a
# microtask, which the clamp does not see) makes ``advance`` throw, naming it.
FAKE_CLOCK = r"""
{
  const reported = (globalThis.reportedErrors = globalThis.reportedErrors || []);
  const realSetImmediate = globalThis.setImmediate;
  const drain = () => new Promise(resolve => realSetImmediate(resolve));
  const frames = new Map();
  let nextFrame = 1;
  const runFrames = () => {
    const due = [...frames.values()];
    frames.clear();
    for (const fn of due) {
      try {
        fn(now - start);
      } catch (err) {
        reported.push({where: 'animation frame', err});
        console.error('uncaught in animation frame', err);
      }
    }
  };
  const flush = async () => {
    for (let i = 0; i < 3; i += 1) {
      await drain();
      runFrames();
    }
    await drain();
  };
  const timers = new Map();
  const start = Date.UTC(2026, 0, 1);
  const SAME_INSTANT_LIMIT = 1000;
  let now = start;
  let nextId = 1;
  let order = 0;
  // The timer whose callback is running, for the HTML timer nesting level.
  let running = null;
  // HTML timer initialization: the nesting level is the running timer's (0
  // outside a timer callback); past five, a timeout under 4 ms becomes 4 ms.
  const clamped = (requested, nesting) => (nesting > 5 && requested < 4 ? 4 : requested);
  const arm = (fn, delay, args, repeat) => {
    if (typeof fn !== 'function') return 0;
    const id = nextId++;
    const requested = Math.max(0, Number(delay) || 0);
    const nesting = running ? running.nesting : 0;
    timers.set(id, {id, fn, args, requested, repeat, nesting: nesting + 1, order: order++,
      due: now + clamped(requested, nesting)});
    return id;
  };
  const describeTimer = timer =>
    timer.fn.name || String(timer.fn).replace(/[ ]*[\n][ ]*/g, ' ').slice(0, 80);
  const cancel = id => { timers.delete(id); };
  const nextDue = limit => {
    let best = null;
    for (const timer of timers.values()) {
      if (timer.due > limit) continue;
      const earlier = timer.due < (best ? best.due : Infinity);
      const tieFirst = best && timer.due === best.due && timer.order < best.order;
      if (!best || earlier || tieFirst) best = timer;
    }
    return best;
  };
  const clock = {
    get now() { return now; },
    get elapsed() { return now - start; },
    pending() {
      return [...timers.values()]
        .sort((x, y) => x.due - y.due || x.order - y.order)
        .map(timer => ({id: timer.id, inMs: timer.due - now, repeat: timer.repeat}));
    },
    async advance(ms) {
      const until = now + Math.max(0, Number(ms) || 0);
      let sameInstant = 0;
      await flush();
      for (let timer = nextDue(until); timer; timer = nextDue(until)) {
        sameInstant = timer.due > now ? 1 : sameInstant + 1;
        if (sameInstant > SAME_INSTANT_LIMIT) {
          throw new Error('FAKE_CLOCK: timer "' + describeTimer(timer) + '" fired ' +
            SAME_INSTANT_LIMIT + ' times without virtual time moving: a callback keeps ' +
            're-arming a 0 ms timer outside the nested-timer clamp');
        }
        now = Math.max(now, timer.due);
        if (!timer.repeat) {
          timers.delete(timer.id);
        } else {
          timer.due = now + clamped(timer.requested, timer.nesting);
          timer.nesting += 1;
          timer.order = order++;
        }
        running = timer;
        try {
          timer.fn(...timer.args);
        } catch (err) {
          reported.push({where: 'timer callback', err});
          console.error('uncaught in timer callback', err);
        } finally {
          running = null;
        }
        await flush();
      }
      now = until;
      await flush();
    },
  };
  const win = globalThis.window || {};
  win.setTimeout = globalThis.setTimeout = (fn, delay, ...args) => arm(fn, delay, args, false);
  win.setInterval = globalThis.setInterval = (fn, delay, ...args) => arm(fn, delay, args, true);
  win.clearTimeout = globalThis.clearTimeout = cancel;
  win.clearInterval = globalThis.clearInterval = cancel;
  win.requestAnimationFrame = globalThis.requestAnimationFrame = fn => {
    const id = nextFrame++;
    frames.set(id, fn);
    return id;
  };
  win.cancelAnimationFrame = globalThis.cancelAnimationFrame = id => { frames.delete(id); };
  Date.now = () => now;
  try {
    performance.now = () => now - start;
  } catch (err) {
    console.warn('performance.now stays on the real clock', err);
  }
  globalThis.clock = clock;
  globalThis.flush = flush;
  globalThis.pinRandom = value => { Math.random = () => value; };
}
"""

# fetch as a queue the test answers, opt-in.  Every call is recorded in
# ``network.requests`` ({url, method, init, signal, state}) and stays pending
# until the test calls ``request.reply(body, {status, headers})`` (a non-string
# body is sent as JSON) or ``request.fail(error)`` (default: a TypeError, as a
# browser rejects on a network failure).  The request's AbortSignal is honored:
# an abort rejects the call with the signal's reason, and a later reply to the
# aborted request is discarded.  Responses are real ``Response`` objects.
# ``network.pending(pattern)`` / ``seen(pattern)`` / ``take(pattern)`` find
# requests by a path suffix (query ignored), a RegExp over the URL or a
# predicate.  An abort after the reply errors the body, so a later
# ``await response.json()`` rejects with the abort reason, as in a browser; a
# body already read is unaffected.  ``await network.reply(pattern, ...)`` and
# ``network.fail`` answer
# the oldest pending match and then flush (FAKE_CLOCK's flush when loaded).
FAKE_FETCH = r"""
{
  const realSetImmediate = globalThis.setImmediate;
  // FAKE_CLOCK's flush also runs animation frames; without it, drain microtasks.
  const settle = async () => {
    if (typeof globalThis.flush === 'function') return globalThis.flush();
    for (let i = 0; i < 3; i += 1) await new Promise(resolve => realSetImmediate(resolve));
  };
  const NULL_BODY = [101, 103, 204, 205, 304];
  const requests = [];
  const matches = (request, pattern) => {
    if (pattern == null) return true;
    if (typeof pattern === 'function') return !!pattern(request);
    if (pattern instanceof RegExp) return pattern.test(request.url);
    return request.url.split('?')[0].endsWith(pattern);
  };
  const describe = () =>
    requests.map(r => r.method + ' ' + r.url + ' (' + r.state + ')').join(', ');
  const fakeFetch = (input, init) => {
    init = init || {};
    const url = typeof input === 'string' ? input : String((input && input.url) || input);
    const signal = init.signal || null;
    const method = String(init.method || 'GET').toUpperCase();
    const request = {url, method, init, signal, state: 'pending'};
    requests.push(request);
    return new Promise((resolve, reject) => {
      request.reply = (body, {status = 200, headers = {}} = {}) => {
        if (request.state === 'aborted') return false;
        if (request.state !== 'pending') {
          throw new Error('request already ' + request.state + ': ' + url);
        }
        request.state = 'replied';
        const text = body == null ? null : typeof body === 'string' ? body : JSON.stringify(body);
        const head = new Headers(headers);
        if (text != null && typeof body !== 'string' && !head.has('content-type'))
          head.set('content-type', 'application/json');
        let stream = null;
        if (text != null && !NULL_BODY.includes(status)) {
          // Bytes are handed over only when the body is read, so an abort before
          // that read can still error it (Fetch "abort fetch").
          const bytes = new TextEncoder().encode(text);
          stream = new ReadableStream({
            start(controller) { request.bodyController = controller; },
            pull(controller) { controller.enqueue(bytes); controller.close(); },
          }, {highWaterMark: 0});
        }
        resolve(new Response(stream, {status, headers: head}));
        return true;
      };
      request.fail = error => {
        if (request.state === 'aborted') return false;
        if (request.state !== 'pending') {
          throw new Error('request already ' + request.state + ': ' + url);
        }
        request.state = 'failed';
        reject(error || new TypeError('Failed to fetch'));
        return true;
      };
      const onAbort = () => {
        const reason =
          signal.reason ?? new DOMException('The operation was aborted.', 'AbortError');
        if (request.state === 'replied') {
          // A no-op once the body was read to the end (the stream is closed).
          if (request.bodyController) request.bodyController.error(reason);
          return;
        }
        if (request.state !== 'pending') return;
        request.state = 'aborted';
        reject(reason);
      };
      if (signal) {
        if (signal.aborted) onAbort();
        else signal.addEventListener('abort', onAbort, {once: true});
      }
    });
  };
  globalThis.fetch = fakeFetch;
  if (globalThis.window) globalThis.window.fetch = fakeFetch;
  globalThis.network = {
    requests,
    seen: pattern => requests.filter(r => matches(r, pattern)),
    pending: pattern => requests.filter(r => r.state === 'pending' && matches(r, pattern)),
    take(pattern) {
      const found = this.pending(pattern);
      if (!found.length) {
        throw new Error('no pending request matches ' + pattern + '; seen: ' + describe());
      }
      return found[0];
    },
    async reply(pattern, body, options) {
      this.take(pattern).reply(body, options);
      await settle();
    },
    async fail(pattern, error) {
      this.take(pattern).fail(error);
      await settle();
    },
    settle,
  };
}
"""

# Tab visibility, opt-in, after PANE_DOM (whose no-op document listeners it
# replaces with recording ones that dispatch as a browser does: a listener is
# added once per function, ``once`` is honored, an AbortSignal passed as
# ``signal`` removes the listener when it aborts (an already-aborted one adds
# nothing), a listener removed during a dispatch is skipped, and a throw is
# collected in ``reportedErrors``).
# ``document.hidden`` / ``visibilityState`` read the state; ``visibility.hide()``
# / ``show()`` change it and dispatch ``visibilitychange`` synchronously, only on
# a real change, as a browser does.  ``visibility.listenerCount(type)`` counts
# the document listeners for a type.
FAKE_VISIBILITY = r"""
{
  const reported = (globalThis.reportedErrors = globalThis.reportedErrors || []);
  const listeners = new Map();
  let hidden = false;
  Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
  Object.defineProperty(document, 'visibilityState', {configurable: true,
    get: () => (hidden ? 'hidden' : 'visible')});
  document.addEventListener = (type, fn, options) => {
    if (!fn) return;
    const signal = options && typeof options === 'object' ? options.signal : null;
    if (signal && signal.aborted) return;
    const rows = listeners.get(type) || [];
    if (rows.some(row => row.fn === fn)) return;
    rows.push({fn, once: !!(options && typeof options === 'object' && options.once)});
    listeners.set(type, rows);
    if (signal) {
      signal.addEventListener('abort', () => document.removeEventListener(type, fn), {once: true});
    }
  };
  document.removeEventListener = (type, fn) => {
    for (const row of listeners.get(type) || []) if (row.fn === fn) row.removed = true;
    listeners.set(type, (listeners.get(type) || []).filter(row => row.fn !== fn));
  };
  document.dispatchEvent = event => {
    for (const row of [...(listeners.get(event.type) || [])]) {
      if (row.removed) continue;
      if (row.once) document.removeEventListener(event.type, row.fn);
      try {
        if (typeof row.fn === 'function') row.fn.call(document, event);
        else row.fn.handleEvent(event);
      } catch (err) {
        reported.push({where: 'document ' + event.type + ' listener', err});
        console.error('uncaught in document ' + event.type + ' listener', err);
      }
    }
    return true;
  };
  const change = next => {
    if (hidden === next) return;
    hidden = next;
    document.dispatchEvent({type: 'visibilitychange', target: document, currentTarget: document,
      bubbles: true});
  };
  globalThis.visibility = {
    get hidden() { return hidden; },
    hide() { change(true); },
    show() { change(false); },
    listenerCount: (type = 'visibilitychange') => (listeners.get(type) || []).length,
  };
}
"""


def has_node() -> bool:
    return shutil.which("node") is not None


def run_node_source(source: str, *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Run ``source`` as an ES module under node and return the process.

    The one home for the write-temp-file / run / unlink plumbing the
    slice-and-run suites share (a sliced console function plus ``throw``
    checks); skips the test when node is absent.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs", delete=False) as f:
        f.write(source)
        tmp = f.name
    try:
        return subprocess.run(["node", tmp], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        pytest.skip("node binary not available on PATH")
    finally:
        os.unlink(tmp)


# Module-level ``pytestmark = node_skip`` in each harness suite — the node
# detection lives here once, so a future change (version floor, env
# override) cannot land in one suite and silently miss another.
node_skip = pytest.mark.skipif(not has_node(), reason="node not available")


def demodulize(path: Path) -> str:
    """Strip ES-module syntax so ``vm.runInThisContext`` (script semantics)
    can evaluate the file: imports drop (the harness loads the whole
    dependency set into one shared context, so cross-file bindings resolve
    as context globals, exactly like the pre-module classic scripts), and
    ``export`` keywords peel off their declarations.

    Single-sourced here for every JS harness: a new module syntax form
    (``export default``, re-exports) must be handled once, not per suite —
    a divergence between per-file copies surfaces as a confusing
    ``vm.runInThisContext`` SyntaxError in whichever suite lagged.
    """
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"^import\s+\{[\s\S]*?\}\s+from\s+\"[^\"]+\";\s*$", "", src, flags=re.M)
    src = re.sub(r"^import\s+[^;\n]+;\s*$", "", src, flags=re.M)
    src = re.sub(
        r"^export\s+(?=(?:async\s+)?(?:function|const|let|var|class)\b)", "", src, flags=re.M
    )
    src = re.sub(r"^export\s*\{[^}]*\};\s*$", "", src, flags=re.M)
    return src


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SHARED_STATIC = _REPO_ROOT / "turnstone/shared_static"
_COORDINATOR_JS = _REPO_ROOT / "turnstone/console/static/coordinator/coordinator.js"


def coordinator_module_for_node(directory: Path) -> Path:
    """Write coordinator.js under ``directory`` as a module node can import.

    The pane imports its shared modules as ``from "/shared/..."``, a path the
    console serves and node cannot resolve; each such import is pointed at the
    shared directory on disk and nothing else changes.  Fails when no import
    matched, so a changed import form cannot leave a copy that silently loads
    without its shared modules rewritten.  Returns the written path.
    """
    source = _COORDINATOR_JS.read_text(encoding="utf-8")
    pattern = 'from "/shared/'
    count = source.count(pattern)
    assert count > 0, f"no {pattern!r} import in {_COORDINATOR_JS}; the rewrite matched nothing"
    module = directory / "coordinator.mjs"
    module.write_text(
        source.replace(pattern, 'from "' + _SHARED_STATIC.as_uri() + "/"), encoding="utf-8"
    )
    return module


def slice_braced_block(source: str, anchor: int) -> str | None:
    """Slice the ``{ … }`` block starting at/just after ``anchor``.

    THE brace walker every JS harness suite shares (the comment-AND-
    string-aware superset of the per-suite predecessors, which disagreed
    on comment handling and window bounds — the same source
    reorganization could pass one suite's structural pin while breaking
    the other's with a slice-dependent failure).  Comment awareness makes
    it correct on raw AND pre-stripped input alike.  Returns ``None``
    when no ``{`` opens within 200 chars of ``anchor`` (a missing brace
    must not silently slice some later unrelated block) or the block is
    unterminated.
    """
    start = source.find("{", anchor)
    if start == -1 or start - anchor > 200:
        return None
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    i = start
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch in {'"', "'", "`"}:
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    return None


def extract_braced(source: str, signature: str) -> str:
    """Extract one JS function/method (signature included) — raising form.

    ``signature`` must end at its opening ``{``.  The loud sibling of
    :func:`slice_braced_block` for suites that treat a missing or
    unterminated function as a hard failure rather than a skip.
    """
    start = source.index(signature)
    brace = start + len(signature) - 1
    if source[brace] != "{":
        raise AssertionError(f"signature does not end at an opening brace: {signature}")
    block = slice_braced_block(source, brace)
    if block is None:
        raise AssertionError(f"unterminated JavaScript function: {signature}")
    return source[start:brace] + block


def strip_js_comments(source: str) -> str:
    """Strip ``//`` and ``/* */`` comments for source-pattern assertions —
    the single implementation every JS harness suite shares.

    STRING-AWARE and OFFSET-PRESERVING (comments become spaces, byte
    length identical): a ``//`` inside a string literal (``"https://…"``)
    is content, not a comment — a string-blind scanner truncates the rest
    of the line, and pattern pins then silently assert against corrupted
    text (a ``not in`` guard passes vacuously after the pattern it
    polices was reintroduced).  Length preservation keeps downstream
    offset math (brace walkers, ``.index`` comparisons) valid.  This is
    the strict superset of every per-suite predecessor, hoisted so the
    suites cannot diverge again.

    Limitation — regex literals (``/pattern/flags``) are not detected: a
    ``//`` inside one would be misread as a line comment.  Safe for
    every region currently scanned; extend the tracker before scanning a
    region with regex literals.
    """
    out: list[str] = []
    n = len(source)
    i = 0
    in_str: str | None = None
    while i < n:
        ch = source[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        # Line comment: replace with spaces up to newline (preserve
        # length so downstream offset math still works).
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            j = source.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        # Block comment: replace with spaces up to closing */.
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            j = source.find("*/", i + 2)
            if j == -1:
                out.append(" " * (n - i))
                i = n
                continue
            out.append(" " * (j + 2 - i))
            i = j + 2
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
        out.append(ch)
        i += 1
    return "".join(out)
