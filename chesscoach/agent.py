"""Fully local agentic coach — `./coach agent`.

A conversational loop where the model decides what to do: Ollama
(qwen2.5:7b) is the brain, and its hands are the coach's own pipeline
(chess.com fetch → Stockfish analysis → report) plus the Supermemory
store. Everything runs on this machine; nothing about your games leaves it.

Post-game only, like the rest of the project: the system prompt refuses
live-game assistance, and scouting reads finished public games.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import chess
import chess.engine

from .analyze import _user_result as _game_result  # same-package helper
from .board import render_board
from .chat import MODEL, OLLAMA
from .fetch import get_profile, get_stats
from .memory import Supermemory
from .pipeline import (NoGamesError, analyze_and_report, rated_recent_games,
                       remember_run, save_report)

# One user turn may trigger a few tool calls, but an 8B model that keeps
# calling tools is looping, not thinking — cut it off.
MAX_TOOL_ROUNDS = 4
# The model generates faster than people read; cap the on-screen rate so
# answers type out calmly instead of bursting (interactive terminals only)
STREAM_CHARS_PER_SEC = 140
# Reports are a few KB each; leave room for several in the conversation.
NUM_CTX = 16384
# Snappy agent turns: fewer games than a full CLI run (cache makes repeats instant)
AGENT_MAX_GAMES = 10

SYSTEM = """You are a grandmaster-level chess coach talking to {user} in a \
terminal. Call them {nick} — that's how a coach addresses their player; \
use the {user} handle only when referring to the chess.com account. You \
are warm and personable, like a longtime coach who knows their player: \
return greetings naturally, engage briefly with small talk, show genuine \
enthusiasm for wins and empathy for losses. Never say you lack feelings \
or emotions, never mention being an AI or a language model — you are \
simply their coach. After pleasantries, steer gently back to their chess.
You have tools — use them instead of guessing:
- analyze_my_games: anything about {user}'s own recent play, results, \
weaknesses, blunders, openings.
- scout_opponent: when {user} names an opponent they will face.
- recall_memory: past sessions, stored games, earlier advice, progress over time.
- remember_note: save advice or plans worth keeping for future sessions.
- show_position: draw a chess board in the terminal. MANDATORY: before \
answering anything about one specific position or blunder, call \
show_position with its FEN exactly as the report gives it (plus the \
played and best moves), then narrate the idea. Never paste raw FEN \
strings into an answer — the player sees boards, not FENs.
Ground every claim in tool results — never invent games, moves or numbers; \
cite move numbers and moves exactly as the reports state them. If the \
reports can't answer something, say so honestly.
You only ever discuss finished games. If asked for help in a game being \
played right now, refuse briefly: that's cheating.
Keep answers short and practical: 2-6 sentences unless asked to go deeper."""

TOOLS = [
    {"type": "function", "function": {
        "name": "analyze_my_games",
        "description": "Fetch and engine-analyze the player's own recent "
                       "chess.com games; returns a full coaching report "
                       "(score, phase weaknesses, openings, worst blunders, "
                       "tactics homework, coach's memory of past sessions).",
        "parameters": {"type": "object", "properties": {
            "max_games": {"type": "integer",
                          "description": "recent games to analyze (default 10)"},
            "months": {"type": "integer",
                       "description": "monthly archives to search (default 2)"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "scout_opponent",
        "description": "Scout an opponent's finished public chess.com games; "
                       "returns a scouting report with their weaknesses, "
                       "openings and a game plan. Pre-game preparation only.",
        "parameters": {"type": "object", "properties": {
            "opponent": {"type": "string",
                         "description": "the opponent's chess.com username"},
            "max_games": {"type": "integer",
                          "description": "recent games to analyze (default 10)"},
        }, "required": ["opponent"]},
    }},
    {"type": "function", "function": {
        "name": "recall_memory",
        "description": "Search the coach's long-term memory (Supermemory) "
                       "for earlier sessions, stored games, advice or "
                       "scouting notes.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": "what to look for, in plain words"},
            "player": {"type": "string",
                       "description": "whose memories (defaults to the "
                                      "coached player)"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "remember_note",
        "description": "Save a coaching note or plan to long-term memory so "
                       "future sessions recall it.",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string", "description": "the note to remember"},
        }, "required": ["note"]},
    }},
    {"type": "function", "function": {
        "name": "show_position",
        "description": "Draw a chess position as a board in the terminal, "
                       "with the played move marked red, the best move "
                       "green, and an engine eval bar. Use when discussing "
                       "any specific position from a report.",
        "parameters": {"type": "object", "properties": {
            "fen": {"type": "string",
                    "description": "the position FEN, exactly as the report "
                                   "states it"},
            "played_san": {"type": "string",
                           "description": "the move that was actually played"},
            "best_san": {"type": "string",
                         "description": "the better move that was missed"},
        }, "required": ["fen"]},
    }},
]


def _status(text: str) -> None:
    """Transient dim status on the current line (tty only) — overwritten by
    the next status and erased before any real output. The user asked to
    see responses only, but long engine runs need a sign of life."""
    if sys.stdout.isatty():
        print(f"\r\033[2m  ♞ {text}\033[0m\033[K", end="", flush=True)


def _clear_status() -> None:
    if sys.stdout.isatty():
        print("\r\033[K", end="", flush=True)


# A FEN in prose (the model was told not to, but small models slip)
_FEN_RE = re.compile(
    r"(?:[rnbqkpRNBQKP1-8]+/){7}[rnbqkpRNBQKP1-8]+ [wb] "
    r"(?:K?Q?k?q?|-) (?:[a-h][36]|-) \d+ \d+")


def _maybe_show_fen_board(reply: str, tools) -> None:
    """Safety net: if an answer slipped a FEN into prose, draw it anyway."""
    m = _FEN_RE.search(reply)
    if m:
        try:
            tools.call("show_position", {"fen": m.group(0)})
        except Exception:
            pass


# name → {param: declared type}, for scrubbing model-invented arguments
_TOOL_PARAMS = {t["function"]["name"]:
                {k: v.get("type") for k, v in
                 t["function"]["parameters"]["properties"].items()}
                for t in TOOLS}


def _clean_args(name: str, args: dict) -> dict:
    """Small local models invent argument names and pass numbers as strings —
    keep only declared params, coerced to their declared type."""
    declared = _TOOL_PARAMS.get(name, {})
    clean = {}
    for k, v in args.items():
        if k not in declared or v is None:
            continue
        if declared[k] == "integer":
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        elif declared[k] == "string":
            v = str(v)
        clean[k] = v
    return clean


class CoachTools:
    """The agent's hands: each tool returns a plain string for the model."""

    def __init__(self, user: str, engine_path: str, data_dir: Path,
                 out_dir: Path, movetime: float = 0.1):
        self.user = user
        self.engine_path = engine_path
        self.data_dir = data_dir
        self.out_dir = out_dir
        self.movetime = movetime
        self.memory = Supermemory()

    def call(self, name: str, args: dict) -> str:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"Error: unknown tool '{name}'."
        try:
            return handler(**_clean_args(name, args))
        except TypeError:
            return f"Error: bad arguments for {name}: {args}"
        except NoGamesError as e:
            return str(e)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "chess.com doesn't know that player — check the spelling."
            return f"chess.com error: {e}"
        except Exception as e:
            return f"Tool {name} failed: {e}"

    def _run_pipeline(self, username: str, months: int, max_games: int,
                      scouting: bool) -> str:
        games = rated_recent_games(username, months, int(max_games),
                                   self.data_dir)
        verb = "scouting" if scouting else "analyzing"
        _status(f"{verb} {username}'s games …")
        report, analyzed, _ = analyze_and_report(
            username, games, engine_path=self.engine_path,
            movetime=self.movetime, data_dir=self.data_dir,
            memory=self.memory, scouting=scouting,
            progress=lambda i, n: _status(f"{verb} {username}'s games … {i}/{n}"))
        save_report(report, username, self.out_dir, scouting)
        remember_run(self.memory, username, analyzed, scouting)
        return report

    def _tool_analyze_my_games(self, max_games: int = AGENT_MAX_GAMES,
                               months: int = 2) -> str:
        return self._run_pipeline(self.user, months, max_games, scouting=False)

    def _tool_scout_opponent(self, opponent: str,
                             max_games: int = AGENT_MAX_GAMES,
                             months: int = 2) -> str:
        if opponent.lower() == self.user.lower():
            return self._tool_analyze_my_games(max_games, months)
        return self._run_pipeline(opponent, months, max_games, scouting=True)

    def _tool_recall_memory(self, query: str, player: str | None = None) -> str:
        if not self.memory.enabled:
            return ("Long-term memory is unavailable (no Supermemory key) — "
                    "run via ./coach to enable it.")
        notes = self.memory.search(query, player or self.user, limit=8)
        # Raw chat transcripts dilute analysis questions — surface them only
        # when the question is actually about past conversations
        about_chat = re.search(r"\b(talk|conversation|convo|say|said|discuss|"
                               r"chat|tell|told|mention|ask)\w*\b",
                               query, re.IGNORECASE)
        if not about_chat:
            focused = [n for n in notes if not n.startswith("Chat transcript")]
            notes = focused or notes
        if not notes:
            return "No memories found for that."
        return "\n---\n".join(notes[:5])

    def _tool_remember_note(self, note: str) -> str:
        if not self.memory.enabled:
            return "Long-term memory is unavailable — the note was NOT saved."
        self.memory.remember_note(self.user, note)
        return "Saved to long-term memory."

    def _tool_show_position(self, fen: str, played_san: str | None = None,
                            best_san: str | None = None) -> str:
        try:
            board = chess.Board(fen)
        except ValueError:
            return "Error: that is not a valid FEN — copy it exactly from the report."
        cp = engine_best = None
        try:  # a quick engine look gives the eval bar + a best move if missing
            with chess.engine.SimpleEngine.popen_uci(self.engine_path) as eng:
                info = eng.analyse(board, chess.engine.Limit(time=0.2))
                cp = info["score"].white().score(mate_score=1500)
                if info.get("pv"):
                    engine_best = board.san(info["pv"][0])
        except Exception:
            pass
        best = best_san or engine_best
        _clear_status()
        print("\n" + render_board(fen, best=best, played=played_san,
                                  eval_cp=cp) + "\n")
        turn = "White" if board.turn == chess.WHITE else "Black"
        out = f"(board displayed to the player) {turn} to move"
        if cp is not None:
            out += f"; engine eval {cp / 100:+.1f} from White's side"
        if best:
            out += f"; best move here is {best}"
        return out + ". Now narrate the key idea in it for the player."


def _chat_stream(messages: list[dict], model: str, on_text) -> dict:
    """One streamed model response. Text pieces go to `on_text` as they
    arrive; tool calls are collected. Returns the full assistant message."""
    payload = {"model": model, "messages": messages, "tools": TOOLS,
               "stream": True, "options": {"num_ctx": NUM_CTX}}
    req = urllib.request.Request(
        OLLAMA + "/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    content: list[str] = []
    tool_calls: list[dict] = []
    with urllib.request.urlopen(req, timeout=300) as resp:
        for line in resp:
            chunk = json.loads(line)
            msg = chunk.get("message") or {}
            piece = msg.get("content") or ""
            if piece:
                content.append(piece)
                on_text(piece)
            tool_calls.extend(msg.get("tool_calls") or [])
            if chunk.get("done"):
                break
    out = {"role": "assistant", "content": "".join(content)}
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out


def run_turn(question: str, messages: list[dict], tools: CoachTools,
             model: str = MODEL) -> str:
    """One user turn: let the model call tools until it has an answer,
    streaming the answer text live as it is generated. Appends to
    `messages` in place, prints everything, returns the final reply text."""
    messages.append({"role": "user", "content": question})
    pace = sys.stdout.isatty()  # full speed when piped (tests, scripts)
    for _ in range(MAX_TOOL_ROUNDS):
        started = False
        next_at = 0.0
        _status("thinking …")

        def on_text(piece: str) -> None:
            nonlocal started, next_at
            if not started:
                _clear_status()
                print("coach › ", end="", flush=True)
                started = True
                next_at = time.monotonic()
            if not pace:
                print(piece, end="", flush=True)
                return
            for ch in piece:
                wait = next_at - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                print(ch, end="", flush=True)
                next_at = max(next_at, time.monotonic()) \
                    + 1.0 / STREAM_CHARS_PER_SEC

        msg = _chat_stream(messages, model, on_text)
        calls = msg.get("tool_calls") or []
        if not calls:
            reply = (msg.get("content") or "").strip()
            if not started:
                _clear_status()
                print("coach › (no answer — try rephrasing)", end="")
            print("\n")
            _maybe_show_fen_board(reply, tools)
            messages.append({"role": "assistant", "content": reply})
            return reply
        if started:  # the model spoke before deciding to use a tool
            print()
        messages.append(msg)
        for c in calls:
            fn = c.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):  # some models return JSON-as-string
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            _status({
                "analyze_my_games": "analyzing your recent games …",
                "scout_opponent": f"scouting {args.get('opponent', 'them')} …",
                "recall_memory": "checking my memory …",
                "remember_note": "noting that down …",
                "show_position": "setting up the board …",
            }.get(name, "working …"))
            messages.append({"role": "tool", "content": tools.call(name, args)})
    _clear_status()
    reply = ("I hit my tool budget for one question — ask again, a bit "
             "more specifically.")
    print(f"coach › {reply}\n")
    messages.append({"role": "assistant", "content": reply})
    return reply


SUMMARY_PROMPT = """Below is a chess coaching conversation with {user}. \
Distill it into 2-4 short lines capturing only what is worth remembering \
next session: what was discussed, anything {user} revealed about their \
play or habits, plans or commitments made, and specific games or \
weaknesses referenced. Plain lines, no preamble, no headings."""


def _conversation_text(user: str, messages: list[dict]) -> str:
    """The dialogue only — no system prompt, no tool payloads (reports are
    already remembered as game/session documents)."""
    lines = []
    for m in messages:
        if m.get("role") == "user":
            lines.append(f"{user}: {m['content']}")
        elif m.get("role") == "assistant" and (m.get("content") or "").strip():
            lines.append(f"coach: {m['content'].strip()}")
    return "\n".join(lines)


def _summarize(user: str, convo: str, model: str) -> str:
    """One plain (tool-free) model call distilling the session."""
    payload = {"model": model, "stream": False,
               "options": {"num_ctx": NUM_CTX},
               "messages": [{"role": "user",
                             "content": SUMMARY_PROMPT.format(user=user)
                             + "\n\n" + convo[-8000:]}]}
    req = urllib.request.Request(
        OLLAMA + "/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return (json.load(resp).get("message", {}).get("content") or "").strip()


def _persist_session(user: str, messages: list[dict], memory,
                     model: str) -> None:
    """Session continuity: store the raw transcript (verbatim recall) and
    a distilled summary (narrative recall) as one document each."""
    if not memory.enabled:
        return
    convo = _conversation_text(user, messages)
    if "coach:" not in convo:  # nothing coached, nothing to remember
        return
    _status("remembering this session …")
    today = date.today().isoformat()
    memory.remember_transcript(user, f"({today})\n{convo}")
    try:
        summary = _summarize(user, convo, model)
    except Exception:
        summary = ""  # a failed summary must never block exit
    if summary:
        memory.remember_session_summary(user, f"({today}) {summary}")
    _clear_status()


def _nickname(user: str) -> str:
    """Friendly form of address: first name from the chess.com profile,
    else the leading letters of the username ("magnus123" → "Magnus")."""
    name = (get_profile(user).get("name") or "").strip()
    if name:
        first = name.split()[0]
        return first if first[0].isupper() else first.capitalize()
    alpha = re.match(r"[A-Za-z]+", user)
    return (alpha.group(0) if alpha else user).capitalize()


def _last_session_note(memory, user: str) -> str | None:
    """Most relevant previous session summary, for the system prompt."""
    if not memory.enabled:
        return None
    notes = memory.search(f"session summary of coaching {user}", user, limit=5)
    return next((n for n in notes if n.startswith("Session summary")), None)


BANNER = """\
 ██████╗ ██████╗  █████╗ ███╗   ██╗██████╗
██╔════╝ ██╔══██╗██╔══██╗████╗  ██║██╔══██╗
██║  ███╗██████╔╝███████║██╔██╗ ██║██║  ██║
██║   ██║██╔══██╗██╔══██║██║╚██╗██║██║  ██║
╚██████╔╝██║  ██║██║  ██║██║ ╚████║██████╔╝
 ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚═════╝
███╗   ███╗ █████╗ ███████╗████████╗███████╗██████╗
████╗ ████║██╔══██╗██╔════╝╚══██╔══╝██╔════╝██╔══██╗
██╔████╔██║███████║███████╗   ██║   █████╗  ██████╔╝
██║╚██╔╝██║██╔══██║╚════██║   ██║   ██╔══╝  ██╔══██╗
██║ ╚═╝ ██║██║  ██║███████║   ██║   ███████╗██║  ██║
╚═╝     ╚═╝╚═╝  ╚═╝╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝"""


SPARK_CHARS = "▁▂▃▄▅▆▇█"
WEEK_SECONDS = 7 * 86400


def _spark(vals: list[int]) -> str:
    """Values mapped onto ▁▂▃▄▅▆▇█ (min–max scaled)."""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return "▄" * len(vals)
    return "".join(SPARK_CHARS[round((v - lo) / (hi - lo) * 7)] for v in vals)


def _my_side(game: dict, user: str) -> dict:
    white = game.get("white") or {}
    if white.get("username", "").lower() == user.lower():
        return white
    return game.get("black") or {}


def _rating_trends(user: str, games: list[dict]
                   ) -> list[tuple[str, list[int], int | None]]:
    """Per time class (most-played first): chronological ratings for the
    last ≤15 games and the rating change vs one week ago (None if the
    window has no game that old). `games` arrive newest-first."""
    per: dict[str, list[tuple[int, int]]] = {}
    for g in games:
        rating = _my_side(g, user).get("rating")
        if not rating:
            continue
        per.setdefault(g.get("time_class", "?"), []).append(
            (g.get("end_time", 0), rating))
    out = []
    for tc, entries in sorted(per.items(), key=lambda kv: -len(kv[1])):
        latest_t, latest_r = entries[0]
        delta = next((latest_r - r for t, r in entries
                      if t <= latest_t - WEEK_SECONDS), None)
        ratings = [r for _, r in reversed(entries[:15])]
        out.append((tc, ratings, delta))
    return out


def _streak(user: str, games: list[dict], n: int = 12) -> list[str]:
    """win/draw/loss of the last n games, oldest → newest."""
    results = []
    for g in reversed(games[:n]):
        is_white = (g.get("white") or {}).get(
            "username", "").lower() == user.lower()
        results.append(_game_result(g, is_white))
    return results


def _stats_line(stats: dict) -> str | None:
    """'blitz 100 · rapid 526 · 39W/53L/4D lifetime' from chess.com stats."""
    parts, w, l, d = [], 0, 0, 0
    for key, label in (("chess_blitz", "blitz"), ("chess_rapid", "rapid"),
                       ("chess_bullet", "bullet")):
        tc = stats.get(key) or {}
        rating = (tc.get("last") or {}).get("rating")
        if rating:
            parts.append(f"{label} {rating}")
        rec = tc.get("record") or {}
        w += rec.get("win") or 0
        l += rec.get("loss") or 0
        d += rec.get("draw") or 0
    if w + l + d:
        parts.append(f"{w}W/{l}L/{d}D lifetime")
    return " · ".join(parts) if parts else None


def _watchlist(last_note: str | None) -> str | None:
    """One phrase from the last session worth flashing at startup."""
    if not last_note:
        return None
    for key, label in (("endgame", "endgame technique"),
                       ("clock", "clock management"),
                       ("time trouble", "clock management"),
                       ("middlegame", "middlegame play"),
                       ("opening", "opening prep"),
                       ("blunder", "blunder-checking when winning"),
                       ("tactic", "tactics")):
        if key in last_note.lower():
            return label
    return None


def _print_banner(user: str, engine_path: str, memory: Supermemory,
                  model: str, nick: str | None = None,
                  stats: dict | None = None,
                  watchlist: str | None = None,
                  games: list[dict] | None = None) -> None:
    tty = sys.stdout.isatty()
    def c(code):
        return f"\033[{code}m" if tty else ""
    gold, dim, green, red, off = c("1;33"), c("2"), c("32"), c("1;31"), c("0")
    print(f"\n{gold}{BANNER}{off}")
    print(f"{dim}        ♞  your grandmaster chess coach — "
          f"100% local, nothing leaves this machine{off}\n")
    who = f"{nick} ({user})" if nick and nick.lower() != user.lower() else user
    print(f"   Coaching {gold}{who}{off}")
    trends = _rating_trends(user, games or [])
    if trends:
        for tc, ratings, delta in trends[:3]:
            line = f"{tc:<6} {gold}{ratings[-1]}{off} {_spark(ratings)}"
            if delta:
                arrow = (f"{green}↑{delta}" if delta > 0
                         else f"{red}↓{-delta}")
                line += f" {arrow}{off}{dim} this week{off}"
            print(f"   {line}")
        marks = {"win": f"{green}█", "loss": f"{red}█", "draw": f"{dim}█"}
        streak = _streak(user, games or [])
        if streak:
            blocks = "".join(marks[r] for r in streak) + off
            print(f"   {dim}last {len(streak)}:{off} {blocks} "
                  f"{dim}(oldest → newest){off}")
    else:  # no recent games fetched — fall back to profile snapshot
        line = _stats_line(stats or {})
        if line:
            print(f"   {line}")
    if watchlist:
        print(f"   ♞ coach's watchlist: {gold}{watchlist}{off}"
              f"{dim} — from your last session{off}")
    # Engine and brain were health-checked before the loop started; memory
    # is the only component that can be down here — whisper when healthy,
    # shout only when something is actually wrong
    if memory.enabled:
        print(f"\n{dim}   all local: Stockfish + {model} + Supermemory "
              f"{off}{green}✓{off}")
    else:
        print(f"\n   {red}✗ Memory{off} {dim}Supermemory is OFF — memories "
              f"won't be kept; run via ./coach to enable{off}")
    print(f'{dim}   "how am I losing games?" · "scout hikaru" · '
          f'"what did we work on last time?" · exit to quit{off}\n')


def agent_loop(user: str, engine_path: str, data_dir: Path, out_dir: Path,
               movetime: float = 0.1, model: str = MODEL) -> None:
    tools = CoachTools(user, engine_path, data_dir, out_dir, movetime)
    nick = _nickname(user)
    system = SYSTEM.format(user=user, nick=nick)
    last_note = _last_session_note(tools.memory, user)
    if last_note:
        system += ("\n\nWhat you remember from your previous session "
                   "(use it to keep continuity — refer back naturally):\n"
                   + last_note)
    messages = [{"role": "system", "content": system}]
    try:  # recent games power the header trends; never block startup on them
        recent = rated_recent_games(user, 2, 60, data_dir)
    except Exception:
        recent = []
    _print_banner(user, engine_path, tools.memory, model, nick,
                  stats=get_stats(user), watchlist=_watchlist(last_note),
                  games=recent)
    try:
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

            # "scout USERNAME" is unambiguous — run it directly, no model round-trip
            m = re.fullmatch(r"scout\s+([\w.-]+)", question, re.IGNORECASE)
            if m:
                report = tools.call("scout_opponent", {"opponent": m.group(1)})
                print(f"\n{report}\n")
                messages += [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content":
                        f"I scouted {m.group(1)}. The scouting report:\n{report}"},
                ]
                continue

            try:
                run_turn(question, messages, tools, model)  # prints as it streams
            except (urllib.error.URLError, OSError) as e:
                _clear_status()
                print(f"\n[ollama error: {e}] — is Ollama running?")
                if messages and messages[-1].get("role") == "user":
                    messages.pop()  # drop the unanswered question from history
                continue
            except KeyboardInterrupt:  # Ctrl-C aborts the answer, not the session
                _clear_status()
                print("\n(interrupted)")
                if messages and messages[-1].get("role") == "user":
                    messages.pop()
                continue
    finally:
        # Runs on exit/quit, Ctrl-C at the prompt, and crashes alike:
        # this session becomes memory before the process dies
        _persist_session(user, messages, tools.memory, model)
