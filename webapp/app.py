"""Grandmaster Chess Coach — web edition (Hugging Face Spaces).

FastAPI wrapper over the same chesscoach modules the CLI uses:
chesscom (archive stats, no engine) answers instantly; Stockfish
analysis runs as a background job with progress polling (one engine at
a time — free-tier CPU is shared). Optional integrations via env:
  SUPERMEMORY_API_KEY  — per-user coach memory (Supermemory cloud)
  GROQ_API_KEY         — coach-voice narration (Groq free tier)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chess
import chess.engine
import chess.svg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

from chesscoach.analyze import analyze_game
from chesscoach.chesscom import (endings, fetch_games, get_profile, get_stats,
                                 opening_records, parse_games, player_exists,
                                 rating_buckets, rating_trends, record)
from chesscoach.memory import Supermemory
from chesscoach.metrics import (blunder_trend, clock_report, cpl_trend,
                                phase_acpl, split_halves)
from chesscoach.report import WINNING_EVAL

DATA = Path(os.environ.get("COACH_DATA", "/tmp/coach-data"))
ENGINE = os.environ.get("STOCKFISH", "/usr/games/stockfish")
MOVETIME = float(os.environ.get("MOVETIME", "0.03"))
MAX_GAMES = int(os.environ.get("MAX_GAMES", "8"))
MONTHS = int(os.environ.get("MONTHS", "2"))
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
STATS_TTL = 300

app = FastAPI(title="Grandmaster Chess Coach")

_stats_cache: dict[str, tuple[float, dict]] = {}
_jobs: dict[str, dict] = {}          # user → {status, progress, result}
_jobs_lock = threading.Lock()
_engine_lock = threading.Lock()      # one Stockfish at a time, ever


def _rated_recent(user: str) -> list[dict]:
    games = fetch_games(user, MONTHS, DATA / "archives")
    games = [g for g in games if g.get("rules") == "chess" and g.get("rated")]
    games.sort(key=lambda g: g.get("end_time", 0), reverse=True)
    return games


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/stats/{user}")
def api_stats(user: str):
    user = user.strip().lower()
    now = time.time()
    hit = _stats_cache.get(user)
    if hit and now - hit[0] < STATS_TTL:
        return hit[1]
    if not player_exists(user):
        raise HTTPException(404, "chess.com doesn't know that username")
    raw = _rated_recent(user)
    parsed = parse_games(raw, user)
    profile = get_profile(user)
    out = {
        "username": user,
        "name": profile.get("name") or user,
        "games": len(parsed),
        "trends": [
            {"time_class": tc, "ratings": ratings, "week_delta": delta}
            for tc, ratings, delta in rating_trends(parsed)],
        "record": record(parsed),
        "openings": [
            {"opening": k, "w": w, "l": l, "d": d}
            for k, (w, l, d) in sorted(opening_records(parsed).items(),
                                       key=lambda kv: -sum(kv[1]))[:10]],
        "endings": endings(parsed),
        "buckets": rating_buckets(parsed),
    }
    _stats_cache[user] = (now, out)
    return out


def _run_analysis(user: str) -> None:
    job = _jobs[user]
    try:
        raw = _rated_recent(user)[:MAX_GAMES]
        parsed = parse_games(raw, user)
        analyzed = []
        with _engine_lock:
            job["status"] = "analyzing"
            with chess.engine.SimpleEngine.popen_uci(ENGINE) as engine:
                for i, g in enumerate(raw, 1):
                    r = analyze_game(g, user, engine, MOVETIME,
                                     DATA / "analysis")
                    if r and r["moves"]:
                        analyzed.append(r)
                    job["progress"] = f"{i}/{len(raw)}"
        if not analyzed:
            job.update(status="error", error="no analyzable games")
            return
        phases = phase_acpl(analyzed)
        homework = []
        blunders = []
        for g in analyzed:
            for m in g["moves"]:
                if m["class"] != "blunder" or m["best_san"] == m["san"]:
                    continue
                item = {"fen": m["fen_before"], "played": m["san"],
                        "best": m["best_san"], "cp": m["cp_loss"],
                        "url": g["url"],
                        "move_no": (m["ply"] + 1) // 2}
                blunders.append(item)
                if m["eval_before"] >= WINNING_EVAL:
                    homework.append(item)
        blunders.sort(key=lambda b: -b["cp"])
        homework.sort(key=lambda b: -b["cp"])
        result = {
            "games": len(analyzed),
            "cpl_halves": split_halves(cpl_trend(analyzed)),
            "blunder_halves": split_halves(blunder_trend(analyzed)),
            "phases": {k: {"acpl": a, "blunders": b, "moves": n}
                       for k, (a, b, n) in phases.items()},
            "clock": clock_report(analyzed,
                                  {p["uuid"]: p for p in parsed}),
            "homework": homework[:6],
            "blunders": blunders[:8],
        }
        job.update(status="done", result=result)
        _remember(user, result)
    except Exception as e:  # job must never die silently
        job.update(status="error", error=str(e))


@app.post("/api/analyze/{user}")
def api_analyze_start(user: str):
    user = user.strip().lower()
    if not player_exists(user):
        raise HTTPException(404, "chess.com doesn't know that username")
    with _jobs_lock:
        job = _jobs.get(user)
        if job and job["status"] in ("queued", "analyzing"):
            return {"status": job["status"]}
        if job and job["status"] == "done":
            return job
        _jobs[user] = {"status": "queued", "progress": "0/0"}
        threading.Thread(target=_run_analysis, args=(user,),
                         daemon=True).start()
    return {"status": "queued"}


@app.get("/api/analyze/{user}")
def api_analyze_poll(user: str):
    job = _jobs.get(user.strip().lower())
    if not job:
        return {"status": "none"}
    return job


@app.get("/api/board.svg")
def api_board(fen: str, played: str | None = None, best: str | None = None):
    try:
        board = chess.Board(fen)
    except ValueError:
        raise HTTPException(400, "bad FEN")
    arrows = []
    for san, color in ((played, "#b03a3a"), (best, "#3a8f3a")):
        if not san:
            continue
        try:
            mv = board.parse_san(san)
            arrows.append(chess.svg.Arrow(mv.from_square, mv.to_square,
                                          color=color))
        except ValueError:
            pass
    svg = chess.svg.board(board, arrows=arrows, size=340,
                          colors={"square light": "#c9c4b4",
                                  "square dark": "#6b675c"})
    return Response(svg, media_type="image/svg+xml")


# ── optional: Supermemory (per-user coach memory) ──────────────────────────

def _remember(user: str, result: dict) -> None:
    mem = Supermemory()
    if not mem.enabled:
        return
    phases = result.get("phases", {})
    worst = max(phases, key=lambda k: phases[k]["acpl"]) if phases else "?"
    mem.remember_session(user, (
        f"Web session: analyzed {result['games']} games; weakest phase "
        f"{worst}; {len(result['blunders'])} notable blunders; "
        f"{len(result['homework'])} thrown-away wins."))


@app.get("/api/memory/{user}")
def api_memory(user: str):
    mem = Supermemory()
    if not mem.enabled:
        return {"enabled": False, "notes": []}
    notes = mem.recall_coaching(user.strip().lower())
    return {"enabled": True, "notes": notes[:5]}


# ── optional: Groq narration (the coach's voice) ───────────────────────────

@app.post("/api/narrate/{user}")
def api_narrate(user: str):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise HTTPException(503, "narration not configured")
    user = user.strip().lower()
    stats = _stats_cache.get(user)
    job = _jobs.get(user)
    facts = []
    if stats:
        s = stats[1]
        w, l, d = s["record"]["overall"]
        facts.append(f"record {w}W/{l}L/{d}D")
        loss_ends = s["endings"].get("loss", {})
        total = sum(loss_ends.values())
        if total:
            t = loss_ends.get("timeout", 0)
            facts.append(f"{t} of {total} losses on time")
    if job and job.get("status") == "done":
        r = job["result"]
        if r["cpl_halves"]:
            facts.append(f"CPL {r['cpl_halves'][0]:.0f}→{r['cpl_halves'][1]:.0f}")
        for ph, v in r["phases"].items():
            facts.append(f"{ph} ACPL {v['acpl']:.0f}")
    if not facts:
        raise HTTPException(400, "run stats/analysis first")
    prompt = (f"You are a warm, direct chess coach. Player: {user}. "
              f"Engine-verified facts: {'; '.join(facts)}. In 3-4 sentences, "
              "tell them the single most important thing to fix and one "
              "encouraging observation. No lists, no headers.")
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps({"model": GROQ_MODEL, "messages": [
            {"role": "user", "content": prompt}]}).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        return {"text": data["choices"][0]["message"]["content"]}
    except Exception as e:
        raise HTTPException(502, f"narration failed: {e}")
