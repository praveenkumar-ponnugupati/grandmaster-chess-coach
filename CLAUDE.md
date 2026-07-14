# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this is

**Grandmaster Chess Coach — chess.com**: local-first, post-game chess coaching
CLI. Hackathon entry for the Supermemory hackathon (**"localhost:6767", deadline
2026-07-18 (Saturday) — extended from 07-13**). Public repo (github.com/praveenkumar-ponnugupati/
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
- Memory (Supermemory) is the **point of the product** (owner's mandate,
  2026-07-12). `./coach` GUARANTEES it: auto-starts the local server if
  :6767 is down and refuses to run memoryless. Inside the Python package the
  old graceful degradation stays (`_post_safe`, `recall` try/except) so a
  mid-run hiccup never destroys a finished analysis — but the entry point
  must never silently produce a stateless run.

## Run & test

```bash
coach                                   # bare = agent REPL (like `claude`); global via
                                        # ~/.local/bin/coach symlink (install.sh step 5)
coach report praveenkumar1619           # classic report, memory auto-wired (cached: <1 s)
coach report praveenkumar1619 --chat    # local chat (Ollama qwen2.5:7b, installed)
coach report hikaru --months 1 --max-games 2 --movetime 0.05  # quick smoke, public account
printf 'QUESTION\nexit\n' | coach       # non-interactive agent smoke test
```

A bare chess.com username still works (`coach hikaru` = classic report);
`agent`/`report`/`scout` are reserved words routed before username handling.
The `coach` script resolves symlinks, so venv/data/reports always live in
the repo no matter the caller's cwd.

`./install.sh` is the one-step installer (idempotent). `./coach` wraps
`python -m chesscoach`, auto-exporting the Supermemory key from
`.supermemory/api-key` (gitignored) when the local server is on :6767.
The server runs from the repo root so its data lives in `.supermemory/`;
if :6767 is down, restart:
`OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_API_KEY=ollama
MODEL=qwen2.5:1.5b nohup ~/.local/bin/supermemory-server >> .supermemory/server.log 2>&1 &`
(memory model switched from llama3.2:3b to qwen2.5:1.5b 2026-07-13, owner's
choice; override via MEMORY_MODEL env — the `coach` wrapper and install.sh
both honor it)
(Claude cannot run install.sh itself — it contains a curl|bash for the
Supermemory installer, which the permission layer blocks.)

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
- `chat.py` — Ollama `/api/chat` streaming; whole report = system prompt
  (coach persona, or scout persona with `scouting=True`).
- `scout.py` — opponent scouting report (`./coach scout OPPONENT`): reuses
  report.py section helpers with flipped headers + a "Game plan"; scout
  sessions remembered/recalled via memory.py (`kind: scout-report`).
- `memory.py` — Supermemory `/v3/documents` + `/v3/search`; container tags
  `["chess-coach", <user>]`; games deduped by `customId: game-<uuid>`;
  `SUPERMEMORY_BASE_URL` overrides cloud (use `http://localhost:6767` for
  self-hosted). `search()` is the generic recall; `remember_note` stores
  agent notes (`kind: coach-note`). Session continuity kinds:
  `session-summary` (distilled per session) + `chat-transcript` (one raw
  doc per session; content prefixes "Session summary for …"/"Chat
  transcript for …" double as recall markers).
- `pipeline.py` — the shared fetch → analyze → report core; both the CLI
  path and the agent's tools sit on it (`rated_recent_games`,
  `analyze_and_report`, `remember_run`, `save_report`).
- `agent.py` — `./coach agent`: conversational agentic REPL. Ollama
  tool-calling (qwen2.5:7b — switched from llama3.1:8b 2026-07-13, streamed NDJSON — answer text prints live via
  `_chat_stream`/`on_text`, paced to STREAM_CHARS_PER_SEC=140 on ttys for
  a calm typewriter feel (piped output unpaced), tool calls collected
  mid-stream; `num_ctx` 16384) over four tools: analyze_my_games / scout_opponent /
  recall_memory / remember_note. Tool execution is invisible by owner's
  request (2026-07-13): no ⚙/progress/saved lines — a dim transient
  status ("♞ analyzing your games … 3/10", `_status`/`_clear_status`,
  tty-only, self-erasing) is the only sign of work; responses are the
  only persistent output.
  Session continuity (shipped 2026-07-13): `_persist_session` in a
  `finally` stores one raw transcript doc + one distilled summary
  (tool-free `_summarize` call) per session on every exit path;
  `_last_session_note` preloads the latest summary into the system
  prompt; recall_memory hides chat-transcript docs unless the question
  is conversational (`about_chat` regex). Ctrl-C mid-answer aborts the
  turn, not the session.
  Persona: warm longtime-coach voice; `_nickname` (first name from the
  chess.com profile via `fetch.get_profile`, fallback = alpha prefix of
  the username) is how the coach addresses the player — banner shows
  "Coaching Praveen (praveenkumar1619)".
  Guardrails for 8B-grade tool calling: `_clean_args` scrubs invented
  params + coerces types, MAX_TOOL_ROUNDS=4 caps loops, and bare
  `scout USERNAME` input bypasses the model entirely (deterministic
  fast path). Agent analyses default to 10 games for snappy turns and
  are auto-remembered in Supermemory. Startup shows the GRANDMASTER
  banner (BANNER + _print_banner, ANSI colors only when stdout is a tty):
  player-facing stats up top — ratings/lifetime record via
  `fetch.get_stats` (`_stats_line`) + a "coach's watchlist" phrase
  keyword-matched from the last session note (`_watchlist`, no model
  call) — and the stack status collapsed to one dim "all local … ✓"
  line that expands to a red ✗ only when memory is OFF (owner's call
  2026-07-13: user stats beat component status).

## Current state / next steps

1. **VERIFIED 2026-07-12: Supermemory end-to-end works** against the
   self-hosted server on localhost:6767 (backed by local Ollama). First run
   stored 10 games + session advice; second run recalled it and rendered the
   "Coach's memory" section. `./coach` wires the key automatically; never
   print or commit the key. The full coach is now self-hosted:
   Stockfish + Ollama + Supermemory, all local. `--chat` also verified
   end-to-end (answers matched the report's worst-blunder line exactly).
2. `scout OPPONENT` shipped 2026-07-12 (scout.py; verified on hikaru incl.
   Scout's memory recall + scout chat). Remaining roadmap: v2 local web
   review board (consumes `data/analysis/` JSON), opening drills, puzzle
   export, progress dashboard over Supermemory, MCP server over the same
   tools.
3. `agent` shipped 2026-07-12 (agent.py + pipeline.py refactor; `__main__`
   now routes both classic and agent paths through pipeline.py). All four
   tools verified end-to-end via `./coach agent hikaru` with the live
   local stack: analyze_my_games (model-initiated), recall_memory
   (recalled real stored blunders), scout fast path (magnuscarlsen),
   remember_note. Piped-stdin testing works:
   `printf 'QUESTION\nexit\n' | ./coach agent hikaru --movetime 0.05`.
4. Nice-to-have fixes: movetime in analysis cache key; chat currently picks
   an arbitrary blunder when asked for "worst" (report order helps but the
   LLM can drift).

## Not tracked in Jira

The owner's Jira (project KAN) is for CopyPaw only. This project tracks work
in the README roadmap + this file. Update both when the state changes.
