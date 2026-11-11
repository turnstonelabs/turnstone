"""turnstone.rollout: one headless run recorded for training.

A rollout runs a task through the headless session loop with the model's tools
acting inside a per-run container, and records every model call's prompt token
IDs, generated token IDs and log-probabilities.  See ``docs/rollout.md``.

- :mod:`turnstone.rollout.episode`: ``Task``, ``RolloutConfig``, ``run_episode``.
- :mod:`turnstone.rollout.recording`: the recording client.
- :mod:`turnstone.rollout.tokens`: the token record and the prefix check.
- :mod:`turnstone.rollout.container`: the container and its execution environment.
- :mod:`turnstone.rollout.toolset`: which tools a rollout offers and why.
- :mod:`turnstone.rollout.probe`: which log-probabilities a server reports.
"""

from turnstone.rollout.episode import RolloutConfig, Task, run_episode

__all__ = ["RolloutConfig", "Task", "run_episode"]
