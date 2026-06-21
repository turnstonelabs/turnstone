# Turnstone local inference — vLLM + LiteLLM on one unified-memory box

Run Turnstone's reasoning **and** perception models on a single unified-memory
accelerator — an **NVIDIA DGX Spark (GB10)** or an **AMD Ryzen AI MAX "Strix
Halo"** — behind one LiteLLM gateway.

> Validated on a DGX Spark (GB10, 128 GiB). The AMD Strix Halo path is
> **guidance, not yet hardware-tested** — the deltas are called out explicitly.

## What this is

Two vLLM servers co-resident on one GPU, fronted by LiteLLM, which exposes
**both** an OpenAI route and an Anthropic route on the same port:

| Service | Model | Role | Turnstone reaches it via |
|---|---|---|---|
| `vllm-qwen` | Qwen 3.6 27B | reasoning + tools | LiteLLM `/v1/messages` (Anthropic) |
| `vllm-gemma` | Gemma 4 12B | vision + audio (omni) | LiteLLM `/v1/chat/completions` (OpenAI) |
| `litellm` | — | gateway, both routes | `:4000` |

The **reranker runs on its own host** — it speaks the Cohere/Jina `/rerank` wire
format, not chat, so Turnstone calls it directly (see *Reranker*, below).

```
                ┌─ /v1/messages         → vllm-qwen   (reasoning / tools)
turnstone ─┬─ litellm :4000 ─┤
           │   └─ /v1/chat/completions  → vllm-gemma  (vision + audio)
           └──────────────── reranker host :8002/rerank  (direct, separate box)
```

**Why two routes.** vLLM serves both `/v1/messages` and `/v1/chat/completions`.
- Qwen uses the **Anthropic** lane — vLLM's native `/v1/messages` is the stronger
  path for reasoning/tool models.
- Gemma uses the **OpenAI** lane — perception needs it: audio (`input_audio`) has
  no equivalent in the Anthropic Messages API, so Turnstone gates audio roles to
  OpenAI-SDK providers. Vision works on either lane; audio works only here.

**Two rules for a single unified-memory card** (both enforced/automated below;
rationale in *Troubleshooting*):
1. **Drop the page cache before `up`** — vLLM profiles against free memory; a
   warm cache makes it under-provision KV.
2. **Sequential startup** — `vllm-gemma` waits for `vllm-qwen` to be healthy
   (`depends_on`) so Qwen claims its KV against clean memory first.

## Requirements

- One unified-memory accelerator, ≈128 GiB (Spark GB10, or Strix Halo with the
  iGPU given most of system RAM via UMA/GTT).
- Docker + Compose v2, and either the **NVIDIA Container Toolkit** (Spark) or
  **ROCm** with `/dev/kfd` + `/dev/dri` access (Strix Halo).
- Weights on disk under `$MODELS_DIR`:
  - `qwen3.6-27B-FP8/` (NVIDIA) or `qwen3.6-27B/` bf16 (AMD)
  - `gemma-4-12B-it/`
- `sudo` for `drop_caches`.
- These examples pass `--trust-remote-code` to vLLM; only use this with trusted model repos (remove the flag if your checkpoint doesn’t require it).

## Setup — NVIDIA DGX Spark (GB10)   ✅ validated

```sh
cp .env.example .env
# edit .env: set MODELS_DIR to your weights dir (defaults otherwise suit a Spark)

# 1. drop the page cache (REQUIRED on unified memory)
sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'

# 2. bring it up — qwen loads first, then gemma, then litellm (auto-sequenced)
docker compose up -d

# 3. watch until both are healthy (qwen ~8 min: FP8 load + graph capture)
watch -n5 'docker compose ps'
```

Verify the KV pools and a round-trip on each route:

```sh
# qwen should report "Maximum concurrency for 262144 ...: ~2.5x"
docker compose logs vllm-qwen | grep "Maximum concurrency"
docker compose logs vllm-gemma | grep "Maximum concurrency"   # ~1.0x at 131072

# Anthropic route -> qwen
curl -s localhost:4000/v1/messages -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' -H 'x-api-key: dummy' \
  -d '{"model":"qwen3.6-27b","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}'

# OpenAI route -> gemma
curl -s localhost:4000/v1/chat/completions -H 'content-type: application/json' \
  -H 'authorization: Bearer dummy' \
  -d '{"model":"gemma-4-12b","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

Reference point (Spark, defaults): Qwen full 256K @ ~2.5× concurrency on default
KV; Gemma full 131072 @ ~1.0×; ~108/121 GiB used.

## Setup — AMD Strix Halo (Ryzen AI MAX, ROCm)   ⚠️ guidance, untested here

Same compose, three deltas in `.env` + the GPU device block:

1. **Image** — the stock `vllm/vllm-openai` is CUDA-only; you need a ROCm build for
   `gfx1151`. Easiest is a community Strix Halo setup —
   [kyuz0/amd-strix-halo-vllm-toolboxes](https://github.com/kyuz0/amd-strix-halo-vllm-toolboxes)
   or the TheRock-ROCm build in [hec-ovi/vllm-qwen](https://github.com/hec-ovi/vllm-qwen)
   — then point `VLLM_IMAGE` at it.
2. **Weights — BF16 is the polished path.** gfx1151 has no FP8 matmul, so even though
   recent ROCm/vLLM *can* load an FP8 checkpoint, the compute falls back to BF16 speed —
   FP8's only win here is ~half the weight memory, and it's still rough (very slow first
   load). Prefer BF16; qwen3.6-27B BF16 at long context is reported working on Strix Halo
   (hec-ovi, above). BF16 27B weights ≈ 54 GiB, so leave less KV room:
   ```sh
   QWEN_MODEL=/models/qwen3.6-27B   # BF16. The -FP8 dir loads too (memory saving, slow/immature)
   QWEN_MAXLEN=131072               # start here co-resident; raise toward 262144 and watch the KV log
   QWEN_UTIL=0.62
   ```
3. **GPU access** — ROCm doesn't use the `deploy:` nvidia reservation. In
   `docker-compose.yml`, **delete the `deploy:` block** on *both* vLLM services
   and replace it with:
   ```yaml
       devices:
         - /dev/kfd
         - /dev/dri
       group_add:
         - video
         - render
       ipc: host
       security_opt:
         - seccomp:unconfined
   ```
   If the runtime rejects the arch, add `HSA_OVERRIDE_GFX_VERSION=11.5.1` to each
   service's `environment:` and retry (value depends on your ROCm build).

Also ensure the iGPU can actually reach ~128 GiB: set the BIOS UMA/"VRAM" split
high, or rely on Linux GTT (`amdgpu.gttsize=...` kernel param) — otherwise vLLM
sees only the small carved-out VRAM. Then follow the Spark steps (drop caches →
`up` → verify).

## Point Turnstone at it

Run Turnstone separately and add the models (e.g. in
`~/.config/turnstone/config.toml`). Note the **two different lanes**: Qwen omits
`/v1` (the SDK appends `/v1/messages`); Gemma includes `/v1`:

```toml
[models.qwen]
model = "qwen3.6-27b"
provider = "anthropic-compatible"
base_url = "http://INFERENCE_HOST:4000"           # /v1/messages (no /v1 suffix)
api_key = "dummy"
context_window = 262144

[models.gemma]
model = "gemma-4-12b"
provider = "openai-compatible"
base_url = "http://INFERENCE_HOST:4000/v1"         # /v1/chat/completions (perception)
api_key = "dummy"
context_window = 131072
[models.gemma.capabilities]
supports_vision = true
supports_audio_input = true

[models.reranker]
model = "qwen3-reranker-8b"
provider = "openai-compatible"                      # moot — reranker uses the /rerank client
base_url = "http://RERANKER_HOST:8002/rerank"       # direct, not via LiteLLM
[models.reranker.capabilities]
supports_rerank = true

[model]
default = "qwen"
```

Set `default = "qwen"` for reasoning; pick Gemma as the workstream model (or the
**STT** role under Models → Roles) for vision/voice; select the reranker under
**Models → Roles → Reranker**.

## Reranker (separate host)

The reranker is light but doesn't co-reside well with Qwen at full context on one
128 GiB card, and it speaks a different wire format — so run it on its own box:

```sh
vllm serve /models/qwen3-reranker-8b \
  --served-model-name qwen3-reranker-8b --runner pooling \
  --hf-overrides '{"architectures":["Qwen3ForSequenceClassification"],"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}' \
  --chat-template /models/qwen3-reranker-8b/chat_template.jinja \
  --gpu-memory-utilization 0.5 --port 8002
```

Point Turnstone's reranker `base_url` at `http://RERANKER_HOST:8002/rerank`.

## Troubleshooting

**`max seq len (X) larger than available KV cache (Y)` — Qwen won't start.**
Not enough KV for the context. In order: (1) you forgot to drop the page cache —
`docker compose down`, drop caches, `up`; (2) raise `QWEN_UTIL`; (3) lower
`GEMMA_UTIL` to hand memory over; (4) lower `QWEN_MAXLEN`.

**`available KV cache memory (0.45 GiB)` despite plenty of free RAM.** The two
vLLMs profiled memory *simultaneously* and raced — one grabbed almost everything.
Keep the `depends_on: vllm-qwen: condition: service_healthy` on gemma (sequential
startup). Never start them together on one card.

**`dependency failed to start: container is unhealthy` during `up`.** The cold
load (weights + graph capture) exceeded the healthcheck `start_period`. We set
900 s; raise it if your disk is slow. The container keeps loading and recovers
(`restart: unless-stopped`) — it's slow, not broken.

**LiteLLM returns connection-refused right after `up`.** It starts in seconds but
the vLLMs take minutes; `depends_on` gates it. If you restart *only* LiteLLM,
give it ~10 s before the first request.

**Gemma audio: "invalid audio file", or audio silently ignored.** Two causes:
(1) the image lacks `vllm[audio]` — use the bundled `gemma.Dockerfile` (default);
(2) Gemma is being reached on the Anthropic lane — audio only rides
`/v1/chat/completions`, so Gemma must be `provider=openai-compatible` in Turnstone
and `openai/...` in `litellm-config.yaml`.

**Qwen replies are only a `thinking` block, no text.** `preserve_thinking` is on;
give it a larger `max_tokens`, or render the thinking separately. Not an error.

**Slow generation (single-digit tokens/s).** Expected for a 27B co-resident with
another model on one Spark/Strix Halo APU — it's memory-bandwidth bound. For more
speed, drop co-residence or context.

**Want more headroom / fitting tighter.** `--kv-cache-dtype fp8` (add to the qwen
command) ~halves KV memory if you're squeezed; default (fp16/bf16) KV is higher
fidelity and usually fits once the cache is dropped. Gemma over-provisioned (high
"concurrency" multiple)? Raise `GEMMA_MAXLEN` to spend the headroom on context,
or lower `GEMMA_UTIL` to free memory.

**AMD: `HIP error` / "gfx not supported".** Use a ROCm vLLM build for `gfx1151`,
set `HSA_OVERRIDE_GFX_VERSION`, confirm `/dev/kfd` + `/dev/dri` are passed and the
user is in the `video`/`render` groups.
