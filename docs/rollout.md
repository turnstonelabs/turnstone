# Rollouts (turnstone-rollout)

A rollout is one headless run recorded for training: a task goes through the
headless session loop, the model's tools act inside a container made for that
run, and every model call's prompt token IDs, generated token IDs and
log-probabilities are kept. The record says of itself whether it can be trained
on.

Source: `turnstone/rollout/`. It builds on things documented elsewhere: the
per-run lifecycle with one attempt, the [stable prefix](eval.md#stable-prefix)
option of the headless session with
[arguments sent back as written](eval.md#arguments-as-the-model-wrote-them), a
[limited tool set](eval.md#tool-set-and-execution-environment), and the
[execution environment](tools.md#execution-environment) of the shell and file
tools.

It needs a chat-completions server that returns token IDs and log-probabilities
on request (`return_token_ids`, `logprobs`), and a local container runtime with
the task's image already present. The runtime has to let a helper container
join another container's process namespace and lower a process's out-of-memory
score there, which a rootless runtime cannot do; see
[the main process](#the-main-process).

With [prompts as token IDs](#prompts-as-token-ids) the requests go to the
server's tokenize and completions routes instead, and the server needs no
reader of reasoning or tool calls.

---

## Running one

```
turnstone-rollout --base-url http://127.0.0.1:8000/v1 --model NAME \
    --prompt "Create hello.txt containing hi, then list the directory." \
    --top-k 20 --top-p 0.95 --out episode.json
```

The task is given by flags (`--prompt`, `--key`, `--file` and the container
flags) or as a whole by a [task file](#a-task-file) (`--task`), never by both.
The server's API key, where it wants one, is read from the environment variable
that `--api-key-env` names. `--token-id-prompts` sends every prompt as token
IDs, which is what keeps a long rollout one token sequence, and what keeps a
model from ending a run as the machinery's failure by what it writes in one
argument. Rollouts that are trained on are run with it; see
[prompts as token IDs](#prompts-as-token-ids).

The flags that say what the task is:

| Flag | What it says |
|------|--------------|
| `--prompt TEXT` | What the user asks (`Task.prompt`) |
| `--key NAME` | The task's key in the record (`Task.key`); `cli` where it is left out |
| `--system-prompt TEXT` | The system prompt (`Task.system_prompt`); left out, a short one for an agent in a sandbox |
| `--file PATH=CONTENT` | A text file put into the container before the first turn (`Task.setup_files`); repeatable |
| `--image NAME`, `--workdir PATH` | The container's image and its working directory ([the container](#the-container) has the defaults) |
| `--user UID:GID` | The user the model's commands run as; `0:0` is the image's root |
| `--writable-root` | Makes the image's whole file system writable (`read_only_root` false). What then bounds what is written is the root size, which a task file can name and a flag cannot; see [what bounds writes](#what-bounds-writes) |
| `--cap NAME` | A capability the commands hold, of those the container allows; repeatable |
| `--env NAME=VALUE` | An environment variable in the container; repeatable |
| `--memory SIZE`, `--cpus NUMBER` | The container's memory limit and its share of processors |
| `--max-turns`, `--timeout`, `--max-generated-tokens`, `--max-command-seconds` | The task's turns, its time limit and the [budgets](#budgets-of-the-models-own) of the model's own |

Two more are the run's, not the task's, and stand beside a task file as well:
`--max-subagents` is the most sub-agents one rollout may start (16), and
`--keep-text` keeps each call's generated text in the record (`text`).

Exit status: 0 for a trainable rollout, however the model ended it; 1 when the
server, the harness or the runtime ended it, or the run did not start; 2 when it
ran to its own end but cannot be trained on; 3 when nothing was run. The table
under [started by another program](#started-by-another-program) says what a
caller can rely on.

A command line, a task file or a configuration that cannot work is refused
before anything runs, with a message on standard error and exit status 3: a
flag that is not known, a task file with a key that is not known, a rule file
that cannot be read, a configuration file that cannot be used (see
[limits come from the framework](#limits-come-from-the-framework)), a context
window that nothing names (name it with `--context-window`), a number below
the least it can be (the same section has the list), budgets that do not fit
together, a server that cannot be sent prompts as token IDs where the run asks
for them.

From Python:

```python
from turnstone.rollout import RolloutConfig, Task, run_episode
from turnstone.rollout.tokens import generated_positions, per_call_items, rebuild_final

record = run_episode(
    Task(prompt="...", setup_files={"data.txt": "..."}, max_turns=15, timeout=300),
    RolloutConfig(
        base_url="http://127.0.0.1:8000/v1",
        model="NAME",
        extra_body={"top_k": 20, "top_p": 0.95},
    ),
)
if record["one_sequence"]:
    rebuild_final(record["tokens"])  # the whole token sequence of the run
    generated_positions(record["tokens"])  # [{call, position, token_id, logprob}, ...]
elif record["trainable"]:
    per_call_items(record["tokens"])  # every call with its own prompt
```

`run_episode` raises instead of returning a record in two cases, both before
the run starts: `SetupError` for a configuration that cannot work (a server
that fails the check for prompts as token IDs included), and
`HostExecutionError` for a sandbox that acts on the machine the rollout runs on
(see [where the tools act](#where-the-tools-act)).

One rollout at a time per process: the session's storage is a process-wide
singleton that the lifecycle sets up and resets around each run. Run rollouts in
parallel as separate processes.

A rollout is attempted once. A second attempt would sample the model again and
repeat what the first did, so a failure is recorded, not retried.

That is the rollout. One model call of the session can still be made twice:
where a reply's stream breaks before the reply has ended, the framework issues
the same request again, twice at most, and the turn is sampled again. Nothing
of the broken attempt is in the token record or in `messages`; it is in
`discarded_calls`, and that is the only place that shows it.

### A task file

`--task FILE` gives the whole task as one JSON object, for a caller that starts
the command as a process and cannot build a `Task`:

```json
{
  "prompt": "The test in tests/test_parse.py fails. Fix the code.",
  "key": "task-0042",
  "files": {"notes.txt": "plain text", "data.bin": {"base64": "AAEC"}},
  "max_turns": 30,
  "max_generated_tokens": 60000,
  "max_command_seconds": 600,
  "container": {"image": "tasks/task-0042:latest", "workdir": "/testbed", "user": "0:0",
                "read_only_root": false, "writable": {}, "root_size": "8g"},
  "before_run": [{"argv": ["pip", "cache", "purge"], "timeout": 120}],
  "after_run": [{"argv": ["git", "status", "--short"], "timeout": 60}]
}
```

Only `prompt` is required. The keys are the fields of `Task`. `container` takes
those of `ContainerSpec`, the items of `before_run` and `after_run` those of
`BeforeRunCommand` and `AfterRunCommand`, and `sidecar` those of `Sidecar`
(`container`, `files`, `setup`, `setup_timeout`, `wait_ports`, `wait_timeout`,
`mcp_servers`). The one difference is `files`, a single key for text and other
content: a file's content is a string (text), `{"text": "..."}` or
`{"base64": "..."}`. A relative path is in the working directory, an absolute
one where it says; a sidecar's paths are absolute. `timeout` left out is 300
seconds, or with `max_generated_tokens` worked out from the budgets, as on the
command line; `null` asks for that.

The file is read strictly, because a task that is not the one its author meant
gives rollouts that look sound and are not. A key the classes do not have is
refused with its name and the keys there are, a key given twice is refused, and
so is a value of the wrong kind (`"5"` or `true` where a number belongs): a
misspelt key must not become a default without a word. What is no number at
all is refused as well, here and wherever the command reads a number (a flag,
`--extra-body`, a verdict that is handed in, the request parameters of the
configuration file): `NaN`, `Infinity` and a number too large to be one are
not JSON, and a limit that is one of them is no limit. A flag that says
something the task file holds (`--prompt`, `--key`, `--image`, `--timeout`,
`--max-turns` and the other task flags) is refused beside `--task`, whatever
its value: with two sources for one value it would be open which one counts.
A task the flags describe gets the same check as one a file describes: a
prompt that is empty, `--max-turns 0`, a file without a path or a container
setting that can be seen to be wrong without asking the runtime
([which ones](#the-container)) are refused before anything runs, whichever way
they are said. So are the numbers only a file can say: a before-run or
after-run command whose `timeout` is not above zero or whose
`max_output_chars` is below zero, an after-run command whose
`max_collect_bytes` is not above zero (at zero every file read out would count
as more than is read), a sidecar whose `setup_timeout` is not above zero,
whose `wait_timeout` is below zero or that waits for a port outside 1 to
65535.

A task file describes what happens inside containers. It cannot name a program
to run on this machine; a [verifier command](#a-verifier-command) is given on
the command line. What is not the task's but the run's (the model's endpoint,
the sampling settings, the limits of the framework, the rules, the server's
version) stays a flag beside `--task`.

### Started by another program

One rollout per process, so a caller that collects many starts the command many
times. What it can rely on:

| | |
|---|---|
| Exit status 0 | A trainable rollout, however the model ended it |
| Exit status 1 | The server, the harness or the runtime ended it, or the run did not start (`finish_cause` is `machinery`); also a run whose record could not be written to `--out` when it was over (standard error says why, and no file is there) |
| Exit status 2 | It ran to its own end and cannot be trained on: its token data, a server version that changed, a check that gave no verdict |
| Exit status 3 | Nothing was run: the command line, the task file or the configuration was refused, and standard error says why |
| `--out FILE` | Looked at before anything runs. Refused (status 3), with what is there left as it is: a folder that is not there or takes no file; a place that is a file the same command reads or starts (the task file, a verdict, a rule file, the configuration file whether `--config`, `TURNSTONE_CONFIG` or the default place names it, the verifier command's program and every file among its arguments), also where a symbolic link leads there; a place that holds anything but a file (a folder, a named pipe, a socket, a device). A file that is already there is removed, and so is a symbolic link: the link, not what it points to (with `--probe-only` it stays until the new verdict takes its place; see below). Then written for 0, 1 and 2, also for a run that did not start; never for 3. It is written under another name (`FILE.<process id>.part`) and renamed, so the file is there whole or not at all |
| Anything else | A process that died leaves another status, or one of 0 to 2 without the file: there is no record |

So after the command, whatever it did, the file at `--out` is this run's record
or is not there: a record an earlier run left at the same place does not
outlive a run that was refused or died. Two cases leave the place as it is. A
command line that could not be read at all (a flag that is not known, a value
that is no number) does not know its `--out`. And a place that is refused for
what stands there (a file the command reads or starts, something that is no
file) is not the command's to remove.

A verdict takes its place in one step. The rollouts of a batch read the
verdict's file while the batch runs, so `--probe-only --out FILE` leaves the
verdict an earlier probe wrote where it is while it asks the server, and then
puts the new one there by a rename: a reader finds the earlier verdict or the
new one, never nothing. The rule above holds once the command has ended:
where it has no verdict to put there (it was refused, it failed, the verdict
could not be written) it removes the earlier file then. Only a probe that is
killed leaves the earlier verdict behind.

The file is strict JSON. A value that is no finite number can still reach a
record from outside (a server can report one as a log-probability, a verifier
can print one): it is written as `null`, and `warnings` says where it stood.

The exit status is read off the record, before anything is printed. What the
command prints to standard output is a summary for a reader and decides
nothing: text the output's encoding cannot write is escaped (the model chooses
the characters of its reply), and an output that is closed, such as a pipe
whose reader has gone, is left alone.

The same holds for standard error, and for whoever writes to either stream.
The framework's logger writes to standard error, a line when the time limit
passes while the model is writing for one. Text a stream did not take (its
reader has gone, its disk is full, its descriptor is closed or was not opened
for writing) is given up before the command returns, and the stream is then
pointed at nothing, so that the interpreter's flush at exit cannot put a
status of its own (120) in the record's place. One case is the framework's,
not the command's: started with standard output closed altogether (`>&-`) and
without `NO_COLOR` in its environment, a Turnstone command dies while it is
being loaded, with status 1 and nothing run. Give it `/dev/null`.

Everything a run sets is a flag or is in the task file. The model's definition
(context window, reply limit, reasoning effort) comes from the configuration
file (`--config`), which is read and never written.

To probe the server once for a whole batch, `--probe-only` runs no rollout. It
prints the [verdict](#which-log-probabilities) as one JSON object (exit status
0 when the reported values are the sampling distribution's, 2 otherwise), and
`--sampler-logprobs '<that object>'` or `--sampler-logprobs @FILE` hands it to
each rollout, which then does not probe. With `--token-id-prompts` the probe is
made on the completions route, the one those rollouts use.

### The order of a run

What comes first happens before the task's time starts, and none of it is the
model's:

1. The configuration is resolved: limits, sampling settings, budgets. One that
   cannot work is refused.
2. The server's version is asked, where the run is given an address
   ([one version of the server](#one-version-of-the-server)). Without an answer
   the server is asked nothing more and the run does not start.
3. With [prompts as token IDs](#prompts-as-token-ids) the server's routes are
   checked, unless the verdict of an earlier check was passed in. A server that
   fails is refused.
4. The server is probed for the log-probabilities it reports
   ([which log-probabilities](#which-log-probabilities)), on the route the run
   uses, unless a verdict was passed in.
5. The container is started, and the task's sidecar; the task's files are put
   in; its [before-run commands](#before-the-run) run.

Then the session runs, under the task's time limit and budgets. Right after its
last model call the server's version is asked again; then the after-run
commands and the verifier run, and the containers are removed.

So the two questions about the version enclose every request that reaches the
model: the check of the routes, the probe, the session's calls and its
sub-agents'.

### Limits come from the framework

A rollout has no limits of its own where the framework has a setting. Each of
these is taken, in order, from the rollout's explicit override, the model's
definition in `config.toml` (`[models.NAME]`), the configured setting, and the
setting's registered default:

| Limit | `RolloutConfig` / flag | Framework setting | Where nothing names one |
|-------|------------------------|-------------------|-------------------------|
| Reply limit per model call | `max_tokens`, `--max-tokens` | the model's `max_tokens`, else `[model] max_tokens` | 32768 |
| Context window | `context_window`, `--context-window` | the model's `context_window`, else `[model] context_window`, else what the model's endpoint reports for that model | the rollout is refused |
| Time one tool call may take | `tool_timeout`, `--tool-timeout` | `[tools] timeout` | 120 s |
| Turns one sub-agent may take | `subagent_max_turns`, `--subagent-max-turns` | `[tools] agent_max_turns` | -1, no limit |
| The truncation setting, which bounds a command's output | `max_output_chars`, `--max-output-chars` | `[tools] truncation` | 0, the framework's automatic share of the remaining context |

The configuration file is the one every Turnstone command reads (`--config`,
`TURNSTONE_CONFIG`). A server's database is not read: a rollout runs in its own
process with throwaway storage. The record lists the value each limit had and
where it came from (`limits`).

A configuration file that cannot be used is refused before anything runs
(exit status 3; `SetupError` from Python), never read as an empty one: a file
that `--config` or `TURNSTONE_CONFIG` names and that is not there, and any
configuration file that cannot be read or is no TOML. Read as empty it would
cost the run its model definition without a word: the reasoning effort, the
reply limit, the request parameters. Where neither names a file, the default
one (`~/.config/turnstone/config.toml`) is read if it exists; only that one may
be absent.

Three rules keep a property of the configuration from being scored as the
model's outcome:

- **A number below the least it can be is refused** before anything runs
  (`SetupError`; the command exits with status 3 and says which), whichever
  way the task is given. A run started on one would say something about the
  number: with a context window of 0 every reply would be cut at one token,
  and the run would end as the model's `length`. The least a number given to
  a run can be:

  | Number (`RolloutConfig`; the flag is the same name with dashes) | The least it can be |
  |---|---|
  | `max_tokens`, `context_window`, `tool_timeout`, `max_read_bytes` | 1 |
  | `subagent_max_turns` | -1, which is no limit of its own |
  | `max_output_chars` | 0, which is the framework's automatic share |
  | `max_subagents` | 0, which lets a run start none |
  | `temperature` | 0 |
  | `server_stall_seconds`, `min_tokens_per_second`, `min_prompt_tokens_per_second`; the task's `timeout`, `max_generated_tokens` and `max_command_seconds`; the verifier's time limit (`verifier_timeout`, `--verifier-timeout`) | anything above 0 |
  | `max_tool_result_chars` | 1,000 |

  A value below its least in the configuration file is passed over, as the
  framework passes it over, and the next source in the order above decides.
- **A context window nothing names is refused**, not guessed (`SetupError`; the
  command exits with a message). A rollout ends, as the model's own outcome,
  before a request that could not fit the window. With a guessed window that
  ending would say something about the guess.
- **The reply limit is at most a quarter of the context window** in a run that
  sends messages: the most the framework itself reserves for a reply. A larger
  one is cut to that, and the record's `limits.max_tokens.source` says so. With
  a reply limit near the window's size the first long tool result would end
  every rollout. With [prompts as token IDs](#the-context-window-exact) the
  window is kept exactly, request by request, and no share of it is set aside.

A reply that reaches the reply limit ends the rollout (see below), so a limit
too small for the model cuts rollouts short. A model whose replies with
reasoning run to tens of thousands of tokens needs the limit its definition
gives it.

### Sampling settings come from the framework too

| Setting | `RolloutConfig` / flag | Framework setting | Where nothing names one |
|---------|------------------------|-------------------|-------------------------|
| Temperature | `temperature`, `--temperature` | the model's `temperature`, else `[model] temperature` | 1, recorded as the rollout's own |
| Reasoning effort | `reasoning_effort`, `--effort` | the model's `reasoning_effort`, else `[model] reasoning_effort` | none is sent: the model's own default |
| Other request parameters | `extra_body`, `--extra-body`, `--top-k`, `--top-p` | the model definition's `capabilities.server_compat.extra_body` | none |

The rollout's own request parameters go on top of the model definition's, key
by key. `top_k` and `top_p` have no setting of their own in the framework: they
are request parameters, given in the model's definition or by the rollout.

A temperature nothing names is 1, the model's own distribution, and is sent
with every request: left to the server's default it would be a value the record
cannot name. The record holds what the run was set to and where each value came
from (`sampling`), and beside it what the requests themselves carried
(`sampling.requested`).

### The rollout's own limits

These are the rollout's own, because they are the task's: the number of turns
(`Task.max_turns`, 15), the time (`Task.timeout`, 300 s), the number of
sub-agents (`RolloutConfig.max_subagents`, 16), and the
[budgets](#budgets-of-the-models-own) a task may give the model. One more is
the rollout's own because the framework has no such setting: the
[bound on a single tool result](#one-bound-on-a-tool-result)
(`RolloutConfig.max_tool_result_chars`, `--max-tool-result-chars`), which is
not set unless a run sets it.

`Task.timeout` bounds the session run, sub-agents included, to the next whole
second. It starts when the sandbox is up: bringing it up, a sidecar's setup and
the task's [before-run commands](#before-the-run) included, is not the model's
time, and the record says how long it took (`startup_seconds`). It does not
bound what comes after either: the check after
the run has time limits of its own, and removing a container takes up to 60
seconds for the removal and 30 to confirm it, which the record reports apart
(`teardown_seconds`). At the limit the session is cancelled and given ten
seconds to stop.

### Budgets of the model's own

A time limit can be used up by both sides: by the model, with long replies and
slow commands, and by the server, by being slow. With the time limit as the
only limit the two cannot be told apart, so a slow server ends rollouts as the
model's own `timeout`, and under load that falls on the long episodes.

A task can therefore give the model budgets in units the server's speed does
not change:

| Budget | What it counts | Running out |
|--------|----------------|-------------|
| `Task.max_generated_tokens`, `--max-generated-tokens` | The tokens the session and its sub-agents generate, together | `finish_state: token_budget`, the model's outcome |
| `Task.max_command_seconds`, `--max-command-seconds` | The time the model's tool calls run while no model call is under way | `finish_state: command_time`, the model's outcome |

**The token budget** is kept before every request. The request's reply limit is
cut to what is left, counting what requests still under way may yet generate,
so sub-agents running side by side cannot overshoot together. A reply of the
session that is cut at what was left ends the rollout as `token_budget`, where
a reply cut at the reply limit ends it as `length` (with prompts as token IDs
what the window has left is a third limit on a reply;
[the context window, exact](#the-context-window-exact) says which ending wins).
With nothing left the
session's next request is not made. A sub-agent whose request finds nothing
left fails, and the model gets that as an ordinary failed tool result; a
sub-agent's reply that was cut is an ordinary result too.

**The command time** is watched while the session runs. Every instant of a run
is one of three things, in this order: a model call is under way, the
session's or a sub-agent's (the server's time), or else a batch of the
session's tool calls is (the commands' time), or else neither (the harness's
own). So the time a sub-agent spends in its model calls is the server's, and
the time its commands take is the commands'. Once the commands' time is used
up the session is told to stop, what it has running is ended, and it is given
ten seconds.

**The time limit becomes a backstop for the machinery.** With a token budget a
run that reaches `Task.timeout` was held up by the server or the harness, or
asked the server for more work than the budgets count: the work on prompts,
which is in neither budget. Which of the two it was is measured:

- `RolloutConfig.min_tokens_per_second` (`--min-tokens-per-second`) is the
  speed the server must at least generate at. A token budget needs it; without
  it the rollout is refused.
- `RolloutConfig.min_prompt_tokens_per_second`
  (`--min-prompt-tokens-per-second`) is the speed it must at least process
  prompts at; left out, a hundred times the generation speed.

At the limit the server's time is compared with what it would take at those
speeds to process the prompt tokens it was sent and generate the tokens it
produced, with one second per request on top. A server that took longer was
too slow: the rollout ends as `backstop`, the machinery's failure, not
trainable. So does one in which the harness's own time passed 30 seconds plus
one second for every model call and every batch of tool calls. A server that
kept its minimum speed did what it was asked in the time that takes, and what
was asked is the model's doing: the rollout ends as the model's `timeout`.

`Task.timeout=None` (the command's `--timeout` left out) works the backstop out
from the budgets, for a task that also names `max_command_seconds`: the time
to generate the whole token budget at the minimum speed, the whole command
time, the time to process one prompt as long as the context window for every
turn and every sub-agent the task may have, and a margin of a minute and a
half. A server at its minimum speed does not reach it before the budgets are
used up unless the model sends more long prompts than that, which is then its
own `timeout`.

Without a token budget nothing changes: the time limit is the model's, and
[how a rollout ends](#how-a-rollout-ends) says when reaching it is the
server's doing instead.

The record holds the budgets, whose the time limit was and what was spent
(`budgets`), where every second went and how fast the server was (`time`), and
at the time limit what decided (`timeout`).

---

## Trainable, and one sequence

The record says two separate things about itself.

`trainable`: every model turn can be trained on from its own prompt, and
nothing but the model decided how the run went. All of this holds:

| What | How it is checked |
|------|-------------------|
| Every turn the session used was recorded, and ended normally | The count of recorded calls equals the session's assistant turns |
| One log-probability per generated token, for the right token | The server is asked to name each log-probability's token by ID, and the two channels are compared token by token |
| No token is missing | The counts are compared with the server's own usage numbers, prompt and completion; a reply without usage numbers fails the check |
| The log-probabilities are those of the distribution the tokens were sampled from | The server is probed before the run (below) |
| A trainer can reproduce the sampling | `temperature`, `top_k` and `top_p` are read off the requests themselves; one a request did not carry fails the check |
| The tokens were sampled from one version of the model | Where the run is given an address to ask: the server's version before the first model call and after the last are the same (see [one version of the server](#one-version-of-the-server)) |
| The run was not ended by the server, the harness or the runtime | See [how a rollout ends](#how-a-rollout-ends) |
| A reward exists | A check asked for after the run gave its verdict; see [checking a rollout afterwards](#checking-a-rollout-afterwards) |

A server that does not name tokens by ID leaves only the counts to compare. Such
a rollout is not trainable unless `RolloutConfig.require_token_id_check` is
switched off, and the record says which was in force (`token_id_check`). It is
required by default because equal counts do not show that each value belongs to
its token, which is what a trainer relies on.

`one_sequence`: `trainable`, and every prompt began with the previous prompt
followed by the tokens generated after it, so the whole rollout is one token
sequence and each generated token has one position in it.

`problems` lists why `trainable` is false; a rollout that is not trainable also
logs an error. Nothing the recorder does can fail a model call: a value in a
reply it cannot read marks that call, it is never raised into the session.

A trainable record need not hold a sampled token. Where the time limit passes
while the model is writing its first reply, the run ends as the model's
`timeout` with `turns: 0`: the token record is empty (`tokens.calls` is `[]`,
`generated_tokens` is 0), `messages` holds the task alone, `one_sequence` is
true, and the tokens that used up the time are in `discarded_calls` and
nowhere else. The outcome is the model's and is scored like any other; a
trainer finds no call to compute anything from, and must not take a first
call for granted.

A rollout that is `trainable` but not `one_sequence` is trained call by call
(`per_call_items`). Do not drop it: what breaks the sequence can be something
the model wrote (see the table further down), and a trainer that drops such
rollouts lets the model choose which of its rollouts count.
`generated_positions` refuses it unless called with `allow_partial=True`: the
calls before a break are not part of the final sequence, and returning only the
others would look like the whole rollout.

That is a run that sends messages. With
[prompts as token IDs](#prompts-as-token-ids) the harness builds the sequence
itself, so there the two go together: a break is a fault of the harness, and
the record is then not trainable.

### How a rollout ends

**A record is left out of training only for causes outside the model's
control.** Whatever the model can bring about is an outcome: the record stays
trainable and the task scores it, as a rule as a failure. Otherwise a model
would learn to leave the episodes that are going badly, and would never be
penalised for it.

`finish_state` is one of these, and `finish_cause` says which kind it is:

| `finish_state` | What happened | Whose doing | Trainable |
|----------------|---------------|-------------|-----------|
| `completed` | The model ended its turn without calling a tool (an empty reply included, and a reply of which a server that reads tool calls itself says it holds some and hands over none) | model | yes |
| `max_turns` | The model used all its turns | model | yes |
| `length` | A reply reached the reply limit; the rollout ends there | model | yes |
| `context_full` | The conversation filled the context window; the run ends before a request that could not fit, and with prompts as token IDs also on a reply that was cut at what the window had left | model | yes |
| `timeout` | The time limit passed while the model was generating or one of its commands was running; with a token budget, although the server kept its minimum speed | model | yes |
| `token_budget` | The budget of generated tokens was used up | model | yes |
| `command_time` | The time the model's commands may run was used up | model | yes |
| `sandbox_destroyed` | After a turn of the model's had come back: the model ended its own container from inside, where the task's settings let its commands do that (see [the main process](#the-main-process)), or a writable root outgrew its size or could no longer be measured (see [what bounds writes](#what-bounds-writes)) | model | yes |
| `request_refused` | After a request of the session had been answered: the server refused a later one for what the conversation held (HTTP status 400 or 422); see [a conversation the server refuses](#a-conversation-the-server-refuses) | model | yes |
| `server_stalled` | Without a token budget: the time limit passed while a model request had had nothing from the server for longer than it is allowed | machinery | no |
| `backstop` | With a token budget: the time limit passed and the server was slower than its minimum speed, or the harness itself took the time | machinery | no |
| `error` | A request of the session failed (after the client's own retries; the first request refused by the server, and a prompt the server finds too long, included), or the harness raised (a stable-prefix error included; with prompts as token IDs, a prompt that could not be assembled, or a task whose own first prompt does not fit the context window) | machinery | no |
| `server_version_unknown` | The server did not say which [version](#one-version-of-the-server) it is before the run, so the run did not start | machinery | no |
| `sandbox_failed` | The container could not be started | machinery | no |
| `sandbox_lost` | The container stopped and nothing the model could do explains it, or it stopped before the first turn of the model's had come back or was found gone before the first of the model's tool calls could run, or the runtime stopped answering | machinery | no |
| `sidecar_failed` | The task's [sidecar](#a-tasks-own-servers-the-sidecar) could not be brought up | machinery | no |
| `before_run_failed` | A [before-run command](#before-the-run) of the task failed or ran out of its time, so the run did not start | machinery | no |
| `sidecar_lost` | The task's sidecar stopped during the run | machinery | no |
| `subagent_failed` | A sub-agent failed for a reason that is not its work | machinery | no |
| `no_model_call`, `abnormal_reply` | The run ended before any model call, or on a finish reason the harness does not know, which only the server gives (a filter of its own, for one) | machinery | no |

Where several apply, a run that did not start keeps its own ending
(`server_version_unknown`, `sandbox_failed`, `sidecar_failed`,
`before_run_failed`); otherwise a lost container decides first, then a lost
sidecar, then a failed sub-agent, then the others.

One exception keeps a run the machinery cut short from being recorded as the
model's outcome. A container the model ended (`sandbox_destroyed`) gives way
to a failure of the machinery that came before it: a sidecar that stopped
(never the model's doing), a sub-agent's failure (counted only while the
session has not been told to stop, so one that is counted came before the
loss was found), and a failed request or a time limit the server ran into
(`error`, `server_stalled`, `backstop`) where the container was not yet known
to be gone when the session's run ended. What fails once the container is
known to be gone does not change the ending: it may follow from the loss.

`context_full` never comes from the server's refusal of a prompt that is too
long; the harness decides it, and how depends on the way the prompts go out.

A run that sends messages does not know a prompt's length before the server
has rendered it, so it estimates before each request: the server's own count
of the previous call's tokens, plus the size in bytes of what was added since,
plus the whole reply limit, which is at most a quarter of the window. A token
is at least one byte and usually three or four, so the new messages count for
several times their tokens. Such a run can end with as little as half of the
window used when a tool result is large, and ends at three quarters at the
latest.

A run with [prompts as token IDs](#prompts-as-token-ids) knows each prompt's
length before it is sent and keeps the window exactly: see
[the context window, exact](#the-context-window-exact). A cap on the whole
sequence that has to be exact needs that mode.

The time limit can be used up by both sides: by the model, with slow commands
and long replies, and by the server, by not answering. Without a token budget
it is the model's budget like its turns, so reaching it is the model's outcome,
with one exception that can be measured: a model request that had had nothing
from the server for `server_stall_seconds` (`--server-stall-seconds`, 60 by
default) when the limit passed. A request the server has not yet answered with
a first token is
allowed more: the time its prompt takes at the minimum prompt speed (1000
tokens a second where none is configured), since the length of a prompt is the
model's doing and a model could otherwise fill its context until the limit fell
into that silence. With a token budget the time limit is a backstop and the
verdict is by the server's speed; see
[budgets of the model's own](#budgets-of-the-models-own). Either way the record
says what the run was waiting on (`timeout`) and where the time went (`time`),
so a trainer can apply a stricter rule of its own.

Besides the endings, these also leave a record trainable, because the model can
cause them: a tool call the session refuses or that fails, malformed arguments
(arguments that are no object included), a sub-agent that returns nothing,
fills its own context or has its conversation refused by the server, reaching
the sub-agent limit, a command that is killed for using up memory, a full
disk.

#### A conversation the server refuses

A server answers a request with status 400 or 422 when it will not take what
the request holds: a conversation its chat template does not render, a body it
cannot read. Whose doing that is follows from what the request holds:

| The request | Whose doing | What happens |
|-------------|-------------|--------------|
| A sub-agent's, any of them | model: the conversation begins with the prompt the model wrote for it | The delegation comes back as a failed tool result with the server's words, and the run goes on (see [sub-agents](#sub-agents)) |
| The session's, after one of its requests was answered | model: the request is the one before it, which the server took, plus what the model wrote and what its tools gave back | The run ends as `request_refused`, trainable; `error` holds the server's words |
| The session's first | machinery: it holds the task and nothing of the model's | `error` |
| The session's, refused as too long | machinery: a run ends by itself before a request that could not fit the window it was told (`context_full`), so the server has another window than that | `error` |

Every other failed request says something about the server or the way to it
(an error of its own, too many requests, no such model, no right to ask, no
answer in time, a lost connection) and stays the machinery's. A refusal is
told by the status of the answer, not by a text, which could quote the model.
One thing is read from the server's words all the same: that a refused prompt
was too long, the last row of the table, which servers say in a handful of
known phrases. A server that says it in other words is taken to have refused
the conversation for what it holds, and the run ends as the model's
`request_refused` where it should be `error`.

An ending is the model's only once the model has done something. A sandbox that
goes away while it is being brought up ends the run as `sandbox_failed`, and one
that goes away before the first model turn has come back as `sandbox_lost`,
whatever the container's settings say about who could have ended it. So does
one that is found gone when the first of the model's tool calls is about to
run: the container is asked whether it is running right before that call,
because one that went away while the model was writing its first reply was
not ended by anything the model ran.

### Which log-probabilities

A trainer that weights by the sampler's probability needs each token's
log-probability under the distribution it was sampled from, after temperature,
top-k and top-p. A server may report the model's own instead, or apply some of
the settings before the values it reports and others after.

Before the run (and outside the task's time) the server is probed, on the route
the run uses (`logprobs.route`: `chat`, or `completions` with
[prompts as token IDs](#prompts-as-token-ids), since a server can report
differently per route), each setting by itself. A control request asks for the
most likely tokens at the first position of the reply to a short prompt, with
temperature 1 and nothing cut off (at its first 24 positions, once the model
has turned out certain of a first token: below). Then one
request each allows the sampler one token through `top_k` and through `top_p`
(under the sampling distribution it is reported with log-probability 0; a
server reporting the model's own repeats the control's value for it), and one
lowers the temperature and asks for the alternatives again (reported after the
temperature, their values lie apart by the control's differences divided by
the temperature; reported before it, by the control's). Each is read at one
position of the reply: the first, unless the model is certain of its first
token (below).

**A wrong verdict is worse than none**, and a server's own values move from one
request to the next while it serves others: by a few hundredths for a likely
token and by a tenth for an unlikely one, measured on one server beside four
rollouts. So each decision rests on a difference that such movement cannot
produce:

- A cut-off's two candidates are 0 and the control's value for the token that
  came back. The control must have left that token no more than about nine
  tenths of the probability (a value at or below -0.1); the reported value
  counts as 0 within 0.01, and as the model's own within half of that own
  value. For one to be taken for the other, the token's probability would have
  to move from under 0.905 to over 0.99.
- The temperature is decided by the slope of the alternatives' values against
  the control's, over every alternative both requests list (twenty are asked
  for): 1 where the values are reported before the temperature, 2 at the
  probe's temperature of 0.5 where they are reported after it, each within a
  tenth. The alternatives span several units of log-probability, so the
  movement above changes the slope by about a hundredth. The value of the
  sampled token itself does not decide, because for a token whose probability
  the temperature hardly changes the two candidates lie a few hundredths
  apart; it only has to be the value listed for that token, and the listed
  values have to be probabilities.

What is not clearly one or the other has no verdict for that setting, which is
not a pass: `reflects` holds `null` for it. A prompt whose first token the
model is all but certain of is skipped and the next of three is tried. The
record keeps what each decision was read from (`logprobs.tried`, one entry per
probe prompt: the `prompt`, the control's `likeliest` value, whether the prompt
was `used`, the `position` its settings were measured at, the value reported
for the token there under each setting measured on it as `temperature`,
`top_k` and `top_p`, and for the temperature the slope found and the number of
alternatives it is the slope of, `temperature_slope` and
`temperature_alternatives`).

**The probe measures the server, not the model**, and a model can be certain
of the first token of every reply (the word its reasoning opens with) while it
is unsure a few tokens on. So once the model has turned out certain of a first
token, the control of each further prompt asks for the first 24 positions of
the reply instead of one; and where the first tokens leave a setting open, the
prompts that were skipped are taken up again, and each open setting is
measured at the first position of the control's reply at which the control
says the model was unsure enough, by the same rules. The prompt that was asked
for a single token is asked for 24 last, and not at all where the model was
certain of every token of the replies at hand. Two replies to one prompt stand
at the same place for as long as their tokens are the same, so what the
control lists at that position is the model's own distribution for every reply
that came to it by the control's tokens, and for no other. A measurement is
therefore read only from a reply whose tokens before the position are shown to
be the control's: each named once among the alternatives listed at its
position, with the value reported for it. A request that leaves the sampler
one token follows the model's likeliest tokens, so the position has to be one
the control's reply reached by those; a control reply that took another token
where the model was certain is not used, and the next prompt is taken.

A setting decided at a later position is held against the first position as
well. The first token of a reply is the one a server computes by its ordinary
path, and a later one need not speak for it. The value of a token the model is
certain of fits either kind of server, so the first position is read by what
it still shows: under a cut-off the alternatives listed there (one possible
token where the values are taken after the cut-off, the control's
alternatives where they are taken before it), and under the temperature the
slope of its alternatives. A first position that shows the other kind is a
contradiction, and the probe ends without a verdict for that setting; one
that shows neither leaves the setting open (`tried[].first`).

A probe is five requests where the first prompt decides, four on a server that
reports the model's own values: two for one token and the others for up to 24.
Where the model is certain of the first token of all three prompts it is
seven, or six: the three controls (one token, then 24 positions twice) and
the same measurements for one prompt, the one with the temperature asked up
to the position it is read at. A model certain of every token costs those
three controls and gets no verdict.
Against a live server it took a second and a half, three seconds as a command
by itself (`--probe-only`) and three to five beside four rollouts. There, 100
such commands on both routes and the 105 rollouts' own probes all came out
right, none of them without a verdict, while the control's value for the
likeliest token took thirteen values between -0.794 and -0.759. With the same
model made certain of its first token (reasoning switched off, and a wording
in front of the probe's prompts that fixes the first word of the reply), 40
probes on both routes beside four rollouts were measured at the second or the
third token and all came out right; made certain of every token (a line to
copy), it got no verdict in six of six.

The position a setting is decided at shows which kind of value a server
reports, and it need not speak for the others: a server that drafts several
tokens in one step takes the later ones by another path. So the two cut-off
requests run over 24 positions, where every reported value must be 0 because
one token is possible at each, and one more request with a temperature and a
small `top_k` asks for the alternatives at each of 24 positions, which must be
that position's whole distribution: their probabilities sum to 1 and the
sampled token is among them. A server whose positions contradict each other
has no verdict for the settings concerned.

The verdict is in the record (`logprobs`, with `reflects` per setting and what
the other positions said under `positions`). A setting the reported values are
taken *before* is accepted only when it leaves the distribution as it is
(temperature 1, top-p 1, top-k off). To probe once for a batch, call
`turnstone.rollout.probe.sampler_logprobs_reported` and pass the verdict (or
its `reflects`) as `RolloutConfig.sampler_logprobs`; from the command line,
`--probe-only` prints the verdict and `--sampler-logprobs` takes it (see
[started by another program](#started-by-another-program)).

What the probe cannot show: the temperature is measured at the position that
decides (and at the first position besides, where a later one decides), so a
server that applies the cut-offs at the other positions and leaves the
temperature out of the values it reports there passes. And a probe speaks for
the moment it ran and the prompts it used, not for every request of a batch.

What the probe needs from the model is one token it is unsure of among the
first 24 of its reply to one of three fixed prompts, reached by its likeliest
tokens. A model certain of every one of them gets no verdict, and `reason`
says so ("the model was certain of every token of every reply it was asked
for"). A model that is certain of its first tokens only can still be left
without one, by chance: each control reply is sampled, and one that takes
another token than the likeliest where the model is certain cannot be used
("... no control reply came by its likeliest tokens to a token it was unsure
of"). Probing again asks again.

### One version of the server

A server whose weights are replaced in place between batches serves two models
under one name. A rollout that saw both was not sampled from one policy, and
nothing in its token data shows that.

A rollout can be given an address that answers with the server's version
(`RolloutConfig.server_version_url`, `--server-version-url`): any JSON value,
or with `server_version_field` (`--server-version-field`) one field of a JSON
object. It is asked twice: right before the first model call of the run can go
out, the probe and every sub-agent call included, and right after the session's
last model call has returned. Both answers are in the record
(`server_version`).

| What happened | The record |
|---------------|------------|
| The same version at both ends | Unchanged |
| The two differ | Not trainable; `problems` names both versions. How the run ended stays what it was |
| No answer after the run (the question is put three times) | Not trainable: whether the version changed is not known. How the run ended stays what it was |
| No answer before the run | The run does not start: `server_version_unknown`, with no model call made, the probe included, and no container started |

What this shows rests on two things the server has to keep. Its version only
moves forward, so that the same answer at both ends means the same version in
between. And it changes its version together with its weights: a server that
swaps the weights first and reports the new version a moment later leaves a gap
in which a rollout can end on new weights under the old version, and the two
questions cannot see into that gap. They are one question at each end, not one
for every call: a difference says that the rollout saw two versions, not which
calls saw which.

A server that does not answer is asked three times, and each question has ten
seconds as a whole, from the connection to the last byte of the answer, so an
answer that keeps coming a few bytes at a time does not hold a run. A server
that has no such address, or answers with something that is no version, is
not asked again. The question carries the rollout's API key only where the
address is on the model endpoint's own host and port.

### What can still make a prompt not extend

The session guarantees that each request's *messages* begin with the previous
request's. Whether the *tokens* do depends on the server writing a replayed
assistant turn exactly as the model generated it. The record checks that
independently on the token IDs the server returned. The known ways it fails, and
what is done about each:

| How a replayed turn can differ | Handling |
|--------------------------------|----------|
| Earlier reasoning left out | Prevented: reasoning is sent back with every request |
| A typed argument written back in the template's spelling (`true` for `True`, `null` for `None`) | Prevented: each argument is sent back as the text the model wrote, read from the recorded bytes (`RolloutConfig.argument_text`) |
| A call whose text cannot be read, or does not mean the parsed arguments (an argument written twice, a number that is not the parsed one) | Flagged: `prefix_events` holds `raw_arguments_unrecovered`; the call is sent back from its parsed values and the token check decides |
| Malformed arguments, legalized before they are sent back | Flagged: `prefix_events` holds `arguments_legalized` |
| More tool calls in a turn than run in one turn | Prevented: every call stays in the turn and gets a result |
| A reply cut at the token limit, where the server would close an open reasoning block or call | Prevented: the rollout ends at that reply (`finish_state: length`) and nothing is sent after it |
| The turn ended on a token the template does not write there (an end-of-text token where it writes its end-of-turn token) | Flagged by the token check, which names it: "the turn's last token, which the server replaced" |
| White space the template adds or drops around text or reasoning, text between tool calls, reasoning that is only white space | Flagged by the token check |
| The model generated tokens that are not how the server splits the same text into tokens (the text is identical, the token IDs are not) | Flagged by the token check only. Nothing sent as messages can prevent it: the server tokenizes the replayed text its own way. Seen in long replies |

A flagged rollout is still `trainable` when its tokens are sound; it is not
`one_sequence`.

All of this is a run that sends messages. The next section is the way out of
the last row, and with it of the whole table.

---

## Prompts as token IDs

A session sends messages, and the server writes the earlier assistant turns
back into each prompt by turning their text into tokens again. A model can
generate a split of a text into tokens that the tokenizer does not produce for
the same text; the next prompt then differs from what was generated, and the
rollout is no longer one sequence. Nothing sent as messages can prevent that
(the last row of [the table above](#what-can-still-make-a-prompt-not-extend)),
and the longer a rollout the likelier it is.

`RolloutConfig(token_id_prompts=True)`, or `--token-id-prompts`, sends every
prompt as token IDs instead. It is off by default. The record says which way a
run's prompts went (`prompt_mode`: `messages` or `token_ids`).

**Rollouts that are trained on send their prompts as token IDs**, and not for
the sequence alone. Sent as messages to a server that reads tool calls itself,
a run can be ended by the model as the machinery's failure: one argument that
the server's own reader cannot hand on (half of a surrogate pair, written as
an escape in a typed argument) breaks the reply's stream, the request is made
again and the turn sampled again, and a model that writes the same every time
ends the run as `error`, which is not trainable. Such a record leaves training
at the model's will, which is what the rule under
[how a rollout ends](#how-a-rollout-ends) is there to prevent, and nothing on
this side can tell it from a server that failed by itself: the broken stream
looks the same, and only the server's words differ, which each attempt keeps
in `discarded_calls[].error`. With prompts as token IDs the reply is read here
and the same reply is an ordinary one: the call runs, and the run goes on.

### How a prompt is built

```
prompt of call t+1 = prompt of call t + tokens generated by call t + tail
```

The prompt of call t is the one the server reported for it, which is checked
to be the one that was sent. The tail is what the chat template writes after
the generated tokens up to the next generation position: the rest of the
assistant turn's closing, the tool results, the next turn's opening. It is
never rendered here. The server's tokenize route renders the request's own
messages, tools and template arguments twice:

- *through*: the conversation up to and including the assistant turn, without
  a generation prompt;
- *whole*: the whole conversation of the new request, with one.

*through* ends with the template's closing of an assistant turn and is a
prefix of *whole*, so what *whole* holds after it is what follows that turn.
The tail is the closing, less its first token when the model generated that
token itself (the end-of-turn token), then the rest of *whole*. The first
prompt of a run is the server's rendering of the first request.

Each of these is checked on every call, and a call on which one fails is not
sent. The run ends as `error` (the machinery's, not trainable) with the reason
in `error`:

| What is relied on | The reason names it |
|-------------------|---------------------|
| The request's messages begin with the previous request's, and an assistant turn follows | "does not begin with the messages of the request before it" |
| The previous call ended by itself | "did not end by itself" |
| The server reported the prompt it was sent | "did not report the prompt ... as it was sent" |
| *through* is a prefix of *whole* | "renders the conversation up to an assistant turn differently once more messages follow it" |
| *through* ends with the closing found before the run | "does not end with the closing found before the run" |
| The tail ends with the generation prompt found before the run | "does not end with the generation prompt found before the run" |

A turn can end on another token than the template's end-of-turn token (a model
may have a second token it stops on). The template's closing is then appended
whole after it, the run goes on as one sequence, and `prefix_events` gets
`turn_ended_on_other_token` with the token.

A prompt built this way is what the model saw and wrote. It need not be what
the chat route renders for the same conversation: the turn as it was read and
stored, written back by the template, can come out as other tokens. *whole* is
that rendering, so each prompt is compared with it, and where the two differ
`prefix_events` gets `prompt_differs_from_rendering`: the call whose prompt it
is (`call`, counted as the token record counts the session's calls), how many
tokens each holds (`prompt_tokens` as built and sent, `rendered_tokens` as the
chat route would have it) and how many they share at their start
(`same_tokens`, which is where they part). Once a turn differs, the prompt of
every later call does, from the same place. The event is noted when the reply
to that prompt has ended, so its `call` is always one of `tokens.calls`: a
prompt that was built and not sent because the window was full, or sent and
never answered whole, leaves no event.

The record stays trainable and one sequence: nothing is wrong with its tokens.
What the event says is that from that call on the model was sampled after a
prompt the chat route would not have produced, so a model served through that
route never sees such a prompt. It is no reason to leave the run out of
training, since the model brings it about by what it writes. Known ways:
tokens that are not how the tokenizer splits their text (what this mode is
for); a call marker written inside the reasoning, which ends the reasoning
for the reader, while the template writes the closing marker the model left
out; a turn that ended on another token; white space the template adds or
drops.

A request repeated after an attempt that did not end gets the same prompt.

With the prompts built this way the rows of the table above no longer apply to
the session's own calls: how the server would have written an earlier turn does
not enter the prompt. The token-level prefix check stays on. A break it finds in
this mode is a fault of the harness, so it makes the record not trainable and
is listed in `problems`.

### The context window, exact

With prompts as token IDs the length of every prompt is known before it is
sent, so the context window is kept exactly:

- the whole sequence of a rollout is never longer than the window, and a
  rollout can use the window to its last token;
- each request's reply limit is the smallest of the reply limit, what the
  token budget grants, and what the window has left after the prompt;
- a request for which nothing is left is not made.

How such a run ends, always as the model's outcome and trainable, with nothing
sent afterwards:

| What happened | `finish_state` |
|---------------|----------------|
| Before a request, the token budget has nothing left | `token_budget` |
| Before a request, the window has nothing left | `context_full` |
| A reply reached the reply limit itself | `length` |
| A reply was cut at what the token budget granted, and that was less than the reply limit | `token_budget` |
| A reply was cut at what the window had left, and that was less than both | `context_full` |

Where two limits are the same number, the earlier one in that order names the
ending: the reply limit before the token budget, the token budget before the
window. Before a request the token budget is looked at first. Each call's
record carries what the window had left for it (`calls[].window_left`).

Two things differ from a run that sends messages. No share of the window is set
aside for a reply, so the reply limit is the configured one, not at most a
quarter of the window. And a task whose own first prompt does not fit the
window ends as `error`, the machinery's: the model had no part in it.

A sub-agent's reply limit is cut the same way; a sub-agent whose prompt no
longer fits has filled its own window, which is its own doing.

A window smaller than the server's own is kept the same way (asked against a
live server with 128,000 of 262,144). A sequence of exactly the server's own
window is accepted by it; a prompt as long as the window is refused.

A tool result is never shortened to fit. A run whose last result is larger
than what the window has left ends there, before the request, with part of the
window unused: the result is in `messages` and the tool log, and the final
sequence ends with the last reply that was generated. The
[bound on a single tool result](#one-bound-on-a-tool-result) keeps one result
from costing more than its share.

A run that sends messages keeps its estimate;
[how a rollout ends](#how-a-rollout-ends) says how early that can end it.

### What the server must have

| | |
|---|---|
| A tokenize route for messages | `POST /tokenize` beside the API root, taking `messages`, `tools`, `chat_template_kwargs` and `add_generation_prompt`, and rendering exactly as the chat route does; it also tokenizes a text (`prompt`, `add_special_tokens: false`) |
| A completions route for token IDs | `POST /v1/completions` with `prompt` as a list of token IDs, the sampler settings, `stream`, `logprobs: 0`, `return_token_ids`, `return_tokens_as_token_ids` and `skip_special_tokens`; it returns per part the text and the token IDs, each token's log-probability named by token ID, the prompt it used, and usage at the end |
| A turn that ends | Generation stops on the template's end-of-turn token, and that token is among the returned IDs |
| Markers kept | The generated text keeps the reply format's markers |
| A way to name the next token, for the check only | `logit_bias` on the completions route, or `allowed_token_ids`. Without either the check cannot see how the server reports a token the model does not write by itself, and leaves that to the replies of the run |

It needs no reader of reasoning or tool calls, and the chat route is used for
one thing only, before the run: to compare its rendering with the tokenize
route's. A server started without any such reader passes the check and runs
rollouts in this mode. The same server refuses a chat request that leaves the
choice of tools to the model, so there a run that sends messages ends at once
as `error`: on such a server this mode is the only one that works.

### The check before the run

Before the run, outside the task's time, `check_token_id_route`
(`turnstone/rollout/idprompt.py`) asks the server for exactly what the mode
uses: it renders a small conversation with an assistant turn, a tool call and a
tool result through the tokenize route; compares that with the prompt the chat
route reports for the same request (asked for one token; if the server refuses
to leave the choice of tools to the model, asked again with no tool choice);
takes the generation prompt and the closing of an assistant turn from
renderings; asks the tokenize route for each marker's token; has the server
generate each marker, by cutting the rendered turn right before it and asking
the completions route for one token, and looks for the marker in the text; and
does the same right before the end of the turn, where the reply must carry
log-probabilities named by token ID, usage and the prompt as sent, and the
end-of-turn token must come back among the IDs with a finish reason of `stop`.

The check fails only for what the server does. Which token a model writes at
those places is the model's business, and a later checkpoint may write another
one. So the model's own likeliest token is taken where it is the one wanted.
Where it is not, the request is repeated with the server steered to the wanted
token: with `logit_bias` set to 100 for it, a parameter of the plain
completions route; and where the server refuses that or does not follow it,
with `allowed_token_ids` holding that one token, which some servers take that
refuse the first (one that drafts several tokens in a step, for one). A way
that did not work is not tried again in the same check. A server that takes
neither could not be brought to generate the token: the step is left unseen,
the verdict names it with the reason (`checks.not_observed`), and the server
passes. What was left unseen is not lost, because every reply of a run is held
to the same two things when it is read (see [the reply](#the-reply)).

A server that fails any of it is refused (`SetupError`, nothing is run; the
command ends with status 3): there is no falling back to messages. So is one
that leaves a request of the check without an answer for two minutes. The
verdict is in the record (`token_id_route`). To check once for a batch, call
`check_token_id_route` and pass the verdict as `RolloutConfig.token_id_route`;
it holds for the same server, model and template arguments.

The check is made after the server's version was asked, where the run asks for
it, and before the probe (see [the order of a run](#the-order-of-a-run)).
The [log-probability probe](#which-log-probabilities) asks on the route the run
uses: with prompts as token IDs it renders its own prompts through the tokenize
route and probes the completions route, and its verdict says so
(`logprobs.route`).

### The reply

The completions route reads nothing, so the reply is read here, by the
description named in `RolloutConfig.reply_format`
([below](#reading-a-reply-without-the-server)), and handed to the session as a
chat-shaped reply once it has ended. The session gets `tool_calls` as the
finish reason of a turn that ended by itself with calls, `stop` without, and
`length` for a reply cut at the limit, whatever it holds.

Everything behind `trainable` is checked as before: one log-probability per
token and for the right token, the counts against the server's usage numbers,
the sampler settings read off the requests. One check is added: the server
must report the prompt it was sent. The budgets, the time accounting, the stall
rule and cancelling work on the same call records.

Two things the reading relies on are looked at on every reply, the session's
and a sub-agent's alike, because the check before the run sees them only where
the server could be brought to generate the tokens concerned:

| What is relied on | A reply that breaks it |
|-------------------|------------------------|
| Generation stops on the template's end-of-turn token | holds that token before its end: "the server went on generating after the template's end-of-turn token" |
| The text of a marker token comes in the same part of the reply as the token | "marker tokens of the reply came without their text" |

Neither is something a model can write, so such a reply is not read as the
model's: the call raises, and the run ends as `error` (the machinery's, not
trainable) or, in a sub-agent, as `subagent_failed`. A server that leaves the
end-of-turn token out of the IDs it returns is found by the counts: the record
is not trainable and `problems` says that the server counted one token more.

A marker token is found in its part's text by the order of the part's tokens
([below](#reading-a-reply-without-the-server)). Where that order does not
decide it, the reply is read by the rule given there, which is something a
model can bring about, and the record is told: `prefix_events` gets
`marker_place_uncertain` with the call (`call`, counted as the token record
counts the session's calls) and how many marker tokens of its reply were
placed by the rule (`tokens`). Such a reply could have been read differently
had the server cut it into other parts. What is noted of a sub-agent's reply
is kept apart from the session's events: in the record under
`subagents.events`, with `call` the `index` of the call in `subagents.calls`.

A sub-agent's calls go the same way: token IDs, the completions route, the
reply read here. Each of its prompts is the server's rendering of its own
messages; its conversation is not part of the rollout's sequence.

---

## Reading a reply without the server

A server may read a reply itself and hand back reasoning, text and tool calls
as separate fields. A completions route hands back the generated text only, and
a server may run with no reader of its own. `turnstone/rollout/replyformat.py`
reads a reply from what was generated.

How a reply is written is a description, a `ReplyFormat`, asked for by name
(`reply_format("parameter_blocks")`). One is defined: reasoning that a marker
closes, then text, then tool calls as tagged blocks with one tagged block per
argument. A server with another format needs another description; nothing else
in the package names a marker. The argument texts that a stable-prefix run
sends back as written ([above](#what-can-still-make-a-prompt-not-extend)) are
read by the same description.

A reader is given *pieces*: text, and the markers the model generated as one
token of their own. The outer markers of the format are single tokens, and they
count as markers only as those tokens. The same characters spelled with
ordinary tokens are text, which is what a model produces when it writes a file
about its own call format.

A server hands a reply on in parts, each with its text and the IDs of the
tokens that text stands for, and `pieces_from_chunks` makes the pieces from
them. A part's text is the text of its tokens in their order, so a marker
token is the occurrence of its text that stands where the token does. Nearly
every part holds a marker's text exactly as often as the marker's token, and
there is nothing to decide. Where a part holds it more often, because the
model spelled the same characters out beside the marker, the order of the
part's tokens decides: a marker token with no other token before it in its
part is the first occurrence, one with none after it the last, and marker
tokens side by side stand side by side. So the same tokens are read the same
however the server cut the reply into parts. One case is left that the part
alone does not decide: a marker token with other tokens before it and after
it, in a part that holds its text more than once. The texts of those other
tokens are not known, so the token is taken for the first occurrence, and the
number of tokens placed that way comes back with the pieces. A marker token
whose text is not in its part at all is counted too.

`read(pieces, tools=..., in_reasoning=...)` returns the reasoning, the text and
the calls. It reads what a server of this format puts in its non-streamed
reply:

| Part | How it is read |
|------|----------------|
| Reasoning | Everything up to the closing marker, as written, white space included; none when there is nothing. A reply cut inside it is all reasoning. A call marker inside it ends it |
| Text | What follows, up to the first call. With calls it is stripped, and white space alone is no text. Without calls it stays as written |
| Calls | Every function block, in order, in one turn. Text between or after calls is dropped. A function block without the call marker counts |
| Argument values | The text between an argument's tags, less one line break at each end, then typed by the tool's schema: the types the schema allows are tried in the order null, integer, number, boolean, object, array, string, and the first that fits wins. A whole number written for a `number` becomes an integer. A text no allowed type fits is read as JSON where it is JSON and stays text otherwise. An argument the schema does not describe, and every argument of a tool that was not offered, stays text |
| Malformed calls | An argument written twice counts as written last. An argument whose closing tag never came is left out. A call cut at the limit keeps its finished arguments. A call without a name is no call |
| No tools offered | No calls are returned |

The arguments come back as JSON text with the keys in the order written.

A reply is read inside the model call, once it has ended, and the time is the
call's. Reading takes time in proportion to the reply's length, whatever the
model wrote: long runs of white space inside or after a tag, openings that are
never closed, a marker repeated thousands of times. (A number of more digits
than the interpreter converts to an integer, 4,300 unless that is set
otherwise, is not read as one: alone it stays text, and so does a JSON value
that holds it.)

What is read can always be sent back. JSON in a reply can spell half of a
surrogate pair as an escape, a character no request body can carry; wherever
a name or a value is read from such JSON, each half that stands alone becomes
the replacement character (U+FFFD).

Where this differs from what a session reads off such a server's *stream*,
which is how a session that sends messages gets its replies:

| | The stream | Here |
|---|-----------|------|
| Text before a call | Keeps the white space around it | Stripped |
| Text between or after calls | Appended to the text | Dropped |
| A call cut at the limit, an argument written twice | The arguments stay unfinished JSON | The finished arguments |
| White space inside the tags (`<function= demo >`, `< parameter = text >`) | The name keeps it (`" demo "`), and the arguments stay unfinished JSON | The name without it, and the key from its first character on (`demo`, `{"text ": "x"}`) |

And where it differs from the same server's non-streamed reply: that reply is
read from the text alone, so there a marker spelled with ordinary tokens counts
as a marker, and the name of a special token spelled out in text is dropped.
Here neither happens.

`tests/test_rollout_replyformat.py` pins this on recorded replies
(`tests/data/rollout_reply_fixtures.json`): live replies with what the server
read in them, and made-up replies, many of them damaged, with what the server's
own reader made of them.

---

## Where the tools act

A rollout's tools act in a sandbox made for it, and a rollout refuses to run
them anywhere else. The commands are the model's: on the machine the rollout
runs on, each of them would run with the rights of whoever started the rollout.

`run_episode` therefore refuses a sandbox whose
[execution environment](tools.md#execution-environment) acts on this machine
(`acts_on_host`), with `HostExecutionError`, before the session is built or a
file is put in. An environment that does not say where it acts is taken to act
here. The rollout's session refuses the same when it is built.

`allow_host_execution=True` (on `run_episode`, and on `RolloutSession` for a
caller that builds one) lifts the refusal. It is for a caller that writes every
command itself, such as a test that scripts the model. With a real model it
runs whatever the model writes on this machine. The command line has no such
switch: `turnstone-rollout` always runs in a container. The record says which
it was (`acts_on_host`).

The [rules on tool calls](#rules-on-tool-calls) are not part of this. **They
are not isolation**: they are patterns, a pattern can be written around, and
nothing that keeps a model's commands off this machine rests on them.

---

## The container

One container per rollout, named `tsgym-<random>`. What it is comes from the
task (`Task.container`, a `ContainerSpec`); the defaults are the contained case:

| Setting | Default | A task may say |
|---------|---------|----------------|
| `image` | `python:3.14-slim` | its own image; it must be present, the runtime is told never to fetch one |
| `workdir` | `/workspace` | another directory |
| `user` | `65534:65534`, unprivileged | any user but the main process's, the image's root (`0:0`) included |
| `read_only_root` | yes | no: the image's whole file system is writable |
| `writable` | `/workspace` (512m) and `/tmp` (256m), memory-backed | other memory-backed directories with their sizes; the commands' user has to be able to write `/tmp` (below) |
| `root_size` | `10g` for a writable root | another size for a writable root; there is no way to ask for none (below) |
| `volume_size` | 64m | the size of each directory the image declares as a volume (below) |
| `capabilities` | none | a few of `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `FSETID`, `KILL`, `SETGID`, `SETUID`, `NET_BIND_SERVICE`, `SYS_CHROOT` |
| `environment` | none | environment variables |
| `cpus`, `memory`, `pids_limit`, `shm_size` | 1, 2g, 256, 64m | other limits |

A setting that can be seen to be wrong without asking the runtime is refused
before anything is started, the task with it (status 3 from the command,
`ValueError` from `ContainerSpec.validate`), for the episode's container and
for a sidecar's alike:

- an `image` that is empty, begins with `-` or holds a space;
- a `workdir` that is not an absolute path;
- a `user` that is not a name or a number, with a group after a colon where it
  names one: `no such`, an empty one, a number above 4294967294 (a name is
  made of letters, digits, `_`, `.` and `-`, does not begin with `-` and may
  end in `$`);
- an environment variable without a name, or with `=` in its name;
- a `memory`, `shm_size`, `volume_size` or `writable` size that is not a size
  above zero as the runtime reads one: a number, whole or with a decimal
  point, and a unit or none (`512m`, `1.5g`, `1048576`); a `root_size` that is
  no size above zero;
- a `writable` directory that is not an absolute path, or holds a colon;
- `cpus` or `pids_limit` that is not above zero;
- a capability no task may name; a NUL character in any text.

Zero is refused for a limit because the runtime takes it to mean that there is
none. What only the runtime can say is found when the container is started and
ends the rollout as `sandbox_failed`: an image that is not there, a user name
the image does not hold, a memory limit below the runtime's smallest, more
processors than the machine has.

Whatever a task says, the container has no network, no new privileges, no
swap, a cap on open files and no core files, the image's own entry point is not
run, and the container is removed with its volumes when the run ends. No
setting mounts a directory or a file of this machine's into it. The only files
of this machine's a container holds are the three the runtime itself mounts
into every container, `/etc/hosts`, `/etc/hostname` and `/etc/resolv.conf`:
read-only over a read-only root, counted toward the root's size over a writable
one (see [what bounds writes](#what-bounds-writes)). The removal is confirmed by asking the runtime about the container by
name; a removal that cannot be confirmed is reported as `sandbox.removed: false`
and a line in `warnings`, never raised, so a finished rollout is not lost to its
teardown.

If the runner is killed, the container is removed by a small process of its own
on the host, out of the container's reach. Its time is set anew for each step,
to as long as that step may take and ten minutes on top: bringing the sandbox
up (15 minutes, and a sidecar's own limits), the task's commands before the run
(their time limits), the run (the task's time limit), the commands after the
run (their time limits), the verifier (its time limit).
It is stopped only once the container is known to be gone; after a removal that
could not be confirmed it tries again within half a minute and keeps trying for
five.

That keeper is the one process meant to outlive a killed runner, and no other
process of a run stays on the host for good. The process that listens to the
runtime's events about the container ends with the runner, however the runner
ends; on a machine whose kernel cannot arrange that, it ends when the container
is removed. The runtime's client of a command that was still running ends with
that command or with the container, and so does a sidecar's connection. A
[verifier command](#a-verifier-command) that was running is not stopped: its
time limit was the runner's, and it goes on to its own end.

On the host the harness signals only process groups of children it started
itself: the runtime's client of a command, a verifier command with what it
left in its group, the keeper when another takes its place or it is no longer
needed. Each such signal goes through the one function the framework's own
shell tools use (`signal_group` in `turnstone/core/background_shells.py`). It
sends nothing unless the id is an integer above 1, is not the runner's own
group, and is the id of a child that has not been collected yet: until then no
other process can have the id, so an id that was freed is never signalled.

The image needs `sh`, `sleep`, `setsid`, `timeout`, `head`, `cat`, `stat`,
`mkdir`, `dirname` and `grep`; a container without them is not started. `bash`
is used for the model's commands when present, otherwise `sh`.

The shell tool writes each command to `/tmp` as a script and has the shell run
it. Before the run starts, one command is run in the container exactly that
way: as the commands' user, with the same script, the same place and the same
shell. A container in which that does not work is not started, and the rollout
ends as `sandbox_failed`: a `writable` that leaves `/tmp` read-only over a
read-only root, a `/tmp` the commands' user cannot write, an `sh` that does not
know `set -o pipefail` in an image without `bash`. Started all the same, every
shell call of the model's would fail there and be scored as its own.
`tar` is needed where a task's files are bytes, where a command after the run
names files to read out, and in a sidecar that is given files.

### The main process

A rollout whose container is lost is not trained on, so ending the container
must not be something a model can do. The container lives as long as its main
process, and this is what keeps the model's commands from that process:

- It is the first process of the container's process namespace. The kernel
  delivers to it no signal from inside that it has no handler for, the kill and
  stop signals included.
- It runs as a user of its own, never the user the commands run as: `65533`, or
  `65532` for a task whose commands run as `65533`. One process may signal
  another, set its limits, trace it and write its out-of-memory score only as
  the same user, or with a capability no task may name. A container whose
  commands would run as the main process's user is not started.
- It is `sleep`, started so that the kernel itself collects the orphaned
  processes handed to it. It never runs again: it starts no program, reads no
  file and uses no processor time, so neither a limit set on it nor a change to
  the files it came from ends it.
- It alone is spared by the out-of-memory killer. The container is created with
  an out-of-memory score nothing inside can lower, so a command cannot spare
  itself: what uses up the memory is what is taken. The main process's own
  score is lowered once, at the start, from outside: by a helper container that
  joins the process namespace, holds the two capabilities that one write takes
  (`SYS_RESOURCE`, `DAC_OVERRIDE`), has no network and a read-only root, runs
  the task image's `sh` for that write and is gone afterwards. The episode's
  container never holds those capabilities.
- Its lifetime is kept from outside.
- A capability that would undo this is refused when the container is made:
  `SYS_BOOT` (a restart ends the main process), `SYS_PTRACE` (the main process
  could be taken over), `SYS_ADMIN`, `SYS_RESOURCE`, `SYS_TIME`, `SYS_MODULE`,
  `SYS_RAWIO`, `MKNOD`, `NET_ADMIN`, `NET_RAW`, `DAC_READ_SEARCH`, and anything
  not in the table above.

A runtime that cannot make that one write fails the start, and the rollout ends
as `sandbox_failed`. A rootless runtime is such a runtime: lowering an
out-of-memory score takes a right it does not have on the host.

`sleep` is started in one of four ways, whichever the image offers first: by
`bash`, by an `env` that can ignore a signal for what it starts, by `python3`,
by `perl`. An image with none of them keeps a shell as the main process. It
waits for one `sleep` that does not end by itself and collects the orphans
while it waits. A shell that cannot start a process gives up, and at the
container's process limit, which the model's commands can hold, it cannot; so
this one starts nothing while commands run. It starts its `sleep` once it has
been spared from the out-of-memory killer, so that the `sleep` is spared with
it, and starts it again from the image's files only if something ended it. The
record says which it is (`sandbox.main_process`: `sleep` or `shell`).

**What a task's own settings give back.** Three settings let a model's commands
end the main process after all, without the runtime seeing why:

- commands that run as root with the `SETUID` capability can act as the main
  process's user;
- where the main process is a shell, commands that can replace the program it
  starts run their own in its place: over a writable root, root can, and so can
  a user who may write that program;
- where the main process is a shell, commands that run as root with the `KILL`
  capability can end the `sleep` it waits for. The shell then has to start
  another, and gives up if it cannot: at the process limit, which those
  commands can hold at that moment.

In such a container an end of the main process that nothing outside explains is
taken to be the model's doing, once a turn of the model's has come back: the
rollout ends as `sandbox_destroyed`, the model's own outcome, trainable, for
the task to score. So does an
out-of-memory kill of the main process where the commands could make it a
candidate again (the first two cases, or root with `DAC_OVERRIDE`). Everywhere
else the end of the main process is the machinery's (`sandbox_lost`), and so is
any end the runtime was asked for from outside the container, whatever the
settings. `sandbox.main_process_reach` names the setting that applies, `null`
when none does, and `sandbox.lost` gives the reason in words, with the exit
code and the count of out-of-memory kills beside it.

This is a rule about what the settings allow, not an observation of what the
model did. In a container with such a setting, a main process that the machine
ended for a reason of its own, unseen by the runtime, is scored as the model's
outcome. A task that needs neither setting should not have them.

### What bounds writes

Memory-backed directories have a size, and their contents count toward the
memory limit, so `memory` has to cover what processes need plus what files may
hold. A read-only root with memory-backed directories is bounded entirely.

A directory the image declares as a volume would be put on this machine's disk
by the runtime, writable and without a size, whatever the root is. It becomes a
memory-backed directory instead (`volume_size` each, or the size `writable`
gives that path). Such a directory starts **empty**: what the image holds at
that path is hidden. An image whose volumes the runtime cannot name is not
started. The record lists the paths (`sandbox.volumes`).

A writable root is not memory-backed: what is written there goes to the host's
container storage. `root_size` bounds it, and a writable root always has one:
`10g` where the task names none. The record says the size and whether it was
the task's or the default (`sandbox.root_size`, `sandbox.root_size_source`:
`task` or `default`). Where the host's storage driver can bound a container the
size is enforced by it; where it cannot, the harness watches the size every two
seconds and stops the container once it is over, which ends the rollout as
`sandbox_destroyed` (the model's outcome). `sandbox.root_bound` says which of
the two applied (`enforced` or `watched`).

Three files are in every container and in no container's own file system:
`/etc/hosts`, `/etc/hostname` and `/etc/resolv.conf` are files the runtime keeps
on this machine's disk and mounts in. Over a read-only root they are mounted
read-only. Over a writable root the root user can write them, and no storage
driver counts them, so the harness does, on either kind of host: at every look
it asks the runtime how large the three are and counts them toward `root_size`
(where the storage driver enforces the size of the container's own file system,
the three alone are compared with it). The runtime is asked with its own copy
command and reads its own files; nothing runs inside the container for it, and
nothing is given more rights than the model's commands have.

A root that cannot be measured is not bounded. A container with a writable root
whose size the runtime does not say when it starts is not started
(`sandbox_failed`). Neither is one that holds more than its `root_size` when it
starts, before any command has run in it: the three files alone hold some
hundred bytes, so a size typed without its unit (`8` for `8g`) is below that.
The size is the task's mistake, and `error` names it and what the container
held.

One that can no longer be measured for 30 seconds while it runs, although the
runtime still answers for it, is stopped like one that is over its size and
scored the same way: `sandbox_destroyed`, the model's outcome. That follows
from who can make the measure fail, not from knowing who did. A model
that is root over a writable root can: what it puts where `/etc` was breaks
the runtime's copy of the three files, and very many files make the listed
size slow. Counted as the machinery's, that would be a way to take a rollout
out of training at will. The cost is that a machine can make it fail too: a
runtime too busy to answer two looks in a row within their time, while it
still answers for the container's state, is scored as the model's destroyed
sandbox as well. `sandbox.lost` holds the reason in words ("the runtime could
not say for 30 s how much it holds (...), and a root that cannot be measured
is not bounded", against "it wrote ... bytes to its file system, more than
the ... it may hold"), and a batch builder that wants to keep the two apart
looks there.

The watch has a cost. Every two seconds it asks the runtime five things for
each container with a watched root (the three copies, the listed size, the
look from inside), each a start of the runtime's client: three where the
storage driver enforces the size. Measured on the runner's side that is about
0.05 processor-seconds per second and container, the runtime's own work not
counted. Sixty such containers on one machine are about three processor
cores and 150 process starts a second, and twice that where each task's
sidecar has a writable root too. A read-only root with memory-backed
directories costs none of it.

The watched bound is not a hard bound:

- between two looks the container can write what its disk speed allows;
- a file deleted while a process holds it open is in no listing of the file
  system and takes room all the same. The watch adds such files as far as a
  look from inside shows them, made with the container's own programs as the
  commands' user, never with more rights than the commands have. A root user
  who replaced those programs, a file held open as another user, or a process
  limit used up so that the look cannot start, is not seen.

The look from inside is a process of the container, so none is begun, and
none is under way, while the container is held still before an
[after-run command](#checking-a-rollout-afterwards): it would be counted as
left running and ended with the rest, and a command that is being started at
the moment a container is held keeps the runtime's list of its processes from
answering until the container runs again. What the last look counted stands
in for that time. Everything else the watch asks is asked of the runtime,
which answers for a container it holds still as for any other: held still, a
root stays measured and bounded.

The watch ends with the container and with nothing else. What a program in the
container prints where a size belongs is taken for one only if it can be one,
and a look that fails, whatever the reason, is a look that could not measure:
it falls under the 30 seconds above. A container whose watch is found ended all
the same is stopped before another command runs in it, like one that can no
longer be measured (`sandbox.lost`: "the watch over its size had ended, and a
root that is not watched is not bounded").

A task cannot ask for a writable root without a size.

### What runs outside the container

The session runs outside, and two things the file tools do happen in the
session's process on text the model controls. Both are bounded: an edit whose
result would be longer than `max_read_bytes` is refused before the text is
built (a replace-all multiplies a file), and `diff_file` compares at most 4,000
lines. An image is decoded only for a model with vision, within the size and
pixel limits every image read has.
The rules on tool calls are matched there too, on commands the model wrote;
their patterns are bounded so that a long command cannot stall the harness (see
[rules on tool calls](#rules-on-tool-calls)).

---

## Before the run

A task can name commands that prepare its container before the model starts
(`Task.before_run`, a list of `BeforeRunCommand`): build leftovers and package
caches purged, a service started, a checkout brought to a known state. They are
the counterpart of the [after-run commands](#checking-a-rollout-afterwards).

```python
from turnstone.rollout.preparation import BeforeRunCommand

Task(
    prompt="...",
    before_run=[BeforeRunCommand(["pip", "cache", "purge"], timeout=120)],
)
```

| | |
|---|---|
| When | Once the container is up and the task's files are in it, before any model call, in the order given |
| As whom | Root (`0:0`) unless the command names a `user`, with the container's capabilities and no others, in the task's working directory |
| What the model sees | Nothing. They are not tool calls: they are not in the messages, the tool log or the token record, and the [rules on tool calls](#rules-on-tool-calls) are not asked about them |
| Whose time | Start-up time (`startup_seconds`), like bringing the container up. Neither the task's time limit nor a budget of the model's has started |
| Time limit | Each command its own `timeout`, 300 s by default. The container is kept from outside for as long as all of them may take |
| In the record | `before_run`: every command's exit status, time, and the beginning and the end of what it wrote (see [the episode record](#the-episode-record)) |

A command that ends with another status than 0, or does not end within its time,
means the run does not start. The container is then not what the task describes,
and a model that worked in it would be scored for that. The commands after it
are not run, no model call is made, nothing is checked afterwards
(`verification` is `null`), and the record ends as `before_run_failed`: the
machinery's failure, not trainable. `before_run.failure` and `error` say which
command and why.

A command must return. What it leaves running stays, which is how a task starts
a service of its own, as long as that does not hold the command's output open:
send a service's output to a file. A command that runs out of its time is not
stopped inside the container; the container is removed.

---

## Tools

The session's tools act inside the container through the
[execution environment](tools.md#execution-environment). The executors are
Turnstone's own, so the tools behave as their descriptions say: a process a
command leaves running ends when the command returns, a command past its time
limit is stopped with everything it started, background shells are read with
`bash_output` and stopped with `kill_shell`, an edit needs a prior read.

Offered by default: the tools whose every effect goes through the environment
(`bash`, `bash_output`, `kill_shell`, `read_file`, `write_file`, `edit_file`,
`search`, `diff_file`) and `task_agent`. Their descriptions name the container's
working directory. A call to any other tool is refused before it runs.

Not offered, because they act outside the container
(`turnstone/rollout/toolset.py` holds the reason for each, and a test fails
when a tool is in neither list):

| Tool | Why |
|------|-----|
| `web_fetch`, `web_search` | Requests leave from the machine the session runs on; `web_fetch` also makes a model call of its own |
| `memory`, `recall` | The durable memory store outlives the run |
| `skills` | Reads and writes the skill store; loading one changes the system prompt |
| `notify` | Sends messages to people |
| `watch` | Schedules commands on the host's watch runner |
| `open_preview` | Reads into the operator's preview pane |
| `read_resource`, `use_prompt`, `tool_search` | MCP catalogs and the tool list, which a rollout keeps fixed |

`RolloutConfig.tool_names` overrides the set; a host-side tool named there runs
outside the container.

A rollout offers no MCP tool of a server configured on this machine, and none
can be reached: the only MCP client a rollout ever has is the one built for its
task's [sidecar](#a-tasks-own-servers-the-sidecar).

What bounds a tool's result is each tool's own affair, and a rollout can put
one bound over all of them: see
[one bound on a tool result](#one-bound-on-a-tool-result), which also lists
what bounds each kind of result without it. A file tool reads a file of up to
`max_read_bytes` (`--max-read-bytes`, 256 KiB by default) whole; a larger one
is an error that says so, never a shortened text.

Not yet handled: the exit of a background shell is not announced to the model in
a headless run, although the tool description says a notice follows. The model
has to ask with `bash_output`.

### One bound on a tool result

A rollout has a cap on its whole length, and one tool result must not be able to
use it up. The tools bound their results each in their own way, and some not at
all:

| Result | What bounds it without this setting |
|--------|-------------------------------------|
| A command's output (`bash`), a file comparison (`diff_file`) | The rendering of a bounded capture: half of `max_output_chars`, and never less than 32,000 characters. With the automatic setting and a window of 262,144 tokens that is 104,858 characters |
| The output of a background shell (`bash_output`) | Whole lines up to `max_output_chars`; with the automatic setting, up to a fifth of the context that is left |
| A file (`read_file`) | The whole file up to `max_read_bytes` (256 KiB), with line numbers on top; a larger file is an error |
| A search | 32,768 characters |
| What a sub-agent hands back (`task_agent`) | Its whole last reply, so its reply limit |
| A tool of the task's own servers | Whatever the server sends |
| An error | As a rule short; an edit whose text matches more than once quotes the lines it matched, however long |
| A result inside a sub-agent | 16,000 characters, the beginning kept |

`RolloutConfig.max_tool_result_chars` (`--max-tool-result-chars`) is one bound
for all of them, in characters, applied where a result enters the session's
conversation: no single result is longer, whatever the tool. Unset, nothing
changes.

A longer result is cut, the same way every time, so the same call gives the same
text and the prompt still extends. The cut says what was left out and how to get
it:

- A file read keeps whole lines from its start and ends with
  `[read_file: the result stops after line 55. 121307 of its 122892 characters
  are left out: one tool result holds at most 2000. Read on with offset=56 and a
  limit.]`
- Any other result keeps its beginning and its end, where a command says how it
  went, around `[... 22052 of 23892 characters left out here: one tool result
  holds at most 2000. Narrow the command, or send its output to a file and read
  part of it. ...]`. The advice is the tool's: a narrower query for a search,
  fewer items or a page at a time for a server's tool, a shorter answer or a
  file for a sub-agent. A command's count is of everything the command wrote.

The record has the bound with the other limits (`limits.max_tool_result_chars`)
and counts the results that were cut (`tool_results.cut`, with each one's tool
and size under `tool_results.cuts`); the tool log gives the size a cut result
had (`cut_from`). A cut result is text from the environment like any other: the
rollout stays trainable.

Inside a sub-agent the framework's own bound applies as before; this one is on
what the sub-agent hands back. The bound is at least 1,000 characters: saying
what was cut takes room, and a smaller one is refused before anything runs.

One thing is done to every tool result, with the bound or without it. A tool
can hand back half of a surrogate pair, which a model can write as an escape
in an argument. No request body can carry one, so the next request could not
be sent at all, and the run would end as `error` over a text the model chose.
Each half is replaced by the replacement character (U+FFFD) where the result
enters the conversation, the same way every time; two halves that stand
together become the character they spell. What the model is sent is what
`messages` and the tool log hold, the call's own arguments stay as the model
wrote them, and `prefix_events` gets `unsendable_text_replaced` with the ids
of the calls and the number of halves. A sub-agent's prompt and its own tool
results are repaired the same way on their way to the server.

With prompts as token IDs a half the model wrote never gets as far as a
result: the reply is read here, and a half in a call's name or arguments is
replaced as the call is read (see
[reading a reply without the server](#reading-a-reply-without-the-server)).
The tool is called with the replacement, `messages` and the tool log hold
the call that way, and its result has nothing to repair. The prompts keep
the tokens the model generated, so from that call on they are not what the
chat route would render, and `prefix_events` says so with
`prompt_differs_from_rendering` instead.

### Rules on tool calls

A rollout has nobody to ask, and no reason to protect a throwaway container the
way an operator's machine is protected. It should still not reinforce a few
habits that are destructive anywhere. So a rollout enforces a rule set of its
own on tool calls, the session's and a sub-agent's alike:

- a rule that says `deny` refuses the call before anything runs. The model gets
  an ordinary tool result with a fixed text naming the rule ("Refused by the
  rule '...': ... The call was not run. ..."), the same for the same call every
  time, so the prompt still extends;
- a rule that says `review` lets the call run, since there is nobody to review
  it, and is recorded;
- the first rule that matches decides, and a call no rule matches runs.

The rules are asked about the call that will run, which is not always what the
model wrote. On the way to a tool the session strips white space off the tool's
name, makes arguments of text that is no JSON (a bare text becomes the tool's
main argument, an object that was never closed gives up the value it holds),
reads a null spelled as text as no value, and writes plain dashes and quotes
for typographic ones in a command. A rule sees what comes out of that: `kill`
with an en dash before `9` is judged, and refused, as the `kill -9` it would
run as. `rules.events` names the tool by the name it runs under, and the two
tool logs (`tool_log`, `subagents.tool_log`) hold the same call: `tool` and
`args` are what ran. What the model wrote is in `messages`.

A refused call is the model's doing: the rollout stays trainable. The tool log
names the rule on the call's entry (`rule`, `rule_says`), a sub-agent's call
names it on its entry in `subagents.tool_log`, and the record lists every match
with whose call it was (`rules.events`).

**The rules are not isolation.** They are patterns, and a pattern can be
written around: a command spelled another way, built from parts, or run from a
script file passes. Nothing rests on them. The container's isolation and its
main process are kept by other means, whether a rule fires or not, and what
keeps a model's commands off the machine a rollout runs on is that a rollout
refuses an environment that acts there
(see [where the tools act](#where-the-tools-act)).

The rules are data, in the framework's heuristic rule format and matched by its
matcher, with one difference. A rule sees a call as text: a shell call's
command, a file tool's path, any other call's arguments. A path the model
writes means something in its sandbox only, so a rule on `write_file` or
`edit_file` is matched against the path as written, and nothing is expanded,
resolved or looked up on the machine the rollout runs on (the framework's
matcher would do that for its own sessions, which act on that machine). The
set shipped with the package (`turnstone/rollout/rules.json`, named
`rollout-sandbox`):

| Rule | Says | What it matches |
|------|------|-----------------|
| `wipe-root` | deny | Deleting `/`, `/*` or a whole system directory such as `/usr` or `/etc` |
| `block-device-write` | deny | Writing to a disk device, making a file system on one |
| `fork-bomb` | deny | The self-replicating shell function |
| `signal-every-process` | deny | A signal sent to every process (`kill ... -1`) |
| `power-state` | deny | `shutdown`, `reboot`, `halt` or `poweroff` as a command |
| `move-root` | deny | Moving `/` |
| `chmod-root` | deny | Opening `/` itself to everyone, recursively |
| `pipe-to-shell`, `download-exec` | review | Running what a download sends |
| `destructive-git` | review | Git commands that discard work |

Everything else is a root user's ordinary work in a container of its own and is
left alone: recursive changes of ownership and permissions, installing
packages, stopping its own services and processes, editing system files,
reading its own process information.

A refusal of ordinary work teaches the model something wrong, so where a
pattern cannot tell a destructive command from ordinary work it lets the
command run:

- A rule looks for its program where a command starts: at the beginning of
  the text, after `;`, `&`, `|`, `{`, `$(` or a line break, and as the text a
  shell is told to run (`sh -c '...'`; `bash`, `dash`, `zsh` and `ksh` alike).
  Between that place and the program there may stand up to four of these: a
  word that runs a command (`sudo`, `exec`, `time`, `nohup`, `xargs`,
  `command`, `env`, `nice`, `timeout`) with up to three options or numbers,
  each option with a value or none; an assignment (`X=1`); a keyword of the
  shell (`if`, `then`, `elif`, `else`, `while`, `until`, `do`, `!`); an
  opening bracket. The program is known by its name and by its path in a
  system directory (`/bin`, `/sbin`, and both under `/usr` and `/usr/local`).
  So `sudo -n rm -rf /`, `sudo -u root rm -rf /`, `env X=1 /bin/rm -rf /`,
  `if rm -rf /; then`, `! rm -rf /` and `timeout 5 kill -9 -1` are refused as
  the plain commands are.
- A program that is only named is not refused: after any other word
  (`git commit -m 'never run the command rm -rf / again'`,
  `grep -rn 'then rm -rf /' scripts/`, `ssh host rm -rf /`), in brackets or
  backticks inside a text (`'remove the cleanup (rm -rf /)'`), under another
  path (`./rm`, `/opt/tools/bin/kill`). Nor is what a pattern does not see
  into: a text handed to a program that is no shell (`su -c '...'`,
  `eval '...'`), a command substitution in backticks, a root path that is
  quoted or set apart (`rm -rf "/"`, `rm -rf -- /`), more words before the
  program than the ones above.
- A quote right after the command ends it only in the text a shell is told to
  run, within that text's first two hundred characters
  (`bash -c 'cd /tmp && rm -rf /'`). Anywhere else the quote says that the
  command stands in a text, which runs (`echo 'first; rm -rf /'`). What still
  cannot be told from a command is refused although it may be text: the
  program after `;`, `&`, `|` or a line break inside quotes, with more text
  after it (`echo 'first; rm -rf / second'`).
- `mv` with the option that names the directory to move into (`mv -t / file`,
  `--target-directory /`) moves a file into the root, not the root, and runs.
- A power command is looked for more narrowly, by its name alone: at the
  beginning of the text, after `;`, `&`, `|` or `(`, after `sudo` or `exec`,
  on a line of its own, and in brackets on one line (`(cd /; reboot)`). After
  other words it runs (`nohup reboot`, `sudo -n reboot`, `then reboot`,
  `/sbin/halt`). It is not taken for a command on a line inside a text in
  quotes, or where it is the label of a case branch (`reboot)`,
  `halt|poweroff)`).
- `mkfs` and `wipefs` are refused on a device (`/dev/...`), not on an ordinary
  file that is to hold a file system.
- The lines of a here-document are text: once a command holds `<<`, a power
  command on a line of its own further down is let through, also after the
  here-document has ended, which a pattern cannot see. On the line it stands
  on it is still refused.
- `/opt` is not one of the system directories: what is installed there is the
  task's to remove.

`RolloutConfig.rules` takes another set (a `RuleSet`, or the path of a rule
file; `--rules PATH` on the command line) or `None` (`--rules none`) for no
rules; `RuleSet.shipped().with_rules([...], name=...)` puts a run's own rules
in front. The record names the set in force by name and by a hash of its
content (`rules`), so batches collected under different rules can be told
apart.

**Every repeat in a pattern has a limit.** A match runs in the session's own
process, on a command the model wrote, and holds the interpreter while it
runs. A pattern with an unbounded repeat (`*`, `+`, `{2,}`) can take time that
grows with the square of the command's length or worse, and a model could
stall the harness with one long command. A rule set therefore refuses such a
pattern when it is built, the shipped set's and a run's own alike: write
`{0,8}` for `*` and `{1,8}` for `+`. The other side of it: a shipped pattern
looks a bounded distance (eight spaces between words, eight options, a few
hundred characters between a download and what runs it), and a command
stretched beyond that is not matched. A line break separates commands as a
semicolon does; the power commands are looked for on a line of their own after
at most two thousand characters of a command, where a text in quotes of up to
two thousand characters counts as one and a longer one ends the look.

Two things do not apply inside a rollout. The fixed list of patterns that
protects the machine a session runs on is not asked in the container: it
refuses ordinary work there, such as any recursive change of ownership or any
command holding the word `shutdown`. On the host it applies as before. And the
model-based judge never runs: a rollout session builds none, because its model
calls would go out on a client of its own, past the recorder.

### Sub-agents

A rollout offers `task_agent`, and for the training sample it is a tool like any
other. The call that starts a sub-agent is the session's own, generated and
recorded; what the sub-agent returns is a tool result, text from the
environment. The sub-agent's own conversation is not part of the sample.

| | |
|---|---|
| Where it acts | In the same container, through the same environment |
| Its tools | The environment tools only; it cannot start another sub-agent, and it is not offered the tools of the task's sidecar (a call it makes to one anyway is refused) |
| The parent's prompt | Unchanged by starting one: the system prompt and tool list are fixed for the run |
| Its model calls | Kept out of the token record and the prefix check. The side record under `subagents` is marked `not_for_training` and `token_data_kept: false`: it holds counts and a summary per call, no token IDs or log-probabilities, and none of the checks behind `trainable` is made on them. With [prompts as token IDs](#prompts-as-token-ids) they go out on the same route as the session's, each prompt the server's rendering of the sub-agent's own messages, and no token of them enters the session's sequence |
| Its tool calls | In the record under `subagents.tool_log`, apart from the session's `tool_log` and, like everything under `subagents`, not part of the sample |
| Its time | Counts toward the task's time limit |
| Limits | `max_subagents` per rollout (a call past it gets a result that says no sub-agent was started), the framework's turn limit for each (`subagent_max_turns`), the framework's reply limit for each of its model calls (with prompts as token IDs cut to what the context window has left after the sub-agent's own prompt), and the task's token budget, which its tokens count toward |
| The rollout's length | `generated_tokens` counts what the session itself generated; sub-agent tokens are under `subagents` and never added in |

What bounds the work sub-agents can do is therefore the number of sub-agents
times the turn limit times the reply limit, the task's time limit, and the
task's token budget where it has one. With the framework's default of no turn
limit and no token budget, the time limit is the bound. The tokens sub-agents
generate are counted in the record.

A sub-agent acts in the same container, so a command the model would not run
itself can be handed to one. A reward that counts what a rollout did therefore
reads two logs. `subagents.tool_log` holds every tool call a sub-agent's model
issued, sub-agent by sub-agent:

| Key | Meaning |
|-----|---------|
| `agent` | Which sub-agent: its number in the order they were started. The entry of the session's `tool_log` that started it carries the same `agent`, and in its `args` what was asked of it |
| `turn` | The sub-agent's own turn that issued the call, counted from 0 |
| `call_id` | The call's id: the id of the session's call that started the sub-agent, then the sub-agent's own. `rules.events` names calls by it |
| `tool`, `args` | The tool and its arguments as the tool got them |
| `result`, `ok` | The first 500 characters of the result, and whether the call succeeded; both `null` for a call the run ended on |
| `rule`, `rule_says` | The rule that matched the call, where one did |

A call that never ran is there too: one a rule refused, a tool the sub-agent is
not offered, an attempt to start a sub-agent of its own. A call the run was cut
in has what the session had noted of it when it was stopped.

A sub-agent is not offered the tools of the task's own servers. Those act on
the task's state, which is what the rollout is scored on; a call a sub-agent
makes to one anyway is refused, and the attempt is in `subagents.tool_log`.

A sub-agent that does its task badly is an ordinary result: the model delegated
and has to deal with what came back. That includes one whose model returns
nothing, one that fills its own context window, one that reaches its turn limit,
one that asks for more when the task's token budget is used up, and a call
refused at the sub-agent limit. None of these makes the record untrainable;
they are the model's doing. It is the same when a sub-agent had already written
some text before its model returned nothing or its context was full: the kind
of failure is read from the whole chain of causes, not from the wrapper the
session puts around it.

A sub-agent whose conversation the server refuses is an ordinary result as
well ([above](#a-conversation-the-server-refuses)). Its first message is the
prompt the model wrote, and a chat template can refuse one: a prompt that
reads as a tool response leaves it without a user query, for example. The
model gets `Task error: the model server refused the sub-agent's conversation
(HTTP 400): ...` with the server's own words, 300 characters of them at most,
and after it whatever text the sub-agent had written before. The run goes on.
The record keeps the reason in `subagents.failures` and counts it in
`subagents.refused_by_server`.

A sub-agent that fails for a reason that is not its work is different: a
request that failed for the server's own reasons, a harness error inside it,
or a failure of the session around it after it had returned. The session
would hand the model a failed tool result, and the rollout would be scored on
a failure that is not the model's. Such a rollout ends there (`finish_state:
subagent_failed`) and is not trainable; `subagents.failures` says what
happened, and `problems` repeats it.

That holds for what goes wrong while the run is under way. Once the session
has been told to stop (the time limit passed, a budget ran out, the sandbox
went away), what fails around a sub-agent follows from the stop: at the time
limit the session's client is closed, and a sub-agent's model call under way
dies with it. Such a failure is not counted, and the ending stays what the
stop made it; the call that died is still in `subagents.calls`.

---

## A task's own servers: the sidecar

Some tasks come with systems of their own, mock services with their state,
served as MCP servers. `Task.sidecar` describes a second container for them:

```python
from turnstone.rollout.container import ContainerSpec
from turnstone.rollout.sidecar import Sidecar

Task(
    prompt="...",
    sidecar=Sidecar(
        container=ContainerSpec(image="...", user="0:0", read_only_root=False, writable={}),
        files={"/srv/server.py": b"..."},  # put in before the setup command
        setup=["sh", "-c", "start-the-servers"],  # must return; what it starts stays
        wait_ports=[39101],  # must be listening before the run
        mcp_servers={"ledger": "http://127.0.0.1:39101/mcp"},  # as seen inside
    ),
)
```

What reaches what:

| From | To the sidecar's servers | How |
|------|--------------------------|-----|
| The session (outside both containers) | yes, and it is the only client | A listener on this machine's loopback interface hands each connection to a small relay started inside the sidecar through the runtime, which connects to the server's port there. The listener carries a connection only when its first request names a secret made for this rollout, which the session's own client sends with every request (the header `x-turnstone-forward`). That header is taken out before the request goes on, and each connection carries one request (the client is told so, and the request goes on with `Connection: close`), so the task's own server is never sent the secret |
| Another user of this machine | no | A loopback port is open to every user of the machine, but a connection that does not name the rollout's secret is closed before anything is started for it. What such connections can hold of the runner is bounded: the head of a request has 20 seconds to arrive, all of it together, and at most 16 connections are read for their head at a time; the others wait in the listener's queue. Connections that keep every place taken delay the session's own client, which waits its turn, but are not carried |
| The model's container | no | Neither container has a network; they do not share one |
| The sidecar | nothing outside itself | No network: no route to this machine, to other rollouts or to the model's container |

The model uses the servers as tools, through the session, and every call is in
the tool log like any other tool's. It is not given a way to connect to them
itself: it would then act on them unseen, and could reach whatever else the
sidecar serves, such as a task's own health or grading endpoint. A task format
that ships a bridge for programs in the model's container does not need it
here. A sub-agent is not offered these tools (see [sub-agents](#sub-agents)).

The MCP client is built from the servers the task names and from nothing else.
Its tool list is read once, when the session is built, and is the same with
every request, so the prompt still extends. It is shut down when the rollout
ends.

The relay needs one of `python3`, `socat` or `nc` in the sidecar's image.

What a model can do to a sidecar is call its tools, with any arguments. That
changes the task's state, which is the task. It cannot start a process there or
stop the container. So:

| What happened | Whose doing | `finish_state` |
|---------------|-------------|----------------|
| A tool call the model got wrong; the server answered with an error | model | an ordinary tool result |
| The sidecar's container, setup command, ports or servers did not come up | machinery | `sidecar_failed`, not trainable |
| The sidecar stopped during the run | machinery | `sidecar_lost`, not trainable |

A sidecar is a container like the episode's: its main process is kept the same
way, it is removed afterwards with the removal confirmed, and the process that
removes it when the runner is killed is given the same times. The record
describes it under `sandbox.sidecar`.

After the run the sidecar is stopped together with the episode's container, and
a verifier is given its name (`context.sidecar_name`), to read from it what the
model's calls made of the task's state. Both containers are removed afterwards.

Bringing the sandbox up, a sidecar's setup included, is not the model's time:
the task's time limit starts when it is up, and the record says how long it
took (`startup_seconds`).

A task's files can be bytes as well as text (`Task.files`). Either kind is put
where its path says: a relative path in the working directory, an absolute one
as it is, owned by the user the model's commands run as.

---

## Checking a rollout afterwards

A reward needs a check the model could not have prepared for. A model with root
in a writable container can change everything a check inside that container
relies on: the test script, the test runner, the shell, the interpreter.

**The general form is a verifier**, a function the caller gives `run_episode`
(a caller that starts the command as a process names a program instead, a
[verifier command](#a-verifier-command)):

```python
from turnstone.rollout.verification import VerificationContext

def verifier(context: VerificationContext) -> dict:
    ...  # read from the stopped container, check, decide
    return {"verdict": "pass", ...}

record = run_episode(task, config, verifier=verifier, verifier_timeout=3600)
```

When the run is over, however it ended, the harness stops the episode's
container, so that nothing the model left running can change a file any more,
and calls the function. A run that did not start is the exception: nothing of
the model's is there to check, so neither the after-run commands nor the
verifier run, and `verification` is `null`. The context names the stopped
container
(`container_name`), which the runtime's own commands can read without running
anything inside it. What the function returns is stored under the record's
`verification`; the model never sees it. Then the episode's container and every
container the function started through the context are removed, with the
removal confirmed, whether the function returned, raised or ran out of its
time.

Helpers a verifier can use (`turnstone.rollout.verification`):

| Helper | What it does |
|--------|--------------|
| `context.export(paths, max_bytes=...)` | Reads paths out of the stopped container with the runtime's own copy, into memory. Links and special files are noted, never followed and never made on this machine. More than `max_bytes` sets `too_large`, as soon as that much has arrived and without waiting for the rest. A path that cannot be read although the container is there (nothing at it, or something above it that is no directory) is listed under `skipped` |
| `context.clean_container(spec)` | Starts a container the model never touched, from a `ContainerSpec`, with the same isolation as an episode's |
| `clean.put(dest, files=..., tree=...)` | Puts in what was exported (`tree`) and then the files the task holds that the model never saw (`files`). The task's files win: a link, a file or a directory of the model's that stands in the way of one of them, or of a directory above it, is removed first. A link is never followed, so nothing of the task's lands where a link of the model's points |
| `clean.run(argv, user=..., timeout=...)` | Runs a command there: exit status, bounded output, time |
| `clean.read(paths)` | Reads files out of the clean container |

**The simple case** is `Task.after_run`: commands run in the episode's own
container, as root, after everything left running there was ended and before
the container is stopped, each with its exit status, bounded output and, if
asked, named files read out. It is only as good as the container. Use it where
the model could not change the programs the command relies on: an unprivileged
user over a read-only root. Do not use it to grade a model that had root over a
writable root; check that in a clean container. A memory-backed directory (the
default working directory) is gone once the container stops, so reading it is
an after-run command's job.

An after-run command is started in the task's working directory, like the
model's own commands. The read of the files it names is not, and neither is
anything else the harness runs in a container for itself (the look of the
[size watch](#what-bounds-writes), the end of a command that ran out of its
time): they start in the root directory. A working directory can be renamed
or removed by a user who owns the directory above it, and the runtime starts
nothing in a directory that is not there. That then costs the model its own
commands and the after-run command, whose exit status says so, and no more:
the read starts, reads what is there and passes over what is not. A relative
path among the files to read is read below the working directory and keeps
the name the task gave it; an absolute one is named by its place below the
root (`/etc/hostname` as `etc/hostname`). The default container's working
directory is a mount of its own that its user can neither rename nor remove.

**What is left running is ended before each after-run command**, whoever it
runs as: a process the model detached from its command, one a before-run
command left, one an earlier after-run command left. Root in the container
cannot do that (the container gives it no right to signal another user's
processes), and nothing is run in the container with more rights than the
container has. So the runtime holds the container still, lists its processes
from outside, and for every user that has one a helper container ends that
user's processes as that user: it shares the container's process namespace
and nothing else, has no capability, and comes from the image, not from files
the model wrote. One helper ends all of a user's processes whatever their
groups. They are listed again, at most three times, and the
container is let run again. Held still, a process can neither start another,
nor end the helper first, nor keep the helper from starting by holding every
process the container may have. A process that is over and only waits for its
parent to collect it is not one of them: it is not counted, and nothing has to
be done about it. One whose first thread has ended while another thread runs
is listed in the same state and runs on; its count of threads tells it from the
first kind, and it is ended and counted like any other. Each command's result
says what was found
(`leftovers`: `found`, `still_running`, `frozen`).

The container is let run again whether or not the runtime confirmed that it
holds it still (`frozen`). The runtime carries out a hold it was asked for
even when its answer comes too late, so the release is asked for every time;
a refusal for a container the runtime does not count as held is no failure,
and where the hold was never confirmed it is asked for once more and ended,
in case the runtime left it half done. A container that is still held after
that can run nothing more: the check has failed, the machinery's, and
`verification.failure` says that the runtime did not let it run again, with
the runtime's own words. The record keeps the ending its run had.

The list is asked for numbers and a state only (`pid`, `ruid`, `rgid`, `nlwp`,
`stat`): nothing a process writes itself is in it, so nothing a process writes
can make it unreadable. A process's name is asked apart, and only to be shown
in `still_running`. A user or group number above 2147483647 is printed in the
list as a negative number and is read as what it is. The runtime starts a
container as no user or group above that number. For a group that costs
nothing: the helper is that user under another group, and a signal goes by
the user. For a user it means that nobody can be asked to end the process as
its user, and it stays.

Whose doing a process still there after the tries is follows from the task's
settings, as for the [main process](#the-main-process). It is the model's own
outcome, listed in `still_running`, with the after-run command run beside it
and the rollout still trainable, in two kinds of container:

- commands that run as root over a writable root: the programs in the
  container are the model's to change;
- commands that run as root with the `SETUID` capability: they can run a
  process under any user number, also one the runtime starts no helper as,
  and under more users than the step has time for.

Anywhere else the check has failed. Root without `SETUID` keeps its user, and
so does any other user a task names that capability for; `SETGID` changes
groups only. A task whose after-run commands are to be relied on gives its
commands neither of the two, or scores a record by its `still_running`.

Holding the container still, listing and ending have 45 seconds together, and
every question to the runtime is given what is left of them. When they are
over no helper is begun any more: the processes are listed one last time, in
time kept back for that, and what that list holds is what stayed. It is
scored like any process that stayed: the model's own outcome in the two kinds
of container above, a failed check anywhere else, where only a runtime too
slow for its helpers explains it. The helper is removed by its name when the
step is over, whatever became of it, and letting the container run again has
a time of its own after that.

The step needs a runtime that can hold a container still and whose list of a
container's processes takes the options of `ps` with the columns above and
`comm`.

Whose doing a result is:

| What happened | Whose doing | The record |
|---------------|-------------|------------|
| The check ran and failed, or its own command ran out of time | model | trainable; the verdict says so |
| The export or the files read out held more than will be read (`too_large`), a file of a size no disk holds included | model | trainable; the verifier decides the verdict |
| An exported path cannot be read because of what stands there (`skipped`) | model | trainable; the verifier decides the verdict |
| Reading out the files an after-run command names failed in any way (its time ran out, the archive program was ended or failed, what it wrote is no archive), in a container whose programs the model could change (root over a writable root) | model | trainable; the command's `collected_summary.failed` says why |
| Reading them out was cut short in any other container (its time ran out, or the archive program was ended, as when the container's memory is used up) and the container still answers at once: what the model decides there is how much stands at the paths | model | trainable; the command's `collected_summary.failed` says why |
| A process left running could not be ended before an after-run command, in a container whose programs the model could change (root over a writable root) or whose commands can change their user (root with `SETUID`) | model | trainable; the command's `leftovers.still_running` lists it |
| The model ended its container, during the run or with something it left behind for the check | model | trainable (`sandbox_destroyed`); `commands_not_run` says why the after-run commands did not run |
| The verifier raised, ran past `verifier_timeout` or returned no `verdict`; a verifier command printed no verdict, ended with another status than 0 or ran past its time | machinery | not trainable: no reward exists |
| The container could not be stopped before the check, a clean container did not start, reading files out failed where the model could not have caused it (no archive program in the image, one that fails or writes no archive, a container that no longer answers), what was left running could not be listed, it could not be ended in any other container than the two above, or the container was held still for that and the runtime did not let it run again | machinery | not trainable |

Three things for the author of a verifier:

- **Bound every step by something the model does not control.** A verifier that
  lets the model's files decide how long it runs (a test suite the model wrote
  to, a build of its tree) can run past `verifier_timeout`, and that is counted
  as the machinery's failure: the rollout is lost to training, which a model
  could bring about. Give each command its own `timeout` and judge a step that
  runs out as failed.
- **A clean container has a size too.** One with a writable root is bounded
  like an episode's: by its spec's `root_size`, and by `10g` where the spec
  names none (see [what bounds writes](#what-bounds-writes)). A step that
  writes more than that there, and a build or a test of the model's code
  decides how much it writes, gets the container stopped; the next call on it
  (`put`, `run`, `read`) raises `ContainerLostError`
  (`turnstone.rollout.container`, an `OSError`). A verifier that lets
  that out has raised, which is the machinery's failure: the rollout is lost
  to training, and a model could bring that about. Catch it and return a
  verdict of failure, or give the clean container a size, or memory-backed
  directories over a read-only root, that the graded code cannot outgrow
  unnoticed.
- The times a clean container and the stopped containers are kept for follow
  the verifier's time limit, not the run's.

### A verifier command

A caller that starts the command as a process has no function to give. It names
a program instead (`run_episode(verifier_command=[...])` from Python):

```
turnstone-rollout ... --task task.json \
    --verifier-command 'python3 grade.py tasks.jsonl -- {key} {container}' \
    --verifier-timeout 1800 --out episode.json
```

Once the run is over and the episode's container is stopped, the program is
started on this machine, without a shell: the text is split into arguments by
shell quoting rules, and nothing else of a shell applies. In each argument
`{container}` becomes the stopped container's name, `{sidecar}` the stopped
sidecar's and `{key}` the task's key. The program reads from the stopped
containers with the runtime's own commands, tests what it read wherever it
likes, and prints its result.

A value is put in after the text was split, so a key is one argument whatever
it holds: spaces, quotes, a line break, a dollar sign, the text of another
placeholder (the key goes in last and is not filled in a second time). It is
still the task's text. A key that begins with a dash is read as an option by
a program that takes options, so the command puts `--` before it, as above.
And the program's own name takes no placeholder: a command whose first
argument holds one is refused, from the command line and from Python alike
(`SetupError`), and nothing is filled in there, because a task's key must not
choose what is started on this machine.

| What the program does | The record |
|-----------------------|------------|
| Prints one JSON object whose `verdict` is not null, and ends with status 0 | `verification.result` is that object. The rollout stays trainable whatever the verdict says: a check that ran and failed is the model's outcome |
| Prints anything else: nothing, text, two objects, an object without a `verdict` or with a null one | No verdict exists. `verification.failure` says so and the record is not trainable |
| Ends with another status than 0 | The same, whatever it printed |
| Does not end within `--verifier-timeout` (3600 s by default) | The same; it is killed with everything in its process group |
| Cannot be started | The same |

The time limit bounds the whole call. When the program has ended, whatever it
left running in its process group is ended too, and its output is not waited
for beyond that: the verdict is what the program itself printed. A process the
program moved into a session of its own is the program's to end.

**What the model did is a verdict of failure, never a missing verdict.** The
program reads what a model left behind, and whatever the model did to it is
the model's outcome, as it is for a verifier function
([above](#checking-a-rollout-afterwards)): a working directory that was moved
or removed, a file that is not there, cannot be read or is a link, more than
the program will read, a test that does not end on the model's code. Each of
them is a verdict (`"verdict": "fail"`, with the reason beside it). A program
that printed no verdict for them would let a model take a rollout out of
training by renaming a directory.

No verdict is only for a failure of the program's own: an image it needs is
missing, the runtime does not answer, its list of tasks has no such task. It
says so by printing no verdict, by printing `"verdict": null`, or by ending
with a status other than 0. The rollout is then left out of training, where a
verdict of failure would score the model for something it did not do.

`verification.program` says what happened: `argv` as it was started,
`exit_code`, `timed_out`, `seconds`, and the beginning and the end of what the
program wrote to standard error (`stderr`) and, where its output was no
verdict, to standard output (`stdout`), with the bytes left out between them
counted.

Afterwards everything is removed as with a verifier function: the episode's
container and the sidecar, with the removal confirmed. Containers the program
starts are its own to remove, also when it is killed at its time limit.

The program runs with the rights of the process that started the rollout. It is
the operator's: it is given on the command line and never comes from a task
file. A function and a command are not both given. A run that did not start is
not checked.

---

## The episode record

`run_episode` returns a JSON-safe dict. Every key below is always there; a key
whose value does not apply is `null`.

| Key | Value |
|-----|-------|
| `key` | `Task.key` |
| `task` | The task as it was given: `prompt`, `key`, `system_prompt`, `setup_files`, `files`, `max_turns`, `timeout`, `max_generated_tokens`, `max_command_seconds`, `container` (the `ContainerSpec`), `before_run`, `after_run`, `sidecar`. Content held as bytes is replaced by `{"bytes": N}` |
| `model`, `endpoint` | The model's name and the API root the requests went to |
| `finish_state`, `finish_cause` | How the rollout ended, and whether that is the model's outcome (`model`) or a failure of the server, the harness or the runtime (`machinery`); see [how a rollout ends](#how-a-rollout-ends) |
| `error` | `null` where the model ended its turn by itself (`completed`). For every other ending, what happened, in words: the limit that was reached and where the last reply was cut, the request that failed or was refused, why the sandbox is gone or did not start, a sub-agent's failure |
| `trainable`, `one_sequence` | See [above](#trainable-and-one-sequence) |
| `problems` | Why `trainable` is false; empty when it is true |
| `warnings` | Operational trouble that does not touch the data, such as an unconfirmed removal |
| `turns` | Model calls of the session that ended normally |
| `generated_tokens` | Tokens the session itself generated |
| `final_content` | The text of the session's last assistant turn, `""` when it had none |
| `messages` | The system prompt and the final conversation, one entry per message: `role` and `content`; on an assistant turn also `reasoning` (the turn's reasoning as text, `null` where it had none) and, where the turn made calls, `tool_calls` (each `id`, `type` and `function` with `name` and `arguments`); on a tool result `tool_call_id`. What the session keeps on a turn for itself, under keys that begin with an underscore, is left out. A tool call's arguments are the text the model wrote for each, as they are sent back. With prompts as token IDs the reply was read here: the text of a turn with calls is stripped, and a call's id is made here (`call_` and 24 hexadecimal digits) |
| `tool_log` | The session's tool calls in order, each `tool`, `args` (as the tool got them), `result` (first 500 characters), `ok`, `turn`, for a call a rule matched `rule` and `rule_says`, on the call that started a sub-agent `agent`, the number that sub-agent's calls carry in `subagents.tool_log`, and on a result that was cut to the [bound on one result](#one-bound-on-a-tool-result) `cut_from`, the characters it had. Each of these is noted call by call, whatever became of the run's other calls. The calls of a reply the run ended on (a reply cut at a limit) never ran and have no entry; a call over the number that run in one turn has an entry that says it was not run. A call that was running when the time limit passed has its entry too, where the session stopped within the ten seconds it is given, with what the session noted of it then: `Cancelled by user. Outcome UNKNOWN ...` and `ok: false`, or for a delegation `(task cancelled by user before completion) ...`, where `ok` is `true` although the sub-agent did not finish (the framework reports a cancelled delegation that way; read the result). That result is also the last message of `messages`, a tool result the model never saw |
| `tool_results` | The results that were cut to that bound: how many (`cut`), and for each its `tool` and the `chars` it had (`cuts`) |
| `usage` | The session's own token counts as the server reported them: `prompt`, `completion`; both 0 in a run that did not start |
| `tokens` | The token record, below |
| `prefix_ok`, `prefix_breaks` | The token-level prefix check: whether it held, and one readable line per break |
| `prefix_events` | What the stable-prefix run noted, one entry per `event`; with prompts as token IDs also what the prompt assembly noted, about the session's own calls only: `turn_ended_on_other_token` (`turn`, `token_id`, `end_of_turn_token_id`), `prompt_differs_from_rendering` (`call`, `prompt_tokens`, `rendered_tokens`, `same_tokens`) and `marker_place_uncertain` (`call`, `tokens`), with `turn` and `call` counted as `tokens.calls[].call` |
| `calls` | One summary per model call of the session that ended normally, in order: `index`, `scope`, `route` (`chat`, or `completions` with prompts as token IDs), `window_left` (with prompts as token IDs, the tokens the context window had left for the reply; otherwise `null`), `streamed`, `request` (`model`, the count of `messages`, their `roles`, the names of the `tools`, the `sampling` keywords, the `extra_body`; with prompts as token IDs the `extra_body` is the completions request's, and `template_arguments` and `prompt_tokens_sent` are there too), `finish_reason`, `usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`), `complete`, `error`, `aligned`, `misaligned_chunks`, `ids_checked`, `chunks`, `seconds`, `longest_silence`, `problems`. `index` numbers every request the run made, from 0, a sub-agent's and a discarded one included, so the session's calls need not be 0, 1, 2; the token record numbers the calls listed here from 0 (`tokens.calls[].call`), and entry *n* of the one list is entry *n* of the other |
| `discarded_calls` | The session's calls that did not end normally (cut by the time limit, failed, or broken off and issued again), the same keys with `prompt_token_ids`, `token_ids` and `logprobs`; `index` as in `calls` |
| `text` | With `keep_text`, each call's generated text; otherwise `null`. With prompts as token IDs it is the completions route's text, which leaves out the end-of-turn token and other special tokens |
| `limits` | `max_tokens`, `context_window`, `tool_timeout`, `subagent_max_turns`, `max_output_chars`, `max_tool_result_chars`, each `{"value": ..., "source": "..."}` |
| `sampling` | Below |
| `logprobs` | Which log-probabilities the server reports: `sampler_logprobs` (`true`, `false` or `null`), `reflects` (the same per setting: `temperature`, `top_k`, `top_p`), `reason`, and from a probe `route` (the route it asked on), `tried` and `positions` |
| `token_id_check` | `"required"` or `"not required"` |
| `prompt_mode` | `"messages"`, or `"token_ids"` for a run with [prompts as token IDs](#prompts-as-token-ids) |
| `token_id_route` | `null` for a run that sends messages; otherwise what the check before the run found, below |
| `server_version` | `null` for a run given no address to ask; otherwise the [server's version](#one-version-of-the-server) at both ends, below |
| `timeout` | `null` unless the time limit was reached; then what the run was waiting on, below |
| `seconds`, `startup_seconds`, `teardown_seconds` | The time from the start of the sandbox to the end of the run; of that, the time bringing the sandbox up took, the task's before-run commands included; and the time removing it took, which comes on top |
| `before_run` | `null` for a task that names no [before-run commands](#before-the-run); otherwise below |
| `time` | Where the time went, below |
| `budgets` | The budgets the run was under and what was spent, below |
| `subagents` | The side record of sub-agent calls, below |
| `sandbox` | The container, below |
| `acts_on_host` | Whether the tools acted on the machine the rollout ran on instead of in a sandbox (see [where the tools act](#where-the-tools-act)) |
| `rules` | `null` without rules; otherwise the set in force (`name`, `sha256`, the count `rules`) and `events`, one per call a rule matched: `call_id`, `tool`, `rule`, `recommendation`, `refused`, `scope` (`parent` or `sub_agent`) |
| `verification` | `null` when no check was asked for, and for a run that did not start; otherwise below |

`task.before_run` and `task.after_run`, one entry per command as the task gave
it:

| Key | Value |
|-----|-------|
| `argv` | The command and its arguments |
| `user` | The user it runs as, `0:0` unless the task says otherwise |
| `timeout` | Its time limit in seconds |
| `max_output_chars` | The most the record keeps of what it wrote |
| `collect` | After-run only: paths read out of the container once the command has ended. What they held is in `verification.commands[].collected` |
| `max_collect_bytes` | After-run only: the most that is read out of those paths together, 8 MiB unless the task says otherwise; more than that sets `collected_summary.too_large` |

`prefix_events`, one entry per `event`. `turn` counts the session's turns
from 0, as `tool_log[].turn` does, and `call` the session's calls, as
`tokens.calls[].call` does:

| `event` | Its other keys | What happened |
|---------|----------------|---------------|
| `raw_arguments_unrecovered` | `turn`, `call_ids` | The text the model wrote for these calls' arguments could not be read from the reply, or does not mean the parsed arguments. The calls are sent back from their parsed values |
| `arguments_legalized` | `turn`, `call_ids` | These calls' arguments were no JSON object, and are sent back as one |
| `tool_calls_over_limit` | `turn`, `calls`, `limit` | The turn made `calls` tool calls and the first `limit` ran. The others stay in the turn and got a result that says they were not run |
| `ended_at_length` | `turn` | The turn's reply was cut at a limit with a tool call in it. The run ended there and the call did not run |
| `compaction_skipped` | none | Something asked for the conversation to be shortened to a summary, which a run with a stable prefix does not do |
| `unsendable_text_replaced` | `call_ids`, `characters` | The results of these calls held half of a surrogate pair, `characters` of them together, which were replaced (see [one bound on a tool result](#one-bound-on-a-tool-result)) |
| `turn_ended_on_other_token` | `turn`, `token_id`, `end_of_turn_token_id` | The turn ended on `token_id`, another token than the template's end-of-turn token (`end_of_turn_token_id`), and the next prompt was built with the template's closing whole after it |
| `prompt_differs_from_rendering` | `call`, `prompt_tokens`, `rendered_tokens`, `same_tokens` | The prompt of this call is not what the chat route renders for the same conversation: it holds `prompt_tokens` tokens, the rendering `rendered_tokens`, and the two share their first `same_tokens`. Noted once the call has returned a whole reply |
| `marker_place_uncertain` | `call`, `tokens` | In this call's reply `tokens` marker tokens were placed in their part's text by the reader's rule, because the part did not say which occurrence of the marker's text the token was |

The last three are noted by the prompt assembly: only with
[prompts as token IDs](#prompts-as-token-ids), and only about the session's
own calls. What is noted of a sub-agent's reply is under `subagents.events`.

`logprobs.tried` and `logprobs.positions`, from a probe:

| Key | Value |
|-----|-------|
| `tried` | One entry per probe prompt that was asked: `prompt` (its text), `likeliest` (the log-probability of the likeliest first token in the control request), `used` (`false` where no measurement was made with the prompt: the model was too sure of that token, and the prompt was either not needed again or held no later token that could be measured) and, in an entry that was used, `position` (the position of the reply its settings were measured at, counted from 0: 0 is the first token, a later one is the first the model was unsure of) and, for each measurement made with that prompt, the value the server reported for the sampled token at that position under the setting's name: `top_k`, `top_p`, `temperature`, and for the temperature `temperature_slope` (the slope found; `null` where fewer than two alternatives could be compared or they span less than one unit) and `temperature_alternatives` (how many alternatives it is the slope of). An entry measured at a later position also holds `likeliest_at_position` (the control's value for the likeliest token there) and `first` (for each setting that position decided, what the first position showed: `true` after the setting, `false` before it, `null` neither). An entry that was looked at beyond its first token and not used holds `certain_positions`: how many positions of the control's reply, from the first on, the model was certain of along its likeliest tokens, which is all the reply had where the model was certain of every token |
| `positions` | What the positions other than the one that decided said, per check: `top_k` and `top_p` (each cut-off asked over several positions) and `whole` (the alternatives listed at each position as one whole distribution), each with `checked` (positions looked at) and `contradicting` (positions that contradict it). A check that was not made is absent |

`sampling`:

| Key | Value |
|-----|-------|
| `requested` | The sampler settings the requests themselves carried (`temperature`, `top_k`, `top_p`, whatever else the request parameters held) |
| `temperature`, `reasoning_effort`, `extra_body` | What the run was set to |
| `sources` | Where each of those three came from |

`time`, in seconds:

| Key | Value |
|-----|-------|
| `own_model_seconds`, `subagent_model_seconds` | The time inside the session's own model calls, and inside its sub-agents' (sub-agents' calls side by side are added up) |
| `longest_server_silence` | The longest any call waited for the server's next part |
| `model_seconds`, `command_seconds`, `harness_seconds` | The session's run, split three ways: a model call was under way (the session's or a sub-agent's); else a batch of tool calls was; else neither |
| `model_calls`, `prompt_tokens`, `generated_tokens` | What the server was asked to do, sub-agents included. A request that never went out is not among it: one the token budget refused, one whose prompt could not be sent, and what the session tried after the time limit, when its client was closed. Such a request still has its entry in `discarded_calls` or `subagents.calls` |
| `prompt_seconds`, `generation_seconds` | The time up to each call's first token, and from there to its end, over the calls that got as far as a first token |
| `generation_tokens_per_second`, `prompt_tokens_per_second` | The server's measured speeds over those calls; `null` where nothing was measured |

Where the run did not start, only the first three keys are there.

`token_id_route`, with prompts as token IDs:

| Key | Value |
|-----|-------|
| `ok`, `reason` | Whether the server can be sent prompts as token IDs, and the first check that failed. In a run that did not start before the check was made, `ok` is `null` and `reason` says so |
| `format`, `template_arguments` | The reply format the check was made for, and the template arguments it rendered with |
| `checks` | What each step found: `tokenize_renders_messages`, `chat_route_renders_the_same`, `turn_is_a_prefix_of_the_longer_rendering`, `turn_ends_with_the_closing`, `rendering_ends_with_the_generation_prompt`, `markers_kept_in_text` (per marker: `true`, or `null` where it was not seen), `completions_route` (the reply with the model's own token at the end of a turn: `token_ids`, `finish_reason`, `text`, `problems`), `end_of_turn_token_ends_the_reply` (`true`, or `null` where it was not seen), `produced_by` (per marker and for `end_of_turn`, where the token was seen: `model` when the model wrote it by itself, else the request parameter the server was steered with, `logit_bias` or `allowed_token_ids`) and `not_observed` (per marker and for `end_of_turn`, where it was not: why; empty when everything was seen) |
| `marker_ids`, `text_markers` | The token of each marker that is one token on this server, and the markers that are not. A marker goes by its name here and wherever something is said per marker (`checks.markers_kept_in_text`, `checks.produced_by`, `checks.not_observed`): `reasoning_open`, `reasoning_close`, `call_open`, `call_close` |
| `closing`, `generation_prompt` | The tokens the template writes at the end of an assistant turn and at the start of a reply |
| `in_reasoning` | Whether a reply begins inside reasoning |

A verdict that was passed in is stored as it was given.

`server_version`:

| Key | Value |
|-----|-------|
| `url`, `field` | The address that was asked, and the field of its answer that holds the version (`null`: the whole answer) |
| `first`, `last` | The version before the first model call and after the last; `null` where there was no answer |
| `same` | Whether the two are equal; `null` where one of them is missing |
| `first_error`, `last_error` | Why an answer could not be had, in words |
| `first_at`, `last_at` | When each answer came, in seconds since the epoch |

`before_run`:

| Key | Value |
|-----|-------|
| `commands` | One result per before-run command that ran, in order: `argv`, `user`, `exit_code`, `timed_out`, `seconds`, `stdout` and `stderr` (the beginning and the end of each stream, half of the command's `max_output_chars` each, around a line that counts the bytes left out between them), `stdout_bytes` and `stderr_bytes` (the whole sizes) |
| `failure` | Why the run could not start, in words; `null` when every command ended with status 0 within its time |
| `seconds` | The time the step took, which is part of `startup_seconds` |

`timeout`, at the time limit:

| Key | Value |
|-----|-------|
| `waiting_on` | `tools`: no model call was under way. `model`: one was, or with a token budget the server's time decided. `harness`: with a token budget, the harness's own time decided |
| `server_silence_seconds`, `server_silence_allowed` | Without a token budget and with `model`: how long the call had had nothing from the server, and how long it was allowed |
| `model_seconds`, `model_seconds_at_minimum_speed` | With a token budget: the server's time, and what its work takes at the minimum speeds |
| `harness_seconds`, `harness_seconds_allowed` | With a token budget: the harness's own time, and its allowance |
| `cause` | With a token budget: the verdict in words |

`budgets`:

| Key | Value |
|-----|-------|
| `wall_seconds` | The time limit in force, given or worked out |
| `wall_clock` | `"the model's time"` without a token budget, `"backstop for the machinery"` with one |
| `max_generated_tokens`, `max_command_seconds` | The task's budgets, `null` where it has none |
| `min_tokens_per_second`, `min_prompt_tokens_per_second` | The minimum speeds in force; the second is the configured one or the one worked out from the first |
| `generated_tokens` | Tokens generated, by the session and its sub-agents |
| `command_seconds` | The time the model's commands ran |

`subagents`:

| Key | Value |
|-----|-------|
| `not_for_training` | Always `true` |
| `token_data_kept` | Always `false`: counts only |
| `started`, `refused_at_limit` | Sub-agents started, and calls refused at `max_subagents` |
| `failures` | Every delegation that came back failed for another reason than the sub-agent's work, in words and in order. An entry that begins `the model server refused the sub-agent's conversation` is a refusal by the server: the model's doing, handed to it as a failed tool result. Every other entry is a failure of the machinery, which ended the run as `subagent_failed` |
| `refused_by_server` | How many of `failures` are conversations the server refused |
| `model_calls`, `prompt_tokens`, `generated_tokens` | Totals over the sub-agents' model calls. `model_calls` counts the entries of `calls`, among them a request that never went out (one the token budget refused, which says so in its `error`); `time.model_calls` leaves such a request out |
| `calls` | One summary per call: `index`, `scope`, `complete`, `finish_reason`, `error`, `prompt_tokens`, `generated_tokens`, `seconds` |
| `events` | With prompts as token IDs, what the prompt assembly noted of a sub-agent's replies, kept apart from `prefix_events`: `marker_place_uncertain` (`call`, the `index` of the call in `calls`; `tokens`). Empty when nothing was noted, and with prompts sent as messages |
| `tool_log` | Every tool call a sub-agent issued, sub-agent by sub-agent: `agent`, `turn`, `call_id`, `tool`, `args`, `result` (first 500 characters), `ok`, and `rule`, `rule_says` where a rule matched; see [sub-agents](#sub-agents) |
| `text` | With `keep_text` only: each call's generated text |

`sandbox`:

| Key | Value |
|-----|-------|
| `name`, `container_id`, `image` | The container |
| `network` | Always `"none"` |
| `workdir`, `user`, `read_only_root`, `capabilities`, `cpus`, `memory`, `pids_limit` | The task's settings as they were applied |
| `writable` | Every memory-backed directory with its size, the image's volumes and shared memory included |
| `volumes` | The directories the image declares as volumes |
| `root_size`, `root_size_source`, `root_bound` | What a writable root was bounded to (a size as the runtime writes it, `10g`), whether the task named that size (`task`) or it is the default (`default`), and how it was kept (`enforced` or `watched`); all `null` for a read-only root |
| `main_user`, `main_process`, `main_process_reach` | The main process's user, what it is (`sleep` or `shell`), and the setting through which the task's commands could end it, `null` when there is none |
| `removed`, `removal_error` | Whether the removal was confirmed, and why not |
| `lost`, `lost_cause` | Why the container stopped while the run needed it, and whose doing that is (`model` or `machinery`); `null` when it did not |
| `oom_kills`, `exit_code` | Out-of-memory kills inside the container, as a rule of a command of the model's, and the main process's exit code if it ended |
| `sidecar` | With a sidecar only: the same keys for its container, and `mcp_servers`. `removed` then speaks for both containers |

`verification`:

| Key | Value |
|-----|-------|
| `commands` | One result per after-run command that ran: `argv`, `exit_code`, `timed_out`, `output`, `output_dropped`, `seconds`, `leftovers` (what was running in the container besides its main process before the command: `found`, how many, all of them ended; `still_running`, those that could not be ended, each with its `uid` (the number of the user it runs as, `null` where the runtime's list gave none) and `command` (its name as the runtime lists it, `""` where it gave none), empty outside a container whose programs are the model's or whose commands can change their user; `frozen`, whether the runtime could hold the container still meanwhile), and where it names files `collected` (path to text: a relative path under the name the task gave it, an absolute one without its first slash) and `collected_summary` (`entries`, how many of each kind: `file`, `dir`, `symlink`, `hardlink`, `other`; `bytes`, `too_large`, and where they apply `skipped`, `failed`) |
| `commands_not_run` | Only where the after-run commands could not run: why |
| `container_stopped` | Whether the container was stopped before the verifier was called |
| `result` | What the verifier returned, or the verifier command printed; it holds `verdict` |
| `program` | With a [verifier command](#a-verifier-command) only: `argv` as it was started, `exit_code`, `timed_out`, `seconds`, `stdout_bytes`, `stderr_bytes`, `stderr` (the beginning and the end of what it wrote there), and `stdout` where its output was no verdict |
| `failure` | A failure of the machinery of the check, in words; the record is then not trainable |
| `helpers_removed` | Whether every container the verifier started was confirmed removed |
| `seconds` | The time the check took, which is not part of `seconds` above |

### Token record

Under `calls`, per model call: `call` (its number, from 0), `shared` (how many
leading tokens of its prompt equal the previous call's prompt plus generated
tokens), `prompt_tail` (the rest), `prompt_len`, `token_ids`, `logprobs`,
`finish_reason`, `start` and `end` (where its generated tokens sit in the final
sequence) and `in_final` (whether they really are there). Beside the calls:
`final_len`, `generated_tokens`, `prefix` (the check's own result: `ok`, the
number of `calls` it looked at, and `breaks`, each with the later `call`, its
`kind` (`mismatch` or `shorter`), the first differing `position`, the `region`
it lies in (`previous_prompt` or `previous_generated`) with the `offset`
inside it, `expected_token_id` and `found_token_id`, `previous_prompt_len`,
`previous_generated_len`, `prompt_len`, and the tokens around the place as
`expected_window` and `found_window`), `trainable` and `one_sequence` as the
token data alone says them. No
token is stored twice; `rebuild_final` gives the whole sequence and
`rebuild_prompts` every call's own prompt.

---

## Known limits

What a user of the harness has to know, in one place:

- **A watched root size is not a hard bound.** See
  [what bounds writes](#what-bounds-writes): files deleted while open are seen
  only as far as the container's own programs show them.
- **A root that cannot be measured is scored as the model's**, also when a
  busy runtime was the reason and not the model; `sandbox.lost` says which of
  the two stops it was. And the watch costs processor time on the machine the
  rollouts run on, per container with a writable root; see
  [what bounds writes](#what-bounds-writes).
- **A rootless runtime cannot run rollouts**: the main process cannot be spared
  from the out-of-memory killer there, and the container is not started.
- **What the kernel limits per user is shared between runs.** The runtime does
  not map a container's users to others of the host, so every container whose
  commands run as the same user (65534 by default, 0 for a root task) draws on
  the same per-user limits of the host's kernel: watches on files, bytes in
  message queues, pseudo-terminals. One run can use up what another needs, and
  a root task shares them with the host's own root. A runtime set up to map
  users gives each its own.
- **`sandbox_destroyed` follows the task's settings**, not what the model did:
  see [the main process](#the-main-process). Only a container found gone
  before the model's first tool call is known not to be the model's doing.
  From then on, and in a run whose model made no tool call at all once its
  first turn has come back, an end nothing outside explains is the model's
  wherever the settings let a command bring it about.
- **The probe measures the temperature at one position**: the one that
  decides, and the first token besides where a later one decides; see
  [which log-probabilities](#which-log-probabilities).
- **A verifier's own time is the machinery's.** One that lets the model's files
  decide how long it runs can be made to run out; see
  [checking a rollout afterwards](#checking-a-rollout-afterwards).
- **Reading files out after the run** is counted as the model's doing where
  root over a writable root let it change the programs, and in any other
  container where the read was cut short and the container still answers. A
  user without root who can still replace the archive program over a writable
  root makes any other failed read the machinery's failure. And a machine that
  stalls for the whole of a read and answers again right after it is taken
  for more at the paths than a read gets through.
- **Ending what is left running relies on the runtime holding the container
  still.** Where it cannot (`leftovers.frozen` is `false`), a process that
  ends every new process of its own user can outlast the tries; outside the
  two kinds of container in which a process that stays is the model's own
  outcome that fails the check, which a model could bring about. Where the
  machine maps the container's users to
  other numbers, the runtime's list names those, and only what runs as the
  commands' user or as root is ended. And it was tried only on a machine with
  the second version of control groups, where a process that is held still
  can be ended: with the first version a kill waits until the process runs
  again, so the tries would find it still there and the check would fail.
- **A process under a user number above 2147483647 is not ended.** The
  runtime starts no helper as such a user. Only commands that run as root
  with `SETUID` can make one; it is then listed in `leftovers.still_running`,
  the after-run commands run beside it, and the record stays trainable. What
  an after-run command found in such a container is as good as its
  `still_running` is empty.
- **The rules can be written around**, and their patterns look a bounded
  distance; see [rules on tool calls](#rules-on-tool-calls).
- **The backstop's verdict is as good as the minimum speeds.** Set them from
  the server as it is under the load of a batch: a minimum above what the
  server keeps marks long runs as `backstop` and loses them to training, one
  far below it scores a slow server's time as the model's `timeout`. The
  record's `time` holds the measured speeds. A prompt the server has not
  counted yet is taken at its size in bytes, which is more tokens than it has.
  And the backstop worked out from the budgets assumes one long prompt per turn
  and per sub-agent; a model that sends more reaches it as its own `timeout`.
- **The command budget counts what the session waits for.**
  `max_command_seconds` counts the time in which a batch of the session's tool
  calls runs and no model call is under way. What the model leaves running
  across its model calls (a background shell, a process it detached) works on
  uncharged while the model generates, and a sub-agent's commands that run
  while another sub-agent's model call is under way are booked as the server's
  time. The container's processor limit, the token budget and the time limit
  still bound it.
- **A minimum speed measured on short prompts is too high for a long context.**
  A server generates more slowly, and takes longer over each token of a
  prompt, the fuller the context is. With a token budget, a model that fills
  its context can bring a long run under minimum speeds that were measured on
  short prompts, and the run then ends as `backstop`, lost to training. Measure
  them with contexts as long as the window lets a run become.
- **A background shell's exit is not announced** to the model in a headless
  run.
- **The probe needs a token the model is unsure of.** It looks among the
  first 24 tokens of the replies to three fixed prompts, the first token
  first. A model certain of all of them gets no verdict, and at sampler
  settings that reshape the distribution its records are then not trainable;
  see [which log-probabilities](#which-log-probabilities).
- **A sidecar that outgrows its file system ends the run as the machinery's.**
  A sidecar over a writable root that writes more than its root may hold is
  stopped; the run then ends as `sidecar_lost` and is not trainable. Where a task's server writes what the model asks for, the model
  can bring that about. Give such a sidecar a read-only root with writable
  directories of a size, or a server that bounds what it stores.
- **An id does not always name one call.** A server may give the calls of
  every reply the same ids, or two calls of one reply the same id. The tool
  log tells them apart by the turn and by the call's place in it, so `rule`,
  `rule_says`, `cut_from`, `agent` and the call as it ran stand on the right
  entry. What names a call by its id alone cannot: `rules.events`, and the
  framework's own note of whether a call failed, which `ok` is read from, so
  two calls of one reply with the same id have the same `ok`. With prompts as
  token IDs the ids are made here and no two are the same.
- **A conversation the server refuses is taken to be the model's doing**, for
  a sub-agent's request and for every request of the session after its first
  (status 400 or 422). A refusal that is the harness's fault, a message the
  server will not take however the model behaves, is then scored as the
  model's too. So is a refusal with one of those statuses that a server gives
  for a reason of its own, which the conversation has no part in.
  `subagents.refused_by_server` and `finish_state: request_refused` keep both
  in view: a batch in which they are common has such a fault. The other way
  round, a request that something in front of the server finds too large
  (status 413, a proxy with a limit on a request's size) stays the
  machinery's `error`, although a model can grow a conversation into such a
  limit: keep it above what a full context window weighs.
- **With prompts sent as messages, a reply the server's own reader cannot hand
  on ends a run as `error`.** A server that reads tool calls itself can fail
  on what the model wrote, half of a surrogate pair written as an escape in a
  typed argument for one: the reply's stream breaks, the call is issued again
  and sampled again, and a model that writes the same every time ends the run
  as the machinery's failure. Nothing on this side can tell that from a server
  that fails by itself. So on such a server a model can take a run that sends
  messages out of training at will. With prompts as token IDs the reply is
  read here and such a reply is an ordinary one, which is why rollouts that
  are trained on use that mode; see
  [prompts as token IDs](#prompts-as-token-ids).

---

## Tests

The container tests (`tests/test_rollout_container.py`,
`tests/test_rollout_verification.py`, `tests/test_rollout_sidecar.py`, one
class of `tests/test_rollout_rules.py`, two of
`tests/test_rollout_preparation.py` and one each of
`tests/test_rollout_verifier_command.py` and
`tests/test_rollout_result_bound.py`) need a container runtime and an image,
so they skip where they are missing. CI runs those files in their own lane
(`test-rollout-container`), which pulls the image by digest and sets
`TURNSTONE_ROLLOUT_REQUIRE_CONTAINER=1`, so that a missing runtime or image
fails the lane instead of skipping the tests. The other rollout tests need no
container and run with the rest of the suite.

No rollout test hands a destructive command to anything that acts on the
machine the tests run on. A refusal is tested against a sandbox that notes
what it was asked to start and starts, in its place, one fixed program of the
test's own that only prints a word, so a refusal that failed shows up as a
note and that word, not as the command; where a command really has to run, it
runs in a container made for the test. Three guards (`tests/conftest.py` with
`tests/_host_guard.py`) are put in place for every test of the suite, each
with patches of its own that a test's `monkeypatch.undo()` does not reach,
and a refusal fails the test also where the code under test catches it. Two
stand where the test process starts a program: in `subprocess.Popen`, which
`run`, `call`, `check_call`, `check_output` and an event loop's subprocesses
all go through, and in `os.system`, `os.exec*`, `os.spawn*` and
`os.posix_spawn*`. The third stands in `os.kill` and `os.killpg`, which a
process object's `kill`, `terminate` and `send_signal` send through.

- **No destructive command is started on the machine.** A shell that would be
  started with a command of a list of destructive shapes (signalling every
  process, removing or moving a top-level directory, turning the machine off,
  writing to a disk, asking for another user's rights, discarding work in a
  git checkout) is refused before a process exists, whether the command is
  the shell's `-c` text or stands in the script file the shell is given, and
  so is such a program started without a shell. That covers the script the
  host's execution environment starts, whoever calls it, and the watch
  runner's shell.
- **The rollout package starts nothing on the machine but the container
  runtime.** A process asked for by any module of `turnstone.rollout`, by
  whichever call, is started only if it is: the runtime with a subcommand
  that acts on containers of the harness's own name and no others (`exec`,
  `rm`, `kill`, `stop`, `cp`, `inspect`, `pause`, `top` and the like), with
  `run` for a container of that name given no option but those the package
  gives `run`, and those of them that could give the container more
  (`--cap-add`, `--security-opt`, `--network`, `--pid`, `--ulimit`,
  `--oom-score-adj`, `--storage-opt`, `--pull`) only with the values the
  package gives them, or with a subcommand that only reads; a program the
  test itself wrote under its temporary directory, which is what a verifier
  command is in a test; or the package's own keeper, in exactly the shape
  the package starts it, removing a container of the harness's with the
  runtime. A privileged `exec` and a subcommand the guard does not know are
  refused. The program is judged as the file the system would start, not as
  a name. The harness's prefix, the keeper's script and the capabilities a
  container may take are written out in the guard, not read from the
  package, and tests hold them equal to the package's.
- **No signal reaches a process the test did not start.** A target that is
  not an `int` (the id of a stand-in object converts to 1), an id of 1 or
  below whatever the signal, the test process's own group or session, and a
  signal to a process that is neither the test process nor one it started
  or one of theirs, or of which the system does not say, are refused before
  anything is sent. A process is the test's when it comes from the test
  process parent by parent, when its group or session was made by a process
  the test process started, or when the environment it was started with
  carries a mark the test set for itself alone. Signal 0, which delivers
  nothing, may ask about any other id, and a signal to an id that no process
  has is the system's to answer.

What the guards do not see:

- what a process the test starts does in its turn: a child interpreter,
  whether it is given code as text, a script file or a module, and a program
  the test wrote under its temporary directory, which the rollout guard lets
  through with any arguments;
- the commands a shell is fed through its standard input, a shell the list
  does not name (`fish`, `csh`, a copy of a shell under another name), and a
  file a shell reads before its command (`BASH_ENV`, `--rcfile`);
- a program not on the list that starts what its arguments spell (`flock`,
  `nsenter`, `unshare`, `chroot`, `script`, `find -exec`, `env -S`);
- a process started without a new program (`os.fork`, `multiprocessing`), by
  a C extension, or by a `preexec_fn`;
- for the rollout guard, a start made for the package by other code: by the
  framework's shell tool where the package's environment is the host's, or
  from a thread that runs only standard-library code, which is judged as that
  code's, so that only the list applies;
- a signal sent other than through `os.kill` and `os.killpg` (`posix.kill`,
  `signal.pidfd_send_signal`, a function taken by name before the test began,
  a C extension), a signal a started process sends, and an id that a process
  gives up and another takes between the check and the signal;
- an id taken again within one test: once a process the test started has
  ended and its group or session has emptied, an unrelated process can take
  the freed id, make a group or session of its own with it and end, and the
  members it leaves then count as the test's;
- a test's own stand-in for `os.kill`, `os.killpg` or `Popen.__init__`, which
  takes that guard's place for as long as it stands, and what session- and
  module-scoped fixtures and the collection of the tests do, which run outside
  the guards;
- a file operation the test process performs itself. The list knows shapes
  that kill, remove, power off and write to disks, not ones that use up memory
  or fill a disk.

What no guard sees of signals and files is answered by running the suite in a
process namespace of its own, with the file system read-only but for a
temporary directory: there a signal to every process reaches only what the
suite started, and no file outside that directory can be written or removed.
The container runtime is not answered so. Its socket stays reachable, because
the container tests need it, and a runtime command from a path no guard judges
(a program the test wrote, a child interpreter, code outside the rollout
package) can still remove another container or mount a directory of the
machine into one.

The base class of an execution environment is the host, so a method that the
container's environment did not replace would act on the machine the rollout
runs on. A test fails when it leaves any method of the base class to the host,
which is how a method added to the base class later is noticed.

Not covered by a test: a size bound enforced by the storage driver (it needs a
host whose driver offers one),
the `socat` and `nc` relays of a sidecar,
and three of the four ways to start `sleep` as the main process (`bash` and
the fallback shell are).
