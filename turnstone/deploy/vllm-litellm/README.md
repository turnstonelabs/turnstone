# Turnstone local inference — vLLM + LiteLLM on one unified-memory box

Run Turnstone's **reasoning, perception, and reranking** models — three of them,
co-resident on a single unified-memory accelerator (an **NVIDIA DGX Spark / GB10**
or an **AMD Ryzen AI MAX "Strix Halo"**) — behind one LiteLLM gateway.

> Validated on a DGX Spark (GB10, 128 GiB). The AMD Strix Halo path is
> **guidance, not yet hardware-tested** — the deltas are called out explicitly.

## What this is

Three vLLM servers on one GPU, with LiteLLM exposing **both** chat API routes on
one port; the reranker is reached directly (it speaks the `/rerank` wire format):

| Service | Model | Role | Turnstone reaches it via |
|---|---|---|---|
| `vllm-qwen` | Qwen 3.6 27B (FP8) | reasoning + tools | LiteLLM `/v1/messages` (Anthropic) |
| `vllm-gemma` | Gemma 4 12B | vision + audio (omni) | LiteLLM `/v1/chat/completions` (OpenAI) |
| `vllm-reranker` | Qwen3-Reranker 4B | retrieval rerank | `:8002/rerank` (direct) |
| `litellm` | — | gateway (both chat routes) | `:4000` |

```
                ┌─ /v1/messages         → vllm-qwen      (reasoning / tools)
turnstone ─┬─ litellm :4000 ─┤
           │   └─ /v1/chat/completions  → vllm-gemma     (vision + audio)
           └──────────────────────────── vllm-reranker :8002/rerank  (direct)
```

Models **load by HF id into a mounted `HF_HOME` cache** — no hand-staged weight
dirs; the first `up` downloads them (set `HF_TOKEN` for gated repos), then
they're cached on disk.

**Why two chat routes.** vLLM serves both `/v1/messages` and `/v1/chat/completions`.
- Qwen uses the **Anthropic** lane — vLLM's native `/v1/messages` is the stronger
  path for reasoning/tool models.
- Gemma uses the **OpenAI** lane — perception needs it: audio (`input_audio`) has
  no equivalent in the Anthropic Messages API, so Turnstone gates audio roles to
  OpenAI-SDK providers. Vision works on either lane; audio works only here.

**Validated shape (GB10, 128 GiB):** qwen full **256K @ ~1.4×** (MTP spec-decode +
runai_streamer), gemma full **131072 @ ~2×**, reranker **@ ~1.4×**; ~117/121 GiB
used. Two hard rules on one unified-memory card (rationale in *Troubleshooting*):
**drop the page cache before `up`**, and **start sequentially** (enforced via
`depends_on: service_healthy`) so each model profiles against clean memory.

## Requirements

- One unified-memory accelerator, ≈128 GiB (Spark GB10, or Strix Halo with the
  iGPU given most of system RAM via UMA/GTT).
- Docker + Compose v2, and either the **NVIDIA Container Toolkit** (Spark) or
  **ROCm** with `/dev/kfd` + `/dev/dri` access (Strix Halo).
- An `HF_TOKEN` for gated repos (e.g. `google/gemma-4-*`) and ~50 GiB of disk for
  the model cache. `sudo` for `drop_caches`.
- These commands pass `--trust-remote-code`; only use it with model repos you trust.

## Setup — NVIDIA DGX Spark (GB10)   ✅ validated

```sh
cp .env.example .env
# edit .env: set HF_TOKEN (gated gemma); defaults otherwise suit a Spark

# 1. drop the page cache (REQUIRED on unified memory)
sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'

# 2. bring it up — qwen → gemma → reranker → litellm, auto-sequenced.
#    First run downloads ~50 GiB into ./hf-cache, so the first start is slow.
docker compose up -d
watch -n5 'docker compose ps'
```

Verify the KV pools and a round-trip on each route:

```sh
docker compose logs vllm-qwen     | grep "Maximum concurrency"   # ~1.4x @ 262144
docker compose logs vllm-gemma    | grep "Maximum concurrency"   # ~2x   @ 131072
docker compose logs vllm-reranker | grep "Maximum concurrency"

# qwen — Anthropic route
curl -s localhost:4000/v1/messages -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' -H 'x-api-key: dummy' \
  -d '{"model":"qwen3.6-27b","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}'
# gemma — OpenAI route
curl -s localhost:4000/v1/chat/completions -H 'content-type: application/json' \
  -H 'authorization: Bearer dummy' \
  -d '{"model":"gemma-4-12b","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
# reranker — direct
curl -s localhost:8002/rerank -H 'content-type: application/json' \
  -d '{"model":"qwen3-reranker-4b","query":"capital of France","documents":["Paris is the capital of France","Berlin is in Germany"]}'
```

## Setup — AMD Strix Halo (Ryzen AI MAX, ROCm)   ⚠️ guidance, untested here

Same compose, with these changes:

1. **Image** — the stock `vllm/vllm-openai` is CUDA-only; you need a ROCm build for
   `gfx1151`. Easiest is a community Strix Halo setup —
   [kyuz0/amd-strix-halo-vllm-toolboxes](https://github.com/kyuz0/amd-strix-halo-vllm-toolboxes)
   or the TheRock-ROCm build in [hec-ovi/vllm-qwen](https://github.com/hec-ovi/vllm-qwen)
   — then set `VLLM_IMAGE` to it.
2. **Qwen weights & loader** — gfx1151 has no FP8 matmul; recent ROCm/vLLM *can*
   load an FP8 checkpoint but compute falls back to BF16 speed and it's rough.
   Prefer BF16: set `QWEN_MODEL=Qwen/Qwen3.6-27B` in `.env`. Then, in the
   `vllm-qwen` command in `docker-compose.yml`, lower `--max-model-len` toward
   131072 (bf16 27B weights ≈ 54 GiB, less KV room) and remove the
   `--load-format runai_streamer` line (it may not help on ROCm; drop it if it
   errors). Those two are compose literals, not `.env` vars.
3. **GPU access** — ROCm doesn't use the `deploy:` nvidia reservation. In
   `docker-compose.yml`, **delete the `deploy:` block** on *each* vLLM service and
   replace it with:
   ```yaml
       devices: [/dev/kfd, /dev/dri]
       group_add: [video, render]
       ipc: host
       security_opt: [seccomp:unconfined]
   ```
   If the runtime rejects the arch, add `HSA_OVERRIDE_GFX_VERSION=11.5.1` to each
   service's `environment:` (value depends on your ROCm build).

Also make sure the iGPU can reach ~128 GiB (BIOS UMA split high, or Linux GTT via
`amdgpu.gttsize=...`). Then follow the Spark steps (drop caches → `up` → verify).

## Point Turnstone at it

Run Turnstone separately and add the models (e.g. in
`~/.config/turnstone/config.toml`). Note the **two lanes**: qwen omits `/v1` (the
SDK appends `/v1/messages`); gemma includes `/v1`; the reranker is direct.

```toml
[models.qwen]
model = "qwen3.6-27b"
provider = "anthropic-compatible"
base_url = "http://INFERENCE_HOST:4000"            # /v1/messages (no /v1 suffix)
api_key = "dummy"
context_window = 262144

[models.gemma]
model = "gemma-4-12b"
provider = "openai-compatible"
base_url = "http://INFERENCE_HOST:4000/v1"          # /v1/chat/completions (perception)
api_key = "dummy"
context_window = 131072
[models.gemma.capabilities]
supports_vision = true
supports_audio_input = true

[models.reranker]
model = "qwen3-reranker-4b"
provider = "openai-compatible"                       # moot — reranker uses the /rerank client
base_url = "http://INFERENCE_HOST:8002/rerank"       # direct, not via LiteLLM
[models.reranker.capabilities]
supports_rerank = true

[model]
default = "qwen"
```

`default = "qwen"` for reasoning; pick Gemma as the workstream model (or the
**STT** role under Models → Roles) for vision/voice; select the reranker under
**Models → Roles → Reranker**.

## Tuning notes

- **`runai_streamer` on qwen only.** It cut qwen's weight load **166 s → ~1 s**
  (~26×). But its streaming buffers add memory that breaks the *small* models'
  tight KV budgets ("No available memory for the cache blocks"), so gemma and the
  reranker use the default loader.
- **MTP spec-decode on qwen** (`--speculative-config '{"method":"mtp",…}'`): qwen3.6
  has a built-in MTP head, giving ~1.6× decode (≈8→13 tok/s) at ~84% acceptance,
  no draft model. Pair with `--max-num-batched-tokens 8192`.
- **qwen 0.50 default-KV holds full 256K (~1.4×).** `--kv-cache-dtype fp8` is the
  reserve lever (halves KV) if you need to give the others more room.
- **Shape:** gemma-4-12B (full-quality perception, `sliding_window` keeps long-ctx
  KV cheap) + a light **4B** reranker fit alongside qwen; the 8B reranker or
  gemma-4-E4B are the levers if you need to trade quality for memory.

## Troubleshooting

**`No available memory for the cache blocks` (a model won't start).** Its util
left no room for KV after weights. Raise that model's `--gpu-memory-utilization`,
or free memory elsewhere (qwen is the big tenant — drop its util or add
`--kv-cache-dtype fp8`). This is also what `runai_streamer` triggers on small
models — keep it on qwen only.

**`max seq len (X) larger than available KV cache (Y)`.** Same family: not enough
KV for the context. First check you dropped the page cache before `up`; then raise
that model's util or lower its `--max-model-len`.

**`available KV cache memory (0.45 GiB)` despite free RAM.** Two models profiled
memory *simultaneously* and raced. Keep the `depends_on: service_healthy` chain
(sequential startup); never start them together on one card. Dropping caches
*between* each model's start helps too.

**`dependency failed to start: container is unhealthy` during `up`.** The cold
load (download + weights + graph capture) exceeded the healthcheck `start_period`
(we set 1800 s for the first run). It keeps loading and recovers — slow, not broken.

**LiteLLM connection-refused right after `up`.** It starts in seconds but the
vLLMs take minutes; `depends_on` gates it. If you restart only LiteLLM, give it ~10 s.

**Gemma audio: "invalid audio file" / ignored.** (1) the image lacks `vllm[audio]`
— use the bundled `gemma.Dockerfile` (default); (2) gemma must be on the **OpenAI**
lane (`/v1/chat/completions`) — `provider=openai-compatible` in Turnstone — the
Anthropic Messages API has no audio block.

**Reranker fails to load the chat template.** Its template ships in the HF repo and
vLLM usually loads it; if not, add `--chat-template <path-in-cache>` to the
`vllm-reranker` command.

**Qwen replies are only a `thinking` block.** `preserve_thinking` is on; give it a
larger `max_tokens`. Not an error.

**Slow generation (single-digit tok/s).** Expected for a 27B co-resident on one
Spark/Strix Halo APU — memory-bandwidth bound. MTP helps; otherwise trade
co-residence or context for speed.

**AMD `HIP error` / "gfx not supported".** Use a ROCm vLLM build for `gfx1151`, set
`HSA_OVERRIDE_GFX_VERSION`, confirm `/dev/kfd` + `/dev/dri` are passed and the user
is in the `video`/`render` groups.
