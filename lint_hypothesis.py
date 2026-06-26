#!/usr/bin/env python3
"""
Consistency linter for HYPOTHESIS.md.
Deterministic checks — no model, no confabulation:
  A. delimiter / emphasis balance
  B. residue regexes (things prior rounds fixed must not reappear)
  C. single-capital-letter collision scan (one letter, two meanings)
  D. definition check for the symbols recent rounds introduced
  E. γ/ρ role-usage scan (gate=authorize/reject-proposal ; ρ=verify/fold-back/response)
  F. display-only symbols (used in $$…$$ but nowhere in prose)
  G. orphan / redundant-declaration scan (symbol used once; or two declaration sites)

Path resolves to HYPOTHESIS.md beside this script, or argv[1] if given.
Known benign flags: E flags the γ,ρ symbol-table row; G2 flags τ_H (it legitimately
owns both a stopping-time/filtration statement and its = inf{…} formula).
"""

import os
import re
import sys

PATH = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "HYPOTHESIS.md")
)
with open(PATH, encoding="utf-8") as _f:
    T = _f.read()
LINES = T.splitlines()


def lineno(idx):  # char index -> 1-based line
    return T.count("\n", 0, idx) + 1


def ctx(idx, w=55):
    a = max(0, idx - w)
    b = min(len(T), idx + w)
    return T[a:b].replace("\n", " ")


# math spans (so we can scan symbols in math only)
math_spans = []
for m in re.finditer(r"\$\$.*?\$\$", T, flags=re.S):
    math_spans.append((m.start(), m.end()))
for m in re.finditer(r"(?<!\$)\$(?!\$).*?(?<!\$)\$(?!\$)", T, flags=re.S):
    math_spans.append((m.start(), m.end()))


def in_math(idx):
    return any(a <= idx < b for a, b in math_spans)


print("=" * 70)
print("A.  BALANCE")
print("=" * 70)
nomath = re.sub(r"\$[^$]*\$", "", T)
display = T.count("$$")
inline = len(re.findall(r"(?<!\$)\$(?!\$)", T))
print(f"  display $$ : {display}  even={display % 2 == 0}")
print(f"  inline  $  : {inline}  even={inline % 2 == 0}")
print(f"  braces {{ }} : net {T.count('{') - T.count('}')}")
print(f"  bold  **   : {nomath.count('**')}  even={nomath.count('**') % 2 == 0}")
print(
    f"  italic *   : {nomath.replace('**', '').count('*')}  even={nomath.replace('**', '').count('*') % 2 == 0}"
)

print("\n" + "=" * 70)
print("B.  RESIDUE REGEXES (expect 0 each)")
print("=" * 70)
residue = {
    "stray p_{ok}": r"p_\{\\mathrm\{ok\}\}",
    "halt/ready leftover": r"halt/ready",
    "(I-γP) discount collision": r"\(I-\\gamma P\)",
    "B as pushforward dummy": r"M_W\(c, B\)",
    "old c_τ-as-output law": r"M_W\(c\) = \\mathrm\{Law\}\(c_\\tau\)",
    "R=id ill-typed": r"R=\\mathrm\{id\}",
    "Y_⊥ after ⊥∈Y decision": r"\\mathcal\{Y\}_\\bot",
    "'terminal sets are'": r"The terminal sets are",
    "ρ rejects ⊥ branch": r"what \$\\rho\$ rejects",
    "rejection at ρ": r"fail-closed rejection at \$\\rho\$",
    "orphan τ^star (unify→τ_H)": r"\\tau\^\\star",
    "unbraced _\\cmd subscript (GitHub emphasis hazard)": r"_\\",
    "\\# in math (GitHub unescapes → raw #)": r"\\#",
}
for lbl, rx in residue.items():
    hits = [lineno(m.start()) for m in re.finditer(rx, T)]
    flag = "OK  " if not hits else "HIT "
    print(f"  {flag}{lbl:32} lines={hits}")

print("\n" + "=" * 70)
print("C.  SINGLE-CAPITAL COLLISION SCAN (eyeball for two meanings)")
print("=" * 70)
for L in ["G", "U", "P", "N", "R", "V", "F", "D", "K", "T"]:
    occ = []
    for m in re.finditer(r"(?<![A-Za-z\\_])" + L + r"(?![A-Za-z_])", T):
        if in_math(m.start()):
            occ.append(m.start())
    if occ:
        print(f"  [{L}]  {len(occ)} math occ:")
        for i in occ:
            print(f"        L{lineno(i):>3}: …{ctx(i, 38)}…")

print("\n" + "=" * 70)
print("D.  DEFINITION CHECK (symbols recent rounds introduced)")
print("=" * 70)
defs = {
    "Π (adversary class)": r"the class \$\\Pi\$ of policies",
    "D (divergent set)": r"D=\\\{s:\\mathbb\{E\}_s\[\\tau_H\]=\\infty\\\}",
    "μ (measure)": r"reference/sampling measure \$\\mu\$",
    "e_0 (no-op response)": r"no-op response \$e_0\\in\\mathcal\{E\}\$",
    "Stop (stop set)": r"stop set \$\\mathrm\{Stop\}\$",
    "p_succ": r"p_\{\\mathrm\{succ\}\}\(s\)",
    "p_safe": r"p_\{\\mathrm\{safe\}\}\(s\)",
    "β (RL discount)": r"discount \$\\beta\$",
    "A_Y (pushforward set)": r"measurable \$A_Y",
    "r (per-step drift)": r"per-step drift \$r\(s\)=",
    "z_t triple": r"z_t = \(c_t, b_t, m_t\)",
    "μ_0 (initial dist)": r"initial \$s_0 \\sim \\mu_0\$",
    "certificate (2-sense)": r"A \*\*certificate\*\* is a \*witness\*",
    "controller/plant/shell": r"\*shell : plant :: the part you write",
    "r_env (3-way drift)": r"r_\{\\text\{env\}\}",
}
for lbl, rx in defs.items():
    found = bool(re.search(rx, T))
    print(f"  {'OK  ' if found else 'MISS'}{lbl}")

print("\n" + "=" * 70)
print("E.  γ / ρ ROLE SCAN")
print("=" * 70)
# γ should sit near authorize/gate/reject-proposal/capability/irreversible/before
# ρ should sit near verify/validate-response/fold-back/after
g_bad = re.compile(r"fold[- ]back|folds back", re.I)  # γ doing ρ's job
r_bad = re.compile(r"rejects the proposal|authoriz|is the gate|gates ", re.I)  # ρ doing γ's job


def scan(sym_rx, label, bad_rx):
    flagged = 0
    for m in re.finditer(sym_rx, T):
        if not in_math(m.start()):
            continue
        window = T[max(0, m.start() - 15) : m.start() + 70].replace("\n", " ")
        if bad_rx.search(window):
            flagged += 1
            print(f"  FLAG {label}  L{lineno(m.start())}: …{window}…")
    if not flagged:
        print(f"  OK   no {label} usages land in the wrong role-neighborhood")


scan(r"\\gamma", "γ", g_bad)
scan(r"\\rho", "ρ", r_bad)

print("\n" + "=" * 70)
print("F.  DISPLAY-ONLY SYMBOLS (in $$…$$, absent from prose)")
print("=" * 70)
disp = " ".join(T[a:b] for a, b in math_spans if T[a : a + 2] == "$$")
prose = re.sub(r"\$\$.*?\$\$", "", T, flags=re.S)
toks = set(re.findall(r"\\[A-Za-z]+(?:_\{[A-Za-z]+\})?|[A-Z]_[A-Za-z]|[A-Za-z]_\\[a-z]+", disp))
suspicious = []
for tk in sorted(toks):
    base = tk.split("_")[0]
    if base and base not in prose and tk not in prose and len(base) > 1:
        suspicious.append(tk)
print("  (heuristic; review only) ", suspicious if suspicious else "none flagged")

print("\n" + "=" * 70)
print("G.  ORPHAN / REDUNDANT-DECLARATION SCAN (review only)")
print("=" * 70)
# G1 — a math symbol occurring exactly once is usually a rename residue or a typo
#      (a unification can strip a symbol of all but one use). LaTeX operators and
#      formatting commands are not symbols, so filter them out. Review, do not trust.
OPS = {
    r"\Pr",
    r"\sum",
    r"\int",
    r"\sup",
    r"\inf",
    r"\infty",
    r"\in",
    r"\notin",
    r"\cap",
    r"\cup",
    r"\setminus",
    r"\subseteq",
    r"\subset",
    r"\mid",
    r"\ge",
    r"\le",
    r"\sim",
    r"\circ",
    r"\cdot",
    r"\star",
    r"\hat",
    r"\bar",
    r"\to",
    r"\Rightarrow",
    r"\rightsquigarrow",
    r"\longrightarrow",
    r"\quad",
    r"\qquad",
    r"\Big",
    r"\big",
    r"\mathbb",
    r"\mathcal",
    r"\mathrm",
    r"\mathbf",
    r"\text",
}
sym_rx = re.compile(r"\\[A-Za-z]+(?:_\{[^{}]*\}|_[A-Za-z0-9])?")
counts = {}
for a, b in math_spans:
    for m in sym_rx.finditer(T[a:b]):
        counts[m.group()] = counts.get(m.group(), 0) + 1
singletons = sorted(s for s, c in counts.items() if c == 1 and s.split("_")[0] not in OPS)
print("  G1 singletons (occur once in math, operators filtered — orphan/typo candidates):")
print("      " + (", ".join(singletons) if singletons else "none"))

# G2 — the bare-τ failure mode the τ-unification introduced: a stopping/hitting-time
#      symbol carrying BOTH an enumeration declaration (a "…stopping/hitting time…"
#      sentence) AND a separate "= \inf\{…}" formula on a *different* line — one of the
#      two sites is usually redundant. A formula restated in adjacent prose is benign
#      (same kind of site), and so is τ_H, which legitimately owns a filtration statement
#      plus its formula. A *newly* enum+formula-split symbol is the smell.
decl_rx = re.compile(
    r"hitting times? are|are stopping times|is a stopping time|stopping times? for the"
)
formula_tail = r"\s*=\s*\\inf\\\{"  # "= \inf\{" — the hitting/stop-time def, not \infty
tau_syms = [r"\tau", r"\tau_A", r"\tau_H", r"\tau_B", r"\tau_F", r"\tau_{H_{\mathrm{ok}}}"]
print("  G2 stopping/hitting-time family (count | enum-decl lines | formula lines):")
for s in tau_syms:
    pat = re.escape(s) + (r"(?![A-Za-z_^{])" if s == r"\tau" else r"(?![A-Za-z0-9])")
    occ = list(re.finditer(pat, T))
    enum_lines, formula_lines = set(), set()
    for m in occ:
        ln = lineno(m.start())
        line = LINES[ln - 1]
        if re.search(pat + formula_tail, line):
            formula_lines.add(ln)
        if decl_rx.search(line):
            enum_lines.add(ln)
    split = any(e != f for e in enum_lines for f in formula_lines)
    note = "   <-- enum + separate formula; eyeball (benign: τ_H)" if split else ""
    print(
        f"      {s:24} count={len(occ):>2}  enum={sorted(enum_lines)}  formula={sorted(formula_lines)}{note}"
    )
print("\nDONE.")
