/* Per-pane follow state shared by coordinator and interactive conversations. */

import { focusTemporarily } from "./conversation.js";

const BOTTOM_THRESHOLD_PX = 48;

export function mountConversationScroll(scroller) {
  const control = document.createElement("div");
  control.className = "conv-scroll-control";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "conv-scroll-latest";
  button.hidden = true;
  button.title = "Jump to latest and resume autoscroll";
  const arrow = document.createElement("span");
  arrow.setAttribute("aria-hidden", "true");
  arrow.textContent = "\u2193";
  const label = document.createElement("span");
  label.textContent = "Jump to latest";
  button.append(arrow, label);
  control.appendChild(button);
  scroller.after(control);

  let following = true;
  let lastTop = scroller.scrollTop;
  let lastExtent = scroller.scrollHeight - scroller.clientHeight;
  let pointerActive = false;
  let touchY = null;
  let frame = null;
  let destroyed = false;

  function visible() {
    return scroller.isConnected && scroller.clientHeight > 0;
  }

  function distanceFromBottom() {
    return scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop;
  }

  function setFollowing(value) {
    following = value;
    // Do not leave keyboard focus on a control that is about to disappear.
    if (following && document.activeElement === button) {
      focusTemporarily(scroller);
    }
    button.hidden = following;
  }

  function onScroll() {
    if (!visible()) return;
    const top = Math.max(0, scroller.scrollTop);
    const extent = scroller.scrollHeight - scroller.clientHeight;
    const distance = extent - top;
    // Reflow can clamp or anchor the viewport before our next pin. Only
    // interpret that movement as user scrolling when geometry is stable or
    // a pointer is dragging. Wheel, touch, and keyboard intent also pause
    // directly so an upward gesture still wins during simultaneous output.
    // Ignore our own unchanged scroll position if more content has arrived
    // before the browser delivers the event from the previous bottom pin.
    if (
      top < lastTop &&
      distance > 1 &&
      (pointerActive || extent === lastExtent)
    ) {
      setFollowing(false);
    } else if (
      distance <= 1 ||
      (top > lastTop && distance <= BOTTOM_THRESHOLD_PX &&
        (pointerActive || extent === lastExtent))
    ) {
      setFollowing(true);
    }
    lastTop = top;
    lastExtent = extent;
  }

  function noteInput(direction) {
    if (!visible()) return;
    // Output may have grown without scrolling a paused pane. Start measuring
    // this gesture from the geometry the user is actually looking at now.
    lastTop = scroller.scrollTop;
    lastExtent = scroller.scrollHeight - scroller.clientHeight;
    if (direction < 0 && lastTop > 0) setFollowing(false);
    else if (direction > 0 && lastExtent - lastTop <= BOTTOM_THRESHOLD_PX) {
      setFollowing(true);
    }
  }

  function onKeyDown(event) {
    if (
      event.defaultPrevented ||
      event.target.closest("input, textarea, select, button, [contenteditable]")
    ) {
      return;
    }
    if (
      ["ArrowUp", "PageUp", "Home"].includes(event.key) ||
      (event.key === " " && event.shiftKey)
    ) {
      noteInput(-1);
    } else if (["ArrowDown", "PageDown", "End", " "].includes(event.key)) {
      noteInput(1);
    }
  }

  function schedule() {
    if (destroyed || !following) return;
    // Keep the pin after the latest render callback. A new message shell
    // can request a scroll before streamingRender queues its markdown paint.
    if (frame !== null) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      frame = null;
      // A newer user scroll wins, including one after Jump to latest or Send.
      // Hidden panes retain their choice; ResizeObserver follows on reveal.
      if (destroyed || !following || !visible()) return;
      scroller.scrollTop = scroller.scrollHeight;
      lastTop = scroller.scrollTop;
      lastExtent = scroller.scrollHeight - scroller.clientHeight;
    });
  }

  function jumpToLatest() {
    if (destroyed) return;
    setFollowing(true);
    schedule();
  }

  const listeners = [];
  function listen(target, type, handler) {
    target.addEventListener(type, handler, { passive: true });
    listeners.push([target, type, handler]);
  }
  listen(scroller, "scroll", onScroll);
  listen(scroller, "wheel", (event) => {
    if (event.deltaY && !event.ctrlKey) noteInput(event.deltaY);
  });
  listen(scroller, "keydown", onKeyDown);
  listen(scroller, "pointerdown", () => {
    pointerActive = true;
    noteInput(0);
  });
  const releasePointer = () => {
    pointerActive = false;
  };
  listen(window, "pointerup", releasePointer);
  listen(window, "pointercancel", releasePointer);
  listen(scroller, "touchstart", (event) => {
    touchY = event.touches.length === 1 ? event.touches[0].clientY : null;
    noteInput(0);
  });
  listen(scroller, "touchmove", (event) => {
    const y = event.touches.length === 1 ? event.touches[0].clientY : null;
    if (touchY !== null && y !== null && y !== touchY) noteInput(touchY - y);
    touchY = y;
  });
  listen(button, "click", jumpToLatest);
  const observer = new ResizeObserver(() => {
    if (!visible()) return;
    if (distanceFromBottom() <= 1) setFollowing(true);
    schedule();
  });
  observer.observe(scroller);

  return {
    isFollowing: () => following,
    schedule,
    jumpToLatest,
    destroy() {
      destroyed = true;
      if (frame !== null) cancelAnimationFrame(frame);
      observer.disconnect();
      for (const [target, type, handler] of listeners) {
        target.removeEventListener(type, handler);
      }
      control.remove();
    },
  };
}
