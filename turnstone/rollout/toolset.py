"""Which tools a rollout offers, and why the others are left out.

A rollout runs the model's tools inside one container, and the container must
be the only place the model's actions take effect.  The tools that satisfy
that are the ones whose definition declares ``environment`` (every effect goes
through the session's execution environment):
:data:`~turnstone.core.tools.ENVIRONMENT_TOOL_NAMES`.

A rollout also offers ``task_agent``.  A sub-agent has no reach of its own: it
is offered the environment tools only, acts through the same environment and
cannot spawn another.  For the training sample it is a tool like any other.

Every other tool an interactive session offers acts somewhere else.
:data:`HOST_SIDE_TOOLS` gives the reason for each; a test keeps the sets
together equal to the interactive tool set, so a new tool has to be placed in
one of them before it can reach a rollout.

A caller may still offer a host-side tool on purpose
(``RolloutConfig.tool_names``); it then runs outside the container, with
whatever reach the reason below describes.

MCP tools are not in either set: they are not built-in tools, and a rollout
session is built without an MCP client, so none exists in it and no configured
server can be reached.  A rollout that brings its own servers would build a
client from those servers only and hand it to the session with
``mcp_tools=True`` (see ``HeadlessSession``); the tool list is still read once
and fixed for the run.  The harness does not start such servers yet.
"""

from __future__ import annotations

from turnstone.core.tools import ENVIRONMENT_TOOL_NAMES

# The default tool set of a rollout: the tools that act inside the sandbox,
# and the sub-agent tool.  A sub-agent is offered the first group only and acts
# through the same environment, so it adds no reach; its conversation stays out
# of the training sample (see ``episode``).
ROLLOUT_TOOL_NAMES: frozenset[str] = ENVIRONMENT_TOOL_NAMES | {"task_agent"}

HOST_SIDE_TOOLS: dict[str, str] = {
    "web_fetch": (
        "requests leave from the machine the session runs on, not from the container, and it "
        "makes a model call of its own that the token record would not hold"
    ),
    "web_search": "queries leave from the machine the session runs on",
    "memory": "writes the durable memory store, which outlives the run and other runs read",
    "recall": "reads the durable memory store and conversation history outside the run",
    "skills": "reads and writes the skill store; its load action changes the system prompt",
    "notify": "sends messages to people",
    "watch": "schedules commands on the watch runner of the machine the session runs on",
    "open_preview": "reads a file or a page into the operator's preview pane",
    "read_resource": "reads resources of MCP servers; a headless session leaves it out",
    "use_prompt": "fetches prompts of MCP servers; a headless session leaves it out",
    "tool_search": "changes the offered tool list, which a rollout keeps fixed",
}
