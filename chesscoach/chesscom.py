"""chess.com data layer — every public-API access, cache, parse, and
archive-derived stat lives here. This module answers "WHAT happened";
the engine layer (analyze.py / Stockfish) answers "why" and sits on top —
nothing here ever starts an engine, and parsed games keep their PGN so
the engine pass can consume them without re-fetching.

Public API (https://api.chess.com/pub/…): no auth, but a User-Agent is
required. Completed monthly archives are immutable and cached forever in
data/archives/; only the current month is re-fetched.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

HEADERS = {"User-Agent": "chess-coach-cli (personal post-game coaching tool)"}
ARCHIVES_URL = "https://api.chess.com/pub/player/{user}/games/archives"
PROFILE_URL = "https://api.chess.com/pub/player/{user}"

_ECOURL_RE = re.compile(r'\[ECOUrl "([^"]+)"\]')
_ECO_RE = re.compile(r'\[ECO "([^"]+)"\]')

# json result codes → what ended the game / how it was drawn
DRAW_RESULTS = ("agreed", "repetition", "stalemate", "insufficient",
                "50move", "timevsinsufficient")
_TERMINATION = {"checkmated": "checkmate", "timeout": "timeout",
                "resigned": "resignation", "abandoned": "abandonment"}


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def get_profile(user: str) -> dict:
    """Public profile blob (display name, title, …); {} if unavailable —
    callers treat the profile as a nicety, never a requirement."""
    try:
        return _get_json(PROFILE_URL.format(user=user.lower()))
    except Exception:
        return {}


def get_stats(user: str) -> dict:
    """Public rating/record stats; {} if unavailable (nicety, not required)."""
    try:
        return _get_json(PROFILE_URL.format(user=user.lower()) + "/stats")
    except Exception:
        return {}


def player_exists(user: str) -> bool:
    """True if chess.com knows this username."""
    try:
        _get_json(PROFILE_URL.format(user=user.lower()))
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def fetch_games(user: str, months: int, cache_dir: Path) -> list[dict]:
    """Games from the player's most recent monthly archives, newest last.

    Completed months are cached forever; the current month is refetched
    each run because it's still growing.
    """
    user = user.lower()
    archives = _get_json(ARCHIVES_URL.format(user=user))["archives"]
    games: list[dict] = []
    recent = archives[-months:]
    for url in recent:
        year, month = url.rsplit("/", 2)[-2:]
        cache = cache_dir / f"{user}-{year}-{month}.json"
        is_current_month = url == archives[-1]
        if cache.exists() and not is_current_month:
            data = json.loads(cache.read_text())
        else:
            data = _get_json(url)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data))
        games.extend(data.get("games", []))
    return games


def _opening_name(eco_url: str) -> str:
    # https://www.chess.com/openings/Sicilian-Defense-Open-2...Nc6 → readable name
    if "/openings/" not in eco_url:
        return "Unknown"
    slug = eco_url.rsplit("/", 1)[-1]
    # Drop trailing move-list suffixes like "-2...Nc6-3.d4" for grouping
    words = []
    for part in slug.split("-"):
        if part and (part[0].isdigit() or "." in part):
            break
        words.append(part)
    return " ".join(words) or "Unknown"


def _result_of(side: dict) -> str:
    raw = side.get("result", "?")
    if raw == "win":
        return "win"
    if raw in DRAW_RESULTS:
        return "draw"
    return "loss"


def parse_game(g: dict, user: str) -> dict | None:
    """One archive game → a flat record of what happened. None if the
    user isn't in the game. The raw PGN rides along for the engine pass."""
    user = user.lower()
    white, black = g.get("white") or {}, g.get("black") or {}
    if white.get("username", "").lower() == user:
        mine, theirs, color = white, black, "white"
    elif black.get("username", "").lower() == user:
        mine, theirs, color = black, white, "black"
    else:
        return None
    result = _result_of(mine)
    # The loser's result code says how the game ended; draws name the kind
    if result == "draw":
        termination = mine.get("result", "draw")
    else:
        loser = theirs if result == "win" else mine
        termination = _TERMINATION.get(loser.get("result", ""), "other")
    pgn = g.get("pgn") or ""
    eco_url = _ECOURL_RE.search(pgn)
    eco = _ECO_RE.search(pgn)
    return {
        "uuid": g.get("uuid", ""),
        "url": g.get("url", ""),
        "end_time": g.get("end_time", 0),
        "time_class": g.get("time_class", "?"),
        "rated": bool(g.get("rated")),
        "color": color,
        "opponent": theirs.get("username", "?"),
        "opp_rating": theirs.get("rating"),
        "my_rating": mine.get("rating"),
        "result": result,
        "termination": termination,
        "opening": _opening_name(eco_url.group(1)) if eco_url else "Unknown",
        "eco": eco.group(1) if eco else None,
        "pgn": pgn,
    }


def parse_games(games: list[dict], user: str) -> list[dict]:
    """Newest-first parsed records (input order is preserved)."""
    return [p for p in (parse_game(g, user) for g in games) if p]


# ── Derived stats — pure aggregation over parsed games, no engine ──────────

WEEK_SECONDS = 7 * 86400
SIMILAR_BAND = 100  # ± rating points that still counts as "similar"


def rating_trends(parsed: list[dict]
                  ) -> list[tuple[str, list[int], int | None]]:
    """Per time class (most-played first): chronological ratings for the
    last ≤15 games and the change vs one week ago (None when the window
    holds nothing that old)."""
    per: dict[str, list[tuple[int, int]]] = {}
    for p in parsed:  # newest first
        if p["my_rating"]:
            per.setdefault(p["time_class"], []).append(
                (p["end_time"], p["my_rating"]))
    out = []
    for tc, entries in sorted(per.items(), key=lambda kv: -len(kv[1])):
        latest_t, latest_r = entries[0]
        delta = next((latest_r - r for t, r in entries
                      if t <= latest_t - WEEK_SECONDS), None)
        out.append((tc, [r for _, r in reversed(entries[:15])], delta))
    return out


def record(parsed: list[dict]) -> dict:
    """W/L/D overall and sliced by color and time control, plus the
    recent-results streak (oldest → newest)."""
    def tally(rows):
        w = sum(1 for p in rows if p["result"] == "win")
        l = sum(1 for p in rows if p["result"] == "loss")
        d = sum(1 for p in rows if p["result"] == "draw")
        return (w, l, d)
    by_color = {c: tally([p for p in parsed if p["color"] == c])
                for c in ("white", "black")}
    tcs = {p["time_class"] for p in parsed}
    by_tc = {tc: tally([p for p in parsed if p["time_class"] == tc])
             for tc in sorted(tcs)}
    return {"overall": tally(parsed), "by_color": by_color,
            "by_time_class": by_tc,
            "streak": [p["result"] for p in reversed(parsed[:12])]}


def opening_records(parsed: list[dict]) -> dict[str, tuple[int, int, int]]:
    """'Opening (Color)' → (wins, losses, draws)."""
    recs: dict[str, tuple[int, int, int]] = {}
    for p in parsed:
        if p["opening"] in ("Unknown", "Undefined"):
            continue
        key = f"{p['opening']} ({p['color'].title()})"
        w, l, d = recs.get(key, (0, 0, 0))
        recs[key] = (w + (p["result"] == "win"),
                     l + (p["result"] == "loss"),
                     d + (p["result"] == "draw"))
    return recs


def rating_buckets(parsed: list[dict], band: int = SIMILAR_BAND) -> dict:
    """W/L/D vs lower / similar / higher-rated opponents — surfaces
    'beats weaker players, folds against stronger'."""
    buckets = {"lower": [0, 0, 0], "similar": [0, 0, 0], "higher": [0, 0, 0]}
    idx = {"win": 0, "loss": 1, "draw": 2}
    for p in parsed:
        if not (p["my_rating"] and p["opp_rating"]):
            continue
        diff = p["opp_rating"] - p["my_rating"]
        which = ("higher" if diff > band
                 else "lower" if diff < -band else "similar")
        buckets[which][idx[p["result"]]] += 1
    return {k: tuple(v) for k, v in buckets.items()}


def endings(parsed: list[dict]) -> dict[str, dict[str, int]]:
    """How games end, split by outcome: {'win'|'loss'|'draw':
    {termination: count}}. Timeout share of losses is the zero-engine
    coaching insight this exists for."""
    out: dict[str, dict[str, int]] = {"win": {}, "loss": {}, "draw": {}}
    for p in parsed:
        bucket = out[p["result"]]
        bucket[p["termination"]] = bucket.get(p["termination"], 0) + 1
    return out
