"""Shared fetch → analyze → report pipeline.

Both entry points sit on top of this: the classic CLI run
(`./coach USERNAME`, `./coach scout OPPONENT`) and the agent's tools,
so a report means the same thing no matter who asked for it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import chess.engine

from .analyze import analyze_game
from .chesscom import fetch_games
from .memory import Supermemory
from .report import build_report, coach_note_texts
from .scout import build_scout_report, scout_note_texts


class NoGamesError(Exception):
    """The player exists but has no recent rated standard games."""


def rated_recent_games(username: str, months: int, max_games: int,
                       data_dir: Path) -> list[dict]:
    """Most recent rated standard-chess games, newest first.

    Raises urllib.error.HTTPError(404) for unknown players and
    NoGamesError when the archives hold nothing rated.
    """
    games = fetch_games(username, months, data_dir / "archives")
    games = [g for g in games if g.get("rules") == "chess" and g.get("rated")]
    games.sort(key=lambda g: g.get("end_time", 0), reverse=True)
    games = games[:max_games]
    if not games:
        raise NoGamesError(
            f"No rated games found for {username} — check the username or "
            "raise --months.")
    return games


def analyze_and_report(username: str, games: list[dict], *, engine_path: str,
                       movetime: float, data_dir: Path, memory: Supermemory,
                       scouting: bool = False,
                       progress: Callable[[int, int], None] | None = None,
                       ) -> tuple[str, list[dict], list[str]]:
    """Engine-analyze `games` and build the Markdown report.

    Returns (report, analyzed_games, past_notes). Past notes come from
    Supermemory recall; storing the new results is remember_run()'s job
    so callers control when memory is written.
    """
    analyzed = []
    with chess.engine.SimpleEngine.popen_uci(engine_path) as engine:
        for i, g in enumerate(games, 1):
            result = analyze_game(g, username, engine, movetime,
                                  data_dir / "analysis")
            if result and result["moves"]:
                analyzed.append(result)
            if progress:
                progress(i, len(games))

    if scouting:
        past_notes = memory.recall_scouting(username)
        report = build_scout_report(username, analyzed, past_notes=past_notes)
    else:
        past_notes = memory.recall_coaching(username)
        report = build_report(username, analyzed, past_notes=past_notes)
    return report, analyzed, past_notes


def remember_run(memory: Supermemory, username: str, analyzed: list[dict],
                 scouting: bool = False) -> bool:
    """Store the run in long-term memory. True if every write landed."""
    if not memory.enabled:
        return False
    for g in analyzed:
        memory.remember_game(username, g)
    if scouting:
        memory.remember_scout(username, "\n".join(scout_note_texts(analyzed)))
    else:
        memory.remember_session(username, "\n".join(coach_note_texts(analyzed)))
    return memory.enabled  # still true only if no write failed mid-way


def save_report(report: str, username: str, out_dir: Path,
                scouting: bool = False) -> Path:
    out_name = (f"scout-{username.lower()}.md" if scouting
                else f"{username.lower()}-coach-report.md")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / out_name
    out_file.write_text(report)
    return out_file
