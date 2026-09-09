#!/usr/bin/env bash
set -euo pipefail
# Use a named tunnel whose hostname is protected by Cloudflare Access.
: "${OLLAMA_TUNNEL_NAME:?Set OLLAMA_TUNNEL_NAME to your Access-protected named tunnel}"
command -v cloudflared >/dev/null
command -v ollama >/dev/null
if ! curl --fail --silent --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null; then
    echo 'Start Ollama with: OLLAMA_HOST=127.0.0.1:11434 ollama serve' >&2
    exit 1
fi
exec cloudflared tunnel run "$OLLAMA_TUNNEL_NAME"
