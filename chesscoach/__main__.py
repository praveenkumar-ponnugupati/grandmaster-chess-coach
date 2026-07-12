"""chess-coach CLI: fetch chess.com games, engine-analyze, coach.

Usage:
    python -m chesscoach USERNAME [--months 2] [--max-games 30]
                                  [--movetime 0.1] [--out reports]
"""
import argparse
import shutil
import sys
from pathlib import Path

import chess.engine

from .analyze import analyze_game
from .fetch import fetch_games
from .report import build_report


def main() -> int:
    p = argparse.ArgumentParser(prog="chess-coach")
    p.add_argument("username", help="chess.com username")
    p.add_argument("--months", type=int, default=2, help="monthly archives to fetch (default 2)")
    p.add_argument("--max-games", type=int, default=30, help="analyze at most N most recent games")
    p.add_argument("--movetime", type=float, default=0.1,
                   help="engine seconds per position (default 0.1 ≈ 6 s per game)")
    p.add_argument("--engine", default=shutil.which("stockfish"), help="path to a UCI engine")
    p.add_argument("--out", default="reports", help="report output directory")
    args = p.parse_args()

    if not args.engine:
        print("No UCI engine found — install one with: brew install stockfish", file=sys.stderr)
        return 1

    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"
    print(f"Fetching games for {args.username} …")
    games = fetch_games(args.username, args.months, data_dir / "archives")
    # Rated standard-chess games only; newest first
    games = [g for g in games if g.get("rules") == "chess" and g.get("rated")]
    games.sort(key=lambda g: g.get("end_time", 0), reverse=True)
    games = games[: args.max_games]
    if not games:
        print("No rated games found — check the username or raise --months.", file=sys.stderr)
        return 1
    print(f"Analyzing {len(games)} games at {args.movetime:.2f}s/move "
          "(cached games are instant) …")

    analyzed = []
    with chess.engine.SimpleEngine.popen_uci(args.engine) as engine:
        for i, g in enumerate(games, 1):
            result = analyze_game(g, args.username, engine, args.movetime,
                                  data_dir / "analysis")
            if result and result["moves"]:
                analyzed.append(result)
            print(f"\r  {i}/{len(games)}", end="", flush=True)
    print()

    report = build_report(args.username, analyzed)
    out_dir = root / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{args.username.lower()}-coach-report.md"
    out_file.write_text(report)
    print(report)
    print(f"\nSaved: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
