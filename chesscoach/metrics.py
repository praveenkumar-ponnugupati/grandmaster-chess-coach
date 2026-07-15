"""Engine-layer coaching metrics — the "why" on top of chesscom's "what".

Pure aggregation over games already analyzed by Stockfish (analyze.py
cache) joined with parsed archive records (chesscom). Nothing here runs
an engine; callers hand in the analysis.

Covers the core coaching numbers: centipawn-loss trend, blunders per
game, phase collapse, and clock analysis from PGN %clk tags — splitting
timeout losses into "was fine, flagged" vs "genuinely outplayed".
"""
from __future__ import annotations

import re

from .report import MIDDLEGAME_PLIES, OPENING_PLIES

_CLK_RE = re.compile(r"\{\[%clk ([\d:.]+)\]\}")

# Eval thresholds (cp, user's view) for judging a timeout loss
FINE_EVAL, LOST_EVAL = 150, -150
PRESSURE_SECONDS = 30


def _chronological(analyzed: list[dict]) -> list[dict]:
    return sorted(analyzed, key=lambda g: g.get("end_time", 0))


def cpl_trend(analyzed: list[dict]) -> list[tuple[int, float]]:
    """(end_time, ACPL) per game, oldest → newest."""
    return [(g["end_time"], g["acpl"]) for g in _chronological(analyzed)]


def blunder_trend(analyzed: list[dict]) -> list[tuple[int, int]]:
    """(end_time, blunder count) per game, oldest → newest."""
    return [(g["end_time"],
             sum(1 for m in g["moves"] if m["class"] == "blunder"))
            for g in _chronological(analyzed)]


def phase_acpl(analyzed: list[dict]) -> dict[str, tuple[float, int, int]]:
    """phase → (ACPL, blunders, moves). Where the eval actually collapses."""
    buckets: dict[str, list[int]] = {"opening": [], "middlegame": [],
                                     "endgame": []}
    for g in analyzed:
        for m in g["moves"]:
            phase = ("opening" if m["ply"] <= OPENING_PLIES
                     else "middlegame" if m["ply"] <= MIDDLEGAME_PLIES
                     else "endgame")
            buckets[phase].append(m["cp_loss"])
    out = {}
    for phase, losses in buckets.items():
        if losses:
            out[phase] = (sum(losses) / len(losses),
                          sum(1 for c in losses if c >= 250), len(losses))
    return out


def _clock_seconds(clk: str) -> float:
    h, m, s = clk.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _user_clocks(pgn: str, user_is_white: bool) -> dict[int, float]:
    """ply → seconds remaining on the user's clock after that move."""
    clks = _CLK_RE.findall(pgn)
    start = 0 if user_is_white else 1
    return {i + 1: _clock_seconds(clks[i])
            for i in range(start, len(clks), 2)}


def clock_report(analyzed: list[dict], parsed_by_uuid: dict[str, dict]) -> dict:
    """Clock truths from %clk tags + engine evals:
    - timeout losses split by the final position's eval (user's view):
      'fine' (was ≥ +1.5), 'outplayed' (≤ −1.5), 'unclear'
    - blunders committed with ≤30s remaining (time-pressure blunders)
    Daily games have no meaningful clock and are skipped."""
    verdicts: dict[str, list[str]] = {"fine": [], "outplayed": [],
                                      "unclear": []}
    pressure_blunders = 0
    pressure_moves = 0
    for g in analyzed:
        p = parsed_by_uuid.get(g["uuid"])
        if not p or p["time_class"] == "daily" or not g["moves"]:
            continue
        clocks = _user_clocks(p["pgn"], g["user_is_white"])
        for m in g["moves"]:
            secs = clocks.get(m["ply"])
            if secs is not None and secs <= PRESSURE_SECONDS:
                pressure_moves += 1
                if m["class"] == "blunder":
                    pressure_blunders += 1
        if p["result"] == "loss" and p["termination"] == "timeout":
            final = g["moves"][-1]["eval_after"]
            which = ("fine" if final >= FINE_EVAL
                     else "outplayed" if final <= LOST_EVAL else "unclear")
            verdicts[which].append(p["url"])
    return {"timeout_losses": verdicts,
            "pressure_blunders": pressure_blunders,
            "pressure_moves": pressure_moves}


def split_halves(series: list[tuple[int, float]]) -> tuple[float, float] | None:
    """(older-half average, newer-half average) — the trend in two numbers."""
    if len(series) < 4:
        return None
    vals = [v for _, v in series]
    mid = len(vals) // 2
    older, newer = vals[:mid], vals[mid:]
    return (sum(older) / len(older), sum(newer) / len(newer))
