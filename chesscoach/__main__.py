"""chess-coach CLI: fetch chess.com games, engine-analyze, coach.

Usage:
    python -m chesscoach                    # conversational agent (local LLM)
    python -m chesscoach agent [USERNAME]   # same, coaching USERNAME
    python -m chesscoach report [USERNAME] [--months 2] [--max-games 30]
                                [--movetime 0.1] [--out reports] [--chat]
    python -m chesscoach USERNAME           # classic report, same options
    python -m chesscoach scout OPPONENT [same options]
"""
import argparse
import shutil
import sys
import urllib.error
from pathlib import Path

from .chesscom import player_exists
from .memory import Supermemory
from .pipeline import (NoGamesError, analyze_and_report, rated_recent_games,
                       remember_run, save_report)


def _account_setup() -> str:
    """First-run interactive setup: ask who we're coaching, verify the
    account exists on chess.com before accepting it."""
    print("First time here — let's set you up.")
    while True:
        try:
            name = input("Who am I coaching? chess.com username: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if not name:
            continue
        print("Checking chess.com …", end=" ", flush=True)
        if player_exists(name):
            print(f"found you, {name}. I'll remember that.")
            return name
        print(f"chess.com doesn't know '{name}' — check the spelling "
              "(see chess.com/member/<username>).")


def main() -> int:
    p = argparse.ArgumentParser(prog="chess-coach")
    p.add_argument("username", nargs="?",
                   help="nothing or 'agent' for the conversational coach, "
                        "'report' or a chess.com username for the classic "
                        "report, 'scout' to scout an opponent")
    p.add_argument("opponent", nargs="?",
                   help="with 'scout': the opponent's chess.com username; "
                        "with 'agent'/'report': who to coach "
                        "(default: remembered user)")
    p.add_argument("--months", type=int, default=2, help="monthly archives to fetch (default 2)")
    p.add_argument("--max-games", type=int, default=30, help="analyze at most N most recent games")
    p.add_argument("--movetime", type=float, default=0.1,
                   help="engine seconds per position (default 0.1 ≈ 6 s per game)")
    p.add_argument("--engine", default=shutil.which("stockfish"), help="path to a UCI engine")
    p.add_argument("--out", default="reports", help="report output directory")
    p.add_argument("--chat", action="store_true",
                   help="after analysis, chat with the coach about your games "
                        "(fully local: Ollama + Qwen 7B)")
    args = p.parse_args()

    if not args.engine:
        print("No UCI engine found — install one with: brew install stockfish", file=sys.stderr)
        return 1

    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"
    last_user = data_dir / "last-user"
    scouting = args.username == "scout"
    # Like `claude`: a bare `coach` drops straight into the conversation
    agent_mode = args.username in ("agent", None)
    if args.username in ("agent", "report"):
        args.username = args.opponent  # optional explicit player
    if scouting:
        if not args.opponent:
            p.error("usage: coach scout OPPONENT")
        args.username = args.opponent
    elif not args.username:
        if last_user.exists():
            args.username = last_user.read_text().strip()
            print(f"Coaching {args.username} (remembered — pass a username to switch)")
        elif sys.stdin.isatty():
            args.username = _account_setup()
        else:
            p.error("username required on the first run, e.g.: coach magnuscarlsen")

    def remember_username():
        # The username is confirmed real; remember it for next time
        if not scouting:
            data_dir.mkdir(parents=True, exist_ok=True)
            last_user.write_text(args.username.lower())

    if agent_mode:
        from .agent import agent_loop
        from .chat import ollama_ready
        problem = ollama_ready()
        if problem:
            print(problem, file=sys.stderr)
            return 1
        if not player_exists(args.username):
            print(f"chess.com doesn't know a player called '{args.username}' — "
                  "check the spelling (see chess.com/member/<username>).",
                  file=sys.stderr)
            return 1
        remember_username()
        agent_loop(args.username, args.engine, data_dir, root / args.out,
                   movetime=args.movetime)
        return 0

    print(f"Fetching games for {args.username} …")
    try:
        games = rated_recent_games(args.username, args.months, args.max_games,
                                   data_dir)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"chess.com doesn't know a player called '{args.username}' — "
                  "check the spelling (see chess.com/member/<username>).", file=sys.stderr)
            return 1
        raise
    except NoGamesError as e:
        remember_username()  # the fetch itself succeeded
        print(e, file=sys.stderr)
        return 1
    remember_username()
    print(f"Analyzing {len(games)} games at {args.movetime:.2f}s/move "
          "(cached games are instant) …")

    memory = Supermemory()
    report, analyzed, past_notes = analyze_and_report(
        args.username, games, engine_path=args.engine, movetime=args.movetime,
        data_dir=data_dir, memory=memory, scouting=scouting,
        progress=lambda i, n: print(f"\r  {i}/{n}", end="", flush=True))
    print()

    out_file = save_report(report, args.username, root / args.out, scouting)

    if args.chat:
        from .chat import chat_loop, ollama_ready
        problem = ollama_ready()
        if problem:
            print(problem, file=sys.stderr)
            return 1
        chat_loop(args.username, report, scouting=scouting)
        return 0
    from .termmd import render_markdown
    print(render_markdown(report))  # raw markdown when piped
    print(f"\nSaved: {out_file}")

    if memory.enabled:
        if remember_run(memory, args.username, analyzed, scouting):
            what = "scouting notes" if scouting else "this session's advice"
            print(f"Supermemory: remembered {len(analyzed)} games + {what}"
                  f" (recalled {len(past_notes)} earlier note(s))")
    else:
        print("Supermemory: SUPERMEMORY_API_KEY not set — coach ran without long-term memory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
