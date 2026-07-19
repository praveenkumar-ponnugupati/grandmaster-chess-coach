"""Grandmaster Chess Coach — Gradio edition (free Hugging Face Spaces).

Same chesscoach engine room as the CLI and the FastAPI app; this file is
only the UI layer, shaped for the free Gradio SDK (the Docker SDK is
paid on HF). Stockfish comes from packages.txt (apt) with a
download-a-static-binary fallback for environments where apt packages
aren't available.

Optional Space secrets:
  SUPERMEMORY_API_KEY — per-user coach memory (Supermemory cloud)
  GROQ_API_KEY        — coach-voice narration (Groq free tier)
"""
from __future__ import annotations

import html as html_mod
import json
import os
import shutil
import stat
import tarfile
import threading
import urllib.request
from pathlib import Path

import chess
import chess.engine
import chess.svg
import gradio as gr

from chesscoach.analyze import analyze_game
from chesscoach.chesscom import (endings, fetch_games, get_profile,
                                 opening_records, parse_games, player_exists,
                                 rating_buckets, rating_trends, record)
from chesscoach.memory import Supermemory
from chesscoach.metrics import (blunder_trend, clock_report, cpl_trend,
                                phase_acpl, split_halves)
from chesscoach.report import WINNING_EVAL

DATA = Path(os.environ.get("COACH_DATA", "/tmp/coach-data"))
MOVETIME = float(os.environ.get("MOVETIME", "0.03"))
MAX_GAMES = int(os.environ.get("MAX_GAMES", "8"))
MONTHS = int(os.environ.get("MONTHS", "2"))
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

GOLD, GREEN, RED, DIM = "#ffd75f", "#5fbf5f", "#d75f5f", "#8a8a8a"
_engine_lock = threading.Lock()  # one Stockfish at a time on shared CPU

STOCKFISH_URL = ("https://github.com/official-stockfish/Stockfish/releases/"
                 "latest/download/stockfish-ubuntu-x86-64.tar")


def _engine_path() -> str:
    """apt stockfish (packages.txt) or a one-time static-binary download."""
    found = os.environ.get("STOCKFISH") or shutil.which("stockfish")
    if found:
        return found
    dest = DATA / "stockfish"
    if dest.exists():
        return str(dest)
    DATA.mkdir(parents=True, exist_ok=True)
    tar_path = DATA / "sf.tar"
    urllib.request.urlretrieve(STOCKFISH_URL, tar_path)
    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            if m.isfile() and "stockfish" in Path(m.name).name:
                m.name = "stockfish"
                tf.extract(m, DATA)
                break
    tar_path.unlink(missing_ok=True)
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    return str(dest)


def esc(s) -> str:
    return html_mod.escape(str(s))


def _bar(pct: float, tone: str | None = None, width: int = 120) -> str:
    tone = tone or (GREEN if pct >= 55 else RED if pct <= 45 else DIM)
    pct = max(0.0, min(100.0, pct))
    return (f'<span style="display:inline-block;width:{width}px;height:10px;'
            f'background:#2c2c2c;border-radius:2px;vertical-align:middle">'
            f'<span style="display:block;width:{pct:.0f}%;height:100%;'
            f'background:{tone};border-radius:2px"></span></span>')


def _pct(w: int, l: int, d: int) -> float:
    n = w + l + d
    return (w + d / 2) / n * 100 if n else 0.0


def _card(title: str, body: str) -> str:
    return (f'<div style="background:#1c1c1c;border:1px solid #2c2c2c;'
            f'border-radius:10px;padding:16px 18px;margin-top:14px;'
            f'font-family:ui-monospace,Menlo,Consolas,monospace;color:#ddd">'
            f'<div style="color:{GOLD};font-size:13px;letter-spacing:1.5px;'
            f'text-transform:uppercase;margin-bottom:10px">{title}</div>'
            f'{body}</div>')


def _rated_recent(user: str) -> list[dict]:
    games = fetch_games(user, MONTHS, DATA / "archives")
    games = [g for g in games if g.get("rules") == "chess" and g.get("rated")]
    games.sort(key=lambda g: g.get("end_time", 0), reverse=True)
    return games


def stats_html(user: str) -> str:
    parsed = parse_games(_rated_recent(user), user)
    name = get_profile(user).get("name") or user
    rows = []
    for tc, ratings, delta in rating_trends(parsed)[:4]:
        lo, hi = min(ratings), max(ratings)
        pos = (ratings[-1] - lo) / (hi - lo) * 100 if hi > lo else 50
        move = delta if delta is not None else ratings[-1] - ratings[0]
        tone = GREEN if move > 0 else RED if move < 0 else DIM
        arrow = (f'<span style="color:{GREEN}">↑{move}</span>' if move > 0
                 else f'<span style="color:{RED}">↓{-move}</span>' if move < 0
                 else f'<span style="color:{DIM}">→</span>')
        rows.append(f'<div>{esc(tc):s} <b style="color:{GOLD}">'
                    f'{ratings[-1]}</b> {_bar(pos, tone)} {arrow}</div>')
    rec = record(parsed)
    rec_rows = [("overall", rec["overall"]),
                ("as White", rec["by_color"]["white"]),
                ("as Black", rec["by_color"]["black"])] + \
               list(rec["by_time_class"].items())
    rec_html = ""
    for label, (w, l, d) in rec_rows:
        if not w + l + d:
            continue
        p = _pct(w, l, d)
        tone = GREEN if p >= 55 else RED if p <= 45 else DIM
        rec_html += (f'<div>{esc(label)} — {w}W/{l}L/{d}D {_bar(p)} '
                     f'<span style="color:{tone}">{p:.0f}%</span></div>')
    streak = "".join(
        f'<span style="display:inline-block;width:13px;height:13px;'
        f'margin-right:3px;border-radius:2px;background:'
        f'{GREEN if r == "win" else RED if r == "loss" else DIM}"></span>'
        for r in rec["streak"])
    rec_html += (f'<div style="margin-top:8px">last {len(rec["streak"])}: '
                 f'{streak} <span style="color:{DIM}">(oldest → newest)'
                 f'</span></div>')
    ops = sorted(opening_records(parsed).items(), key=lambda kv: -sum(kv[1]))
    flagged = min((o for o in ops if sum(o[1]) >= 3),
                  key=lambda kv: _pct(*kv[1]), default=None)
    ops_html = ""
    for opname, (w, l, d) in ops[:10]:
        p = _pct(w, l, d)
        flag = (f' <b style="color:{RED}">◀ fix this</b>'
                if flagged and opname == flagged[0] else "")
        ops_html += (f'<div>{esc(opname)} — {w}W/{l}L/{d}D '
                     f'{_bar(p)} {p:.0f}%{flag}</div>')
    ends = endings(parsed)
    loss = ends.get("loss", {})
    total = sum(loss.values())
    ends_html = ""
    if total:
        parts = " · ".join(f"{esc(k)} {v} ({v / total * 100:.0f}%)"
                           for k, v in sorted(loss.items(),
                                              key=lambda kv: -kv[1]))
        ends_html = f"<div>losses by: {parts}</div>"
        t = loss.get("timeout", 0)
        if t / total >= 0.25:
            ends_html += (f'<div style="color:{RED};font-weight:700;'
                          f'margin-top:6px">◀ {t} of {total} losses are on '
                          f'time — that\'s clock, not chess</div>')
    for k, (w, l, d) in rating_buckets(parsed).items():
        n = w + l + d
        if n:
            ends_html += (f'<div>vs {esc(k)} {_bar(_pct(w, l, d))} '
                          f'{_pct(w, l, d):.0f}% <span style="color:{DIM}">'
                          f'({n} games)</span></div>')
    return (_card(f"{esc(name)} · {len(parsed)} rated games",
                  "".join(rows) or f'<span style="color:{DIM}">no rated '
                                   f'games in the window</span>')
            + _card("record", rec_html)
            + _card("openings — where the points leak", ops_html or "–")
            + _card("how your games end", ends_html or "–"))


def _board_svg(fen: str, played: str | None, best: str | None) -> str:
    board = chess.Board(fen)
    arrows = []
    for san, color in ((played, "#b03a3a"), (best, "#3a8f3a")):
        if san:
            try:
                mv = board.parse_san(san)
                arrows.append(chess.svg.Arrow(mv.from_square, mv.to_square,
                                              color=color))
            except ValueError:
                pass
    return chess.svg.board(board, arrows=arrows, size=300,
                           colors={"square light": "#c9c4b4",
                                   "square dark": "#6b675c"})


def analyze_stream(user: str):
    """Generator: progress lines, then the full engine panel as HTML."""
    user = (user or "").strip().lower()
    if not user:
        yield "enter a username first"
        return
    raw = _rated_recent(user)[:MAX_GAMES]
    parsed = parse_games(raw, user)
    analyzed = []
    engine_path = _engine_path()
    with _engine_lock:
        with chess.engine.SimpleEngine.popen_uci(engine_path) as engine:
            for i, g in enumerate(raw, 1):
                yield (f'<div style="color:{DIM}">♞ engine thinking … '
                       f'{i}/{len(raw)}</div>')
                r = analyze_game(g, user, engine, MOVETIME, DATA / "analysis")
                if r and r["moves"]:
                    analyzed.append(r)
    if not analyzed:
        yield "no analyzable games found"
        return
    body = ""
    halves = split_halves(cpl_trend(analyzed))
    if halves:
        o, n = halves
        up = n < o
        body += (f'<div>accuracy: CPL {o:.0f} → <b style="color:{GOLD}">'
                 f'{n:.0f}</b> <span style="color:{GREEN if up else RED}">'
                 f'{"↓ improving" if up else "↑ worsening"}</span></div>')
    halves = split_halves(blunder_trend(analyzed))
    if halves:
        o, n = halves
        up = n < o
        body += (f'<div>blunders: {o:.1f} → <b style="color:{GOLD}">{n:.1f}'
                 f'</b>/game <span style="color:{GREEN if up else RED}">'
                 f'{"↓ improving" if up else "↑ worsening"}</span></div>')
    phases = phase_acpl(analyzed)
    if phases:
        top = max(a for a, _, _ in phases.values())
        worst = max(phases, key=lambda k: phases[k][0])
        body += (f'<div style="color:{DIM};margin-top:8px">where the eval '
                 f'collapses:</div>')
        for ph in ("opening", "middlegame", "endgame"):
            if ph not in phases:
                continue
            acpl, bl, _n = phases[ph]
            tone = RED if ph == worst else DIM
            mark = (f' <b style="color:{RED}">◀ collapse</b>'
                    if ph == worst else "")
            body += (f'<div>{ph} {_bar(acpl / top * 100, tone)} '
                     f'{acpl:.0f} cp/move · {bl} blunders{mark}</div>')
    clocks = clock_report(analyzed, {p["uuid"]: p for p in parsed})
    tl = clocks["timeout_losses"]
    if any(tl.values()):
        body += (f'<div style="margin-top:8px">timeout losses: '
                 f'<b style="color:{RED}">{len(tl["fine"])} while FINE</b> · '
                 f'{len(tl["outplayed"])} outplayed · '
                 f'{len(tl["unclear"])} unclear</div>')
    if clocks["pressure_moves"]:
        body += (f'<div>under 30s: {clocks["pressure_blunders"]} blunders '
                 f'in {clocks["pressure_moves"]} moves</div>')
    homework = [(m, g) for g in analyzed for m in g["moves"]
                if m["class"] == "blunder" and m["best_san"] != m["san"]
                and m["eval_before"] >= WINNING_EVAL]
    homework.sort(key=lambda t: -t[0]["cp_loss"])
    if homework:
        body += (f'<div style="color:{DIM};margin:12px 0 6px">wins you threw '
                 f'away — find the green move:</div>'
                 f'<div style="display:flex;flex-wrap:wrap;gap:14px">')
        for m, g in homework[:6]:
            cap = (f'move {(m["ply"] + 1) // 2}: played '
                   f'<span style="color:{RED}">{esc(m["san"])}</span>, best '
                   f'<span style="color:{GREEN}">{esc(m["best_san"])}</span> '
                   f'(−{m["cp_loss"] / 100:.1f} pawns) · '
                   f'<a style="color:{GOLD}" href="{esc(g["url"])}" '
                   f'target="_blank" rel="noopener">game</a>')
            body += (f'<figure style="margin:0;max-width:300px">'
                     f'{_board_svg(m["fen_before"], m["san"], m["best_san"])}'
                     f'<figcaption style="font-size:12px;color:{DIM};'
                     f'margin-top:4px">{cap}</figcaption></figure>')
        body += "</div>"
    result = _card("engine deep-dive", body)
    mem = Supermemory()
    if mem.enabled:
        worst_ph = max(phases, key=lambda k: phases[k][0]) if phases else "?"
        mem.remember_session(user, (
            f"Web session: analyzed {len(analyzed)} games; weakest phase "
            f"{worst_ph}; {len(homework)} thrown-away wins."))
        notes = mem.recall_coaching(user)
        if notes:
            result += _card("coach's memory", "".join(
                f'<div style="color:{DIM};margin-bottom:6px">▏ {esc(n)}</div>'
                for n in notes[:5]))
    voice = _narrate(user, analyzed, phases, loss_endings(parsed))
    if voice:
        result += _card("coach says", (
            f'<div style="border-left:3px solid {GOLD};padding:8px 12px;'
            f'line-height:1.55">{esc(voice)}</div>'))
    yield result


def loss_endings(parsed: list[dict]) -> dict:
    return endings(parsed).get("loss", {})


def _narrate(user, analyzed, phases, loss) -> str | None:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    facts = [f"{ph} ACPL {a:.0f}" for ph, (a, _b, _n) in phases.items()]
    total = sum(loss.values())
    if total:
        facts.append(f"{loss.get('timeout', 0)} of {total} losses on time")
    prompt = (f"You are a warm, direct chess coach. Player: {user}. "
              f"Engine-verified facts: {'; '.join(facts)}. In 3-4 sentences "
              "tell them the single most important thing to fix and one "
              "encouraging observation. No lists.")
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps({"model": GROQ_MODEL, "messages": [
            {"role": "user", "content": prompt}]}).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["choices"][0]["message"]["content"]
    except Exception:
        return None


def lookup(user: str):
    user = (user or "").strip().lower()
    if not user:
        return "enter a chess.com username"
    if not player_exists(user):
        return (f'<div style="color:{RED}">chess.com doesn\'t know '
                f'"{esc(user)}" — check the spelling</div>')
    try:
        return stats_html(user)
    except Exception as e:
        return f'<div style="color:{RED}">failed: {esc(e)}</div>'


CSS = """
.gradio-container { background:#121212 !important; max-width:900px !important;
                    margin:0 auto !important; }
footer { display:none !important; }
"""

with gr.Blocks(title="♞ Grandmaster Chess Coach") as demo:
    gr.HTML(f'<h1 style="color:{GOLD};font-family:ui-monospace,Menlo,'
            f'monospace;letter-spacing:2px;margin-bottom:0">♞ GRANDMASTER'
            f'</h1><div style="color:{DIM};font-family:ui-monospace,Menlo,'
            f'monospace;font-size:13px">your chess coach — enter a chess.com '
            f'username, see the flaws, fix them</div>')
    with gr.Row():
        user_in = gr.Textbox(show_label=False, scale=4,
                             placeholder="chess.com username, e.g. hikaru")
        go = gr.Button("look me up", scale=1, variant="primary")
    stats_out = gr.HTML()
    analyze_btn = gr.Button("run engine analysis (~30–60s)")
    analysis_out = gr.HTML()
    go.click(lookup, user_in, stats_out)
    user_in.submit(lookup, user_in, stats_out)
    analyze_btn.click(analyze_stream, user_in, analysis_out)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860, css=CSS,
                theme=gr.themes.Monochrome())
