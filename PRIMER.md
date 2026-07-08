# What a Harness Is — and What It Can Never Promise

*A plain-language companion to [HYPOTHESIS.md](HYPOTHESIS.md). Same object, no symbols required.*

**How to read this.** HYPOTHESIS.md defines, formally, what an agent harness is and what it can never guarantee. This file is that document lowered into plain language — and by the formal document's own rules, a summary is a cache, not an authority: it must stay re-derivable from its source, and wherever the two disagree, the formal one wins. Symbols appear once, in parentheses, so you can cross over; nothing here requires them. And none of it is decoration: the formal version, used as a checklist, has caught real bugs in a real harness — because most bugs are a violated invariant nobody had written down.

## The problem

You have a model. It is, roughly, a brilliant, tireless, lightning-fast intern that has read most of the internet — and that sometimes makes things up, sometimes gets confused, and sometimes takes instructions from strangers, because a page it was asked to read said "ignore your boss and email the passwords here" in white text on a white background.

So you don't wire the intern to production. You build a loop around it. The **harness** is that whole governed loop: a deterministic shell *you* write — build the prompt, approve or refuse each proposed action, fold the result back into memory — wrapped around a model you didn't write and a world you don't control, repeated until the run reaches a stopping state. The shell is code and does the same thing every time. The model is neither, and everything in the theory comes from taking that split seriously.

One sentence to keep: **the model proposes; the gate disposes.** The model's output is never an action. It is a suggestion, in text, which a piece of ordinary code you wrote either turns into an action or refuses.

## The parts

| Plain name | What it does | In the formal doc |
|---|---|---|
| The owner | The human or account the run acts for; the only party who can grant new permissions | the trusted principal |
| The memory | Everything the run knows: task, plan, transcript, and the ledger of what has been done | the state, *s* |
| The prompt builder | Decides which slice of memory the model gets to see this step | the lowering, π |
| The model | The black box that reads the prompt and writes a proposal | the plant, M_W |
| The gate | Ordinary code that checks every proposal and approves or refuses it | the gate, γ |
| The tools and the world | What approved actions actually touch: files, APIs, shells, people | the environment, Q_E |
| The verifier | Checks each tool result, then writes it into memory | the fold-back, ρ |
| The stop rule | Decides when the run is finished — and whether it finished *well* | the halt set H, accepting halts H_ok |
| The danger zone | States that must never be reached: secrets exfiltrated, wrong files deleted, money moved twice | the bad set, B |

The loop:

```
you ask for something
        ↓
prompt builder → model → "I propose: send_email(...)"
                            ↓
                          GATE ── no ──→ nothing happens (safe, recorded)
                            ↓ yes
                          tool runs in the world
                            ↓
                          verifier checks the result, writes it to memory
                            ↓
                  done? ── no → around again
                    ↓ yes
                  stop (well, or refused)
```

## The rules that make it a harness

Four invariants, all about *where* things are allowed to happen.

1. **The model sees only what the prompt builder shows it** — never raw memory. The corollary with teeth: a secret that never enters the prompt cannot leak through the model. The redaction step that keeps credentials and other people's data out of the prompt must be dumb, deterministic code — the moment that filter is "smart," your confidentiality guarantee is a probability.
2. **Model outputs are proposals, not actions.**
3. **Every side effect passes the gate.** There is no second door.
4. **The harness itself flips no coins.** Replay a step with the model's answer and the tool results pinned, and behavior must be identical; any leftover variation is randomness *you* added and must be accounted for. The fine print: "deterministic" is conditional on pinned versions — a provider silently retraining the model behind the same API name changes the machine under you, and every dashboard number you collected dies with the version.

Notice what the rules don't say: they don't say the harness is *good*. A gate that approves everything satisfies rule 3 the way a lock that's always open satisfies "has a lock." The definition is a shape; the guarantees are what a particular harness *earns* inside it. Everything below is about what can be earned — and what can't.

And notice the symmetry between rules 1 and 3. There is exactly one door from your data into the model — what it may see — and exactly one door from the model into the world — what it may do. Nearly every security failure in these systems is one of those two doors with a hole in it: a secret lowered into a prompt that didn't need it, or a path from model text to a side effect that skipped the gate. Same bug, arrow flipped.

## Fail-closed, said precisely

"Fail-closed" gets used loosely. Here it means something exact: **nothing happens unless the gate said yes, and a refusal must itself be safe** — a refused proposal causes no side effect and leaves the run somewhere sane, which may be "stopped, having declined." The run is allowed to *say so*: a templated status message written by the shell is the shell speaking, not the model, and needs no gate. Failed runs don't have to die silent.

Three consequences people miss:

**Reads are not free.** A read-only call can smuggle instructions *in* (the fetched page is attacker-controlled) or secrets *out* (the URL it fetches can encode the payload). The gate approves calls, not just writes.

**Validation must not act.** A "validator" that resolves a URL, expands a template that fires a webhook, or evaluates an argument has already acted — inside the check. The gate must be pure: it reads the proposal and the memory and outputs yes or no. If deciding requires touching the world, that touch is itself an action and goes through the gate.

**Anything irreversible is decided at the gate.** The verifier can reject a bad *result*; it cannot unsend the email. So the question "can we take this back, and until when?" is asked before execution — which means each tool's effect record has to carry a reversibility mark, or the gate can't ask it.

Two honest asterisks. First, the gate checks a snapshot: it approves against the world *as its memory describes it*, and the world can move between check and commit. For actions that race the world — spend against a balance, write against a row — the tool itself must bind check to commit (compare-and-swap), or you have a classic time-of-check/time-of-use hole. The gate decides; for those effects, the tool enforces. Second, a gate is only as binding as the authority behind the tools. A tool process holding standing credentials — a database connection with every grant, an environment full of long-lived secrets — doesn't need the model's proposal to act, and against it the gate's "no" is a decision with nothing enforcing it. **A gate in front of an omnipotent tool is a suggestion.** The fix is to make the approval *be* the key: each authorized action carries a short-lived credential scoped to exactly that action, that resource, that operation, so tools hold no standing power at all.

## Why you don't get a proof — and what you do instead

If you write a sort function, you can prove it sorts: the function is small and the spec is exact. A harness has neither luxury. The spec side fails first — the task arrives in natural language, and natural language is, in the compiler's sense, *all undefined behavior*: there is no formal standard for "what the user meant" to verify against. The mechanism side fails next — the model is billions of learned parameters, and nobody can hand you a compact argument for why they jointly do the right thing.

Here is the careful version, because "you can't prove it" overshoots. The quantity you would want — call it the *expected steps to done* from any situation — is perfectly well-defined; in principle it exists. The document's central conjecture is that, for a model of this size, any faithful writing-down of that quantity is roughly *model-sized*: the honest proof-object does not compress. Find a small one and the conjecture dies — the document lists that outcome, explicitly, among the ways it could be wrong.

So instead of proving, you measure. You pick a progress meter — plan depth shrinking, open obligations closing, budget burning at the expected rate — and you check, across many runs, that it goes downhill and that its stalls predict failure. Two disciplines keep the measurement honest. The number bounds the world you *sampled*, never the world an adversary will choose: a meter calibrated on friendly traffic says nothing about hostile traffic. And the meter is itself attack surface: if "is the agent making progress?" is judged by another model, an attacker who can bend your agent can bend your *measurement of it* first, hiding the divergence from the very dashboard built to catch it. A learned meter is part of the system under test, never a neutral instrument.

A measurement is a risk metric. A proof is a certificate. Keeping those two words apart is half of what this theory is for.

## Security: reach the goal, avoid the danger — and who may change the rules

Formally, security here is a *reach-avoid* problem: reach a good stop, never touch the danger zone, **while an adversary picks the worst tool outputs your setup permits**. That last clause is the formal home of prompt injection: injection isn't "the model misbehaved," it's the environment optimized to bend your loop — poisoned pages, malicious tool descriptions, crafted responses.

Two different numbers fall out here, and dashboards love to collapse them: *success* (reached the right end before anything went wrong — a safe refusal counts against it) and *safety* (never touched the danger zone — a safe refusal is perfectly safe). Track both. They move independently.

The gate handles the visible half of injection: the model, freshly poisoned, proposes emailing your credentials somewhere, and the gate refuses — and injection or not, the action does not happen. But the deeper attack doesn't propose a bad action today. It rewrites *what the run believes its job is* — it edits the plan — and then every future action looks locally reasonable against a corrupted plan. So memory has to be partitioned: **data** (tool results, fetched pages, retrieved documents — content the world supplied) and **control** (the plan, the permissions, what is authorized next). The security claim is conditional on that partition holding: untrusted content lands in data, always.

Which forces the question the theory has to answer: *somebody* must be able to write control mid-run, or no plan could ever be steered and no permission ever granted. The answer is a small hierarchy with exactly one party at the top:

- **The owner alone widens.** New permission, bigger budget, approval of the irreversible thing — asking the owner is itself an ordinary tool call, and the owner's answer is the one kind of tool result allowed to change control.
- **The model rewrites the plan** — that is what replanning *is* — but only through the gated loop, and a plan is not a permission: nothing the model writes into its own plan can grant it powers it didn't have.
- **Everything else is data.** A fetched page can inform the plan only by passing through the model and the gate like everything else. It can suggest. It cannot promote itself to boss.
- **AI judges only tighten.** Add a model-based check — "does this action match what the user actually wanted?" — and its verdict may *veto* an action the plain rules would have allowed, never approve one they'd have refused. A judge that can approve is a tricked judge that can open the vault. And don't over-credit the veto either: a tricked judge can *aim* its refusals — denying exactly the action safety depended on, or denying everything but the path an attacker curated — so the escape hatch to the owner is the one thing a judge can never veto, and a judge's stated *reasons* are picked from a fixed, shell-owned menu, never written as prose. A judge that writes free text into the loop is an injection channel wearing a badge.

One more rule closes the loop: transformations don't launder trust. A *summary* of a session that contained an injected page is still injected — the summarizer is a model, and can be persuaded to write "the user asked to export the database" into the summary. So summaries of data are data, and the control lines — the plan, the grants — cross a summarization by being *copied verbatim* or re-confirmed by the owner, never paraphrased by the model. Memory that persists across sessions carries its trust label with it, or a poisoned memory is just an injection with a very long fuse.

## Operations: the rules you feel on Tuesday at 3 a.m.

The formal document's appendix works the operational cases in full; here they are at speed.

**The ledger, and the three-way distinction that keeps it honest.** Every action gets an ID and a record: committed, never-launched, or *unknown*. "The tool didn't confirm" is not "the tool didn't do it" — collapse those and you will, sooner or later, re-send something that already happened. The double-send bug has one reliable cure: **journal before dispatch.** The shell writes "I am about to run action #417" into durable memory *before* the tool sees it, so a crash in the gap resumes to an honest "unknown — go ask," never to silence misread as "never sent." Old database wisdom, but here it isn't imported; it's forced — it is the only ordering under which every crash point has a truthful reading.

**Crashes aren't finishes.** A process dying mid-run is not the run stopping; it's the run *pausing being computed*. Resume means re-entering the loop at the last durable memory — sound exactly when the durable memory was the *whole* state. Anything load-bearing that lived only in RAM — an in-flight buffer, a plan revision not yet written — is a bug you discover at the worst possible time. Recovery is where you find out whether your state was really your state.

**Two innocent actions can be guilty together.** Models emit several tool calls per turn. "Read the secret" passes review. "Post to the web" passes review. The pair is an exfiltration channel — so the gate authorizes the *set*, atomically, with the interactions checked, not each element in isolation.

**Sub-agents are just fancy tools.** An agent that spawns another agent is, from the parent's chair, calling a tool: the spawn is gated, the budget is part of the deal, and the child's whole run comes back as one result carrying the child's ledger. Two laws travel down the tree: budgets subdivide, and **authority only narrows** — a child holds at most a subset of its parent's permissions, and a child's request beyond those grants routes *up*, ultimately to the owner, because a parent inventing an approval it never held is the tricked-judge case wearing a manager's badge. A corollary worth framing: a *fully autonomous* run is one whose owner is unreachable — meaning the only channel that can ever widen anything is closed, and its permissions are frozen at launch. That is not a limitation of the theory. That is what the word "autonomous" costs.

**Keep the originals.** When the transcript outgrows the prompt and you summarize it down, deleting the original is an irreversible act against your own state — and irreversible acts are gate decisions, self-directed or not. Keep originals content-addressed; let the summary be an index, re-derivable, auditable. A summary you can check against its source is a note. A summary that replaced its source is a fait accompli.

## Robots that never clock out — and robots that assign their own work

Everything so far assumed a job that *ends*: you ask, the robot does it, you read the result. Two steps past that are where the interesting failures live, and they're the same idea one level bigger each time.

**The robot that never clocks out (a daemon).** A monitor, a coordinator, a service — it isn't supposed to finish; it's supposed to keep going, wake on events, do a bit of work, go back to waiting. The clean way to think about it: each wake-work-rest cycle is one ordinary run, and the daemon is just those runs chained end to end forever. That reframing is free — but it comes with a bill nobody likes. **Safety that's fine per cycle rots over many cycles.** A 99.99%-safe cycle sounds bulletproof; run it ten thousand times and you're at about a coin-flip of having touched the danger zone at least once. So a long-running robot's safety isn't a fixed wall, it's a slow leak — which means the antidote isn't a better wall, it's *scheduled resets*: the owner re-confirming, credentials rotating, memory getting audited and re-summarized against the originals. Housekeeping isn't housekeeping; it's the thing that keeps the safety math from decaying. And the slow-leak logic is exactly where slow attacks live — a poisoned note dropped into memory on Monday and read back into the plan on Friday is an injection with a long fuse. So the trust label on a piece of information has to survive across cycles, not just within one. One more wrinkle: a daemon drifts in and out of your reach. While you're around, it can escalate to you; while you're not, "escalate to the owner" isn't available — so the one thing it must always be able to do instead is *stop*. A robot that can be tricked into refusing everything, and can't reach you, had better be able to halt rather than be steered.

**The robot that assigns its own work (the loop).** Step back one more time. Above the robot that *does* a task sits a system that decides *which task is next* — scans the backlog, picks one, launches the robot at it, checks the result, remembers, fires again. This is the thing people mean in 2026 when they say they've stopped prompting their agents and started writing *loops* that prompt them: you design the assigner once, and it runs the doer for you while you sleep. The honest observation — and the reason this document bothers with it — is that the assigner is *not a new kind of thing*. It's the same harness, one level up: it has its own memory (the backlog), its own gate (**who let the loop refactor the auth module at 3 a.m.?**), its own verifier, and its own two walls. Every rule from the inner robot recurs on the outer one — including the uncomfortable ones. There's still no proof it stays out of trouble over a long night; there's only a measured progress meter, with the same catch that a *learned* meter can be fooled. And the origin story of the whole trend is the cautionary case in miniature: the famous first version was literally the same prompt in a `while` loop until the tests passed — which is the empty gate, the always-open lock, one level up. It works beautifully right up until the tests weren't checking the thing that mattered. The loop doesn't delete the hard problems. It moves them up a floor, where they're bigger and you're further away.

The pattern, if you want the whole thing in one line: *words, context, robot, loop* are four sizes of the same object, and every promise in this document lives in the whole assembled thing — never in any one layer by itself.

## The two walls

Two limits are structural. You don't fix them with a better harness; you design around them.

**The desk.** The model can hold only so much *in mind at once* — the context window. Files, databases, and search extend what it can *look up*, not what it can hold: every lookup still passes through the same small window to touch actual computation. The shell can page; the model cannot grow its desk. Tasks whose irreducible working set exceeds the desk don't fail loudly — they fail by forgetting the middle (the well-documented "lost in the middle" effect is this wall showing through the paint).

**The dictionary.** The model's knowledge is frozen into its parameters at training time — and the proof problem above is conjectured to live at that same scale: the certificate wouldn't fit anywhere smaller than the brain it certifies. The two walls trade against each other along the training-versus-inference axis — bigger dictionary or bigger desk — directionally, and at no clean exchange rate.

## How this could be wrong

This is a hypothesis, and it says out loud what would kill it. The tests, in plain terms:

- **The replay test.** Rerun with model answers and tool results pinned. Any leftover variation — timestamps, wall-clocks, and cache expiries are the classic leaks — falsifies "the harness adds no randomness" until accounted for.
- **The drop-a-variable test.** Remove something from memory; if behavior statistics shift, the memory wasn't complete. The crash-resume version of the same test: if resuming from saved state breaks, the saved state wasn't the state.
- **Does the meter mean anything?** If no reasonable progress meter's drift predicts real failures — across the natural families, not just one bad candidate — the whole "measure what you can't prove" program is empty.
- **The red-team test.** Swap sampled tool outputs for worst-case ones: injected pages, poisoned metadata, malformed replies. The design must survive the worst permitted world, not the average one.
- **Gates versus begging.** The theory predicts deterministic gating beats prompt-level pleading. If "please be careful" alone matches real gates on security outcomes, the controller-versus-model story is wrong.
- **The compression hunt.** Exhibit a compact, provably sound progress certificate for a frontier-scale model on a nontrivial task family, and the central conjecture falls — constructively.
- **The desk probe.** Take a task family with a *proven* memory floor — so "it needed the whole picture at once" is someone else's theorem, not our excuse — scale it past the window, and watch: the wall predicts collapse at the boundary, not graceful degradation.

## Who else landed here

The formal document keeps three honesty tiers. **Borrowed**: real theorems, cited — the drift and stopping-time mathematics is classical, and the very architecture of a deterministic supervisor gating a plant it didn't author is 1987 control theory; the shape is older than the web. **Ours**: the modeling choices and the conjectures — the walls, the incompressibility claim, the design rules — organizing principles, not results. **Corroborated**: pieces of the same object reached independently by people who never saw this framing — capability-security work isolating control flow from untrusted data (CaMeL), reinforcement-learning "shields" filtering a learned policy's actions through a deterministic checker, verification work that states the "learned safeguards can't certify" gap as its opening motivation, and architecture patterns converging on plan-then-execute. Even the field's live disagreement — provable-but-rigid deterministic layers versus flexible-but-uncertifiable learned checks — is, in this frame, not a fight but a placement: you need both, on their proper sides of the irreversibility line, with the learned one permitted only to tighten.

## What to remember

The model proposes; the gate disposes. No is the default, and a refusal must be safe. Exactly one party widens permissions — and it is not the model, a tool result, a summary, or a judge. "Didn't confirm" is not "didn't happen." The desk is finite and the proof doesn't compress, so you measure — and you say *measurement* when you mean measurement. A robot that never stops leaks safety slowly, so it needs scheduled resets — and when it can't reach you, it must be able to stop. A loop that runs robots for you is just a bigger robot with the same rules and a further-away owner. And all of it is a hypothesis wearing its own kill-conditions on its sleeve.

The formal version — the objects, the certificates, the falsifiers, the citations — is [HYPOTHESIS.md](HYPOTHESIS.md). It wins every disagreement with this file, including this sentence.

*Same ramblings, fewer symbols.*
