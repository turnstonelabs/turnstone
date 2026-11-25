"""Pattern rules a rollout enforces on the model's tool calls.

A rollout has no person to ask and no reason to protect a throwaway container
the way an operator's machine is protected.  It still should not reinforce a
handful of habits that are destructive anywhere: deleting the root of a file
system, writing to block devices, starting processes without end, signalling
every process, turning the machine off.

So a rollout has a rule set of its own.  It is data (``rules.json`` beside this
module), in the framework's heuristic rule format and matched by the
framework's matcher (:func:`turnstone.core.judge.evaluate_heuristic`), with
one difference, for a file tool's call (see :meth:`RuleSet.match`):

- a rule that says ``deny`` refuses the call before anything runs.  The model
  gets an ordinary tool result with a fixed text naming the rule, the same text
  for the same call every time;
- a rule that says ``review`` lets the call run and is noted in the record:
  there is nobody to review it;
- the first rule that matches decides; a call no rule matches runs.

A refused call is the model's doing: the rollout stays trainable.

The rules are patterns, and a pattern can be written around.  Nothing rests on
them: not the container's isolation and not the container's main process,
which are kept by other means whether a rule fires or not.  They are not what
keeps a model's commands off the machine a rollout runs on either: a rollout
refuses an execution environment that acts there.

A match looks at the call's text and at nothing else.  A path the model wrote
means something in the rollout's sandbox only, so it is never expanded,
resolved or looked up on the machine the rollout runs on: that machine's files
must not decide what a rule says about a call, and the model must not have the
session look at a path of its choosing there.

A match runs in the session's own process, on a command the model wrote, and
holds the interpreter while it runs.  A pattern whose every repeat has a limit
(``{0,8}``, never ``*`` or ``+``) does a bounded amount of work from each
position of the command, so matching takes time proportional to the command's
length; one unbounded repeat can make it quadratic or worse, and a model could
then stall the harness with one long command.  A rule set therefore refuses a
pattern with an unbounded repeat when it is built.  Limits that are nested
multiply, so they are kept small.

The fixed list of patterns that protects the machine a session runs on is not
asked inside a rollout's container (the container's execution environment
answers for itself): it refuses ordinary work there, such as any recursive
change of ownership.  The model-based judge does not run in a rollout.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

from turnstone.core.judge import evaluate_heuristic
from turnstone.core.rule_registry import HeuristicRuleDef

_SHIPPED = os.path.join(os.path.dirname(__file__), "rules.json")
# The tools whose call the framework's matcher reads by its path, which it
# expands and resolves on the machine it runs on.
_FILE_TOOLS = ("write_file", "edit_file")
_MATCHED = "Matched rule: "
# What is left of a pattern once escaped characters and character classes are
# taken out holds ``*``, ``+`` or ``{n,}`` only where something is repeated
# without a limit.
_ESCAPE_OR_CLASS = re.compile(r"\\.|\[[^\]]*\]")
_OPEN_REPEAT = re.compile(r"[*+]|[{][0-9]*,[}]")


def unbounded_repeat(pattern: str) -> bool:
    """Whether *pattern* repeats something without a limit."""
    return bool(_OPEN_REPEAT.search(_ESCAPE_OR_CLASS.sub("", pattern)))


@dataclass(frozen=True)
class RuleSet:
    """Named rules in the order they are tried."""

    name: str
    rules: tuple[HeuristicRuleDef, ...]

    def __post_init__(self) -> None:
        for rule in self.rules:
            for pattern in rule.arg_patterns:
                if unbounded_repeat(pattern):
                    raise ValueError(
                        f"rule {rule.name!r}: the pattern {pattern!r} repeats something "
                        "without a limit; give every repeat one ({0,8} for *, {1,8} for +), "
                        "so that a long command cannot stall the match"
                    )

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> RuleSet:
        return cls(str(data["name"]), tuple(HeuristicRuleDef(**rule) for rule in data["rules"]))

    @classmethod
    def from_file(cls, path: str) -> RuleSet:
        with open(path, encoding="utf-8") as handle:
            return cls.from_data(json.load(handle))

    @classmethod
    def shipped(cls) -> RuleSet:
        """The set a rollout uses unless it is given another."""
        return cls.from_file(_SHIPPED)

    def with_rules(self, extra: list[HeuristicRuleDef], *, name: str) -> RuleSet:
        """This set with *extra* tried first, under a name of its own."""
        return RuleSet(name, (*extra, *self.rules))

    def identity(self) -> dict[str, Any]:
        """What tells one rule set from another: its name and a hash of its rules."""
        content = json.dumps([asdict(rule) for rule in self.rules], sort_keys=True)
        return {
            "name": self.name,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "rules": len(self.rules),
        }

    def match(
        self, func_name: str, arguments: dict[str, Any], *, resolved_path: str | None = None
    ) -> HeuristicRuleDef | None:
        """The first rule that matches the call, ``None`` when none does.

        A rule matches when its tool pattern matches the tool's name and one of
        its patterns is found in the call's text: a shell call's command, a
        file tool's path, any other call's arguments as compact JSON.

        A file tool's path is the text the model wrote.  The framework's
        matcher would expand and resolve it on the machine it runs on; here it
        means something in the sandbox only, so that is not done.  A caller
        that has asked the sandbox what the path resolves to there passes it
        as *resolved_path*, and a rule is then matched against both, the path
        first.
        """
        if func_name in _FILE_TOOLS:
            text = str(arguments.get("path", ""))
            if resolved_path and resolved_path != text:
                text = f"{text} {resolved_path}"
            for rule in self.rules:
                if not fnmatch.fnmatch(func_name, rule.tool_pattern):
                    continue
                if not rule.arg_patterns or any(re.search(p, text) for p in rule.arg_patterns):
                    return rule
            return None
        verdict = evaluate_heuristic(func_name, arguments, func_name, rules=self.rules)
        matched = [e[len(_MATCHED) :] for e in verdict.evidence if e.startswith(_MATCHED)]
        if not matched:
            return None
        return next((rule for rule in self.rules if rule.name == matched[0]), None)


def refusal_text(rule: HeuristicRuleDef) -> str:
    """What the model is told when *rule* refuses a call.  Fixed for the rule."""
    return (
        f"Refused by the rule '{rule.name}': {rule.reasoning_template}. "
        "The call was not run. Do what the task needs another way."
    )
