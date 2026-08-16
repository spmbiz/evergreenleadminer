#!/usr/bin/env bash
set -euo pipefail

MODEL_DIR="${1:-$PWD/model_cache}"
MODEL_FILE="${2:-Qwen3-4B-Q4_K_M.gguf}"
IMAGE="${LLAMA_CPP_IMAGE:-ghcr.io/ggml-org/llama.cpp:server}"
PORT="${QWEN_PORT:-8080}"

# Docker treats a relative -v source such as `model_cache` as a named volume.
# Canonicalize the host directory first so /models is always a real bind mount.
if [[ ! -d "$MODEL_DIR" ]]; then
  echo "Qwen model directory missing: $MODEL_DIR" >&2
  exit 2
fi
MODEL_DIR="$(cd "$MODEL_DIR" && pwd -P)"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"

if [[ ! -s "$MODEL_PATH" ]]; then
  echo "Qwen model missing: $MODEL_PATH" >&2
  exit 2
fi

echo "QWEN4B_MODEL_HOST_PATH=$MODEL_PATH"

docker rm -f hospitality-qwen4b >/dev/null 2>&1 || true
docker pull --platform linux/amd64 "$IMAGE"

start_server() {
  local reasoning_flag="$1"
  local -a args=(
    docker run -d --name hospitality-qwen4b --platform linux/amd64
    --mount "type=bind,src=${MODEL_DIR},dst=/models,readonly"
    -p "127.0.0.1:${PORT}:8080"
    "$IMAGE"
    -m "/models/${MODEL_FILE}"
    --host 0.0.0.0 --port 8080
    --ctx-size 8192 --parallel 2 --threads 4
    --temp 0.3 --top-p 0.8
  )
  if [[ "$reasoning_flag" == "off" ]]; then
    args+=(--reasoning off)
  fi
  "${args[@]}" >/dev/null
}

health_wait() {
  for _ in $(seq 1 45); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    if ! docker inspect -f '{{.State.Running}}' hospitality-qwen4b 2>/dev/null | grep -q true; then
      return 1
    fi
    sleep 2
  done
  return 1
}

start_server off
if health_wait; then
  echo "QWEN4B_READY=http://127.0.0.1:${PORT} reasoning=off"
  exit 0
fi

echo "Qwen server failed with --reasoning off; retrying compatible mode" >&2
docker logs --tail 80 hospitality-qwen4b >&2 || true
docker rm -f hospitality-qwen4b >/dev/null 2>&1 || true
start_server auto
if health_wait; then
  echo "QWEN4B_READY=http://127.0.0.1:${PORT} reasoning=template_default"
  exit 0
fi

docker logs --tail 120 hospitality-qwen4b >&2 || true
exit 3
