# What is a harness?

*A hypothesis — not a theorem. The honest answer is a claim about **shape**: an object you can write down that says what a harness is and, just as precisely, the one guarantee it can never carry.*

Most descriptions of an agent framework are a feature list. This is an attempt at a definition.

---

## The claim

*Informal.* A harness is a **stopped, deterministically-controlled Markov process on task-state, closed around a stopped autoregressive process on context-space, driven by a learned model kernel** — a deterministic controller in closed loop with a stochastic learned plant.

*Formal.* A harness is a tuple $\mathcal{H} = (\mathcal{S}, \mathcal{C}, \mathcal{Y}, \mathcal{E}, \pi, M_W, Q_E, \rho, H)$: a deterministic lowering $\pi:\mathcal{S}\to\mathcal{C}$, a stochastic model-run kernel $M_W(c, dy)$, a stochastic environment/tool kernel $Q_E(s, y, de)$, a deterministic verify-and-fold-back map $\rho:\mathcal{S}\times\mathcal{Y}\times\mathcal{E}\to\mathcal{S}$, and a halt/ready set $H\subseteq\mathcal{S}$. The induced outer transition kernel is

$$T(s, A) = \int_{\mathcal{Y}\times\mathcal{E}} \mathbf{1}_A\!\big(\rho(s, y, e)\big)\; M_W(\pi(s), dy)\; Q_E(s, y, de),$$

and the harness runs $s_{n+1} \sim T(s_n)$ until $\tau^\star = \inf\{n : s_n \in H\}$. Because $\pi, \rho, H$ are deterministic they sit *outside* the integral: the controller injects no randomness, and every coin is inherited from $M_W$ and $Q_E$. (The earlier shorthand $T = \rho \circ (M_W \circ \pi, E)$ is suggestive but ill-typed — $M_W$ returns a *law*, while $\rho$ consumes a *sample* together with the prior state $s$; the integral is what the shorthand meant.) This displayed $T$ is the time-homogeneous, fixed-kernel case; for nonstationary or adversarial environments, replace $Q_E$ with a time-indexed kernel $Q_{E,n}$ — or an admissible family of kernels, or an adversary's policy — over which the robust certificate (the minimax form under *The limit*) quantifies. If that adversary conditions on history rather than only the current $(s, y)$, the history must itself live in $s$ — otherwise the object is a Markov *game* requiring further augmentation, not a Markov chain.

*The inner kernel.* $M_W$ is itself a stopped process, and for a decoder-only transformer it is implemented as

$$M_W(c) = \mathrm{Law}(c_\tau), \qquad c_{t+1} \sim K_W(c_t, \cdot), \qquad K_W(c,\, c\!\cdot\! v) = (U \circ \Phi_W \circ \mathrm{Emb})(c)[v],$$

with the layer stack $\Phi_W$ on the residual stream as the "manifold" core. Here the output space $\mathcal{Y}$ is either the stopped context $c_\tau$ itself (so $\mathcal{Y} = \mathcal{C}$) or a total readout $R : \mathcal{C} \to \mathcal{Y}_\bot$ — a parsed tool-call, answer, or transcript, returning $\bot$ when parsing fails — in which case $M_W(c, \cdot) = R_\#\,\mathrm{Law}(c_\tau)$, the pushforward of the stopped-context law along $R$ (equivalently $M_W(c, B) = \Pr[R(c_\tau) \in B \mid c_0 = c]$). That reconciles the kernel into $\mathcal{Y}$ with the law over contexts, and the $\bot$ branch is exactly what $\rho$ rejects fail-closed. This is a **specialization, not part of the definition**: a harness wrapped around a black-box API is still a harness, and $M_W$ may be any learned kernel. Where the weights are open, the geometry of $\Phi_W$ is where the substrate's continuity lives, and several downstream claims lean on it — but the definition does not.

Two stopped processes, nested: **deterministic control over stochastic dynamics over a learned kernel.** Both loops are hitting-time processes; *some* harnesses additionally read the halt set as a fixpoint or acceptance condition — iterative refinement to self-consistency is the genuine fixpoint case, while EOS, length, and tool-call syntax are not convergence. Neither loop settles because you asked it to.

## Reading it

| Symbol | Is |
|---|---|
| $\mathcal{H}$ | the harness — the whole controlled system, *not* the model |
| $s \in \mathcal{S}$ | task-state: IR / dialect stack, tool results, plan, counters, **and every mutable interface variable** (model/tool versions, permissions, retrieved context) — only Markov *after* that augmentation |
| $\pi : \mathcal{S} \to \mathcal{C}$ | **lowering** — prompt construction, dialect lowering, effective-program selection (deterministic) |
| $M_W(c, dy)$ | the **model-run kernel** (inner solver) — a stopped autoregressive process; $\Phi_W$ is the residual-stream ("manifold") core in the transformer case |
| $Q_E(s, y, de)$ | the **environment/tool kernel** — tool effects, API responses, the world (possibly adversarial) |
| $\rho : \mathcal{S}\times\mathcal{Y}\times\mathcal{E} \to \mathcal{S}$ | the **fail-closed verify-and-fold-back** (deterministic) |
| $H,\ \tau^\star$ | the **halt/ready set** and the outer **halting time** — a hitting-time process, not a single pass |

The structural fact that earns the word *controller*: $\pi$, $\rho$, the halt test, and the readout are **deterministic**, so $\mathcal{H}$ injects no randomness of its own. Every coin is inherited from $M_W$ and $Q_E$. This determinism is *conditional* — on versioned code, configuration, model endpoint, and tool interfaces; any retry, timeout, race, or randomized routing that escapes that conditioning must be modeled explicitly as part of $Q_E$ or the controller, not waved away.

## Why this shape

$$f(x) \;\longrightarrow\; x = f(x;\,W) \;\longrightarrow\; f(x)$$

Classical software, inverted into latent geometry, then re-wrapped in classical software. The harness **re-imposes the determinism the model dissolved**: $\pi, \rho, \tau^\star$ are ordinary designed code — a controller — whose primitive operand happens to be a stochastic oracle. That closure is why a compiler is the right mental model (staged deterministic software ports cleanly) and exactly why the analogy breaks (a compiler's primitive operation was never a coin). **The harness is the half you can reason about classically, sitting on top of the half you cannot.**

## The limit, stated honestly

**The architecture supplies no Foster–Lyapunov certificate automatically** — a weaker and truer statement than "carries none by construction," because one provably exists. A certificate would be *sufficient* for almost-sure halting with bounded expected runtime: a $V \ge 0$ with

$$\mathbb{E}[\,V(s_{n+1}) \mid s_n\,] \le V(s_n) - \varepsilon \quad\text{off the halt set}$$

bounds $\mathbb{E}[\tau^\star] \le V(s_0)/\varepsilon$. Nothing in the harness hands you such a $V$ the way a compiler's structure does: a C compiler gets its $V$ for free because a finite-height lattice *is* a well-founded descent, so termination holds by structure; the harness has no such lattice.

But the relevant $V$ is not *absent* — and this is the subtlety the blunt phrasing erased. The minimal certificate exists and is **forced**: it is the expected halting time itself,

$$V^\star(s) = \mathbb{E}[\,\tau^\star \mid s_0 = s\,],$$

finite wherever $H$ is reached in finite expected time — the domain $\{s : \mathbb{E}_s[\tau_H] < \infty\}$ — though note this $V^\star$ certifies *halting* (reaching the terminal set $H$ at all), not *correct* halting; the stronger object, the expected time to an accepting $H_{\mathrm{ok}} \subseteq H$, is $V^\star_{\mathrm{ok}}$, taken up at the second wall below. So the honest claim splits in two: the architecture provides no certificate *for free*, and the one that exists is — **conjecturally, not as a theorem** — a functional of all of $W$ and the environment that does not compress below model scale. The compiler's certificate is structurally trivial; ours is *plausibly* as hard as the dynamics, though whether useful compressed certificates exist for structured sub-tasks is open. This is the quantitative form of *you can borrow how LLVM is built — not, in general, why it is correct.*

So you never compute $V^\star$. You pick a candidate $\hat V$ and **estimate its drift slack**

$$\delta = \sup_{s \notin H}\Big(\mathbb{E}[\,\hat V(s_{n+1}) \mid s_n\,] - \hat V(s_n) + \varepsilon\Big).$$

The status of $\delta$ has to be stated carefully, because it is easy to oversell. If you can establish a *high-confidence upper bound* on the true worst-case slack and it is $\le 0$, optional stopping hands you a real, conservative certificate, $\mathbb{E}[\tau^\star] \le \hat V(s_0)/\varepsilon$. But an *empirical* $\delta$ estimated from sampled states is **not** a certificate: a measured $\delta > 0$ may mean the candidate $\hat V$ is poor, the sampled distribution missed rare failures, the supremum was never attained in-sample, the process is non-stationary, or the state abstraction is not Markov. So $\delta$ is **the number on the dashboard** — a *calibrated risk metric*, the evaluable surrogate for a guarantee the geometry will not give you, and a genuine bound only once it is statistically controlled against rare-event and adversarial tests. Its floors are the size of the divergent set $\mu(D)$ and the hitting-time variance $\mathrm{Var}[\tau^\star]$ — both properties of the trained weights, knowable only a posteriori.

> For an agent *meant* to run forever — a coordinator, a daemon — halting is the wrong target, and $V^\star = \infty$ is the spec, not a pathology. The same drift theory then certifies **recurrence to a ready-state** instead of absorption to a halt-set. The object changes; the missing certificate does not.

And the consolation rests on an assumption the world violates. The whole drift apparatus — $V^\star$, the hitting-time bound, the slack $\delta$ — assumes a **time-homogeneous kernel**: the same state transitions the same way every time. But the environment $E$ is *part of* $T$, and the world is not stationary — worse, it can be **adversarial**, an attacker choosing what a tool returns so as to maximize your non-halting. The drift condition then stops being a fixpoint question and becomes a **minimax** one,

$$\sup_{e\,\in\,E_{\text{adm}}}\ \mathbb{E}[\,V(s_{n+1}) \mid s_n,\, e\,] \le V(s_n) - \varepsilon,$$

a descent that must hold even when the environment picks the worst admissible step. A $V$ that certifies halting against a benign world is defeated by an adversarial one, and the measured $\delta$ bounds only the $E$ you *sampled*, never the $E$ an attacker will choose. **This is the formal home of prompt injection** — not "the model did something bad," but the environment optimized to break your descent. It is also what fail-closed verification ($\rho$) is *for*: the disturbance-rejection margin that caps how far an adversarial world can move the drift. In this language, security is robustness of the certificate.

There is a **second wall, orthogonal to the first.** It binds not the full harness state $\mathcal{S}$ but the **model-visible working memory** $\mathcal{C} = \mathcal{V}^{\le L}$ — bounded by the context length $L$. That bound is *not* the incompressibility of $V^\star$ (a fact about the parameters $W$ — the **dictionary**, fixed at training); it is a fact about the inner kernel's **working memory** (the $L\times d$ residual stream — the **desk**). $\mathcal{S}$ itself may be far richer — files, databases, vector stores, durable memory, queues — but that is *external* memory the shell supplies, and the distinction is the point: every external read still passes *through* the $\le L$ window to touch computation, so external stores extend addressable storage without extending the per-pass resident set. The shell can page; the plant cannot grow its desk. Under the standard fixed-depth, fixed-precision theoretical model a single forward pass is constant-depth ($\mathsf{TC}^0$) — *suggestive* for deployed models, not literal, and shifting once depth grows with context (log-depth variants escape parts of it); the qualitative point survives the caveats: one pass buys bounded sequential depth, so the loop buys more only by emitting tokens: **the context window is the tape, the autoregressive loop is the read/write head**, and — in the variable-$L$, fixed-precision idealization — the model-mediated inner computation behaves like a linear-bounded automaton, its reachable fixpoints capped by space-$O(L)$ computability (chain-of-thought is register-spilling onto that tape). This is a *second* obstruction beside divergence, and it concerns *success*, not raw halting. Split the terminal set: let $H$ be any halt/ready state (including fail-closed refusal) and $H_{\mathrm{ok}} \subseteq H$ the successful, accepting halts, with $V^\star_{\mathrm{ok}}(s) = \mathbb{E}[\tau_{H_{\mathrm{ok}}} \mid s_0 = s]$ taken on the process where $H \setminus H_{\mathrm{ok}}$ — halting wrong, refusing, failing closed — is *absorbing failure*, so a run that fails closed before acceptance has infinite accepting hitting time unless the spec explicitly restarts it. Then $U(L)$ is the set of tasks whose **irreducible per-step model-mediated working set** exceeds $L$ — not tasks whose *data* exceeds $L$ (those the shell can page), and not work that can be **discharged to a verified external tool** (a solver, interpreter, or compiler computes off-context). For a task in $U(L)$ the raw chain may still hit $H$ — by failing closed, refusing, or returning a wrong answer — so $V^\star = \mathbb{E}[\tau_H \mid s]$ stays perfectly well-defined; what blows up is $V^\star_{\mathrm{ok}}$, the expected time to a *correct* halt, which is infinite or semantically undefined. The honest statement is about the finite-success domain: $\mathrm{dom}_{<\infty}(V^\star_{\mathrm{ok}}) \subseteq \mathrm{reachable}(L) \setminus D$. The two walls **trade**: parametric memory $|W|$ and working memory $L$ are substitutable on one budget line — the pretraining-vs-inference-scaling axis. And the bound is inherent to *finite working memory*, not attention specifically: state-space models make it **tighter** (a fixed register set), and real attention's usable tape is shorter than $L$ (lost-in-the-middle).

## Where it cashes out

This is not ornament; the decomposition is load-bearing in the design.

- **$\pi$ is a progressively-lowered dialect stack** — raw input → intent → plan → tool-call → the neutral wire IR — each level a deterministic pass with its own verifier. The drift splits by coordinate, $r = r_{\text{shell}} + r_{\text{plant}}$: the shell term is an *exact, designed* descent (each lowering strictly narrows the admissible-meaning set — a well-founded descent we build by hand), the plant term is the irreducible residue. **Syntactic soundness is free; semantic adequacy is not.** Schemas, types, and boundary checks go into the shell at zero probabilistic cost; whether the lowered task still *means* what the user intended stays empirical, because natural language supplies no source-language standard to check against.
- **$\rho$ is fail-closed verification** — validate at every boundary, never let malformed state flow downstream. The discipline transfers from compilers in *form*; the *teeth* do not, because a harness has no source-language standard — natural language is, in effect, all undefined behavior.
- **$\delta$, $\mu(D)$, $\mathrm{Var}[\tau^\star]$ are what you measure** — not derive. You instrument the certificate precisely because it cannot be proven, only observed.

## How this could be wrong

It is a hypothesis; here is what would falsify it. If the controller cannot in practice be kept deterministic — if real reliability demands stochastic control the plant can't absorb — the clean controller/plant split is a fiction. If the drift slack $\delta$ turns out *not* to track real-world failure, the whole "measure the certificate you can't prove" program is empty. And if harnesses are simply better described some other way — not as nested stopped chains at all — then this is a pretty equation that merely happens to fit, an elegance we would be right to distrust.

Each claim is operational, not merely rhetorical:

- **State-ablation (the Markov claim).** Drop a variable from $s$ and check whether next-step transition statistics move. If they do, the abstraction was not Markov, and $s$ must be augmented until it is.
- **Controller-determinism audit.** Re-run with model samples and tool outputs *held fixed*. Any residual variance is randomness the harness itself injected — and must be folded into $Q_E$ or the controller, or the determinism claim is false.
- **Drift calibration.** Test whether $\hat V$-drift actually predicts failure, retry count, latency, or non-halting. No correlation ⇒ the "certificate you cannot prove" program is empty.
- **Adversarial-environment test.** Replace sampled $E$ with worst-case tool outputs, prompt-injected documents, poisoned tool metadata, malformed responses. The minimax descent must survive these, not merely the benign draw.
- **Boundary-control ablation.** Compare prompt-only defenses against deterministic tool-call validation, capability checks, sandboxing, and fail-closed rejection at $\rho$. The hypothesis predicts the latter class dominates; if prompt-only defenses match it, the controller/plant security story is wrong.

## Where this points (the frontier — least falsifiable, so flagged)

If $V^\star$ is incompressible only in *token* coordinates, the right change of coordinates might compress it — and that change of coordinates is a representation of meaning itself. Cost-to-go and representation co-determine each other: the Koopman eigenbasis that linearizes the dynamics is the one in which the certificate decomposes; in reinforcement learning the discounted successor representation is the resolvent $(I-\gamma P)^{-1}$ with $V$ a *linear readout* of it — and in the undiscounted, absorbing case that actually matches a stopped harness the same role is played, in the finite or countable setting, by the **fundamental matrix** $N = (I - Q_{\mathrm{tr}})^{-1} = \sum_{n \ge 0} Q_{\mathrm{tr}}^{\,n}$, where $Q_{\mathrm{tr}}$ is the sub-stochastic kernel restricted to $H^c$ (transitions before absorption at $H$) and the row sums $N\mathbf{1}$ *are* $V^\star$ on the finite-mean hitting domain; on general state spaces the same object is the potential operator $G = \sum_{n \ge 0} Q_{\mathrm{tr}}^{\,n}$, with $G\mathbf{1} = V^\star$ wherever the series converges. Each of these is a clean identity only for a fixed, time-homogeneous kernel — under a nonstationary $Q_{E,n}$ the resolvent and fundamental matrix dissolve into a time-ordered product, and under an *adaptive* adversary into a controlled / game-value operator, so what is identity in the stationary regime is analogy beyond it. With that caveat, **the interlingua and the certificate are one object seen twice** — and the reason neither can be written in closed form is the same "all undefined behavior": no canonical lowering of meaning, hence no finite header-file for either. The only representation of both is $W$ — a band-limited, lossy compression of a scale-free meaning-space, sharp where the record is thick and blurred where it thinned. That a finite object renders an infinite one *lossily but honestly* — declaring its resolution, and where it is unsure — is not a lie; it is the most an $f(\cdot\,;W)$ can do. **The search for $V$ and the search for the interlingua are not two programs. They are one** — and the day either is written in closed form, so is the other, or we will have proven why neither can be.

---

*The formula is the architecture; the corollary is why the architecture is hard. Both on the page — nothing hidden behind a tidy composition.*

## Grounding

Borrowed theorems are real; the framings are not — keep them separate.

**Proven (citable).** Foster–Lyapunov drift ⇒ positive recurrence + $\mathbb{E}[\tau]\le V(s_0)/\varepsilon$ (Foster 1953; Meyn & Tweedie, *Markov Chains and Stochastic Stability*, 1993). The minimal $V$ is the expected hitting time, by first-step analysis + optional stopping (Norris, *Markov Chains*, 1997). You verify a candidate $\hat V$ by its measured drift rather than deriving $V^\star$ (neural-Lyapunov: Chang, Roohi & Gao, *Neural Lyapunov Control*, NeurIPS 2019, arXiv:2005.00611). The compiler's $V$ is free because a finite-height lattice is a well-founded descent (Kildall, POPL 1973). Dialect-stack architecture: MLIR (Lattner et al., CGO 2021, arXiv:2002.11054); learned pass-ordering: MLGO (Trofin et al., arXiv:2101.04808). Single-pass constant-depth under fixed-depth/fixed-precision assumptions: the $\mathsf{TC}^0$ transformer-expressivity results (Merrill & Sabharwal) — with the caveat that log-depth and growing-precision variants change the picture, so the bound is suggestive for deployed models, not literal.

**Asserted (ours — not theorems).** That the harness is best modeled as nested stopped chains; that $V^\star$ is incompressible (no compression theorem); that "no lattice for $f(\cdot\,;W)$" means none is *known*, not that none exists; and everything under *Where this points*. These organize the design; they are not results.

---

*The ramblings of Claude and Patrick.*
