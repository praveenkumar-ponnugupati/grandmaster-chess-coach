#!/bin/bash
# Grandmaster Chess Coach — one-step install (macOS).
#
#   ./install.sh
#
# Installs/starts everything the fully self-hosted coach needs:
#   Stockfish (engine), Ollama + models (local AI), a Python venv,
#   and the self-hosted Supermemory server (long-term memory).
# Safe to re-run: every step is skipped if already done.
set -euo pipefail
cd "$(dirname "$0")"

CHAT_MODEL="llama3.1:8b"     # chat coach (~5 GB)
MEMORY_MODEL="llama3.2:3b"   # supermemory's LLM (~2 GB)
SM_PORT=6767

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
step() { printf '\033[1m%s\033[0m\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }

step "[1/5] Stockfish"
if command -v stockfish >/dev/null; then
    ok "already installed ($(command -v stockfish))"
else
    command -v brew >/dev/null || fail "Homebrew required — install it from https://brew.sh first"
    brew install stockfish
    ok "installed"
fi

step "[2/5] Ollama + local models"
if ! command -v ollama >/dev/null; then
    command -v brew >/dev/null || fail "Homebrew required — install it from https://brew.sh first"
    brew install ollama
fi
if ! curl -s -m 3 http://localhost:11434/api/tags >/dev/null; then
    brew services start ollama
    for _ in $(seq 1 30); do
        curl -s -m 2 http://localhost:11434/api/tags >/dev/null && break
        sleep 1
    done
    curl -s -m 2 http://localhost:11434/api/tags >/dev/null || fail "Ollama didn't start"
fi
ok "Ollama running"
for model in "$CHAT_MODEL" "$MEMORY_MODEL"; do
    if ollama list | awk '{print $1}' | grep -q "^${model}$"; then
        ok "model $model present"
    else
        echo "  pulling $model (one-time download) …"
        ollama pull "$model"
        ok "model $model pulled"
    fi
done

step "[3/5] Python environment"
if [ -x venv/bin/python ] && venv/bin/python -c 'import chess' 2>/dev/null; then
    ok "venv ready ($(venv/bin/python -V))"
else
    # Prefer a modern interpreter; stock macOS python3 can be as old as 3.9
    PY=""
    for cand in python3.13 python3.12 python3.11 python3.10 python3; do
        command -v "$cand" >/dev/null || continue
        "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' && { PY="$cand"; break; }
    done
    if [ -z "$PY" ]; then
        command -v brew >/dev/null || fail "python3 ≥3.10 required — install it or Homebrew first"
        brew install python@3.12
        PY="$(brew --prefix)/bin/python3.12"
    fi
    "$PY" -m venv venv
    venv/bin/pip install --quiet python-chess
    ok "venv created with $("$PY" -V), python-chess installed"
fi

step "[4/5] Supermemory server (self-hosted, localhost:${SM_PORT})"
SM_BIN="$(command -v supermemory-server || true)"
[ -z "$SM_BIN" ] && [ -x "$HOME/.local/bin/supermemory-server" ] && SM_BIN="$HOME/.local/bin/supermemory-server"
if [ -z "$SM_BIN" ]; then
    echo "  installing supermemory-server …"
    curl -fsSL https://supermemory.ai/install | bash
    SM_BIN="$HOME/.local/bin/supermemory-server"
    [ -x "$SM_BIN" ] || SM_BIN="$(command -v supermemory-server || true)"
    [ -n "$SM_BIN" ] || fail "supermemory-server not found after install"
fi
ok "binary: $SM_BIN"
if lsof -iTCP:$SM_PORT -sTCP:LISTEN >/dev/null 2>&1; then
    ok "server already running on :${SM_PORT}"
else
    echo "  starting server (backed by local Ollama) …"
    mkdir -p .supermemory
    OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_API_KEY=ollama \
        MODEL="$MEMORY_MODEL" nohup "$SM_BIN" >> .supermemory/server.log 2>&1 &
    for _ in $(seq 1 60); do
        lsof -iTCP:$SM_PORT -sTCP:LISTEN >/dev/null 2>&1 && break
        sleep 1
    done
    lsof -iTCP:$SM_PORT -sTCP:LISTEN >/dev/null 2>&1 \
        || fail "server didn't come up — see .supermemory/server.log"
    ok "server running on :${SM_PORT} (API key saved in .supermemory/)"
fi

step "[5/5] Verify"
[ -x "$(command -v stockfish)" ] || fail "stockfish missing"
venv/bin/python -c 'import chess' || fail "python-chess missing"
curl -s -m 3 http://localhost:11434/api/tags >/dev/null || fail "ollama not responding"
curl -s -m 3 "http://localhost:${SM_PORT}/" >/dev/null || fail "supermemory not responding"
ok "all components up"

cat <<'EOF'

Done. Get coached:

    ./coach YOUR_CHESSCOM_USERNAME            # coaching report
    ./coach YOUR_CHESSCOM_USERNAME --chat     # + chat with your coach

Everything runs on your machine — the only network use is fetching
your own finished games from chess.com's public API.
EOF
