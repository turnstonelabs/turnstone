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

---

## Running one

```
turnstone-rollout --base-url http://127.0.0.1:8000/v1 --model NAME \
    --prompt "Create hello.txt containing hi, then list the directory." \
    --top-k 20 --top-p 0.95 --out episode.json
```

Exit status: 0 for a trainable rollout, however the model ended it; 1 when the
server, the harness or the runtime ended it; 2 when it ran to its own end but
its token data cannot be trained on.

A configuration that cannot work is refused before anything runs, with a
message and exit status 1: a context window that nothing names (name it with
`--context-window`), or budgets that do not fit together.

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
the run starts: `SetupError` for a configuration that cannot work, and
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
| Characters of a command's output the model gets | `max_output_chars` | `[tools] truncation` | 0, the framework's automatic share of the remaining context |

The configuration file is the one every Turnstone command reads (`--config`,
`TURNSTONE_CONFIG`). A server's database is not read: a rollout runs in its own
process with throwaway storage. The record lists the value each limit had and
where it came from (`limits`).

Three rules keep a property of the configuration from being scored as the
model's outcome:

- **A number below the least it can be is refused** before anything runs
  (`SetupError`; the command exits with a message that says which), whichever
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

  A value below its least in the configuration file is passed over, as the
  framework passes it over, and the next source in the order above decides.
- **A context window nothing names is refused**, not guessed (`SetupError`; the
  command exits with a message). A rollout ends, as the model's own outcome,
  before a request that could not fit the window. With a guessed window that
  ending would say something about the guess.
- **The reply limit is at most a quarter of the context window**, the most the
  framework itself reserves for a reply. A larger one is cut to that, and the
  record's `limits.max_tokens.source` says so. With a reply limit near the
  window's size the first long tool result would end every rollout.

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
[budgets](#budgets-of-the-models-own) a task may give the model.

`Task.timeout` bounds the session run, sub-agents included, to the next whole
second. It starts when the sandbox is up: bringing it up, a sidecar's setup
included, is not the model's time, and the record says how long it took
(`startup_seconds`). It does not bound what comes after either: the check after
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
a reply cut at the reply limit ends it as `length`. With nothing left the
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
| `context_full` | The conversation filled the context window; the run ends before a request that could not fit | model | yes |
| `timeout` | The time limit passed while the model was generating or one of its commands was running; with a token budget, although the server kept its minimum speed | model | yes |
| `token_budget` | The budget of generated tokens was used up | model | yes |
| `command_time` | The time the model's commands may run was used up | model | yes |
| `sandbox_destroyed` | The model ended its own container from inside, where the task's settings let its commands do that (see [the main process](#the-main-process)), or a writable root outgrew its size or could no longer be measured (see [what bounds writes](#what-bounds-writes)) | model | yes |
| `request_refused` | After a request of the session had been answered: the server refused a later one for what the conversation held (HTTP status 400 or 422); see [a conversation the server refuses](#a-conversation-the-server-refuses) | model | yes |
| `server_stalled` | Without a token budget: the time limit passed while a model request had had nothing from the server for longer than it is allowed | machinery | no |
| `backstop` | With a token budget: the time limit passed and the server was slower than its minimum speed, or the harness itself took the time | machinery | no |
| `error` | A request of the session failed (after the client's own retries; the first request refused by the server, and a prompt the server finds too long, included), or the harness raised (a stable-prefix error included) | machinery | no |
| `sandbox_failed` | The container could not be started | machinery | no |
| `sandbox_lost` | The container stopped and nothing the model could do explains it, or the runtime stopped answering | machinery | no |
| `sidecar_failed` | The task's [sidecar](#a-tasks-own-servers-the-sidecar) could not be brought up | machinery | no |
| `sidecar_lost` | The task's sidecar stopped during the run | machinery | no |
| `subagent_failed` | A sub-agent failed for a reason that is not its work | machinery | no |
| `no_model_call`, `abnormal_reply` | The run ended before any model call, or on a finish reason the harness does not know, which only the server gives (a filter of its own, for one) | machinery | no |

Where several apply, a lost container decides first, then a lost sidecar, then
a failed sub-agent, then the others.

`context_full` is decided before each request, from the server's own count of
the previous call's tokens and the size in bytes of what was added since (a
token is at least one byte), with room for a whole reply. It ends a run a
little early, never on the server's refusal of a prompt that is too long.

The time limit can be used up by both sides: by the model, with slow commands
and long replies, and by the server, by not answering. Without a token budget
it is the model's budget like its turns, so reaching it is the model's outcome,
with one exception that can be measured: a model request that had had nothing
from the server for `server_stall_seconds` (60 by default) when the limit
passed. A request the server has not yet answered with a first token is
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

### Which log-probabilities

A trainer that weights by the sampler's probability needs each token's
log-probability under the distribution it was sampled from, after temperature,
top-k and top-p. A server may report the model's own instead, or apply some of
the settings before the values it reports and others after.

Before the run (and outside the task's time) the server is probed, each setting
by itself. A control request asks for the most likely tokens at the first
position of a short prompt with temperature 1 and nothing cut off. Then one
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
its `reflects`) as `RolloutConfig.sampler_logprobs`.

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
before anything is started, the task with it (`ValueError` from
`ContainerSpec.validate`), for the episode's container and for a sidecar's
alike:

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
up (15 minutes, and a sidecar's own limits), the run (the task's time limit),
the commands after the run (their time limits), the verifier (its time limit).
It is stopped only once the container is known to be gone; after a removal that
could not be confirmed it tries again within half a minute and keeps trying for
five.

On the host the harness signals only process groups of children it started
itself: the runtime's client of a command, and the keeper when another takes
its place or it is no longer needed. Each such signal goes through the one
function the framework's own
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
taken to be the model's doing: the rollout ends as `sandbox_destroyed`, the
model's own outcome, trainable, for the task to score. So does an
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

Two limits apply to what a tool returns. A command's output is cut when it is
produced, head and tail kept, to the framework's truncation setting
(`max_output_chars`). A file tool reads a
file of up to `max_read_bytes` whole; a larger one is an error that says so,
never a shortened text.

Not yet handled: the exit of a background shell is not announced to the model in
a headless run, although the tool description says a notice follows. The model
has to ask with `bash_output`.

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
run as. `rules.events` names the tool by the name it runs under.

A refused call is the model's doing: the rollout stays trainable. The tool log
names the rule on the call's entry (`rule`, `rule_says`), and the record lists
every match with whose call it was (`rules.events`).

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
| Its model calls | Kept out of the token record and the prefix check. The side record under `subagents` is marked `not_for_training` and `token_data_kept: false`: it holds counts and a summary per call, no token IDs or log-probabilities, and none of the checks behind `trainable` is made on them |
| Its time | Counts toward the task's time limit |
| Limits | `max_subagents` per rollout (a call past it gets a result that says no sub-agent was started), the framework's turn limit for each (`subagent_max_turns`), the framework's reply limit for each of its model calls, and the task's token budget, which its tokens count toward |
| The rollout's length | `generated_tokens` counts what the session itself generated; sub-agent tokens are under `subagents` and never added in |

What bounds the work sub-agents can do is therefore the number of sub-agents
times the turn limit times the reply limit, the task's time limit, and the
task's token budget where it has one. With the framework's default of no turn
limit and no token budget, the time limit is the bound. The tokens sub-agents
generate are counted in the record.

A sub-agent is not offered the tools of the task's own servers. Those act on
the task's state, which is what the rollout is scored on. Called by the
session, each such call is a generated, recorded turn and is in the tool log; a
sub-agent's calls are in neither, so it would act on that state unseen.

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
here. A sub-agent is not offered these tools, for the same reason
(see [sub-agents](#sub-agents)).

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

A task's files can be bytes as well as text (`Task.files`).

---

## Checking a rollout afterwards

A reward needs a check the model could not have prepared for. A model with root
in a writable container can change everything a check inside that container
relies on: the test script, the test runner, the shell, the interpreter.

**The general form is a verifier**, a function the caller gives `run_episode`:

```python
from turnstone.rollout.verification import VerificationContext

def verifier(context: VerificationContext) -> dict:
    ...  # read from the stopped container, check, decide
    return {"verdict": "pass", ...}

record = run_episode(task, config, verifier=verifier, verifier_timeout=3600)
```

When the run is over, however it ended, the harness stops the episode's
container, so that nothing the model left running can change a file any more,
and calls the function. The context names the stopped container
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
runs as: a process the model detached from its command, one an earlier
after-run command left. Root in the container
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
| The verifier raised, ran past `verifier_timeout` or returned no `verdict` | machinery | not trainable: no reward exists |
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

---

## The episode record

`run_episode` returns a JSON-safe dict. Every key below is always there; a key
whose value does not apply is `null`.

| Key | Value |
|-----|-------|
| `key` | `Task.key` |
| `task` | The task as it was given: `prompt`, `key`, `system_prompt`, `setup_files`, `files`, `max_turns`, `timeout`, `max_generated_tokens`, `max_command_seconds`, `container` (the `ContainerSpec`), `after_run`, `sidecar`. Content held as bytes is replaced by `{"bytes": N}` |
| `model`, `endpoint` | The model's name and the API root the requests went to |
| `finish_state`, `finish_cause` | How the rollout ended, and whether that is the model's outcome (`model`) or a failure of the server, the harness or the runtime (`machinery`); see [how a rollout ends](#how-a-rollout-ends) |
| `error` | `null` where the model ended its turn by itself (`completed`). For every other ending, what happened, in words: the limit that was reached and where the last reply was cut, the request that failed or was refused, why the sandbox is gone or did not start, a sub-agent's failure |
| `trainable`, `one_sequence` | See [above](#trainable-and-one-sequence) |
| `problems` | Why `trainable` is false; empty when it is true |
| `warnings` | Operational trouble that does not touch the data, such as an unconfirmed removal |
| `turns` | Model calls of the session that ended normally |
| `generated_tokens` | Tokens the session itself generated |
| `final_content` | The text of the session's last assistant turn, `""` when it had none |
| `messages` | The system prompt and the final conversation, one entry per message: `role` and `content`; on an assistant turn also `reasoning` (the turn's reasoning as text, `null` where it had none) and, where the turn made calls, `tool_calls` (each `id`, `type` and `function` with `name` and `arguments`); on a tool result `tool_call_id`. What the session keeps on a turn for itself, under keys that begin with an underscore, is left out. A tool call's arguments are the text the model wrote for each, as they are sent back |
| `tool_log` | The session's tool calls in order, each `tool`, `args` (as the tool got them), `result` (first 500 characters), `ok`, `turn`, and for a call a rule matched `rule` and `rule_says` |
| `usage` | The session's own token counts as the server reported them: `prompt`, `completion`; both 0 in a run that did not start |
| `tokens` | The token record, below |
| `prefix_ok`, `prefix_breaks` | The token-level prefix check: whether it held, and one readable line per break |
| `prefix_events` | What the stable-prefix run noted, one entry per `event` |
| `calls` | One summary per model call of the session that ended normally, in order: `index`, `scope`, `streamed`, `request` (`model`, the count of `messages`, their `roles`, the names of the `tools`, the `sampling` keywords, the `extra_body`), `finish_reason`, `usage`, `complete`, `error`, `aligned`, `misaligned_chunks`, `ids_checked`, `chunks`, `seconds`, `longest_silence`, `problems` |
| `discarded_calls` | The session's calls that did not end normally (cut by the time limit, failed, or broken off and issued again), the same keys with `prompt_token_ids`, `token_ids` and `logprobs`; `index` as in `calls` |
| `text` | With `keep_text`, each call's generated text; otherwise `null` |
| `limits` | `max_tokens`, `context_window`, `tool_timeout`, `subagent_max_turns`, `max_output_chars`, each `{"value": ..., "source": "..."}` |
| `sampling` | Below |
| `logprobs` | Which log-probabilities the server reports: `sampler_logprobs` (`true`, `false` or `null`), `reflects` (the same per setting: `temperature`, `top_k`, `top_p`), `reason`, and from a probe `tried` and `positions` |
| `token_id_check` | `"required"` or `"not required"` |
| `timeout` | `null` unless the time limit was reached; then what the run was waiting on, below |
| `seconds`, `startup_seconds`, `teardown_seconds` | The time from the start of the sandbox to the end of the run; of that, the time bringing the sandbox up took; and the time removing it took, which comes on top |
| `time` | Where the time went, below |
| `budgets` | The budgets the run was under and what was spent, below |
| `subagents` | The side record of sub-agent calls, below |
| `sandbox` | The container, below |
| `acts_on_host` | Whether the tools acted on the machine the rollout ran on instead of in a sandbox (see [where the tools act](#where-the-tools-act)) |
| `rules` | `null` without rules; otherwise the set in force (`name`, `sha256`, the count `rules`) and `events`, one per call a rule matched: `call_id`, `tool`, `rule`, `recommendation`, `refused`, `scope` (`parent` or `sub_agent`) |
| `verification` | `null` when no check was asked for; otherwise below |

`prefix_events`, one entry per `event`. `turn` counts the session's turns
from 0, as `tool_log[].turn` does:

| `event` | Its other keys | What happened |
|---------|----------------|---------------|
| `raw_arguments_unrecovered` | `turn`, `call_ids` | The text the model wrote for these calls' arguments could not be read from the reply, or does not mean the parsed arguments. The calls are sent back from their parsed values |
| `arguments_legalized` | `turn`, `call_ids` | These calls' arguments were no JSON object, and are sent back as one |
| `tool_calls_over_limit` | `turn`, `calls`, `limit` | The turn made `calls` tool calls and the first `limit` ran. The others stay in the turn and got a result that says they were not run |
| `ended_at_length` | `turn` | The turn's reply was cut at a limit with a tool call in it. The run ended there and the call did not run |
| `compaction_skipped` | none | Something asked for the conversation to be shortened to a summary, which a run with a stable prefix does not do |

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

Where the sandbox did not start, only the first three keys are there.

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
| `commands` | One result per after-run command that ran: `argv`, `exit_code`, `timed_out`, `output`, `output_dropped`, `seconds`, `leftovers` (what was running in the container besides its main process before the command: `found`, how many, all of them ended; `still_running`, those that could not be ended, each with its `uid` (the number of the user it runs as, `null` where the runtime's list gave none) and `command` (its name as the runtime lists it, `""` where it gave none), empty outside a container whose programs are the model's or whose commands can change their user; `frozen`, whether the runtime could hold the container still meanwhile), and where it names files `collected` (path to text: a relative path under the name the task gave it, an absolute one without its first slash) and `collected_summary` (`entries` by kind, `bytes`, `too_large`, and where they apply `skipped`, `failed`) |
| `commands_not_run` | Only where the after-run commands could not run: why |
| `container_stopped` | Whether the container was stopped before the verifier was called |
| `result` | What the verifier returned; it holds `verdict` |
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
  see [the main process](#the-main-process).
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

---

## Tests

The container tests (`tests/test_rollout_container.py`,
`tests/test_rollout_verification.py`, `tests/test_rollout_sidecar.py`, and one
class of `tests/test_rollout_rules.py`) need a container runtime and an image,
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
  test itself wrote under its temporary directory; or the package's own
  keeper, in exactly the shape
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
