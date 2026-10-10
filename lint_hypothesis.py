#!/usr/bin/env python3
"""
Consistency linter for HYPOTHESIS.md.
Deterministic checks — no model, no confabulation:
  A. delimiter / emphasis balance
  B. residue regexes (things prior rounds fixed must not reappear)
  C. single-capital-letter collision scan (one letter, two meanings)
  D. definition check for the symbols recent rounds introduced
  E. γ/ρ role-usage scan (gate=authorize/reject-proposal ; ρ=verify/fold-back/response)
  F. display-only symbols (used in ```math fences but nowhere in prose)
  G. orphan / redundant-declaration scan (symbol used once; or two declaration sites)

Math delimiters: inline math is GitHub's $`…`$ form and display math is a ```math fence at
column 0. Inside bare $…$ / $$…$$ GitHub still runs Markdown — `_` pairs into emphasis
across spans and backslash escapes eat \\{ \\! \\, \\; — and an indented ```math fence (in a
list item) renders as a plain code block, so both are residues (#1341). A literal dollar
inside protected math is written \\$, as GitHub documents.

Usage: lint_hypothesis.py [--check] [PATH]. PATH defaults to HYPOTHESIS.md beside this
script. --check stops after sections A and B and exits non-zero if a delimiter, fence,
span-body or residue check failed (the pre-commit hook); the emphasis counts are
informational. Without it every section prints, for review. The document has no code
spans and no fenced blocks other than math; a dollar inside one would read as a delimiter.
Known benign flags: E flags the γ,ρ symbol-table row; G2 flags τ_H (it legitimately
owns both a stopping-time/filtration statement and its = inf{…} formula).
"""

import os
import re
import sys

CHECK = "--check" in sys.argv
_args = [a for a in sys.argv[1:] if a != "--check"]
PATH = (
    _args[0] if _args else os.path.join(os.path.dirname(os.path.abspath(__file__)), "HYPOTHESIS.md")
)
with open(PATH, encoding="utf-8") as _f:
    T = _f.read()
LINES = T.splitlines()
errors = []  # deterministic failures from sections A and B; --check exits non-zero on any


def lineno(idx):  # char index -> 1-based line
    return T.count("\n", 0, idx) + 1


def ctx(idx, w=55):
    a = max(0, idx - w)
    b = min(len(T), idx + w)
    return T[a:b].replace("\n", " ")


# math spans (so we can scan symbols in math only); delimiters per the docstring. Inline
# spans are paired from the $` / `$ token stream, whose alternation section A checks, so a
# reversed or unclosed span cannot silently pair with its neighbour. An opener preceded by a
# backslash is a literal \$ at the end of a span, not a delimiter.
TOKEN_RX = re.compile(r"(?<!\\)\$`|`\$")
DISPLAY_RX = re.compile(r"^```math[ \t]*\n.*?\n```[ \t]*$", flags=re.S | re.M)
BARE_RX = re.compile(r"(?<!`)\$(?!`)")
tokens = list(TOKEN_RX.finditer(T))
math_spans = []
for m in DISPLAY_RX.finditer(T):
    math_spans.append((m.start(), m.end()))
    if any(line.lstrip().startswith("```") for line in m.group().split("\n")[1:-1]):
        errors.append(
            f"display math at line {lineno(m.start())} runs on past a malformed closing fence"
        )
for opener, closer in zip(tokens[0::2], tokens[1::2], strict=False):
    if opener.group() == "$`" and closer.group() == "`$":
        math_spans.append((opener.start(), closer.end()))
        body = T[opener.end() : closer.start()]
        if "`" in body or re.search(r"\n[ \t]*\n", body):
            errors.append(
                f"inline math at line {lineno(opener.start())} holds a backtick or blank line,"
                " which ends the span early"
            )
math_spans.sort()
for a, b in math_spans:
    body = T[a:b].replace("\\\\", "")
    opens, closes = len(re.findall(r"(?<!\\)\{", body)), len(re.findall(r"(?<!\\)\}", body))
    if opens != closes:
        errors.append(
            f"math at line {lineno(a)} has unbalanced braces ({opens} open, {closes} close)"
        )


def in_math(idx):
    return any(a <= idx < b for a, b in math_spans)


def strip_math(text):
    out, last = [], 0
    for a, b in math_spans:
        out.append(text[last:a])
        last = b
    out.append(text[last:])
    return "".join(out)


print("=" * 70)
print("A.  BALANCE")
print("=" * 70)
nomath = strip_math(T)
misplaced = next((t for i, t in enumerate(tokens) if t.group() != ("$`", "`$")[i % 2]), None)
if misplaced is None and len(tokens) % 2:
    misplaced = tokens[-1]
if misplaced is None:
    print(f"  inline  $`…`$   : {len(tokens) // 2} spans, delimiters alternate")
else:
    print(f"  inline  $`…`$   : delimiter out of order at line {lineno(misplaced.start())}")
    errors.append(f"inline math delimiter out of order at line {lineno(misplaced.start())}")
fences = sum(1 for line in LINES if line.lstrip().startswith("```"))
display = sum(1 for line in LINES if line.rstrip() == "```math")
parsed = len(DISPLAY_RX.findall(T))
print(f"  display ```math : {display} openers, {parsed} parsed  fences even={fences % 2 == 0}")
if fences % 2 or display != parsed:
    errors.append(f"display math fences: {display} openers, {parsed} parsed, {fences} fence lines")
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
    "ρ rejects ⊥ branch": r"what \$`\\rho`\$ rejects",
    "rejection at ρ": r"fail-closed rejection at \$`\\rho`\$",
    "orphan τ^star (unify→τ_H)": r"\\tau\^\\star",
    "unbraced _\\cmd subscript (style: brace it)": r"_\\",
    "\\# in math (style: the pushforward is R_{\\sharp})": r"\\#",
}
hits_by_label = {
    lbl: [lineno(m.start()) for m in re.finditer(rx, T)] for lbl, rx in residue.items()
}
hits_by_label["bare $…$ / $$ math (GitHub runs Markdown inside; use $`…`$ / ```math)"] = [
    lineno(m.start())
    for m in BARE_RX.finditer(T)
    if not (in_math(m.start()) and T[m.start() - 1 : m.start()] == "\\")
]
hits_by_label["indented ```math fence (renders as a code block; keep it at column 0)"] = [
    i
    for i, line in enumerate(LINES, 1)
    if line != line.lstrip() and line.lstrip().startswith("```math")
]
for lbl, hits in hits_by_label.items():
    flag = "OK  " if not hits else "HIT "
    print(f"  {flag}{lbl:32} lines={hits}")
    if hits:
        errors.append(f"residue: {lbl} at lines {hits}")
for e in errors:
    print(f"  ERROR: {e}")
if CHECK:
    print(f"\nCHECK: {'FAIL' if errors else 'OK'}")
    sys.exit(1 if errors else 0)

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
    "Π (adversary class)": r"the class \$`\\Pi`\$ of policies",
    "D (divergent set)": r"D=\\\{s:\\mathbb\{E\}_s\[\\tau_H\]=\\infty\\\}",
    "μ (measure)": r"reference/sampling measure \$`\\mu`\$",
    "e_0 (no-op response)": r"no-op response \$`e_0\\in\\mathcal\{E\}`\$",
    "Stop (stop set)": r"stop set \$`\\mathrm\{Stop\}`\$",
    "p_succ": r"p_\{\\mathrm\{succ\}\}\(s\)",
    "p_safe": r"p_\{\\mathrm\{safe\}\}\(s\)",
    "β (RL discount)": r"discount \$`\\beta`\$",
    "A_Y (pushforward set)": r"measurable \$`A_Y",
    "r (per-step drift)": r"per-step drift \$`r\(s\)=",
    "z_t triple": r"z_t = \(c_t, b_t, m_t\)",
    "μ_0 (initial dist)": r"initial \$`s_0 \\sim \\mu_0`\$",
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
print("F.  DISPLAY-ONLY SYMBOLS (in ```math fences, absent from prose)")
print("=" * 70)
disp = " ".join(DISPLAY_RX.findall(T))
prose = DISPLAY_RX.sub("", T)
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
