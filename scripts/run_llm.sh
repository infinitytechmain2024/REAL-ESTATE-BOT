#!/usr/bin/env bash
# Run a local model for the bot, on the machine that hosts it.
#
# This is the alternative to Ollama, and the reason to prefer it is one flag:
# --cache-type-k. The KV cache is what makes a large context expensive -- on an
# 8B model a 128k window costs roughly 19 GB of it -- and quantising the cache
# cuts that several-fold. Ollama does not expose that knob, and its default
# context is 4096 tokens, which it silently truncates to.
#
# The server binds to loopback. A model server on a home network is an open
# endpoint: no authentication, and it will answer anyone who asks.
set -euo pipefail

MODEL="${LLAMA_MODEL:-unsloth/Qwen3-8B-GGUF:UD-Q4_K_XL}"
PORT="${LLAMA_PORT:-8080}"
CTX="${LLAMA_CTX:-32768}"
KV_TYPE="${LLAMA_KV_TYPE:-q8_0}"
BIN="${LLAMA_BIN:-llama-server}"

if ! command -v "$BIN" >/dev/null 2>&1; then
    cat >&2 <<'MISSING'
llama-server is not installed.

    macOS:   brew install llama.cpp
    Linux:   see https://github.com/ggml-org/llama.cpp (or your package manager)

Then run this again.
MISSING
    exit 1
fi

# A repo id downloads from Hugging Face on first run and is cached after that;
# anything else is treated as a path to a .gguf file you already have. Older
# builds spell -hf as --hf-repo.
if [[ "$MODEL" == */* && "$MODEL" != *.gguf ]]; then
    MODEL_ARGS=(-hf "$MODEL")
else
    MODEL_ARGS=(-m "$MODEL")
fi

cat <<INFO
Model:    $MODEL
Context:  $CTX tokens (Ollama's default is 4096)
KV cache: $KV_TYPE
Address:  http://127.0.0.1:$PORT/v1

Point the bot at it by putting this in .env:

    LLM_PROVIDER=openai_compatible
    LLM_BASE_URL=http://127.0.0.1:$PORT/v1
    LLM_API_KEY=not-needed
    LLM_MODEL=local

INFO

exec "$BIN" \
    "${MODEL_ARGS[@]}" \
    --host 127.0.0.1 \
    --port "$PORT" \
    --ctx-size "$CTX" \
    --cache-type-k "$KV_TYPE" \
    --cache-type-v "$KV_TYPE" \
    --jinja \
    "$@"
