# chess-coach

Post-game coaching from your own chess.com games: fetches your archive via
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

## Roadmap

- v2: local web UI with an interactive review board (replay games, step
  through the homework positions)
- Opening drill mode (repertoire mining is already in the report)
- Puzzle export (PGN/Lichess study) from the homework FENs
