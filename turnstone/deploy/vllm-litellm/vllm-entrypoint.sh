#!/bin/sh
# vllm-entrypoint.sh — generic preflight-then-exec guard for vLLM services.
# Corrects quantized-only backend x unquantized spec-decode conflicts, then
# execs `vllm serve`. Pass-through corrector, NOT a command builder: inspects
# argv, applies VLLM_SPEC_CONFLICT, appends optional env flags only when set
# and absent from argv. Applies to any service using it (qwen today;
# gemma/reranker inherit harmlessly as no-ops without spec decode).
#
# Ref: vLLM #54788 (draft moe_backend override), #64751, #59606, #65139,
# Unsloth HF#4 (can't use MTP with moe_backend=flashinfer_b12x).
set -eu

log() { printf '%s\n' "vllm-entrypoint: $*" >&2; }

# --- normalize invocation: entrypoint receives compose `command:` as argv ---
# Handles: bare flags (`--model ...`), `serve ...`, or `vllm serve ...`.
if [ "${1:-}" = "vllm" ]; then shift; fi
case "${1:-}" in
  serve) ;;
  -*) set -- serve "$@" ;;
  *) set -- serve "$@" ;;
esac

has_flag() {
  # has_flag <flag-name-without-dashes> — true if argv holds --flag or --flag=val
  _f="--$1"
  for _a in "$@"; do
    case "$_a" in
      "$_f"|"$_f="*) return 0 ;;
    esac
  done
  return 1
}

flag_value() {
  # flag_value <flag> — prints value of --flag <val> or --flag=<val>, empty if absent
  _f="--$1"; _prev=""
  for _a in "$@"; do
    if [ -n "$_prev" ]; then printf '%s' "$_a"; return 0; fi
    case "$_a" in
      "$_f="*) printf '%s' "${_a#*=}"; return 0 ;;
      "$_f") _prev=1 ;;
    esac
  done
  printf ''
}

is_quant_backend() {
  # is_quant_backend <value> — quantized-only backends that reject BF16 drafts
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    b12x|flashinfer_b12x) return 0 ;;
  esac
  return 1
}

spec_method_of() {
  # prints lowercased spec method from --spec-method or --speculative-config JSON, empty if none
  _m="$(flag_value spec-method "$@")"
  if [ -n "$_m" ]; then printf '%s' "$_m" | tr '[:upper:]' '[:lower:]'; return 0; fi
  _sc="$(flag_value speculative-config "$@")"
  if [ -n "$_sc" ]; then
    # crude JSON method extraction (no jq in image): "method" : "mtp" (any spacing/case)
    printf '%s' "$_sc" | tr '[:upper:]' '[:lower:]' | sed -n 's/.*"method"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
    return 0
  fi
  printf ''
}

is_draft_spec_method() {
  # unquantized-draft spec family: mtp / dflash / eagle / nextn (+ *_mtp aliases).
  # ngram/ngram_gpu/suffix intentionally EXCLUDED (no draft model -> no conflict).
  case "$1" in
    *mtp*|*dflash*|*eagle*|*nextn*) return 0 ;;
  esac
  return 1
}

# --- detect conflict side A: quantized-only backend present ---
BACKEND_FLAGS=""
for _b in moe-backend linear-backend attention-backend; do
  _v="$(flag_value "$_b" "$@")"
  if [ -n "$_v" ] && is_quant_backend "$_v"; then
    BACKEND_FLAGS="$BACKEND_FLAGS --$_b=$_v"
  fi
done
# env-provided backends also count as side A (they will be appended below)
for _pair in "MOE_BACKEND:moe-backend" "LINEAR_BACKEND:linear-backend" "ATTENTION_BACKEND:attention-backend"; do
  _e="${_pair%%:*}"; _fl="${_pair##*:}"
  eval "_ev=\${$_e:-}"
  if [ -n "${_ev:-}" ] && is_quant_backend "${_ev:-}"; then
    if ! has_flag "$_fl" "$@"; then BACKEND_FLAGS="$BACKEND_FLAGS --$_fl=$_ev(env)"; fi
  fi
done

# --- detect conflict side B: unquantized spec draft present ---
SPEC_METHOD="$(spec_method_of "$@")"
SPEC_PRESENT=0
if [ -n "$SPEC_METHOD" ]; then
  if is_draft_spec_method "$SPEC_METHOD"; then SPEC_PRESENT=1; fi
elif has_flag "speculative-config" "$@"; then
  # --speculative-config without parseable method: assume draft (fail-loud in `fail` mode)
  SPEC_PRESENT=1; SPEC_METHOD="(unparsed)"
fi

MODE="${VLLM_SPEC_CONFLICT:-prefer-mtp}"
case "$MODE" in
  prefer-mtp|prefer-b12x|fail) ;;
  *) log "refusing: unknown VLLM_SPEC_CONFLICT='$MODE' (want prefer-mtp|prefer-b12x|fail)"; exit 2 ;;
esac

# --- decide phase: no argv mutation, only computes what to do ---
DROP=""
_new=""

if [ -n "$BACKEND_FLAGS" ] && [ "$SPEC_PRESENT" = "1" ]; then
  case "$MODE" in
    prefer-mtp)
      log "warning: quantized-only backend(s) [$BACKEND_FLAGS ] conflict with spec draft '$SPEC_METHOD'; keeping draft, dropping backend flag(s) (prefer-mtp)"
      DROP=""
      # Name-based: removes ALL occurrences of a conflicting backend flag.
      # (Duplicate same-flag entries collapse to default — pathological input,
      # vLLM last-wins would have kept the last; safe fallback, not fidelity.)
      for _b in moe-backend linear-backend attention-backend; do
        _v="$(flag_value "$_b" "$@")"
        if [ -n "$_v" ] && is_quant_backend "$_v"; then
          DROP="$DROP $_b"
        fi
      done
      # clear env backends so they are not re-appended below
      MOE_BACKEND=""; LINEAR_BACKEND=""; ATTENTION_BACKEND=""
      # inject draft moe_backend triton when MTP-like and missing (per #54788)
      _sc="$(flag_value speculative-config "$@")"
      case "$_sc" in
        *moe_backend*|*moe-backend*) ;;
        *)
          _tok="$(flag_value spec-tokens "$@")"; [ -z "$_tok" ] && _tok="2"
          _new=$(printf '{"method": "mtp", "num_speculative_tokens": %s, "moe_backend": "triton"}' "$_tok")
          DROP="$DROP speculative-config"
          log "warning: injected moe_backend=triton into speculative-config draft override"
          ;;
      esac
      ;;
    prefer-b12x)
      log "warning: quantized-only backend(s) [$BACKEND_FLAGS ] conflict with spec draft '$SPEC_METHOD'; keeping backend, dropping --speculative-config/--spec-method (prefer-b12x)"
      DROP="speculative-config spec-method"
      ;;
    fail)
      log "refusing: quantized-only backend(s) [$BACKEND_FLAGS ] + spec draft '$SPEC_METHOD' — set VLLM_SPEC_CONFLICT=prefer-mtp|prefer-b12x to choose (fail)"
      exit 2
      ;;
  esac
fi

# --- mutate phase: single filter pass (order + word boundaries preserved), then quoted appends ---
_should_drop() {
  # _should_drop <flag-name> — true if DROP lists it
  case " $DROP " in
    *" $1 "*) return 0 ;;
  esac
  return 1
}

if [ -n "$DROP" ]; then
  _n=$#; _i=0; _skip=0
  while [ "$_i" -lt "$_n" ]; do
    _a=$1; shift; _i=$((_i+1))
    if [ "$_skip" = "1" ]; then _skip=0; continue; fi
    case "$_a" in
      --*=*)
        _fn=${_a%%=*}; _fn=${_fn#--}
        if _should_drop "$_fn"; then continue; fi
        ;;
      --*)
        _fn=${_a#--}
        if _should_drop "$_fn"; then _skip=1; continue; fi
        ;;
    esac
    set -- "$@" "$_a"
  done
fi

# injection (prefer-mtp only, when _new was built): single argv word
if [ -n "${_new:-}" ]; then
  set -- "$@" --speculative-config "$_new"
fi

# --- append optional env-provided flags only when set and absent (quoted) ---
# UVA / weight offload
if [ -n "${CPU_OFFLOAD_GB:-}" ] && ! has_flag "cpu-offload-gb" "$@"; then
  set -- "$@" --cpu-offload-gb "${CPU_OFFLOAD_GB:-}"
fi
if [ -n "${CPU_OFFLOAD_PARAMS:-}" ] && ! has_flag "cpu-offload-params" "$@"; then
  set -- "$@" --cpu-offload-params "${CPU_OFFLOAD_PARAMS:-}"
fi
if [ -n "${OFFLOAD_BACKEND:-}" ] && ! has_flag "offload-backend" "$@"; then
  set -- "$@" --offload-backend "${OFFLOAD_BACKEND:-}"
fi
# explicit backend selects (when no conflict path cleared them)
if [ -n "${MOE_BACKEND:-}" ] && ! has_flag "moe-backend" "$@"; then
  set -- "$@" --moe-backend "${MOE_BACKEND:-}"
fi
if [ -n "${LINEAR_BACKEND:-}" ] && ! has_flag "linear-backend" "$@"; then
  set -- "$@" --linear-backend "${LINEAR_BACKEND:-}"
fi
if [ -n "${ATTENTION_BACKEND:-}" ] && ! has_flag "attention-backend" "$@"; then
  set -- "$@" --attention-backend "${ATTENTION_BACKEND:-}"
fi
# KV offload: native path uses --kv-offloading-*; lmcache backend uses explicit --kv-transfer-config
if [ "${KV_OFFLOADING_BACKEND:-}" = "lmcache" ] && [ -n "${KV_OFFLOADING_SIZE:-}" ]; then
  if ! has_flag "kv-transfer-config" "$@"; then
    _h="${LMCACHE_HOST:-lmcache}"; _p="${LMCACHE_PORT:-5555}"
    _ktc=$(printf '{"kv_connector":"LMCacheMPConnector","kv_connector_module_path":"lmcache.integration.vllm.lmcache_mp_connector","kv_role":"kv_both","kv_connector_extra_config":{"lmcache.mp.host":"%s","lmcache.mp.port":%s}}' "$_h" "$_p")
    set -- "$@" --kv-transfer-config "$_ktc"
    log "info: lmcache backend -> explicit kv-transfer-config host=$_h port=$_p"
  fi
else
  if [ -n "${KV_OFFLOADING_SIZE:-}" ] && ! has_flag "kv-offloading-size" "$@"; then
    set -- "$@" --kv-offloading-size "${KV_OFFLOADING_SIZE:-}"
  fi
  if [ -n "${KV_OFFLOADING_BACKEND:-}" ] && ! has_flag "kv-offloading-backend" "$@"; then
    set -- "$@" --kv-offloading-backend "${KV_OFFLOADING_BACKEND:-}"
  fi
fi
# CUTE_DSL_ARCH is an env passthrough for Unsloth-Fast builds (not a vLLM flag)
if [ -n "${CUTE_DSL_ARCH:-}" ]; then export CUTE_DSL_ARCH; fi

log "info: exec vllm $*"
exec vllm "$@"
