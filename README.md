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

```bash
export SUPERMEMORY_API_KEY=sm_…   # from https://console.supermemory.ai
./venv/bin/python -m chesscoach YOUR_USERNAME
```

No key → the coach runs statelessly; memory is an enhancement, never a
dependency.

## Setup

```bash
brew install stockfish
python3 -m venv venv && ./venv/bin/pip install python-chess
```

## Use

```bash
./venv/bin/python -m chesscoach YOUR_USERNAME            # last 2 months, 30 games
./venv/bin/python -m chesscoach YOUR_USERNAME --months 6 --max-games 100 --movetime 0.2
```

Reports land in `reports/`. Analysis is cached per game in `data/analysis/`,
so re-runs only pay for new games. Higher `--movetime` = more accurate
classification, linearly slower (0.1 s/move ≈ 6 s per game).

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
