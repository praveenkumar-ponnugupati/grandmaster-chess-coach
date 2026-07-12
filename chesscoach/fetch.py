"""chess.com public API client — no auth needed, but a UA is required."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

HEADERS = {"User-Agent": "chess-coach-cli (personal post-game coaching tool)"}
ARCHIVES_URL = "https://api.chess.com/pub/player/{user}/games/archives"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


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
