# What is a harness?

*A hypothesis — not a theorem. The honest answer is a claim about **shape**: an object you can write down that says what a harness is and, just as precisely, the one guarantee it can never carry.*

Most descriptions of an agent framework are a feature list. This is an attempt at a definition.

---

## The claim

A harness is a **stopped, deterministically-controlled Markov chain on task-state, closed around a stopped autoregressive chain on context-space that factors through learned residual-stream geometry** — a deterministic controller in closed loop with a stochastic learned plant.

$$\mathcal{H}:\quad s_{n+1} \sim T(s_n)\ \ \text{for } n < \tau^\star, \qquad T = \rho \circ (M_W \circ \pi,\, E)$$

$$M_W(c) = \mathrm{Law}(c_\tau), \qquad c_{t+1} \sim K_W(c_t, \cdot), \qquad K_W(c,\, c\!\cdot\! v) = (U \circ \Phi_W \circ \mathrm{Emb})(c)[v]$$

Two stopped chains, nested: **deterministic control over stochastic dynamics over learned geometry.** Only the inner chain is geometric. Both are fixpoint searches. Neither converges because you asked it to.

## Reading it

| Symbol | Is |
|---|---|
| $\mathcal{H}$ | the harness — the whole controlled system, *not* the model |
| $s$ | task-state: IR / dialect stack, tool results, plan, counters — strictly richer than context |
| $\pi : \mathcal{S} \to \mathcal{C}$ | **lowering** — prompt construction, dialect lowering, effective-program selection |
| $M_W$ | the **inner solver** — the autoregressive chain run to a stopping time; $\Phi_W$ is the residual-stream ("manifold") core |
| $E,\ \rho$ | **environment** (tool effects) and the **fail-closed verify-and-fold-back** |
| $\tau^\star$ | the outer **halting time** — the loop is a fixpoint search, not a single pass |

The structural fact that earns the word *controller*: $\pi$, $\rho$, the halt test, and the readout are **deterministic**, so $\mathcal{H}$ injects no randomness of its own. Every coin in the system is inherited from $M_W$ and the world $E$.

## Why this shape

$$f(x) \;\longrightarrow\; x = f(x;\,W) \;\longrightarrow\; f(x)$$

Classical software, inverted into latent geometry, then re-wrapped in classical software. The harness **re-imposes the determinism the model dissolved**: $\pi, \rho, \tau^\star$ are ordinary designed code — a controller — whose primitive operand happens to be a stochastic oracle. That closure is why a compiler is the right mental model (staged deterministic software ports cleanly) and exactly why the analogy breaks (a compiler's primitive operation was never a coin). **The harness is the half you can reason about classically, sitting on top of the half you cannot.**

## The limit, stated honestly

$\mathcal{H}$ carries **no Foster–Lyapunov descent function by construction.** Almost-sure halting with bounded expected runtime would need a $V \ge 0$ with

$$\mathbb{E}[\,V(s_{n+1}) \mid s_n\,] \le V(s_n) - \varepsilon \quad\text{off the halt set.}$$

A C compiler gets its $V$ for free: a finite-height lattice *is* a well-founded descent, so termination holds by structure. Here the minimal such $V$ is **forced** — it is the expected halting time itself,

$$V^\star(s) = \mathbb{E}[\,\tau^\star \mid s_0 = s\,].$$

The subtlety that matters: $V^\star$ is not *absent*. It exists, and is finite wherever the loop is positive-recurrent to the halt set. The problem is that $V^\star$ is a functional of all of $W$ and the environment and **does not compress below model scale**. The compiler's certificate is structurally trivial; ours is as hard as the dynamics. This is the exact, quantitative form of *you can borrow how LLVM is built — not why it is correct.*

So you never compute $V^\star$. You pick a candidate $\hat V$ and **measure its drift slack**

$$\delta = \sup_{s \notin H}\Big(\mathbb{E}[\,\hat V(s_{n+1}) \mid s_n\,] - \hat V(s_n) + \varepsilon\Big).$$

If $\delta \le 0$, optional stopping hands you a real, conservative certificate, $\mathbb{E}[\tau^\star] \le \hat V(s_0)/\varepsilon$. If $\delta > 0$, you get a measurable non-halting radius that grows with $\delta$. **$\delta$ is the number on the dashboard** — the evaluable surrogate for a guarantee the geometry will never give you. Its floors are the size of the divergent set $\mu(D)$ and the hitting-time variance $\mathrm{Var}[\tau^\star]$ — both properties of the trained weights, knowable only a posteriori.

> For an agent *meant* to run forever — a coordinator, a daemon — halting is the wrong target, and $V^\star = \infty$ is the spec, not a pathology. The same drift theory then certifies **recurrence to a ready-state** instead of absorption to a halt-set. The object changes; the missing certificate does not.

And the consolation rests on an assumption the world violates. The whole drift apparatus — $V^\star$, the hitting-time bound, the slack $\delta$ — assumes a **time-homogeneous kernel**: the same state transitions the same way every time. But the environment $E$ is *part of* $T$, and the world is not stationary — worse, it can be **adversarial**, an attacker choosing what a tool returns so as to maximize your non-halting. The drift condition then stops being a fixpoint question and becomes a **minimax** one,

$$\sup_{e\,\in\,E_{\text{adm}}}\ \mathbb{E}[\,V(s_{n+1}) \mid s_n,\, e\,] \le V(s_n) - \varepsilon,$$

a descent that must hold even when the environment picks the worst admissible step. A $V$ that certifies halting against a benign world is defeated by an adversarial one, and the measured $\delta$ bounds only the $E$ you *sampled*, never the $E$ an attacker will choose. **This is the formal home of prompt injection** — not "the model did something bad," but the environment optimized to break your descent. It is also what fail-closed verification ($\rho$) is *for*: the disturbance-rejection margin that caps how far an adversarial world can move the drift. In this language, security is robustness of the certificate.

## Where it cashes out

This is not ornament; the decomposition is load-bearing in the design.

- **$\pi$ is a progressively-lowered dialect stack** — raw input → intent → plan → tool-call → the neutral wire IR — each level a deterministic pass with its own verifier. The drift splits by coordinate, $r = r_{\text{shell}} + r_{\text{plant}}$: the shell term is an *exact, designed* descent (each lowering strictly narrows the admissible-meaning set — a well-founded descent we build by hand), the plant term is the irreducible residue. **Soundness is free; speculation must be measured.**
- **$\rho$ is fail-closed verification** — validate at every boundary, never let malformed state flow downstream. The discipline transfers from compilers in *form*; the *teeth* do not, because a harness has no source-language standard — natural language is, in effect, all undefined behavior.
- **$\delta$, $\mu(D)$, $\mathrm{Var}[\tau^\star]$ are what you measure** — not derive. You instrument the certificate precisely because it cannot be proven, only observed.

## How this could be wrong

It is a hypothesis; here is what would falsify it. If the controller cannot in practice be kept deterministic — if real reliability demands stochastic control the plant can't absorb — the clean controller/plant split is a fiction. If the drift slack $\delta$ turns out *not* to track real-world failure, the whole "measure the certificate you can't prove" program is empty. And if harnesses are simply better described some other way — not as nested stopped chains at all — then this is a pretty equation that merely happens to fit, an elegance we would be right to distrust.

---

*The formula is the architecture; the corollary is why the architecture is hard. Both on the page — nothing hidden behind a tidy composition.*
