"""Metacognitive prompting — situational nudges for proactive memory use.

Static nudge text templates (``NUDGE_*``), detection heuristics
(``detect_correction``, ``detect_completion``), the :class:`RepeatDetector`
streak counter, and the cooldown-aware :func:`should_nudge` /
:func:`format_nudge` / :func:`format_idle_children_nudge` /
:func:`format_idle_tasks_nudge` helpers.

The wake-trigger lifecycle (``IdleNudgeWatcher`` plus the
``install_idle_nudge_watcher`` / ``shutdown_idle_nudge_watchers``
lifespan helpers) lives in :mod:`turnstone.core.idle_nudge_watcher`.
"""

from __future__ import annotations

import re
import time

# Default cooldown (s) between nudges of the same type.  Production
# paths pass ``cooldown_secs`` explicitly from
# ``MemoryConfig.nudge_cooldown`` (config-store ``memory.nudge_cooldown``,
# default 300); this constant is the fallback for tests and unit-style
# callers without a ``MemoryConfig`` and is kept aligned with that
# canonical default so both paths behave the same.
_COOLDOWN_SECS = 300

# Repeat-detection threshold — number of *consecutive* identical tool
# calls (same name + same arguments) before a repeat warning fires.
# Two-in-a-row is too noisy because legitimate retries on transient
# failures look identical; three-in-a-row is the cheapest signal that
# the model is stuck on the same call.
_REPEAT_THRESHOLD = 3


class RepeatDetector:
    """Detect a streak of identical tool-call signatures.

    ``record(sig)`` returns ``True`` once *sig* has been recorded
    ``threshold`` times in a row (default 3).  Recording a different
    signature resets the streak — interleaved tool calls aren't a
    stuck loop, only repeated identical ones are.  After a fire, the
    caller is expected to call ``clear()`` to start a fresh streak.
    """

    def __init__(self, threshold: int = _REPEAT_THRESHOLD) -> None:
        self._threshold = threshold
        self._sig: str | None = None
        self._count = 0

    def record(self, sig: str) -> bool:
        """Record *sig*; return ``True`` when the streak hits the threshold."""
        if sig == self._sig:
            self._count += 1
        else:
            self._sig = sig
            self._count = 1
        return self._count >= self._threshold

    def clear(self) -> None:
        self._sig = None
        self._count = 0


# ---------------------------------------------------------------------------
# Nudge messages (brief, model-facing hints)
# ---------------------------------------------------------------------------

NUDGE_CORRECTION = (
    "Note: The user's message may contain a correction or preference. "
    "Pay close attention — if they explain what went wrong or how they'd "
    "prefer you to work, consider saving that as a feedback memory "
    "(memory action='save', type='feedback') so you don't repeat this."
)

NUDGE_DENIAL = (
    "Note: The user just rejected a tool action. Their feedback may "
    "explain why — pay attention to whether this reflects a persistent "
    "preference (e.g. 'never use force-push', 'don't modify that file'). "
    "If so, save it as a feedback memory for future sessions."
)

NUDGE_RESUME = (
    "This workstream has prior conversation history. Before proceeding, "
    "use memory(action='search') to check for relevant context — there "
    "may be saved preferences, project notes, or prior decisions that "
    "apply to this work."
)

NUDGE_COMPLETION = (
    "The task may be wrapping up. Consider whether there are learnings, "
    "decisions, or user preferences from this session worth persisting "
    "as memories (memory action='save') so future sessions can benefit."
)

NUDGE_START = (
    "You have saved memories from prior sessions that may be relevant. "
    "Consider using memory(action='search') with keywords from the "
    "user's request to find applicable context, preferences, or guidance."
)

NUDGE_TOOL_ERROR = (
    "A tool just returned an error. Before retrying, check your memories — "
    "the user may have given feedback about this tool or error pattern in a "
    "previous session. Use memory(action='search') to find relevant guidance."
)

NUDGE_REPEAT = (
    "You just called the same tool with the same arguments as a previous "
    "call in this conversation. Repeating the exact same action will produce "
    "the same result. Stop and reconsider your approach — try a different "
    "tool, different arguments, or ask the user for clarification."
)

NUDGE_COMPACTION = (
    "The conversation is approaching the context limit and will be compacted "
    "shortly — older messages will be replaced by a summary. Reach a natural "
    "stopping point. Before continuing, record in this turn your current goal, "
    "the tasks that remain, and your intended next steps; anything not written "
    "down here may be lost when compaction runs. If you can give a final answer "
    "now, do so — otherwise state clearly where to resume."
)

NUDGE_COMPACTION_RESUME = (
    "The conversation was just compacted to free context. If there is remaining "
    "work, continue from the summary above — pick up the open tasks and next "
    "steps you recorded and keep going without waiting for further instructions. "
    "The summary is a digest, not the record: if it is missing a detail you "
    "need, the recall tool can search the compacted portion of this "
    "conversation. If the task is already complete, give your final answer."
)

# Variant for sessions whose persona hides the recall tool (empty/hard
# visibility sets): same resume instruction, no pointer at a tool that isn't
# on the wire — mirrors the compaction summary's own recall-pointer gating.
NUDGE_COMPACTION_RESUME_NO_RECALL = (
    "The conversation was just compacted to free context. If there is remaining "
    "work, continue from the summary above — pick up the open tasks and next "
    "steps you recorded and keep going without waiting for further instructions. "
    "The summary is a digest, not the record: the full transcript remains in "
    "stored conversation history. If the task is already complete, give your "
    "final answer."
)

_NUDGE_MAP: dict[str, str] = {
    "correction": NUDGE_CORRECTION,
    "denial": NUDGE_DENIAL,
    "resume": NUDGE_RESUME,
    "completion": NUDGE_COMPLETION,
    "start": NUDGE_START,
    "tool_error": NUDGE_TOOL_ERROR,
    "repeat": NUDGE_REPEAT,
    "compaction_pending": NUDGE_COMPACTION,
    # idle_children, idle_tasks and watch_triggered carry no static body
    # — the per-fire text comes from a producer
    # (``format_idle_children_nudge`` / ``format_idle_tasks_nudge`` for
    # the former two, ``format_watch_message`` + ``sanitize_payload`` in
    # the watch dispatch closure for the latter).  Empty string here
    # keeps :func:`format_nudge` round-tripping honestly while still
    # letting :func:`should_nudge` and ``_NUDGE_MAP``-as-registry
    # consumers recognise the type.
    "idle_children": "",
    "idle_tasks": "",
    "watch_triggered": "",
    # background_shell_exit (#817) likewise: per-fire text is composed by
    # ``ChatSession._on_background_shell_exit`` and rides the shared
    # external-event rail, never :func:`format_nudge`.
    "background_shell_exit": "",
    # participant_joined likewise carries no static body — the per-fire text
    # ("<name> has joined this shared workstream…") is composed by its producer
    # (``ChatSession._maybe_note_new_participant``) and emitted via
    # ``_append_system_turn``, never through :func:`format_nudge`.  The entry
    # exists only to keep this map mirroring ``tool_advisory.SYSTEM_TURN_SOURCES``
    # (enforced by ``test_vocabulary_mirrors_nudge_map_both_directions``); nothing
    # calls ``should_nudge("participant_joined", …)`` so it never auto-fires.
    "participant_joined": "",
}

# Nudge types whose copy directs the model at the memory tool ("save that
# as a feedback memory", "use memory(action='search')").  A memory-off
# persona suppresses these — advertising a tool the persona hides produces
# the same "I don't have access" apologies the memory-advisory gating
# fixed — while behavioural nudges (repeat, compaction_pending,
# idle_children, watch_triggered) keep firing.
MEMORY_NUDGE_TYPES: frozenset[str] = frozenset(
    {"correction", "denial", "resume", "completion", "start", "tool_error"}
)

# Nudge types whose copy names a specific tool the model is told to call,
# mapped to that tool.  ``ChatSession._nudges_enabled`` suppresses a type
# whose required tool the persona envelope hides — a nudge instructing
# ``tasks(...)`` at a coordinator whose persona omits the ``tasks`` tool
# produces the same "I don't have access" apology loop the memory-nudge
# gating was built to stop.  The memory types all require ``memory``
# (this map is the generalisation of :data:`MEMORY_NUDGE_TYPES`'s pairing
# with ``_persona_tool_visible("memory")``); ``idle_tasks`` requires
# ``tasks`` because its body's PRIMARY ask — the typed ``needs_user``
# escalation, the child link, the ``done`` record — is bookkeeping
# through ``tasks(...)``: a persona without the tool cannot take the
# action the nudge exists for.  (The body also carries wait-on-a-child
# and take-the-step branches; the gate is ruled on that primary ask,
# not on every line being a ``tasks`` call.)
#
# ``idle_children`` is deliberately ABSENT even though its body names
# ``wait_for_workstream``: it is a liveness wake, and the wake itself is
# the point.  Its body is a ROSTER — "you are idle, these children are
# still active" plus the list — and that information is useful to a
# model that cannot call the suggested tool (it can inspect or message
# the children, or carry on knowing work is in flight).  The tool line
# is the decoration; the facts are the payload.  Suppressing the wake
# because a persona hid the suggested tool would strand the
# coordinator, which is the failure the wake exists to prevent.
NUDGE_REQUIRED_TOOL: dict[str, str] = {
    **dict.fromkeys(MEMORY_NUDGE_TYPES, "memory"),
    "idle_tasks": "tasks",
}


# Display cap for the ``idle_children`` body — list at most this many
# children inline, append "...and N more" overflow line beyond that.
NUDGE_IDLE_CHILDREN_DISPLAY_CAP = 6

# Suggested ``wait_for_workstream(ws_ids=[...])`` cap — matches
# ``WAIT_MAX_WS_IDS`` in :mod:`turnstone.core.coordinator_client` so the
# emitted suggestion is callable as-is.
NUDGE_IDLE_CHILDREN_WAIT_CAP = 32


def wait_call(ws_ids: list[str]) -> str:
    """The one producer of a ``wait_for_workstream(...)`` suggestion.

    BOTH idle bodies emit this call — the ``idle_children`` roster's
    trailing line and the ``idle_tasks`` body's blocked-on-a-child door —
    and they are one function rather than two f-strings precisely
    because they were two f-strings for one release and immediately
    disagreed: the roster emitted a fully populated, copy-paste-ready
    call while the tasks body ended in the bare prose "then
    wait_for_workstream."  A model reading both in one drain (the two
    nudges CO-DELIVER) saw one feature speak two dialects.

    Quoting is deliberately mixed and is NOT a style slip: ``ws_ids``
    rides Python's own ``repr`` of a ``list[str]`` (single quotes, the
    shape every other coordinator tool example in
    ``prompts/tools_coordinator.md`` uses) while ``mode`` is spelled with
    double quotes.  Both are valid in the call syntax the model emits,
    and this exact byte sequence is what the roster has shipped and been
    tuned against; re-quoting it is a body change and belongs in a sweep,
    not in a refactor.

    Callers cap *ws_ids* at :data:`NUDGE_IDLE_CHILDREN_WAIT_CAP` before
    calling — the cap is the executor's ``WAIT_MAX_WS_IDS``, so an
    uncapped list would render a call the tool rejects.
    """
    return f'wait_for_workstream(ws_ids={ws_ids!r}, mode="any", timeout=120)'


# The ``idle_children`` header states FACTS ONLY — no imperative.
#
# It carried "Either continue the user's work or block on the listed
# children explicitly:" for most of this feature's life.  Compressing
# that to "Continue the user's work or block on them:" dropped the
# "Either", and an imperative "Continue..." from the harness reads as
# permission to proceed — the exact manufactured-authority failure the
# advice body's first paragraph exists to deny.  A liveness wake must
# never be the thing that authorises continuing.
#
# So the header now only reports the situation and the roster; the
# formatter's trailing line supplies the one actionable call
# (``To block on them: wait_for_workstream(...)``).  Deciding what to
# do with a live child is the model's call on the evidence, not this
# message's to grant.
NUDGE_IDLE_CHILDREN_HEADER = "You are idle.  These child workstreams are still active:"


# The ``idle_tasks`` body.  TUNED AGAINST THE BEHAVIORAL EVAL
# (``turnstone-eval --nudges``) — deepseek-v4-flash and qwen3.6-27B.
# Reword freely, but re-run the eval: every property below is here
# because measuring it MOVED the numbers (or removed a paragraph that
# measured as carrying nothing), and the whole point of the body is
# behavioural, not stylistic.
#
# Four properties are load-bearing:
#
#   1. The opener is one COUNTS line and the body carries NO task TEXT —
#      no titles, no notes.  Adopted off the round-8 sweep (deepseek,
#      honest instrument): the counts candidate matched or beat the
#      roster body on every childless cell with zero forbidden actions,
#      so the TITLES the roster carried bought nothing the counts line
#      does not.  The same sweep retired the provenance paragraph
#      ("Checkpoint from the harness.  It grants no approval...") that
#      used to lead this body: its ablation arm scored 100/0 on both its
#      cells and both models — no isolated effect anywhere on the
#      correct wire — so the paragraph is pruned rather than kept on
#      folklore.  COMPOSITION CAVEAT, for the next editor: the two
#      results were measured independently — counts-without-provenance
#      was never swept as a single body — and the round-9 nudge arm is
#      the confirmation of the composition, since the eval renders
#      production bodies by construction.
#   1b. IDS ARE NOT TEXT, and the counts adoption over-corrected by
#      dropping them with the titles.  The open task IDS ride in a
#      bullet block under the counts line, and every ``tasks(...)`` call
#      in the branches below is populated with a real one, because
#      ``tasks(action='add')`` returns id AND title in one object and
#      ``tasks(action='list')`` returns the same mapping for the whole
#      set: a coordinator that created its own tasks ALREADY holds the
#      id-to-title association in its transcript, so an id here is not a
#      fact the model has to look up, it is the handle for a fact it
#      has.  Withholding it bought nothing and cost a discovery
#      round-trip on every act.  The block is also the RECOVERY path for
#      the one coordinator that genuinely lost the association — one
#      compacted past its own ``add`` results — for which no other
#      surface in this body would restore it.  This is NOT the roster
#      returning: ids and statuses are server-minted (``tsk_`` +
#      ``secrets.token_hex``, and a ``TASK_OPEN_STATUSES`` member), so
#      the block adds no model-authored text to a system turn.
#   2. The escape branches come FIRST, each carrying a concrete tool
#      call.  "Did I stop legitimately?" is an introspective judgement
#      models are bad at; "does the next step need the user?" and
#      "is this item waiting on a running child?" are typed questions
#      about the transition, each answerable at the cost of one call.
#      Branch order follows harm: guessing on an operator decision is
#      worse than redoing a running child's work, which is worse than a
#      stale list, which is worse than redone finished work.
#   2b. ONE worked example per branch, never a menu of N calls.  The
#      branches are populated from the open set, but each renders a
#      SINGLE call: a ``done`` branch listing every open id would read as
#      an invitation to close the list, which is precisely what that
#      branch's evidence anchoring (property 3) exists to prevent.  The
#      same id rides every branch call under mutually exclusive statuses,
#      which is what keeps them readable as templates rather than as the
#      harness directing which row gets which status — a per-branch id
#      would manufacture exactly that authority.  THREE calls, not four:
#      the take-the-step branch deliberately carries none, because its
#      whole content is "this one is yours", and the blocked-on-a-child
#      branch is itself conditional (property 4), so a childless body
#      carries two.
#   3. The ``done`` branch is last and anchored to evidence.  A task
#      status is model-reported and unattested — ``done`` is cheap to
#      assert, unverifiable, and silences this nudge with no external
#      consequence.  It is offered (bookkeeping lag is real, and without
#      it a stale list makes the model redo finished work) but never
#      advertised as the easy way out.  Two clauses carry it and both
#      were earned: scoping the escalation branch ("escalate decisions,
#      approvals, and grants — not bookkeeping") and naming the complete
#      response ("ending your turn with a short status") took the
#      finished-unmarked cell from 30% to ~90%.  The failure they fix is
#      branch-scope confusion — the model reading "is this done?" as a
#      judgement only the user may make — NOT missing permission; a
#      bare "you may mark it done" clause moved nothing.
#   4. It never asserts the children are gone, and it never instructs
#      about children it knows are absent.  ONE observed fact governs
#      BOTH children-bearing elements — the caveat sentence and the
#      blocked-on-a-child branch — because they are one claim in two
#      registers, and splitting them produced exactly the contradiction
#      you would predict: for one release the sentence was correctly
#      omitted for a childless coordinator while the branch below it
#      still said "if an item is waiting on a child workstream still
#      running", followed by two calls that coordinator could not make.
#      This nudge can co-deliver beside an ``idle_children`` wake (tasks
#      first) or fire ALONE while children run — the liveness nudge can
#      be blocked by its own cap or wait gate — so both elements are
#      carried whenever any child row exists in a live state: the body
#      never asserts children are gone, and the hedge it does carry is
#      unconditionally true.  (A FAILED children read renders no body
#      at all — the observer fails its whole event closed.)
#      Deleting either from the CHILDREN-PRESENT body reopens the
#      resume-over-live-children hazard the old cross-domain fire gate
#      existed for.  On a coordinator with no children both are measured
#      noise (round-5 sweep: the sentence induces a ``list_workstreams``
#      round-trip that is the entire nudge-arm failure mode on the
#      childless cells) and both are omitted — an omission asserts
#      nothing about children at all.  A body with NOTHING about children
#      in it is also what makes the eval's ablation arm a clean
#      single-factor question: does this body need to mention children?
#
# The caveat is its own constant so the omission is a LITERAL-anchored
# removal rather than a positional cut, and so the two forms of the body
# cannot drift apart: there is one sentence, spliced in here and spliced
# out by :func:`format_idle_tasks_nudge`.  It carries its own leading
# two-space sentence separator, so it reads as the second sentence of
# the counts line it now rides, and removing it leaves "…N open
# task(s): …\n" — the opening paragraph both forms of the body share.
NUDGE_IDLE_TASKS_CHILDREN_CAVEAT = (
    "  Children of yours may still be running or may have finished "
    "while you worked; check before redoing anything a child owns — "
    "wait_for_workstream returns immediately for a finished child."
)

# THE SLOTS.  Each is a literal that the shipped tail contains and
# :func:`format_idle_tasks_nudge` substitutes at render time, exactly as
# the caveat above is a literal the formatter REMOVES: literal-anchored,
# never positional, so a reworded neighbour can never shift a
# substitution onto the wrong bytes.
#
# Both read as "fill this in" and NEITHER reads as a plausible id, which
# is the whole reason the previous placeholders went:
# ``task_id='tsk_...'`` was a literal ellipsis (harmless, merely useless)
# but ``child_ws_id='a1b2c3d4'`` was eight hex characters — the exact
# shape of a real ws_id, invented — and a model that copied it issued a
# call that could not resolve.  A placeholder must be uncopyable BY
# SHAPE, not by convention, which is what the angle brackets buy; they
# match ``note='<what you need, one sentence>'``, the slot that is
# correctly never populated because its content is the model's to author.
#
# THE TWO SLOTS ARE NOT THE SAME KIND OF THING, and conflating them is
# how this comment was wrong once already:
#
#   * :data:`NUDGE_IDLE_TASKS_ID_SLOT` is a genuine FALLBACK, and it is
#     production-reachable: a hand-edited envelope whose open rows carry
#     no usable id renders it, and ``tasks(action='list')`` is then the
#     honest recovery — the harness holds no id and says so.
#   * :data:`NUDGE_IDLE_TASKS_CHILD_SLOT` is a pure TEMPLATE VARIABLE.
#     No production body can contain it.  The branch it sits in renders
#     only when the caller passed live child ids, and it is substituted
#     whenever it renders; the states that would leave it unsubstituted
#     are an INDETERMINATE children read (the observer now declines to
#     fire at all, so the formatter never sees it) and a live child row
#     with an empty ``ws_id`` (not producible — every creation path mints
#     through ``uuid4().hex`` or ``secrets.token_hex``).
#
# The constant therefore stays for a STRUCTURAL reason and not a
# defensive one: the branch lives inside :data:`NUDGE_IDLE_TASKS_TAIL`,
# which is one literal — the body's single tuned, overridable,
# fingerprinted surface — and a literal needs a literal in its
# ``child_ws_id='…'``.  The alternatives were both worse: hard-coding an
# id there is the ``a1b2c3d4`` defect returning, and building the branch
# outside the tail would take it out of the surface an operator can tune
# and the fingerprint can hash.
NUDGE_IDLE_TASKS_ID_SLOT = '<task_id from tasks(action="list")>'
NUDGE_IDLE_TASKS_CHILD_SLOT = "<ws_id from list_workstreams()>"

# The blocked-on-a-child branch's wait call, in its unsubstituted form.
# Built through :func:`wait_call` rather than typed out, so the text the
# formatter matches on is byte-identical to the call it substitutes in —
# a hand-typed template that drifted from the producer by one space would
# silently stop matching and ship the template to the model.
NUDGE_IDLE_TASKS_WAIT_SLOT = wait_call([NUDGE_IDLE_TASKS_CHILD_SLOT])

# The open-id block.  Carries its own leading blank line, the same idiom
# as the caveat above and for the same reason: the formatter either
# replaces it with the real bullets or removes it whole (a coordinator
# whose every open row is id-less — a hand-edited envelope — has no
# block to render), and both operations must leave the surrounding
# paragraph spacing intact without either one knowing where it sits.
NUDGE_IDLE_TASKS_OPEN_LIST_SLOT = "\n\n  - <your open task ids appear here, one per line>"

# THE BLOCKED-ON-A-CHILD BRANCH, whole — prose, link call and wait call.
#
# Conditional on EXACTLY the children caveat's condition, and its own
# constant for exactly the caveat's reason: so the removal is a
# LITERAL-anchored cut rather than a positional one, and so the two forms
# of the body cannot drift apart.
#
# It was unconditional for one release, and that was an inconsistency
# with a live cost: a coordinator with no children had the caveat
# SENTENCE about children correctly omitted two paragraphs above, and
# then read an INSTRUCTION about children — "if an item is waiting on a
# child workstream still running" — followed by two calls it could not
# make, pointing at a lookup that returns nothing.  Omitting the sentence
# while keeping the instruction is the same defect the caveat conditional
# exists to fix, left standing one block below it.  One observed fact now
# governs every children-bearing element of this body.
#
# Same fail-safe direction as the caveat, and it is the direction that
# makes the branch's placeholder legitimate: an INDETERMINATE read keeps
# the branch, because the harness cannot then rule out a child, and only
# omitting it can be wrong.  On that path the model is pointed at
# ``list_workstreams()`` — a lookup that may well return rows, since the
# read that failed is the harness's, not the model's.
#
# Carries its own leading blank line (the caveat's idiom), so the cut
# leaves "…not queued for confirmation.\n\nIf the next step is yours to
# take, take it." — the paragraph seam both forms share.
NUDGE_IDLE_TASKS_CHILD_DOOR = (
    "\n"
    "\n"
    "If an item is waiting on a child workstream still running, "
    "record the link and wait instead of redoing its work:\n"
    "\n"
    f"    tasks(action='update', task_id='{NUDGE_IDLE_TASKS_ID_SLOT}', "
    "status='in_progress',\n"
    f"          child_ws_id='{NUDGE_IDLE_TASKS_CHILD_SLOT}')\n"
    f"    {NUDGE_IDLE_TASKS_WAIT_SLOT}"
)

# Everything in the ``idle_tasks`` body AFTER the counts line the
# formatter builds: the conditional children caveat, the open-id block,
# then the typed branches — of which THREE carry a ``tasks(...)`` call
# and one ("If the next step is yours to take, take it.") deliberately
# carries none.  Named for its position because that is its contract —
# the formatter owns the opener, this constant owns the rest, and the
# seam between them is the one place the body is assembled.
NUDGE_IDLE_TASKS_TAIL = (
    f"{NUDGE_IDLE_TASKS_CHILDREN_CAVEAT}"
    f"{NUDGE_IDLE_TASKS_OPEN_LIST_SLOT}\n"
    "\n"
    "If the next step needs the user — a decision, an approval, a "
    "scope or credential you were not given — that is not yours to "
    "resolve:\n"
    "\n"
    f"    tasks(action='update', task_id='{NUDGE_IDLE_TASKS_ID_SLOT}', "
    "status='needs_user',\n"
    "          note='<what you need, one sentence>')\n"
    "\n"
    "Stopping there is the correct outcome.  Do not substitute your "
    "judgment for the user's.  Escalate decisions, approvals, and "
    "grants — not bookkeeping: an item whose output is visible "
    "in this transcript is recorded done, not queued for "
    "confirmation."
    f"{NUDGE_IDLE_TASKS_CHILD_DOOR}\n"
    "\n"
    "If the next step is yours to take, take it.\n"
    "\n"
    "If an item's output is visible in this transcript, record "
    "it — ending your turn with a short status is a complete "
    "response:\n"
    "\n"
    f"    tasks(action='update', task_id='{NUDGE_IDLE_TASKS_ID_SLOT}', "
    "status='done')"
)


# ASCII control chars + Unicode steering vectors (bidi-override,
# zero-width, line/paragraph separators, BOM, tag chars).  Treated
# uniformly as control chars and replaced with a space; angle-bracket
# tag-breakers are stripped separately below.  Defense-in-depth today
# (self-injection within one user's tenant — children inherit parent
# ``user_id`` and watch commands are user-supplied), but becomes
# load-bearing the moment a producer ingests payloads from a different
# trust boundary (a future watch trigger consuming external webhook
# bodies, etc).
#
# Two CONTROL classes, picked at the call site by the caller's
# structural requirements:
#   * :data:`_NAME_CONTROL_CHARS` — STRICT: also strips TAB/LF/CR.
#     Used by :func:`sanitize_name` and :func:`sanitize_display` for
#     single-line user-controlled fields (task ``title``/``note`` on
#     the operator surfaces — a field with ``\n`` in it would otherwise
#     break the surrounding one-line structure and let a malicious task
#     title forge sibling rows).
#   * :data:`_PAYLOAD_CONTROL_CHARS` — PERMISSIVE: preserves TAB/LF/CR.
#     Used by :func:`sanitize_payload` for multi-line payloads where
#     line layout is part of the signal (watch shell output —
#     stripping LF/CR would collapse multi-line output to one line).
#
# THREE sanitisers, and the second axis that picks between them is the
# AUDIENCE of the string being built — that axis, not the control class,
# decides whether :data:`_PAYLOAD_TAG_BREAKERS` runs:
#   * MODEL-FACING interpolation (a nudge body, a system turn) →
#     :func:`sanitize_name` / :func:`sanitize_payload`.  Angle brackets
#     are DELETED, because ``fence.wrap`` neutralises only the
#     ``[start system-reminder]`` operator marker and never a
#     ``</thinking>`` / ``<answer>`` tag, so a workstream or task named
#     ``</thinking>...`` would otherwise be interpolated straight into
#     the model's reasoning channel.
#   * OPERATOR-DISPLAY rendering (the tasks pane, the idle-tasks card,
#     the approval preview) → :func:`sanitize_display`.  Angle brackets
#     are KEPT, because a display exists to show what storage holds:
#     deleting them rendered a stored ``hold p99 <200ms`` as
#     ``hold p99 200ms`` — the constraint inverted on the surface the
#     operator rules on, while ``tasks(action='list')`` handed the model
#     the original.
_CONTROL_CHARS_TAIL = (
    r"\u200b-\u200f"  # zero-width / LRM / RLM
    r"\u202a-\u202e"  # bidi overrides
    r"\u2066-\u2069"  # bidi isolates
    r"\u2028\u2029"  # line / paragraph separator
    r"\ufeff"  # BOM
    r"]"
    r"|[\U000e0000-\U000e007f]"  # Unicode tag chars (separate range above BMP)
)
_NAME_CONTROL_CHARS = re.compile(
    r"[\x00-\x1f\x7f" + _CONTROL_CHARS_TAIL  # ASCII control (incl. \t\n\r) + DEL
)
_PAYLOAD_CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f" + _CONTROL_CHARS_TAIL  # ASCII control (skip \t\n\r) + DEL
)
_PAYLOAD_TAG_BREAKERS = re.compile(r"[<>]")


def sanitize_name(text: str) -> str:
    """Strict sanitiser for single-line user-controlled name fields.

    Strips ASCII control chars **including** TAB/LF/CR plus Unicode
    steering vectors and angle-bracket tag breakers.  Use for fields
    rendered as a single bullet item / label inside a MODEL-FACING body,
    where embedded newlines would forge a fake sibling row and angle
    brackets would steer the reasoning channel.

    NEITHER idle formatter calls this any more —
    :func:`format_idle_children_nudge` is ids-and-states only, and
    :func:`format_idle_tasks_nudge` is counts-and-branches only, both
    all server-minted — but this function is the mandatory route for
    any model- or user-authored field either body ever grows (the
    belt-and-braces rule in both formatters' docstrings), which is why
    it stays although no production path currently calls it.
    """
    if not text:
        return ""
    cleaned = _NAME_CONTROL_CHARS.sub(" ", text)
    cleaned = _PAYLOAD_TAG_BREAKERS.sub("", cleaned)
    return cleaned.strip()


def sanitize_payload(text: str) -> str:
    """Permissive sanitiser for multi-line user-controlled nudge payloads.

    Used by ``format_watch_message`` output rendered into the
    ``watch_triggered`` nudge body.

    The wire-boundary fence escaping (``fence.neutralize`` at fold time)
    only defangs the ``[start system-reminder]`` operator marker; other
    angle-bracketed markers (``</thinking>``, ``<answer>``,
    ``<artifact>``, …) and Unicode steering vectors (RTL override,
    zero-width chars, tag chars) can still steer some models.  Strip
    both classes before interpolation — self-injection only today
    (watch commands are user-supplied), but the cost is one ``re.sub``
    per payload.

    TAB / LF / CR are preserved (see ``_PAYLOAD_CONTROL_CHARS``) so
    multi-line shell output in watch payloads keeps its line structure.
    For single-line name fields where newlines would break surrounding
    structure, use :func:`sanitize_name` instead.
    """
    if not text:
        return ""
    cleaned = _PAYLOAD_CONTROL_CHARS.sub(" ", text)
    cleaned = _PAYLOAD_TAG_BREAKERS.sub("", cleaned)
    return cleaned.strip()


def sanitize_display(text: str) -> str:
    """Strict sanitiser for OPERATOR-FACING single-line renders.

    Same control class as :func:`sanitize_name` — ASCII controls
    **including** TAB/LF/CR, plus bidi overrides, zero-width runs, BOM
    and tag chars — because these are single-line fields: a newline in a
    title forges an extra header line in the channel formatter and an
    extra row in ``buildConvCmd``'s line-classified command view, and a
    bidi override reorders the decision the operator reads.

    Angle brackets are KEPT, and that is the entire difference from
    :func:`sanitize_name`.  Serves the two surfaces that render stored
    task text to an operator — the tasks pane
    (``_sanitize_task_envelope_for_display``) and the approval preview
    (``ChatSession._prepare_tasks``'s ``_pf``) — where deleting
    ``<``/``>`` silently rewrote ordinary planning text: a stored
    "cut p99 latency to <200ms" rendered as "...to 200ms", inverting the
    constraint on the surface the operator acts on while
    ``tasks(action='list')`` fed the model the original.  (The
    idle-tasks card used to be a third such surface; its metadata is
    counts only now and carries no stored text.)

    NEVER use this on text interpolated into a MODEL-FACING body.  The
    wire-boundary fence defangs the operator marker only, so a
    bracket-preserving projection of a task titled ``</thinking>...``
    is a steering vector inside a system turn — strictly worse than the
    display bug this function exists to fix.  Those paths keep
    :func:`sanitize_name` / :func:`sanitize_payload`.
    """
    if not text:
        return ""
    return _NAME_CONTROL_CHARS.sub(" ", text).strip()


def format_idle_children_nudge(children: list[dict[str, str]]) -> str:
    """Render the ``idle_children`` reminder body — ids and states ONLY.

    *children* is a list of dicts carrying at least ``ws_id`` and
    ``state`` — the row-mapping shape coordinator-side storage exposes.
    Returns raw text *without* any envelope; the nudge is emitted as a
    first-class ``{"role": "system"}`` turn whose content is this text
    (folded to a ``[start system-reminder]`` block at the wire boundary on
    non-native models).

    Every interpolated value is SERVER-MINTED: the 8-char ``ws_id``
    prefix per row, the enum-derived state, and the full ``ws_id``\\ s in
    the trailing ``wait_for_workstream`` suggestion.  Child NAMES are
    model-authored (a coordinator names its children when it spawns
    them) and are deliberately NOT rendered — interpolating them lowered
    the plant's own output back into a trusted system turn, where a
    child named ``</thinking>...`` steers the reasoning channel of the
    very model that named it.  A row's ``name`` key, if present, is
    ignored; there is no ``(unnamed)`` fallback because there is no name
    column at all.

    No sanitiser runs here because nothing model-authored remains to
    sanitise.  BELT-AND-BRACES RULE: any future model- or user-authored
    field added to this body MUST go back through :func:`sanitize_name`
    before interpolation — server-minted provenance is the only
    exemption.  (:func:`format_idle_tasks_nudge` carries the same rule;
    neither idle body interpolates authored text today.)

    Display caps at :data:`NUDGE_IDLE_CHILDREN_DISPLAY_CAP` with an
    overflow line; the trailing ``wait_for_workstream`` suggestion's
    ``ws_ids`` list caps at :data:`NUDGE_IDLE_CHILDREN_WAIT_CAP`.
    Empty input returns the empty string so callers can short-circuit
    on ``if not text: return``.
    """
    if not children:
        return ""
    lines = [NUDGE_IDLE_CHILDREN_HEADER, ""]
    shown = children[:NUDGE_IDLE_CHILDREN_DISPLAY_CAP]
    for c in shown:
        ws_id = c.get("ws_id", "")
        state = c.get("state", "?")
        lines.append(f"  - {ws_id[:8]} ({state})")
    overflow = len(children) - len(shown)
    if overflow > 0:
        lines.append(f"  ...and {overflow} more")
    lines.append("")
    wait_ids = [c.get("ws_id", "") for c in children[:NUDGE_IDLE_CHILDREN_WAIT_CAP]]
    lines.append(f"To block on them: {wait_call(wait_ids)}.")
    return "\n".join(lines)


def field_str(value: object) -> str:
    """Coerce a task-row field to ``str`` for rendering.

    ``None`` (a JSON ``null`` in the stored envelope) maps to ``""`` —
    NOT to ``str(None)``, whose ``"None"`` is truthy and once rendered a
    literal ``None`` note line in the operator card while the prose said
    nothing.  Other non-strings coerce via ``str`` so an ``int`` title
    renders identically in the card and the prose instead of raising
    ``TypeError`` inside :func:`sanitize_name`'s regex.
    """
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


# Task-field length caps, shared here beside :func:`field_str` and the
# sanitiser family because TWO layers enforce them and must never
# disagree: ``coordinator_client.tasks_add`` / ``tasks_update`` reject
# authoritatively, and ``ChatSession._prepare_tasks`` rejects early so the
# operator is never shown an approval card for a call the write path will
# refuse.  Private copies on either side of that pair would drift into
# exactly the split the early copy exists to remove.  Both caps are
# measured on the STRIPPED value at every site, so leading whitespace
# never counts against them.

# Max task title length.  Exceeded titles return an error rather than
# silently truncating — mutating the coordinator's planning state
# under its nose masks real planning bugs (the model may rely on the
# title it SENT, not the stored one).
TASK_TITLE_MAX = 200
# Max task note length.  Same limit and the same reject-don't-truncate
# rule as the title: a note carries the coordinator's one-sentence ask to
# the user, which is precisely the string where silent trimming loses
# the information the field exists to carry.  One number rather than two
# so the schema has one sentence to explain.
TASK_NOTE_MAX = 200


def task_too_long_message(field: str, length: int, cap: int) -> str:
    """The model-facing refusal for an over-cap task field.

    Beside the caps, and for the same reason: both layers speak this
    sentence to the MODEL.  ``ChatSession._prepare_tasks`` refuses first
    (prefixed with its action, so the model is told which of a batch's
    rows failed) and ``coordinator_client.tasks_add`` / ``tasks_update``
    refuse authoritatively — meaning the copy the model always hits is
    the one that was never the source of truth.  Held as two literals
    the pair was narrowed in lockstep by hand, and a one-sided edit
    would tell the model two different reasons for the same refusal
    depending on which layer it reached.  One producer makes that edit
    impossible rather than merely detectable.
    """
    return f"{field} too long ({length} chars, max {cap}).  Shorten and retry."


def task_unrenderable_message(field: str, length: int) -> str:
    """The model-facing refusal for task text that sanitises to nothing.

    One producer for both layers, for the reason given on
    :func:`task_too_long_message`.  The hint names the character CLASS
    that was refused, and deliberately says nothing about angle brackets
    — operator surfaces render those now, and a hint that mentions them
    talks a model out of a legitimate ``"hold p99 <200ms"`` title.  The
    renderability oracle itself is :func:`sanitize_display` at every
    caller, so the two layers cannot disagree about what "renderable"
    means either.
    """
    return (
        f"{field} contains no renderable characters ({length} chars of "
        "control/zero-width/bidi codepoints, which operator displays "
        f"strip).  Rewrite {field} in plain printable text and retry."
    )


def format_idle_tasks_nudge(
    open_counts: dict[str, int],
    *,
    open_task_ids: list[tuple[str, str]],
    child_ws_ids: list[str] | None,
) -> str:
    """Render the ``idle_tasks`` reminder body — counts, open IDS and
    typed branches, NO task text.

    The opener is one counts line ("You still have N open task(s): X
    in_progress, Y pending"), and that line IS the situation statement:
    the provenance paragraph that used to carry it was pruned on the
    round-8 numbers (its ablation showed no isolated effect on the
    correct wire), and the TITLES the roster carried went with it (the
    counts candidate matched or beat the roster on every childless cell
    with zero forbidden actions).  The rest of the body is
    :data:`NUDGE_IDLE_TASKS_TAIL` — the conditional children caveat, the
    open-id block, and the typed branches, one of which (the
    blocked-on-a-child one) is itself conditional on the same fact the
    caveat is.

    *open_counts* maps each OPEN task status to how many of the
    coordinator's tasks hold it.  The producer derives it from its own
    open filter over ``TASK_OPEN_STATUSES`` (see
    ``CoordinatorIdleObserver._open_counts``), EVERY open status
    present with zero counts included, so the line's shape is constant
    across coordinators; this function renders whatever mapping it is
    given, in sorted key order, and restates no status vocabulary of
    its own.

    *open_task_ids* is ``[(id, status), ...]`` over the SAME open set,
    in envelope order (``CoordinatorIdleObserver._open_task_ids``).  It
    is a narrow projection rather than the open ROWS on purpose: a
    formatter that never receives a title cannot render one, which is a
    stronger guarantee than a docstring forbidding it.  Every id renders
    twice over — once in the block under the counts line, once as the
    populated ``task_id`` of every branch call — because a call the
    model can run beats a call it must first go and resolve, and the
    association an id needs is already in its transcript
    (``tasks(action='add')`` answers with id AND title).

    A row is USABLE only when its id is non-empty and survives
    :func:`sanitize_name` unchanged.  Task ids are server-minted at the
    write path (``tsk_`` + ``secrets.token_hex``) but are read back out
    of a JSON blob that a hand-edited DB or an older writer can leave
    ragged, so this is the belt-and-braces route applied where it can
    also be honest about executability: an id the sanitiser would ALTER
    is dropped rather than mangled, because a mangled id renders a call
    that cannot resolve — the precise failure the invented
    ``child_ws_id='a1b2c3d4'`` constant used to cause.  With no usable
    row the block is removed and the branches keep
    :data:`NUDGE_IDLE_TASKS_ID_SLOT` — one of the two states in which
    this body still asks for a discovery round-trip (the other is the
    eval override's cut-nothing ``None``; production's failed read no
    longer renders a body at all), and in both the round-trip is the
    honest answer because the harness genuinely holds no usable id.

    The single branch example prefers an ``in_progress`` row, falling
    back to the first usable one.  It is one id across every branch call
    (see property 2b on :data:`NUDGE_IDLE_TASKS_TAIL`): the same row
    shown under mutually exclusive statuses reads as a template,
    while a per-branch id would read as the harness ruling which row is
    done.

    *child_ws_ids* is keyword-only and required, and it is the SINGLE
    value governing every children-bearing element of this body — the
    caveat sentence, the blocked-on-a-child branch, and that branch's two
    slots.  One value because one storage read answers all of them, and
    because a body that omits the sentence about children while keeping
    the instruction about children is a contradiction the reader has to
    resolve.  The trichotomy is exactly the one the caller's read
    produces:

    * ``[]`` — an affirmative "this coordinator has no child row in a
      live state" — removes :data:`NUDGE_IDLE_TASKS_CHILDREN_CAVEAT` AND
      :data:`NUDGE_IDLE_TASKS_CHILD_DOOR`, both by literal match rather
      than by position, so a reworded neighbour cannot shift either cut.
      Nothing about children survives: no sentence, no branch, no
      placeholder pointing at a lookup that is known to return nothing.
    * a NON-EMPTY list keeps both and populates the branch: the wait call
      takes every id (capped at :data:`NUDGE_IDLE_CHILDREN_WAIT_CAP`,
      ``mode="any"``), which is unambiguous because a list slot has no
      wrong element to pick, and ``child_ws_id`` takes the first — most
      recently updated, since the caller's query orders by
      ``updated DESC``.
    * ``None`` — "I am not asserting a children state" — keeps both and
      substitutes neither slot.  PRODUCTION NEVER PASSES THIS.  The
      observer used to, for an indeterminate read, and now fails its
      whole event closed instead — a failed storage read silences BOTH
      idle nudges before either cap is charged: a nudge fired off a
      query that just failed is a reminder we cannot substantiate,
      aimed at a backend that is telling us it is unwell.  Not sending
      is as safe as hedging and strictly cheaper, so the hedge lost its
      reason.

      The branch stays because it has a live caller:
      ``turnstone.eval.nudges.render_tasks_body`` passes it under
      ``--body-override``, where the tail is operator-authored candidate
      text and BOTH literal cuts must be suppressed so a candidate that
      quotes the shipped caveat verbatim is not silently mauled.  ``None``
      is the only input that says "cut nothing" without also inventing a
      ws_id to substitute.  If that caller ever goes, so should this
      branch — it is not kept for safety.

    Populating the child slots asserts nothing about the TASK: it fills
    the only values a call there could take.  Whether any item is in
    fact waiting on a child stays the model's judgement, which is why
    the branch is still a conditional sentence.

    NO SANITISER runs on the counts, the statuses or the ws_ids: the
    counts are integers, the statuses are the producer's own
    ``TASK_OPEN_STATUSES`` vocabulary, and the ws_ids come from the
    workstreams table's primary key — the same server-minted source
    :func:`format_idle_children_nudge` already interpolates raw into its
    wait call, so a stricter rule here would be an asymmetry with no
    reason behind it.  Task ids take the sanitiser as an
    ALTERATION CHECK (above) because their store is softer.
    BELT-AND-BRACES RULE (the :func:`format_idle_children_nudge`
    precedent): any model- or user-authored field this body ever grows
    MUST go back through :func:`sanitize_name` before interpolation —
    server-minted provenance is the only exemption.

    COMPOSITION CAVEAT, carried where the next editor will see it:
    counts-without-provenance was never measured as a single body —
    the counts candidate and the provenance ablation each measured
    clean independently — and the round-9 nudge arm is the
    confirmation of this composition, since the eval renders production
    bodies by construction.  The id block and the populated calls are
    NOT yet swept either.

    Returns raw text *without* any envelope, matching
    :func:`format_idle_children_nudge` — the nudge is emitted as a
    first-class ``{"role": "system"}`` turn whose content is this text.
    An empty or all-zero mapping returns the empty string so callers
    can short-circuit on ``if not text: return``.
    """
    total = sum(open_counts.values())
    if total <= 0:
        return ""
    tail = NUDGE_IDLE_TASKS_TAIL

    # ``== []``, never a truthiness test: ``None`` is the indeterminate
    # read and must keep both (see the docstring).
    #
    # ONE condition, BOTH children-bearing elements.  The caveat sentence
    # and the blocked-on-a-child branch are removed together or kept
    # together, because a body that omits the sentence about children
    # while keeping the instruction about children is the defect the
    # sentence's conditional exists to fix, one block lower down.
    if child_ws_ids == []:
        tail = tail.replace(NUDGE_IDLE_TASKS_CHILDREN_CAVEAT, "", 1)
        tail = tail.replace(NUDGE_IDLE_TASKS_CHILD_DOOR, "", 1)

    usable = [(tid, status) for tid, status in open_task_ids if tid and sanitize_name(tid) == tid]
    if usable:
        block = "\n".join(f"  - {tid} ({status})" for tid, status in usable)
        tail = tail.replace(NUDGE_IDLE_TASKS_OPEN_LIST_SLOT, f"\n\n{block}", 1)
        example = next(
            (tid for tid, status in usable if status == "in_progress"),
            usable[0][0],
        )
        tail = tail.replace(NUDGE_IDLE_TASKS_ID_SLOT, example)
    else:
        tail = tail.replace(NUDGE_IDLE_TASKS_OPEN_LIST_SLOT, "", 1)

    # A falsy ws_id would render ``child_ws_id=''`` — populated in
    # appearance and unrunnable in fact, which is the shape this whole
    # change exists to remove.  Not producible today (every creation path
    # mints through ``uuid4().hex`` or ``secrets.token_hex``), so this is
    # a guard against a future writer rather than a live branch; it is one
    # comprehension and it keeps "substituted ⇒ runnable" true by
    # construction rather than by an argument about id minting three
    # modules away.  Filtered for RENDERING only: the branch cut above
    # read the raw list, so a child row that EXISTS without a usable id
    # still keeps the branch.
    live = [ws_id for ws_id in (child_ws_ids or []) if ws_id]
    if live:
        # The wait call FIRST: its slot literal contains the child slot,
        # so substituting the scalar first would consume the bytes this
        # replace matches on and strand the prose call.
        tail = tail.replace(
            NUDGE_IDLE_TASKS_WAIT_SLOT,
            wait_call(live[:NUDGE_IDLE_CHILDREN_WAIT_CAP]),
            1,
        )
        tail = tail.replace(NUDGE_IDLE_TASKS_CHILD_SLOT, live[0])

    split = ", ".join(f"{open_counts[status]} {status}" for status in sorted(open_counts))
    noun = "task" if total == 1 else "tasks"
    return f"You still have {total} open {noun}: {split}.{tail}"


# ---------------------------------------------------------------------------
# Detection heuristics — strong/weak tiers
#
# Strong patterns fire unconditionally.  Weak patterns carry inherent
# ambiguity ("no …", "thanks …") and only fire when the surrounding
# message looks like a genuine correction/completion rather than normal
# conversation.
# ---------------------------------------------------------------------------

_STRONG_CORRECTION: list[re.Pattern[str]] = [
    re.compile(r"(?i)^no[,.]"),  # "no," / "no." — clear rejection
    re.compile(r"(?i)\bdon'?t\b"),
    re.compile(r"(?i)^stop\b"),
    re.compile(r"(?i)^actually[,\s]"),
    re.compile(r"(?i)^instead[,\s]"),
    re.compile(r"(?i)\bnot like that\b"),
    re.compile(r"(?i)^wrong\b"),
    re.compile(r"(?i)\bthat'?s not\b"),
    re.compile(r"(?i)^I said\b"),
    re.compile(r"(?i)^I meant\b"),
    re.compile(r"(?i)\bnever\b.*\balways\b"),
    re.compile(r"(?i)^please don'?t\b"),
]

# "no <word>" is ambiguous — only match when the next word is a pronoun,
# demonstrative, article, or verb that signals the user is redirecting,
# not a fixed phrase like "no problem" or "no worries".  Allowlist >
# blocklist: we don't need to enumerate every benign "no X" phrase.
_WEAK_CORRECTION: list[re.Pattern[str]] = [
    re.compile(
        r"(?i)^no\s+(?:I\b|you\b|we\b|they\b|it\b|he\b|she\b"
        r"|that\b|this\b|those\b|these\b"
        r"|the\b|a\b|an\b"
        r"|not\b|do\b|did\b|but\b)"
    ),
]

_STRONG_COMPLETION: list[re.Pattern[str]] = [
    re.compile(r"(?i)\bthat'?s all\b"),
    re.compile(r"(?i)^lgtm\b"),
]

# These patterns are common in both completion AND mid-conversation
# acknowledgment.  Only fire when the message is short and has no
# continuation markers (question marks, follow-up requests).
_WEAK_COMPLETION: list[re.Pattern[str]] = [
    re.compile(r"(?i)^thanks\b(?!\s+for\b)"),  # "thanks for X" = acknowledgment
    re.compile(r"(?i)\blooks good\b"),
    re.compile(r"(?i)^perfect\b"),
    re.compile(r"(?i)^great job\b"),
    re.compile(r"(?i)\bthat works\b"),
    re.compile(r"(?i)^done\b"),
]

_WEAK_MSG_CAP = 80  # weak completion patterns suppressed above this length

_CONTINUATION = re.compile(
    r"(?i)(?:\?|(?:can you|could you|please\s|also\s|but\s|now\s|next\s"
    r"|and\s+then|after\s+that|one\s+more|however))"
)


def detect_correction(message: str) -> bool:
    """Return True if the message looks like a user correction."""
    if not message:
        return False
    if any(p.search(message) for p in _STRONG_CORRECTION):
        return True
    return any(p.search(message) for p in _WEAK_CORRECTION)


def detect_completion(message: str) -> bool:
    """Return True if the message signals session completion."""
    if not message:
        return False
    if any(p.search(message) for p in _STRONG_COMPLETION):
        return True
    if len(message) > _WEAK_MSG_CAP:
        return False
    if _CONTINUATION.search(message):
        return False
    return any(p.search(message) for p in _WEAK_COMPLETION)


def _cooldown_allows(
    nudge_type: str,
    state: dict[str, float],
    *,
    cooldown_secs: int = _COOLDOWN_SECS,
) -> bool:
    """Read-only cooldown peek — does NOT record a fire timestamp.

    Use this as a cheap pre-gate before expensive work (storage queries,
    message walks).  The authoritative follow-up re-checks cooldown
    before the stamp is written — :func:`should_nudge` for
    check-and-stamp callers, or :func:`nudge_allowed` +
    :func:`record_nudge` where delivery has its own later gate (the
    coordinator observer's split).  A producer that races between this
    peek and the follow-up would just lose the fire to the other
    producer — benign.
    """
    last = state.get(nudge_type)
    if last is None:
        return True
    return time.monotonic() - last >= cooldown_secs


def nudge_allowed(
    nudge_type: str,
    state: dict[str, float],
    *,
    message_count: int = 0,
    memory_count: int = 0,
    cooldown_secs: int = _COOLDOWN_SECS,
) -> bool:
    """Every gate :func:`should_nudge` applies, WITHOUT recording a fire.

    Split out so a producer with a further authoritative gate after this
    one (the coordinator observer charges a per-type cap slot before it
    enqueues) can ask "may this fire?" and record only once the fire
    actually happened.  Recording earlier spends the cooldown on a nudge
    that was never delivered.

    Note the gates this applies that a bare ``_cooldown_allows`` peek
    does NOT: unknown type, ``message_count <= 1``, the ``start``
    first-message rule, and the memory-count requirements.  A caller
    that charges budget before consulting THIS function would charge on
    every one of those refusals.
    """
    if nudge_type not in _NUDGE_MAP:
        return False
    # Don't nudge on the very first message (except resume/start)
    if message_count <= 1 and nudge_type not in ("resume", "start"):
        return False
    # Start nudge only on first message
    if nudge_type == "start" and message_count != 1:
        return False
    # Tool error nudge only if there are memories to search
    if nudge_type == "tool_error" and memory_count == 0:
        return False
    # Resume/start nudge only if there are memories to recall
    if nudge_type in ("resume", "start") and memory_count == 0:
        return False
    # Rate limit: one nudge per type per cooldown window
    last = state.get(nudge_type)
    return not (last is not None and time.monotonic() - last < cooldown_secs)


def record_nudge(nudge_type: str, state: dict[str, float]) -> None:
    """Stamp a fire, starting this type's cooldown window.

    Call at the point the nudge is actually DELIVERED (enqueued), not
    when it is merely permitted — see :func:`nudge_allowed`.
    """
    state[nudge_type] = time.monotonic()


def should_nudge(
    nudge_type: str,
    state: dict[str, float],
    *,
    message_count: int = 0,
    memory_count: int = 0,
    cooldown_secs: int = _COOLDOWN_SECS,
) -> bool:
    """Check whether a nudge should fire, respecting cooldowns and context.

    Records the fire timestamp on success — the check-and-stamp shape
    every caller whose delivery immediately follows the check wants.
    A caller with an authoritative gate BETWEEN the check and delivery
    must use :func:`nudge_allowed` + :func:`record_nudge` instead, or a
    refusal at that later gate burns the cooldown for a nudge nobody
    received.
    """
    if not nudge_allowed(
        nudge_type,
        state,
        message_count=message_count,
        memory_count=memory_count,
        cooldown_secs=cooldown_secs,
    ):
        return False
    record_nudge(nudge_type, state)
    return True


def format_nudge(nudge_type: str) -> str:
    """Return the nudge text for the given type."""
    return _NUDGE_MAP.get(nudge_type, "")
