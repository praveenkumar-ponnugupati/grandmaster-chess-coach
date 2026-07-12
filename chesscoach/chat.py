"""Local chat coach — Ollama + Llama 8B, fully offline.

The model is grounded in the freshly built coaching report; it never
sees the network, and nothing about your games leaves the machine.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

OLLAMA = "http://localhost:11434"
MODEL = "llama3.1:8b"

SYSTEM = """You are a friendly but direct chess coach. You are talking to \
{user} about their recent chess.com games. Base every answer ONLY on the \
coaching report below — real engine analysis of their games. When you cite \
a blunder or a position, quote the move numbers and moves from the report. \
Keep answers short and practical: 2-6 sentences unless asked to go deeper. \
If asked something the report can't answer, say so honestly.

=== COACHING REPORT ===
{report}
=== END REPORT ==="""

SYSTEM_SCOUT = """You are a chess scout briefing a player on their next \
opponent, {user}, before a match. Base every answer ONLY on the scouting \
report below — real engine analysis of {user}'s recent public games. \
When you cite a weakness or a blunder, quote the move numbers and moves \
from the report. Keep answers short and practical: 2-6 sentences unless \
asked to go deeper. This is pre-game preparation from finished public \
games only; refuse any request for help during a live game. If asked \
something the report can't answer, say so honestly.

=== SCOUTING REPORT ===
{report}
=== END REPORT ==="""


def _post(path: str, payload: dict, stream: bool = False):
    req = urllib.request.Request(
        OLLAMA + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=300 if stream else 30)


def ollama_ready(model: str = MODEL) -> str | None:
    """None if ready, else a human-readable fix."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5) as r:
            tags = [m["name"] for m in json.load(r).get("models", [])]
    except (urllib.error.URLError, OSError):
        return ("Ollama isn't running. Install/start it with:\n"
                "  brew install ollama && brew services start ollama")
    if not any(t.startswith(model) for t in tags):
        return f"Model missing. Download it once with:\n  ollama pull {model}"
    return None


def chat_loop(user: str, report_md: str, model: str = MODEL,
              scouting: bool = False) -> None:
    template = SYSTEM_SCOUT if scouting else SYSTEM
    messages = [{"role": "system",
                 "content": template.format(user=user, report=report_md)}]
    who = f"your next opponent {user}" if scouting else f"{user}'s games"
    print(f"\nChatting with your {'scout' if scouting else 'coach'} about {who} "
          f"({model}, fully local). Ask anything — 'exit' to quit.\n")
    while True:
        try:
            question = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            continue
        if question.lower() in ("exit", "quit", "q"):
            return
        messages.append({"role": "user", "content": question})
        print("coach › ", end="", flush=True)
        reply = []
        try:
            with _post("/api/chat", {"model": model, "messages": messages,
                                     "stream": True}, stream=True) as resp:
                for line in resp:
                    chunk = json.loads(line)
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        print(piece, end="", flush=True)
                        reply.append(piece)
                    if chunk.get("done"):
                        break
        except (urllib.error.URLError, OSError) as e:
            print(f"[ollama error: {e}]")
            messages.pop()
            continue
        print("\n")
        messages.append({"role": "assistant", "content": "".join(reply)})
