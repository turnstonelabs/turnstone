# Gemma 4 is an omni model (vision + audio). The stock vLLM image ships without
# the audio extra, so audio uploads fail with a misleading "invalid audio file".
# This layer adds vllm[audio]. Builds on top of whatever VLLM_IMAGE you use, so
# it works for both the NVIDIA and ROCm base images.
#
# Vision works WITHOUT this — if you only need vision, point vllm-gemma at
# ${VLLM_IMAGE} directly in docker-compose.yml and drop the build: block.
ARG VLLM_IMAGE=vllm/vllm-openai:latest
FROM ${VLLM_IMAGE}
RUN python3 -m pip install --no-cache-dir "vllm[audio]"
