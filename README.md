# Grandmaster Chess Coach — chess.com

Local-first post-game coaching from your own chess.com games: fetches your archive via
the free public API, analyzes every move you played with Stockfish, and
writes a Markdown coaching report.

The report covers:

- **Overview** — score, average centipawn loss, blunder/mistake counts, per-time-control table
- **Phase breakdown** — where in the game you bleed evaluation (opening / middlegame / endgame)
- **Your openings** — the lines you actually play as White and Black, with score and early-game ACPL
- **Worst blunders** — with the move you played, the move you missed, and the game link
- **Tactics homework** — FENs of winning positions you threw away: find the move you missed
- **Coach's notes** — heuristic study advice from the aggregates

Strictly post-game: this tool never suggests moves during live play
(that violates chess.com fair play).

## Long-term memory (Supermemory)

With a [Supermemory](https://supermemory.ai) API key, the coach remembers
you between sessions:

- every analyzed game is stored as a memory (deduped by game id, tagged
  per player), including each blunder with its position FEN
- every coaching session's advice is stored
- each new report opens a **Coach's memory** section recalling what was
  flagged before, so you can see whether you actually fixed it

`./install.sh` sets up a **self-hosted** Supermemory server on
`localhost:6767` backed by your local Ollama, and `./coach` guarantees
it: if the server isn't running it is started automatically, and the
coach won't run without its memory — a coach who forgets you isn't a
coach. Fully local, no cloud account. (A cloud key from
[console.supermemory.ai](https://console.supermemory.ai) works too:
`export SUPERMEMORY_API_KEY=sm_…`.)

## Install (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/praveenkumar-ponnugupati/grandmaster-chess-coach/main/install.sh | bash
```

(Or clone first and run `./install.sh` — same thing; the one-liner
clones into `~/chess-coach` for you.)

That installs and starts the whole self-hosted stack — Stockfish
(engine), Ollama + Llama models (local AI), a Python venv, and the
Supermemory server. Needs macOS with [Homebrew](https://brew.sh); the
model downloads (~7 GB) happen once. Safe to re-run: completed steps
are skipped.

## Use

After install, `coach` works from **any folder** — like `claude`, typing
it bare drops you straight into a conversation with your coach:

```bash
coach                        # talk to your coach (the agent)
coach report                 # classic coaching report
coach report --chat          # report + chat grounded in it
coach report --months 6 --max-games 100 --movetime 0.2
```

The first run is a tiny account setup: the coach asks for your chess.com
username, verifies it actually exists, and never asks again. Pass a
username any time (`coach report someoneelse`, `coach agent someoneelse`)
to switch players.

Reports land in `reports/`. Analysis is cached per game in `data/analysis/`,
so re-runs only pay for new games. Higher `--movetime` = more accurate
classification, linearly slower (0.1 s/move ≈ 6 s per game).

## Scout your next opponent

```bash
coach scout THEIR_USERNAME             # scouting report
coach scout THEIR_USERNAME --chat      # brief with your scout
```

Same engine analysis, pointed at a rival's finished public games: where
they bleed evaluation, the openings they lean on, their worst blunders,
and a **game plan** for facing them. With Supermemory, a **Scout's
memory** section recalls what you noted about them last time. Pre-game
prep from public archives only — never live assistance.

## Talk to the coach agent (fully local)

```bash
coach                        # bare command = the agent, from any folder
```

The agent turns the coach into something you talk to instead of a
command you run: ask in plain words and the local model *decides what to
do* — fetching and engine-analyzing your games, scouting an opponent you
mention, searching its long-term memory, or saving a note for next time.

```
you › how am I losing games lately?
  ♞ analyzing your games … 7/10        ← transient status, erases itself
coach › Your weakest phase is the endgame (ACPL 172) …

you › scout hikaru                     # unambiguous commands skip the model
you › what did we work on last time?
you › remember this: rook endgames this week
```

Answers stream in live at a calm reading pace; the only thing that stays
on screen is the conversation itself.

The agent has **true session continuity**: when you quit, it distills the
conversation into a session summary and stores the transcript in
Supermemory. Next launch it picks up where you left off — "what did we
talk about yesterday?" and "did I ever mention panicking in time
trouble?" both genuinely work, across restarts, all locally.

The brain is Qwen 2.5 7B on your own Ollama; the hands are the same
Stockfish pipeline and Supermemory store as the CLI. Every analysis it
runs is remembered, every answer is grounded in real engine numbers,
and nothing leaves your machine. Post-game only, as always.

## Chat with your coach (fully local)

```bash
coach report --chat
```

An interactive coach grounded in your report — "why do I keep losing
endgames?", "walk me through my worst blunder" — running entirely on
your machine via Ollama + Qwen 2.5 7B. No cloud, no keys, your games
never leave your Mac.

## License

Copyright © 2026 Praveen Kumar Ponnugupati. All rights reserved.

The source is public for demonstration and evaluation (hackathon judging),
but this is **not** open-source software: no permission is granted to copy,
modify, redistribute, or reuse the code. Feel free to open issues.

## Roadmap

- v2: local web UI with an interactive review board (replay games, step
  through the homework positions)
- Opening drill mode (repertoire mining is already in the report)
- Puzzle export (PGN/Lichess study) from the homework FENs
- MCP server exposing the same tools, so any MCP client (e.g. Claude
  Code) can drive the coach with a bigger brain
- ~~Opponent scouting~~ — shipped (`./coach scout OPPONENT`)
- ~~Agentic CLI~~ — shipped (`./coach agent`)
