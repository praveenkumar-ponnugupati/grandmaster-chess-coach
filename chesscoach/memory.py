"""Supermemory integration — the coach's long-term memory.

Analyzed games and coaching sessions are stored as Supermemory documents
(container-tagged per player). Before writing a new report the coach
recalls what it flagged in earlier sessions, so advice tracks progress
instead of restarting from zero every run.

Requires SUPERMEMORY_API_KEY in the environment; without it everything
degrades to a no-op and the CLI works standalone.
"""
from __future__ import annotations

import json
import os
import urllib.request

# Cloud by default; point SUPERMEMORY_BASE_URL at a self-hosted instance
# (e.g. http://localhost:6767) for a fully local stack.
DEFAULT_BASE = "https://api.supermemory.ai"
TAG = "chess-coach"


class Supermemory:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.key = api_key or os.environ.get("SUPERMEMORY_API_KEY")
        base = (base_url or os.environ.get("SUPERMEMORY_BASE_URL", DEFAULT_BASE))
        self.api = base.rstrip("/") + "/v3"

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _post_safe(self, path: str, payload: dict) -> bool:
        """Memory is an enhancement — a failed write must never kill the
        report. Returns False (and disables further writes) on auth/API
        errors so a bad key produces one warning, not N."""
        try:
            self._post(path, payload)
            return True
        except Exception as e:
            print(f"Supermemory: write failed ({e}) — continuing without memory")
            self.key = None
            return False

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.api + path,
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
        self._post_safe("/documents", {
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
        self._post_safe("/documents", {
            "content": f"Coaching session for {user}:\n{summary}",
            "containerTags": [TAG, user.lower()],
            "metadata": {"kind": "coach-session"},
        })

    def remember_note(self, user: str, note: str) -> None:
        """A free-form coaching note (the agent saves these mid-conversation)."""
        if not self.enabled or not note:
            return
        self._post_safe("/documents", {
            "content": f"Coach note for {user}: {note}",
            "containerTags": [TAG, user.lower()],
            "metadata": {"kind": "coach-note"},
        })

    def remember_session_summary(self, user: str, summary: str) -> None:
        """Durable 'what we covered' distillation of one agent session —
        the narrative half of session continuity."""
        if not self.enabled or not summary:
            return
        self._post_safe("/documents", {
            "content": f"Session summary for {user}: {summary}",
            "containerTags": [TAG, user.lower()],
            "metadata": {"kind": "session-summary"},
        })

    def remember_transcript(self, user: str, transcript: str) -> None:
        """One document per agent session holding the raw conversation —
        the verbatim half of session continuity (Supermemory chunks it
        for retrieval; one doc per session keeps write cost at one pass)."""
        if not self.enabled or not transcript:
            return
        self._post_safe("/documents", {
            "content": f"Chat transcript for {user}: {transcript}",
            "containerTags": [TAG, user.lower()],
            "metadata": {"kind": "chat-transcript"},
        })

    def remember_scout(self, opponent: str, summary: str) -> None:
        if not self.enabled or not summary:
            return
        self._post_safe("/documents", {
            "content": f"Scouting report on {opponent}:\n{summary}",
            "containerTags": [TAG, opponent.lower()],
            "metadata": {"kind": "scout-report"},
        })

    def recall_coaching(self, user: str, limit: int = 5) -> list[str]:
        """Earlier coaching advice for this player, most relevant first."""
        return self._recall(
            f"coaching advice, weaknesses and study plan for {user}",
            user, "Coaching session", limit)

    def recall_scouting(self, opponent: str, limit: int = 5) -> list[str]:
        """Earlier scouting notes on this opponent, most relevant first."""
        return self._recall(
            f"scouting report, weaknesses and game plan against {opponent}",
            opponent, "Scouting report", limit)

    def search(self, query: str, user: str, limit: int = 5) -> list[str]:
        """Free-text memory search over everything stored for this player
        (games, sessions, scout reports, notes), most relevant first."""
        if not self.enabled:
            return []
        try:
            res = self._post("/search", {
                "q": query,
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
            if text:
                notes.append(text.strip())
        return notes

    def _recall(self, query: str, user: str, marker: str, limit: int) -> list[str]:
        return [n for n in self.search(query, user, limit) if marker in n]
