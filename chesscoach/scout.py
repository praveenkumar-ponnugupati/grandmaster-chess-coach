"""Scouting report — engine analysis of an opponent's finished public games.

Pre-game preparation from public archives is normal chess practice
(databases, opening books); live assistance is not, and none is offered
here — this only ever reads completed games.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from .report import (MIDDLEGAME_PLIES, OPENING_PLIES, WINNING_EVAL,
                     _openings, _overview, _phases, _score, _worst_blunders)


def build_scout_report(opponent: str, games: list[dict],
                       past_notes: list[str] | None = None) -> str:
    lines = [f"# Scouting report — {opponent}",
             f"_{len(games)} recent games analyzed · generated "
             f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · "
             "post-game prep from public archives, never live assistance_",
             ""]
    lines += _overview(games)
    lines += _phases(games, title="Where they bleed evaluation")
    lines += _openings(games, whose="Their")
    lines += _worst_blunders(games, header="Their worst blunders")
    if past_notes:
        lines += ["## Scout's memory", "",
                  "What you noted about this opponent before "
                  "(recalled via Supermemory):", ""]
        lines += [f"> {n.splitlines()[-1] if n.splitlines() else n}"
                  for n in past_notes]
        lines += [""]
    lines += ["## Game plan", ""]
    lines += [f"- {n}" for n in scout_note_texts(games)]
    lines += [""]
    return "\n".join(lines)


def scout_note_texts(games: list[dict]) -> list[str]:
    """How to play this opponent — shared by the report and the scout memory."""
    notes = []
    phase_loss = defaultdict(list)
    for g in games:
        for m in g["moves"]:
            phase = ("opening" if m["ply"] <= OPENING_PLIES
                     else "middlegame" if m["ply"] <= MIDDLEGAME_PLIES
                     else "endgame")
            phase_loss[phase].append(m["cp_loss"])
    if phase_loss:
        worst = max(phase_loss.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        notes.append(f"Their weakest phase is the **{worst[0]}** "
                     f"(ACPL {sum(worst[1])/len(worst[1]):.0f}) — steer the game there.")

    thrown = sum(1 for g in games for m in g["moves"]
                 if m["class"] == "blunder" and m["eval_before"] >= WINNING_EVAL)
    if thrown:
        notes.append(f"They blundered away winning positions **{thrown} time(s)** — "
                     "even when you stand worse, keep setting problems; they crack.")

    for color, flag in (("White", True), ("Black", False)):
        rows = defaultdict(list)
        for g in games:
            if g["user_is_white"] == flag:
                rows[g["opening"]].append(g)
        if rows:
            name, gs = max(rows.items(), key=lambda kv: len(kv[1]))
            if len(gs) >= 2 and name != "Unknown":
                notes.append(f"As {color} they lean on the **{name}** "
                             f"({len(gs)} games, {_score(gs)}) — prepare your line against it.")
    return notes
