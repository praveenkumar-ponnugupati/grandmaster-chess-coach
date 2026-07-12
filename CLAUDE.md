# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this is

**Grandmaster Chess Coach — chess.com**: local-first, post-game chess coaching
CLI. Hackathon entry for the Supermemory hackathon (**"localhost:6767", deadline
2026-07-13 23:59 PST**). Public repo (github.com/praveenkumar-ponnugupati/
grandmaster-chess-coach) but **NOT open source** — all rights reserved on
purpose (owner's choice; an early MIT commit was superseded, don't re-add a
license).

Pipeline: chess.com public API → Stockfish 18 per-move analysis of the user's
moves → Markdown coaching report → optional local AI chat → optional
Supermemory long-term memory.

Owner's chess.com username: **praveenkumar1619** (his real reports live in
`reports/`, gitignored). His profile from real data: endgame ACPL 151 is his
weak phase, 14 thrown wins in 12 games — the "Coach's memory" progress story
is the demo hook.

## Hard rules

- **Post-game only.** Never build anything that assists during a live game —
  chess.com fair-play violation. Scouting finished public games is fine.
- **Dependency-light**: stdlib + `python-chess` only. Stockfish (UCI binary)
  and Ollama (HTTP on :11434) are external processes, not pip deps.
- Logic lives in `chesscoach/`; `__main__.py` stays thin.
- `data/` and `reports/` are gitignored — never commit user game data.
- Memory (Supermemory) is an **enhancement, never a dependency**: no key or a
  failing server must never break a run (see `_post_safe`, `recall` try/except).

## Run & test

```bash
./venv/bin/python -m chesscoach praveenkumar1619                # report (cached: <1 s)
./venv/bin/python -m chesscoach praveenkumar1619 --chat         # local chat (Ollama llama3.1:8b, installed)
./venv/bin/python -m chesscoach hikaru --months 1 --max-games 2 --movetime 0.05   # quick smoke on public account
```

No test suite yet; verification is CLI runs like the above. venv is checked
out locally (python-chess 1.11.2, Python 3.12). Stockfish 18 at
`/usr/local/bin/stockfish` (Homebrew).

## Architecture (one line each)

- `fetch.py` — chess.com public API (UA header required); monthly archives
  cached in `data/archives/`, completed months immutable, current month always refetched.
- `analyze.py` — Stockfish evals of the user's moves only; cp-loss classes
  50/100/250; mate folded to ±1500; cached per game uuid in `data/analysis/`.
  KNOWN LIMITATION: `--movetime` is not part of the cache key.
- `report.py` — pure aggregation → Markdown (overview, phase table, openings,
  worst blunders, FEN "tactics homework", coach notes). Display filters skip
  noise where best_san == played san.
- `chat.py` — Ollama `/api/chat` streaming; whole report = system prompt.
- `memory.py` — Supermemory `/v3/documents` + `/v3/search`; container tags
  `["chess-coach", <user>]`; games deduped by `customId: game-<uuid>`;
  `SUPERMEMORY_BASE_URL` overrides cloud (use `http://localhost:6767` for
  self-hosted).

## Current state / next steps

1. **UNTESTED: Supermemory end-to-end** (the differentiator). Local server was
   about to be installed when the last session ended. Steps: user runs
   `curl -fsSL https://supermemory.ai/install | bash` themselves (Claude's
   permission layer blocks remote-script installs — do NOT retry it from
   Claude; ask the user to run it), then
   `OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_API_KEY=ollama
   OPENAI_MODEL=llama3.1:8b supermemory-server` (listens on :6767, prints an
   API key on first boot). Then run the report twice with
   `SUPERMEMORY_API_KEY=<key> SUPERMEMORY_BASE_URL=http://localhost:6767` —
   second run must show the "Coach's memory" section.
2. Planned features (README roadmap): `scout OPPONENT` subcommand (~80% reuse),
   v2 local web review board (consumes `data/analysis/` JSON), opening drills,
   puzzle export, progress dashboard over Supermemory.
3. Nice-to-have fixes: movetime in analysis cache key; chat currently picks
   an arbitrary blunder when asked for "worst" (report order helps but the
   LLM can drift).

## Not tracked in Jira

The owner's Jira (project KAN) is for CopyPaw only. This project tracks work
in the README roadmap + this file. Update both when the state changes.
