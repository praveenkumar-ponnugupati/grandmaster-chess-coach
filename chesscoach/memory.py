"""Supermemory integration — the coach's long-term memory.

Analyzed games and coaching sessions are stored as Supermemory documents
(container-tagged per player). Before writing a new report the coach
recalls what it flagged in earlier sessions, so advice tracks progress
instead of restarting from zero every run.

Requires SUPERMEMORY_API_KEY in the environment; without it everything
degrades to a no-op and the CLI works standalone.
"""
import json
import os
import urllib.request

API = "https://api.supermemory.ai/v3"
TAG = "chess-coach"


class Supermemory:
    def __init__(self, api_key: str | None = None):
        self.key = api_key or os.environ.get("SUPERMEMORY_API_KEY")

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            API + path,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def remember_game(self, user: str, g: dict) -> None:
        """One document per game. customId = game uuid, so re-running the
        CLI updates instead of duplicating."""
        if not self.enabled:
            return
        color = "White" if g["user_is_white"] else "Black"
        lines = [f"{user} played the {g['opening']} as {color} "
                 f"({g['time_class']}), {g['user_result']}, ACPL {g['acpl']}."]
        for m in g["moves"]:
            if m["class"] == "blunder":
                move_no = (m["ply"] + 1) // 2
                lines.append(
                    f"Blunder on move {move_no}: played {m['san']}, best was "
                    f"{m['best_san'] or '?'} (lost {m['cp_loss']} cp). "
                    f"Position (FEN): {m['fen_before']}"
                )
        self._post("/documents", {
            "content": "\n".join(lines),
            "customId": f"game-{g['uuid']}",
            "containerTags": [TAG, user.lower()],
            "metadata": {
                "kind": "game",
                "result": g["user_result"],
                "opening": g["opening"],
                "time_class": g["time_class"],
                "acpl": g["acpl"],
                "url": g["url"],
            },
        })

    def remember_session(self, user: str, summary: str) -> None:
        if not self.enabled or not summary:
            return
        self._post("/documents", {
            "content": f"Coaching session for {user}:\n{summary}",
            "containerTags": [TAG, user.lower()],
            "metadata": {"kind": "coach-session"},
        })

    def recall_coaching(self, user: str, limit: int = 5) -> list[str]:
        """Earlier coaching advice for this player, most relevant first."""
        if not self.enabled:
            return []
        try:
            res = self._post("/search", {
                "q": f"coaching advice, weaknesses and study plan for {user}",
                "containerTags": [TAG, user.lower()],
                "limit": limit,
            })
        except Exception:
            return []  # memory is an enhancement, never a hard dependency
        notes = []
        for r in res.get("results", []):
            if not isinstance(r, dict):
                continue
            text = (r.get("memory") or r.get("content")
                    or " ".join(c.get("content", "")
                                for c in r.get("chunks", []) if isinstance(c, dict)))
            if text and "Coaching session" in text:
                notes.append(text.strip())
        return notes
