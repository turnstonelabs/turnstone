/* eslint-disable no-var */
/**
 * Boot sequence easter egg — plays after first admin account creation.
 *
 * playBootSequence(opts, onComplete)
 *   opts: { version, username, displayName, permissions }
 *   onComplete: called when sequence finishes or user clicks to skip
 */

// prettier-ignore
function playBootSequence(opts, onComplete) {
  var done = false;
  function finish() {
    if (done) return;
    done = true;
    if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    if (onComplete) onComplete();
  }

  // Full-screen overlay
  var overlay = document.createElement("div");
  overlay.style.cssText =
    "position:fixed;inset:0;z-index:99999;background:#0d1117;" +
    "display:flex;flex-direction:column;cursor:pointer;" +
    "font-family:'JetBrains Mono','Fira Code',monospace;overflow:hidden";
  overlay.addEventListener("click", finish);
  document.addEventListener("keydown", function _skip(e) {
    if (!done) { finish(); document.removeEventListener("keydown", _skip); }
  });
  document.body.appendChild(overlay);

  // Terminal container for boot messages
  var term = document.createElement("div");
  term.style.cssText =
    "flex:1;padding:24px 32px;overflow:hidden;font-size:13px;" +
    "line-height:1.6;color:#34d399;white-space:pre";
  overlay.appendChild(term);

  var t0 = performance.now();

  // Phase 1: blinking cursor
  var cursor = document.createElement("span");
  cursor.textContent = "_";
  cursor.style.cssText = "animation:boot-blink 0.6s step-end infinite";
  var style = document.createElement("style");
  style.textContent =
    "@keyframes boot-blink{0%,100%{opacity:1}50%{opacity:0}}" +
    "@keyframes boot-fadein{from{opacity:0}to{opacity:1}}";
  overlay.appendChild(style);
  term.appendChild(cursor);

  // Phase 2: type "Loading....."
  setTimeout(function () {
    if (done) return;
    cursor.remove();
    var loadText = "Loading";
    var idx = 0;
    var typeInterval = setInterval(function () {
      if (done) { clearInterval(typeInterval); return; }
      if (idx < loadText.length) {
        term.textContent += loadText[idx];
        idx++;
      } else if (idx < loadText.length + 5) {
        term.textContent += ".";
        idx++;
      } else {
        clearInterval(typeInterval);
        setTimeout(function () { if (!done) startBoot(); }, 300);
      }
    }, 60);
  }, 800);

  // Phase 3: boot messages
  function startBoot() {
    if (done) return;
    term.textContent = "";

    var version = opts.version || "0.0.0";
    var user = opts.username || "admin";
    var perms = opts.permissions || "admin";

    var lines = [
      "turnstone console v" + version,
      "",
      "[    {T}] Initializing storage backend... SQLite ready",
      "[    {T}] Auth subsystem... HS256 (1 user provisioned)",
      "[    {T}] Session identity... " + user,
      "[    {T}] Permissions granted... " + perms,
      "[    {T}] Structured memory system... initialized",
      "[    {T}] MCP client manager... standby",
      "[    {T}] Intent judge engine... heuristic tier ready",
      "[    {T}] Output guard... active",
      "[    {T}] Tool search index... BM25 ready",
      "[    {T}] Skill registry... scanning",
      "[    {T}] Prometheus metrics... registered",
      "[    {T}] Rate limiter... token bucket initialized",
      "",
      "[    {T}] All systems nominal.",
    ];

    var lineIdx = 0;
    function printNext() {
      if (done || lineIdx >= lines.length) {
        if (!done) glitchOut();
        return;
      }
      var line = lines[lineIdx];
      var elapsed = ((performance.now() - t0) / 1000).toFixed(6);
      line = line.replace("{T}", elapsed);
      term.textContent += line + "\n";
      term.scrollTop = term.scrollHeight;
      lineIdx++;
      var delay = lineIdx <= 1 ? 200 : (30 + Math.random() * 60);
      setTimeout(printNext, delay);
    }
    printNext();
  }

  // Phase 3.5: CRT glitch effect before clearing
  function glitchOut() {
    if (done) return;
    var glyphSets = "\u2588\u2593\u2592\u2591\u2584\u2580\u2502\u2500\u2524\u251c\u256c";
    var original = term.textContent;
    var chars = original.split("");
    var frame = 0;
    var maxFrames = 8;
    var glitchInterval = setInterval(function () {
      if (done) { clearInterval(glitchInterval); return; }
      frame++;
      if (frame > maxFrames) {
        clearInterval(glitchInterval);
        term.textContent = "";
        setTimeout(showWelcome, 200);
        return;
      }
      // Progressively corrupt more characters each frame
      var corruption = frame / maxFrames;
      var glitched = "";
      for (var i = 0; i < chars.length; i++) {
        if (chars[i] === "\n") {
          glitched += "\n";
        } else if (Math.random() < corruption) {
          glitched += glyphSets[Math.floor(Math.random() * glyphSets.length)];
        } else {
          glitched += chars[i];
        }
      }
      term.textContent = glitched;
      // Horizontal jitter
      term.style.transform = "translateX(" + (Math.random() * 4 - 2) + "px)";
    }, 50);
  }

  // Phase 4: welcome message
  function showWelcome() {
    if (done) return;
    term.style.cssText =
      "flex:1;display:flex;flex-direction:column;align-items:center;" +
      "justify-content:center;animation:boot-fadein 1.2s ease-out";

    var name = opts.displayName || opts.username || "operator";
    term.innerHTML =
      '<div style="text-align:center;color:#e2e8f0;font-size:18px;font-weight:500;' +
      'font-family:Outfit,sans-serif;letter-spacing:0.02em;line-height:1.8">' +
      '<div>Welcome, ' + escapeHtml(name) + '.</div>' +
      '<div style="opacity:0.7;font-size:15px;margin-top:4px">I have been waiting for you.</div>' +
      "</div>";

    setTimeout(finish, 3500);
  }

  // escapeHtml (inline — boot.js has no deps)
  function escapeHtml(s) {
    var d = document.createElement("div");
    d.appendChild(document.createTextNode(s));
    return d.innerHTML;
  }
}
