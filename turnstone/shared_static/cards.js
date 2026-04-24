/* Shared session card primitive — used by ui/static (Saved Workstreams)
   and console/static (Saved Coordinators).  Single source so the two
   surfaces don't drift on field cascade, ARIA roles, keyboard handling,
   or DOM shape.

   Card structure (also see /shared/cards.css for styling):

     .dashboard-card               role="button" tabindex="0"
       .card-title                 sess.alias || title || name || ws_id[:12]
       .card-meta                  "X msgs · Y ago "
         .card-wsid                ws_id[:7]

   Built with safe DOM APIs (createElement + textContent) — never
   innerHTML — so user-supplied alias/title/name fields don't reach the
   DOM as HTML.

   Caller passes:
     sess          — {ws_id, alias?, title?, name?, message_count?, updated?}
     opts.onActivate(sess)  — fired on click + Enter/Space
     opts.ariaLabel(sess)?  — optional aria-label override; default
                              "Resume: {label}"
     opts.busy?             — boolean; adds `is-busy` class (visual dim
                              + cursor: progress) and suppresses re-entry
                              into onActivate.

   Returns the card DOM node.  Caller appends it.

   Depends on: formatRelativeTime (from /shared/utils.js).
*/

function renderSessionCard(sess, opts) {
  opts = opts || {};
  var card = document.createElement("div");
  card.className = "dashboard-card" + (opts.busy ? " is-busy" : "");
  card.dataset.wsId = sess.ws_id;
  var label = sess.alias || sess.title || sess.name || sess.ws_id;
  card.setAttribute("role", "button");
  card.setAttribute("tabindex", "0");
  card.setAttribute(
    "aria-label",
    typeof opts.ariaLabel === "function"
      ? opts.ariaLabel(sess)
      : "Resume: " + label,
  );

  var activate = function () {
    if (card.classList.contains("is-busy")) return;
    if (typeof opts.onActivate === "function") opts.onActivate(sess, card);
  };
  card.onclick = activate;
  card.onkeydown = function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate();
    }
  };

  var title =
    sess.alias || sess.title || sess.name || sess.ws_id.substring(0, 12);
  var titleEl = document.createElement("div");
  titleEl.className = "card-title";
  titleEl.textContent = title;

  var metaEl = document.createElement("div");
  metaEl.className = "card-meta";
  var metaText = (sess.message_count || 0) + " msgs";
  if (sess.updated && typeof formatRelativeTime === "function") {
    metaText += " · " + formatRelativeTime(sess.updated);
  }
  metaEl.appendChild(document.createTextNode(metaText + " "));
  var wsidEl = document.createElement("span");
  wsidEl.className = "card-wsid";
  wsidEl.textContent = sess.ws_id.substring(0, 7);
  metaEl.appendChild(wsidEl);

  card.appendChild(titleEl);
  card.appendChild(metaEl);
  return card;
}
