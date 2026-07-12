"""Coaching report (Markdown) from analyzed games."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

# Game phases by ply for the phase breakdown
OPENING_PLIES = 20
MIDDLEGAME_PLIES = 60

# A blunder from a clearly winning position = a missed win worth drilling
WINNING_EVAL = 150


def build_report(user: str, games: list[dict],
                 past_notes: list[str] | None = None) -> str:
    lines = [f"# Chess coach report — {user}",
             f"_{len(games)} games analyzed · generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC_",
             ""]
    lines += _overview(games)
    lines += _phases(games)
    lines += _openings(games)
    lines += _worst_blunders(games)
    lines += _tactics_homework(games)
    if past_notes:
        lines += ["## Coach's memory", "",
                  "What your coach flagged in earlier sessions "
                  "(recalled via Supermemory) — check yourself against it:", ""]
        lines += [f"> {n.splitlines()[-1] if n.splitlines() else n}" for n in past_notes]
        lines += [""]
    lines += _coach_notes(games)
    return "\n".join(lines)


def coach_note_texts(games: list[dict]) -> list[str]:
    """The advice bullets — shared by the report and the session memory."""
    return _notes(games)


def _score(games) -> str:
    w = sum(1 for g in games if g["user_result"] == "win")
    d = sum(1 for g in games if g["user_result"] == "draw")
    l = len(games) - w - d
    pct = (w + d / 2) / len(games) * 100 if games else 0
    return f"{w}W / {d}D / {l}L ({pct:.0f}%)"


def _overview(games) -> list[str]:
    lines = ["## Overview", ""]
    lines.append(f"- **Score:** {_score(games)}")
    acpl = [g["acpl"] for g in games if g["moves"]]
    if acpl:
        lines.append(f"- **Average centipawn loss (ACPL):** {sum(acpl)/len(acpl):.0f} "
                     "(lower is better; <30 strong, 30–60 club level, >80 needs work)")
    for kind, plural in (("blunder", "Blunders"), ("mistake", "Mistakes"),
                         ("inaccuracy", "Inaccuracies")):
        n = sum(1 for g in games for m in g["moves"] if m["class"] == kind)
        lines.append(f"- **{plural}:** {n} ({n/len(games):.1f}/game)")
    by_tc = defaultdict(list)
    for g in games:
        by_tc[g["time_class"]].append(g)
    if len(by_tc) > 1:
        lines.append("")
        lines.append("| Time control | Games | Score | ACPL |")
        lines.append("|---|---|---|---|")
        for tc, gs in sorted(by_tc.items(), key=lambda kv: -len(kv[1])):
            a = [g["acpl"] for g in gs if g["moves"]]
            lines.append(f"| {tc} | {len(gs)} | {_score(gs)} | "
                         f"{sum(a)/len(a):.0f} |" if a else f"| {tc} | {len(gs)} | {_score(gs)} | – |")
    lines.append("")
    return lines


def _phases(games, title="Where you lose your games") -> list[str]:
    buckets = {"opening": [], "middlegame": [], "endgame": []}
    for g in games:
        for m in g["moves"]:
            if m["ply"] <= OPENING_PLIES:
                buckets["opening"].append(m["cp_loss"])
            elif m["ply"] <= MIDDLEGAME_PLIES:
                buckets["middlegame"].append(m["cp_loss"])
            else:
                buckets["endgame"].append(m["cp_loss"])
    lines = [f"## {title}", "",
             "| Phase | Moves | ACPL | Blunders |", "|---|---|---|---|"]
    for phase, losses in buckets.items():
        if not losses:
            continue
        blunders = sum(1 for c in losses if c >= 250)
        lines.append(f"| {phase.title()} | {len(losses)} | "
                     f"{sum(losses)/len(losses):.0f} | {blunders} |")
    lines.append("")
    return lines


def _openings(games, whose="Your") -> list[str]:
    lines = [f"## {whose} openings", ""]
    for color, flag in (("White", True), ("Black", False)):
        rows = defaultdict(list)
        for g in games:
            if g["user_is_white"] == flag:
                rows[g["opening"]].append(g)
        if not rows:
            continue
        lines.append(f"### As {color}")
        lines.append("")
        lines.append("| Opening | Games | Score | Opening ACPL |")
        lines.append("|---|---|---|---|")
        for name, gs in sorted(rows.items(), key=lambda kv: -len(kv[1]))[:8]:
            early = [m["cp_loss"] for g in gs for m in g["moves"]
                     if m["ply"] <= OPENING_PLIES]
            eacpl = f"{sum(early)/len(early):.0f}" if early else "–"
            lines.append(f"| {name} | {len(gs)} | {_score(gs)} | {eacpl} |")
        lines.append("")
    return lines


def _worst_blunders(games, header="Worst blunders") -> list[str]:
    swings = []
    for g in games:
        for m in g["moves"]:
            # best==played means shallow-search noise, not a real blunder
            if m["class"] == "blunder" and m["best_san"] != m["san"]:
                swings.append((m["cp_loss"], m, g))
    swings.sort(key=lambda t: -t[0])
    lines = [f"## {header}", ""]
    if not swings:
        return lines + ["None found — nice.", ""]
    for cp, m, g in swings[:10]:
        move_no = (m["ply"] + 1) // 2
        lines.append(f"- Move {move_no}: played **{m['san']}**, better was "
                     f"**{m['best_san'] or '?'}** (−{cp / 100:.1f} pawns) — "
                     f"[game]({g['url']})")
    lines.append("")
    return lines


def _tactics_homework(games) -> list[str]:
    """Positions where a winning game was thrown away — replay these."""
    spots = []
    for g in games:
        for m in g["moves"]:
            if (m["class"] == "blunder" and m["eval_before"] >= WINNING_EVAL
                    and m["best_san"] != m["san"]):
                spots.append((m["eval_before"] - m["eval_after"], m, g))
    spots.sort(key=lambda t: -t[0])
    lines = ["## Tactics homework — wins you threw away", "",
             "Set up each position (paste the FEN into any analysis board) "
             "and find the move you missed.", ""]
    if not spots:
        return lines + ["No thrown-away wins in this batch.", ""]
    for i, (swing, m, g) in enumerate(spots[:10], 1):
        move_no = (m["ply"] + 1) // 2
        lines.append(f"{i}. Move {move_no} of [this game]({g['url']}) — "
                     f"you played {m['san']}; the winning idea was {m['best_san'] or '?'}.")
        lines.append(f"   `{m['fen_before']}`")
    lines.append("")
    return lines


def _coach_notes(games) -> list[str]:
    return ["## Coach's notes", ""] + [f"- {n}" for n in _notes(games)] + [""]


def _notes(games) -> list[str]:
    """Heuristic 'what to work on' summary from the aggregates."""
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
        notes.append(f"Your weakest phase is the **{worst[0]}** "
                     f"(ACPL {sum(worst[1])/len(worst[1]):.0f}) — bias study time there.")

    losses = [g for g in games if g["user_result"] == "loss" and g["moves"]]
    if losses:
        decided_late = sum(1 for g in losses
                           if max(g["moves"], key=lambda m: m["cp_loss"])["ply"] > MIDDLEGAME_PLIES)
        if decided_late >= len(losses) / 2:
            notes.append("Most of your losses are decided **after move 30** — "
                         "endgame technique and clock management will pay off most.")

    thrown = sum(1 for g in games for m in g["moves"]
                 if m["class"] == "blunder" and m["eval_before"] >= WINNING_EVAL)
    if thrown:
        notes.append(f"You reached a clearly winning position and blundered it away "
                     f"**{thrown} time(s)** — when winning, slow down and blunder-check "
                     "every capture and check.")

    return notes
