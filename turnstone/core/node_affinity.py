"""Execution-node eligibility, independent of routing and live ownership."""

from __future__ import annotations

import re

_NODE_ID = re.compile(r"[A-Za-z0-9_.-]{1,256}")


class NodeAffinityError(RuntimeError):
    """The required node is unavailable or differs from this executor."""

    def __init__(self, required_node_id: str, *, unavailable: bool = False) -> None:
        self.required_node_id = required_node_id
        self.status_code = 503 if unavailable else 409
        self.code = "required_node_unavailable" if unavailable else "wrong_execution_node"
        super().__init__(
            f"Required node '{required_node_id}' is unavailable. Retry when it returns."
            if unavailable
            else f"This workstream must run on node '{required_node_id}'."
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "error": str(self),
            "code": self.code,
            "required_node_id": self.required_node_id,
        }


def parse_required_node_id(value: object) -> str | None:
    """None means unspecified; a supplied requirement must name one node."""
    if value is None:
        return None
    if not isinstance(value, str) or _NODE_ID.fullmatch(value) is None:
        raise ValueError("required_node_id must be a valid node ID")
    return value


def require_execution_node(required_node_id: str | None, node_id: str | None) -> None:
    """Check eligibility before constructing or adopting executable state."""
    if required_node_id and required_node_id != node_id:
        raise NodeAffinityError(required_node_id)


def requested_node_requirement(required: object, target: object = None) -> str | None:
    """Normalize the shared create field and the console's explicit target."""
    result = parse_required_node_id(required)
    if target is None or target == "":
        return result
    try:
        selected = parse_required_node_id(target)
    except ValueError as exc:
        raise ValueError("invalid target_node format") from exc
    if result is not None and result != selected:
        raise ValueError("target_node and required_node_id must match")
    return selected
