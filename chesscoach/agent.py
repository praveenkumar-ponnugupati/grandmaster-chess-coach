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
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

try:  # arrow-key editing + history in input(); harmless if unavailable
    import readline
except ImportError:  # pragma: no cover
    readline = None

import chess
import chess.engine

from .board import render_board
from .chat import KEEP_ALIVE, MODEL, OLLAMA
from .chesscom import (endings, get_profile, get_stats, opening_records,
                       parse_games, rating_buckets, rating_trends, record)
from .memory import Supermemory
from .metrics import (blunder_trend, clock_report, cpl_trend, phase_acpl,
                      split_halves)
from .termmd import render_markdown
from .tui import Cockpit, analyzed_games, drill, replay
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
- show_openings: draw a win-rate-by-opening bar chart. Call it for anything \
about {user}'s openings, then narrate the standout — name the worst \
opening and its numbers.
- show_endings: draw how {user}'s games end (checkmate/resignation/timeout) \
and their score vs lower/similar/higher-rated opponents. When {user} asks \
how or why they are losing, call BOTH show_openings and show_endings, then \
narrate the one insight that matters most.
- show_trends: engine-verified metrics — accuracy (centipawn loss) trend, \
blunders per game trend, which phase the eval collapses in, and clock \
truth (timeout losses in fine positions vs outplayed; blunders under \
30s). Call for questions about accuracy, improvement, progress, blunder \
habits or time trouble.
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
        "name": "show_openings",
        "description": "Draw a horizontal bar chart of the player's "
                       "results by opening (win%, games, net score, worst "
                       "flagged). Use when asked how/where they lose games "
                       "or about their opening repertoire.",
        "parameters": {"type": "object", "properties": {
            "months": {"type": "integer",
                       "description": "monthly archives to include (default 2)"},
            "max_games": {"type": "integer",
                          "description": "recent games to include (default 60)"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "show_endings",
        "description": "Draw how the player's games end (termination mix, "
                       "timeout flag) plus their score vs lower/similar/"
                       "higher-rated opponents. Use alongside show_openings "
                       "for how/why-am-I-losing questions.",
        "parameters": {"type": "object", "properties": {
            "months": {"type": "integer",
                       "description": "monthly archives to include (default 2)"},
            "max_games": {"type": "integer",
                          "description": "recent games to include (default 60)"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "show_trends",
        "description": "Draw engine-verified coaching metrics: accuracy "
                       "(CPL) trend, blunders-per-game trend, phase "
                       "collapse breakdown, and clock analysis (timeout "
                       "losses in fine positions, blunders under 30s). "
                       "Use for accuracy/progress/blunder/time questions.",
        "parameters": {"type": "object", "properties": {
            "months": {"type": "integer",
                       "description": "monthly archives to include (default 2)"},
            "max_games": {"type": "integer",
                          "description": "recent games to analyze (default 15)"},
        }, "required": []},
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


_COCKPIT: Cockpit | None = None  # set while the agent loop runs on a tty


def _status(text: str) -> None:
    """Sign of life while tools run. With the cockpit active it lives in
    the pinned header; otherwise a transient dim line (tty only)."""
    if _COCKPIT is not None:
        _COCKPIT.update(text)
    elif sys.stdout.isatty():
        print(f"\r\033[2m  ♞ {text}\033[0m\033[K", end="", flush=True)


def _clear_status() -> None:
    if _COCKPIT is not None:
        _COCKPIT.update(None)
    elif sys.stdout.isatty():
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

    def _parsed_recent(self, months: int = 2, max_games: int = 60) -> list[dict]:
        return parse_games(
            rated_recent_games(self.user, int(months), int(max_games),
                               self.data_dir), self.user)

    def _tool_show_openings(self, months: int = 2, max_games: int = 60) -> str:
        recs = opening_records(self._parsed_recent(months, max_games))
        if not recs:
            return "No opening data found in the recent games."
        panel, facts = _openings_panel(recs)
        _clear_status()
        print("\n" + panel + "\n")
        return (f"(chart displayed to the player) {facts}. Narrate the "
                "standout numbers — especially the worst opening — in your "
                "own coaching voice; do not repeat the whole table.")

    def _tool_show_endings(self, months: int = 2, max_games: int = 60) -> str:
        parsed = self._parsed_recent(months, max_games)
        if not parsed:
            return "No recent games found."
        panel, facts = _endings_panel(parsed)
        _clear_status()
        print("\n" + panel + "\n")
        return (f"(chart displayed to the player) {facts}. Narrate the one "
                "insight that matters most in your own coaching voice.")

    def _tool_show_trends(self, months: int = 2, max_games: int = 15) -> str:
        raw = rated_recent_games(self.user, int(months), int(max_games),
                                 self.data_dir)
        parsed = parse_games(raw, self.user)
        _status(f"engine-checking {len(raw)} games …")
        _, analyzed, _ = analyze_and_report(
            self.user, raw, engine_path=self.engine_path,
            movetime=self.movetime, data_dir=self.data_dir,
            memory=self.memory, scouting=False,
            progress=lambda i, n: _status(f"engine-checking games … {i}/{n}"))
        if not analyzed:
            return "No analyzable games found."
        panel, facts = _metrics_panel(analyzed, parsed)
        _clear_status()
        print("\n" + panel + "\n")
        remember_run(self.memory, self.user, analyzed, scouting=False)
        return (f"(metrics displayed to the player) {facts}. Narrate the "
                "single most important insight in your coaching voice.")

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
               "stream": True, "keep_alive": KEEP_ALIVE,
               "options": {"num_ctx": NUM_CTX}}
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


def _warm_model(system: str, model: str) -> None:
    """Prompt caching, Ollama-style: load the model and prefill the system
    prompt into its KV cache (num_predict=0 → process, generate nothing).
    Runs in the background during banner/typing time, so the first
    question's prompt-processing only pays for the question itself."""
    # tools included: Ollama renders tool schemas into the prompt, so a
    # warm-up without them would cache a different prefix than real turns
    payload = {"model": model, "stream": False, "keep_alive": KEEP_ALIVE,
               "messages": [{"role": "system", "content": system}],
               "tools": TOOLS,
               "options": {"num_ctx": NUM_CTX, "num_predict": 0}}
    req = urllib.request.Request(
        OLLAMA + "/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except Exception:
        pass  # warming is an optimization, never a failure


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
                prefix = ("\033[1;33mcoach ›\033[0m " if pace
                          else "coach › ")
                print(prefix, end="", flush=True)
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
                "show_openings": "charting your openings …",
                "show_endings": "checking how your games end …",
                "show_trends": "running the engine over your games …",
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
    payload = {"model": model, "stream": False, "keep_alive": KEEP_ALIVE,
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


RULE_WIDTH = 58


def _rule(color: bool = True) -> str:
    """Dim divider separating a stat drop from the coach's words."""
    line = "─" * RULE_WIDTH
    return f"\033[2m{line}\033[0m" if color else line


def _prompt() -> str:
    """Gold `you ›` prompt. Non-printing color codes are wrapped in
    \\001/\\002 when readline is active so line editing keeps correct
    widths; plain text when piped."""
    if not sys.stdout.isatty():
        return "you › "
    if readline:
        return "\001\033[1;33m\002you ›\001\033[0m\002 "
    return "\033[1;33myou ›\033[0m "


def _setup_history(data_dir: Path) -> None:
    """Arrow-key recall of past questions, persisted across sessions."""
    if readline is None:
        return
    hist = data_dir / ".repl-history"
    try:
        if hist.exists():
            readline.read_history_file(hist)
        readline.set_history_length(500)
    except OSError:
        pass


def _save_history(data_dir: Path) -> None:
    if readline is None:
        return
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(data_dir / ".repl-history")
    except OSError:
        pass


HELP = """\
   commands   stats · openings · trends · scout USERNAME · help · clear · exit
   full-screen: replay [N]  step through your Nth-recent analyzed game (←/→)
                drill       tactics quiz on the wins you threw away
   or just talk to your coach:
     "how am I losing games?" · "show my worst blunder on the board"
     "what did we work on last time?" · "remember this: …"
     "compare me against hikaru" · "am I improving?"\
"""


def _openings_panel(recs: dict, color: bool | None = None,
                    top: int = 8) -> tuple[str, str]:
    """(panel_text, narration_facts): horizontal win-rate bars, worst
    flagged. Facts go back to the model so the coach narrates numbers
    that actually match the chart."""
    if color is None:
        color = sys.stdout.isatty()
    def c(code, s):
        return f"\033[{code}m{s}\033[0m" if color else s
    rows = sorted(recs.items(), key=lambda kv: -sum(kv[1]))[:top]
    flagged = min((r for r in rows if sum(r[1]) >= 3),
                  key=lambda kv: (kv[1][0] + kv[1][2] / 2) / sum(kv[1]),
                  default=None)

    def shorten(name: str, limit: int = 30) -> str:
        """Trim long opening names but keep the (Color) tag — it's data."""
        if len(name) <= limit:
            return name
        m = re.search(r" \((White|Black)\)$", name)
        tail = m.group(0) if m else ""
        head = name[:len(name) - len(tail)]
        return head[:limit - len(tail) - 1].rstrip() + "…" + tail

    rows = [(shorten(n), wld) for n, wld in rows]
    name_w = max(len(n) for n, _ in rows)
    lines = [_rule(color)]
    facts = []
    for name, (w, l, d) in rows:
        n = w + l + d
        pct = (w + d / 2) / n * 100
        filled = round(pct / 100 * 16)
        bar = "█" * filled + "░" * (16 - filled)
        bar = c("32", bar) if pct >= 55 else c("31", bar) if pct <= 45 else bar
        net = w - l
        net_s = (c("32", f"+{net}") if net > 0
                 else c("31", str(net)) if net < 0 else c("2", "±0"))
        games_s = f"{n:>2} game" + ("s" if n != 1 else " ")
        line = (f"   {name:<{name_w}} {bar} {pct:3.0f}%  "
                f"{games_s}  {net_s}")
        if flagged and name == flagged[0]:
            line += c("1;31", "  ◀ fix this")
        lines.append(line)
        facts.append(f"{name}: {pct:.0f}% over {n} (W{w}/L{l}/D{d})")
    lines.append(_rule(color))
    worst = (f" WORST: {flagged[0]}" if flagged else "")
    return "\n".join(lines), "; ".join(facts) + worst


TREND_BAR_WIDTH = 12


def _trend_bar(vals: list[int], delta: int | None = None,
               width: int = TREND_BAR_WIDTH, color: bool = True) -> str:
    """Horizontal trend bar using ONLY █ and ░ (single-height glyphs — the
    same technique as the streak strip, so no vertical overlap is possible).
    Fill = where the current rating sits within the recent window's range;
    fill color = direction (green up / red down / dim flat); arrow + delta
    appended (week delta when known, else the window move)."""
    def c(code, s):
        return f"\033[{code}m{s}\033[0m" if color else s
    lo, hi = min(vals), max(vals)
    pos = (vals[-1] - lo) / (hi - lo) if hi > lo else 0.5
    filled = max(1, round(pos * width))
    move = delta if delta is not None else vals[-1] - vals[0]
    # explicit grays, not the dim attribute — dim ░ vanishes on dark themes
    tone = "32" if move > 0 else "31" if move < 0 else "38;5;250"
    bar = c(tone, "█" * filled) + c("38;5;240", "░" * (width - filled))
    arrow = (c("32", f"↑{move}") if move > 0
             else c("31", f"↓{-move}") if move < 0 else c("38;5;250", "→"))
    return f"{bar} {arrow}"


def _trend_demo() -> None:
    """Standalone renderer test — run in YOUR terminal:
    ./venv/bin/python -c "from chesscoach.agent import _trend_demo; _trend_demo()"
    Only █ and ░ are used (the streak strip's glyphs). Three stacked lines
    below must stay three cleanly separate lines."""
    gold, off = "\033[1;33m", "\033[0m"
    cases = [("rising", [400, 440, 480, 526], None),
             ("falling", [526, 480, 440, 400], None),
             ("flat", [400] * 10, None),
             ("recovering", [526, 400, 450, 470], None),
             ("week-delta", [480, 500, 526], 40)]
    for name, vals, d in cases:
        print(f"  {name:<11} {_trend_bar(vals, d)}   \033[2m{vals}{off}")
    print()
    print("header mock (three adjacent rows — must not touch):")
    for tc, vals, d in (("daily", [400] * 10, None),
                        ("rapid", [520, 480, 450, 400, 470, 526], 40),
                        ("blitz", [180, 160, 100, 100, 100], -12)):
        print(f"   {tc:<6} {gold}{vals[-1]:>4}{off} {_trend_bar(vals, d)}")


def _pct(w: int, l: int, d: int) -> float:
    n = w + l + d
    return (w + d / 2) / n * 100 if n else 0.0


def _pct_bar(pct: float, width: int = 10, color: bool = True) -> str:
    """█/░ win-rate bar, green when strong, red when weak."""
    def c(code, s):
        return f"\033[{code}m{s}\033[0m" if color else s
    filled = max(0, min(width, round(pct / 100 * width)))
    tone = "32" if pct >= 55 else "31" if pct <= 45 else "38;5;250"
    return c(tone, "█" * filled) + c("38;5;240", "░" * (width - filled))


def _trends_block(parsed: list[dict], color: bool = True) -> str:
    """The per-format trend bars + streak strip (banner and `stats`)."""
    def c(code, s):
        return f"\033[{code}m{s}\033[0m" if color else s
    lines = []
    for tc, ratings, delta in rating_trends(parsed)[:3]:
        lines.append(f"   {tc:<6} {c('1;33', f'{ratings[-1]:>4}')} "
                     f"{_trend_bar(ratings, delta, color=color)}")
    streak = record(parsed)["streak"] if parsed else []
    if streak:
        marks = {"win": c("32", "█"), "loss": c("31", "█"),
                 "draw": c("38;5;245", "█")}
        lines.append(f"   {c('2', f'last {len(streak)}:')} "
                     + "".join(marks[r] for r in streak)
                     + f" {c('2', '(oldest → newest)')}")
    return "\n".join(lines)


def _record_panel(parsed: list[dict],
                  color: bool | None = None) -> tuple[str, str]:
    """W/L/D overall and sliced by color / time control. Returns
    (panel, facts-for-narration)."""
    if color is None:
        color = sys.stdout.isatty()
    def c(code, s):
        return f"\033[{code}m{s}\033[0m" if color else s
    rec = record(parsed)
    rows = [("overall", rec["overall"]),
            ("as White", rec["by_color"]["white"]),
            ("as Black", rec["by_color"]["black"])]
    rows += [(tc, wld) for tc, wld in rec["by_time_class"].items()]
    lines, facts = [_rule(color)], []
    for label, (w, l, d) in rows:
        if not w + l + d:
            continue
        pct = _pct(w, l, d)
        pct_s = (c("32", f"{pct:3.0f}%") if pct >= 55
                 else c("31", f"{pct:3.0f}%") if pct <= 45
                 else f"{pct:3.0f}%")
        lines.append(f"   {label:<9} {w:>2}W/{l:>2}L/{d}D  "
                     f"{_pct_bar(pct, color=color)} {pct_s}")
        facts.append(f"{label}: {w}W/{l}L/{d}D ({pct:.0f}%)")
    lines.append(_rule(color))
    return "\n".join(lines), "; ".join(facts)


def _endings_panel(parsed: list[dict],
                   color: bool | None = None) -> tuple[str, str]:
    """How games end (termination mix, timeout flag) + results vs
    opponent strength. Returns (panel, facts-for-narration)."""
    if color is None:
        color = sys.stdout.isatty()
    def c(code, s):
        return f"\033[{code}m{s}\033[0m" if color else s
    ends, buckets = endings(parsed), rating_buckets(parsed)
    lines, facts = [_rule(color), f"   {c('2', 'how your games end')}"], []
    for outcome, label in (("loss", "losses by"), ("win", "wins by")):
        items = sorted(ends[outcome].items(), key=lambda kv: -kv[1])
        total = sum(n for _, n in items)
        if not total:
            continue
        parts = [f"{t} {n} ({n / total * 100:.0f}%)" for t, n in items]
        lines.append(f"   {label:<10} " + f"{c('2', ' · ')}".join(parts))
        facts.append(f"{label}: " + ", ".join(parts))
    loss_total = sum(ends["loss"].values())
    timeouts = ends["loss"].get("timeout", 0)
    if loss_total and timeouts / loss_total >= 0.25:
        lines.append(c("1;31", f"   ◀ {timeouts} of {loss_total} losses "
                              "are on time — that's clock, not chess"))
        facts.append(f"TIMEOUT FLAG: {timeouts}/{loss_total} losses on time")
    lines.append(f"   {c('2', 'vs opponent strength (±100)')}")
    for label in ("lower", "similar", "higher"):
        w, l, d = buckets[label]
        n = w + l + d
        if not n:
            continue
        pct = _pct(w, l, d)
        lines.append(f"   vs {label:<8} {_pct_bar(pct, color=color)} "
                     f"{pct:3.0f}%  ({n} game{'s' if n != 1 else ''})")
        facts.append(f"vs {label}-rated: {pct:.0f}% over {n}")
    lines.append(_rule(color))
    return "\n".join(lines), "; ".join(facts)


def _metrics_panel(analyzed: list[dict], parsed: list[dict],
                   color: bool | None = None) -> tuple[str, str]:
    """Engine metrics: accuracy trend, blunders/game trend, phase
    collapse, clock truths. Returns (panel, facts-for-narration)."""
    if color is None:
        color = sys.stdout.isatty()
    def c(code, s):
        return f"\033[{code}m{s}\033[0m" if color else s
    def better_worse(old, new, invert=True):
        """Lower is better for CPL/blunders (invert=True)."""
        d = new - old
        good = d < 0 if invert else d > 0
        if abs(d) < 0.05 * max(abs(old), 1):
            return c("38;5;250", "→ steady")
        word = "improving" if good else "worsening"
        arrow = "↓" if d < 0 else "↑"
        return c("32" if good else "31", f"{arrow} {word}")
    lines, facts = [_rule(color)], []

    halves = split_halves(cpl_trend(analyzed))
    if halves:
        old, new = halves
        lines.append(f"   accuracy    CPL {old:.0f} → {c('1;33', f'{new:.0f}')}"
                     f"  {better_worse(old, new)}"
                     f"  {c('2', '(older half vs newer half, lower = better)')}")
        facts.append(f"CPL {old:.0f}→{new:.0f}")
    halves = split_halves(blunder_trend(analyzed))
    if halves:
        old, new = halves
        lines.append(f"   blunders    {old:.1f} → {c('1;33', f'{new:.1f}')}"
                     f" per game  {better_worse(old, new)}")
        facts.append(f"blunders/game {old:.1f}→{new:.1f}")

    phases = phase_acpl(analyzed)
    if phases:
        worst = max(phases, key=lambda k: phases[k][0])
        lines.append(f"   {c('2', 'where the eval collapses (ACPL)')}")
        top = max(a for a, _, _ in phases.values())
        for phase in ("opening", "middlegame", "endgame"):
            if phase not in phases:
                continue
            acpl, blunders, n = phases[phase]
            # fill = share of the damage; the worst phase burns red
            filled = max(1, round(acpl / top * 10)) if top else 0
            tone = "31" if phase == worst else "38;5;250"
            bar = (c(tone, "█" * filled)
                   + c("38;5;240", "░" * (10 - filled)))
            mark = c("1;31", "  ◀ collapse") if phase == worst else ""
            lines.append(f"   {phase:<10} {bar} {acpl:4.0f} cp/move · "
                         f"{blunders} blunders{mark}")
            facts.append(f"{phase} ACPL {acpl:.0f} ({blunders} blunders)")

    clocks = clock_report(analyzed, {p["uuid"]: p for p in parsed})
    t = clocks["timeout_losses"]
    n_timeouts = sum(len(v) for v in t.values())
    if n_timeouts:
        lines.append(f"   {c('2', 'timeout losses, judged by the engine')}")
        lines.append(f"   flagged while FINE  {c('1;31', str(len(t['fine'])))}"
                     f"   outplayed anyway  {len(t['outplayed'])}"
                     f"   unclear  {len(t['unclear'])}")
        facts.append(f"timeout losses: {len(t['fine'])} in fine positions, "
                     f"{len(t['outplayed'])} outplayed, "
                     f"{len(t['unclear'])} unclear")
    if clocks["pressure_moves"]:
        lines.append(f"   under 30s:  {clocks['pressure_blunders']} blunders "
                     f"in {clocks['pressure_moves']} moves")
        facts.append(f"{clocks['pressure_blunders']} blunders in "
                     f"{clocks['pressure_moves']} sub-30s moves")
    lines.append(_rule(color))
    return "\n".join(lines), "; ".join(facts)


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
    block = _trends_block(games or [], color=tty)
    if block:
        print(block)
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
          f'"what did we work on last time?" · help · exit{off}\n')


def agent_loop(user: str, engine_path: str, data_dir: Path, out_dir: Path,
               movetime: float = 0.1, model: str = MODEL) -> None:
    tools = CoachTools(user, engine_path, data_dir, out_dir, movetime)
    # Startup data comes from four independent sources — fetch concurrently
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_nick = pool.submit(_nickname, user)
        f_stats = pool.submit(get_stats, user)
        f_note = pool.submit(_last_session_note, tools.memory, user)
        f_recent = pool.submit(
            lambda: parse_games(rated_recent_games(user, 2, 60, data_dir),
                                user))
        def got(fut, fallback):
            try:
                return fut.result()
            except Exception:
                return fallback
        nick = got(f_nick, user)
        stats = got(f_stats, {})
        last_note = got(f_note, None)
        recent = got(f_recent, [])
    system = SYSTEM.format(user=user, nick=nick)
    if last_note:
        system += ("\n\nWhat you remember from your previous session "
                   "(use it to keep continuity — refer back naturally):\n"
                   + last_note)
    messages = [{"role": "system", "content": system}]
    # Prefill the system prompt into the model's cache while the user reads
    # the banner — the first answer starts noticeably sooner.
    threading.Thread(target=_warm_model, args=(system, model),
                     daemon=True).start()
    _print_banner(user, engine_path, tools.memory, model, nick,
                  stats=stats, watchlist=_watchlist(last_note), games=recent)
    _setup_history(data_dir)
    global _COCKPIT
    trends = rating_trends(recent)
    form = (f"{trends[0][0]} {trends[0][1][-1]}" if trends else "")
    mem_mark = "memory ✓" if tools.memory.enabled else "memory ✗"
    _COCKPIT = Cockpit(" · ".join(x for x in (nick, form, mem_mark) if x))
    _COCKPIT.start()
    try:
        while True:
            try:
                question = input(_prompt()).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not question:
                continue
            if question.lower() in ("exit", "quit", "q"):
                return
            if question.lower() in ("help", "?"):
                print("\n" + HELP + "\n")
                continue
            if question.lower() == "clear":
                print("\033[2J\033[H", end="", flush=True)
                if _COCKPIT:
                    _COCKPIT.after_clear()
                continue

            # full-screen moments: replay [N] and drill
            m = re.fullmatch(r"replay(?:\s+(\d+))?", question, re.IGNORECASE)
            if m or question.lower() == "drill":
                try:
                    parsed = tools._parsed_recent()
                except Exception as e:
                    print(f"couldn't fetch games: {e}")
                    continue
                pool = analyzed_games(parsed, data_dir)
                if not pool:
                    print("no analyzed games yet — ask me about your games "
                          "first, then come back")
                    continue
                if _COCKPIT:
                    _COCKPIT.stop()  # full-screen owns the terminal now
                try:
                    if m:
                        n = int(m.group(1) or 1)
                        if n < 1 or n > len(pool):
                            print(f"I have {len(pool)} analyzed games — "
                                  f"replay 1..{len(pool)}")
                            continue
                        replay(pool[n - 1], user)
                    else:
                        drill(pool, user, tools.memory)
                except Exception as e:  # a broken screen must not kill the chat
                    print(f"[{question.split()[0]} failed: {e}]")
                finally:
                    if _COCKPIT:
                        _COCKPIT.start()
                continue

            # "stats" is unambiguous — trends + record, no model round-trip
            if re.fullmatch(r"stats?", question, re.IGNORECASE):
                parsed = tools._parsed_recent()
                panel, facts = _record_panel(parsed)
                trend = _trends_block(parsed, color=sys.stdout.isatty())
                print(("\n" + trend + "\n" if trend else "\n") + panel + "\n")
                messages += [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content":
                        f"I showed {nick} their stats. {facts}"},
                ]
                continue

            # "trends" is unambiguous — engine metrics, no model round-trip
            if re.fullmatch(r"trends?|metrics", question, re.IGNORECASE):
                result = tools.call("show_trends", {})
                messages += [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content":
                        f"I showed {nick} their engine metrics. {result}"},
                ]
                continue

            # "openings" is unambiguous — draw the chart, no model round-trip
            if re.fullmatch(r"openings?", question, re.IGNORECASE):
                result = tools.call("show_openings", {})
                messages += [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content":
                        f"I showed {nick} the openings chart. {result}"},
                ]
                continue

            # "scout USERNAME" is unambiguous — run it directly, no model round-trip
            m = re.fullmatch(r"scout\s+([\w.-]+)", question, re.IGNORECASE)
            if m:
                report = tools.call("scout_opponent", {"opponent": m.group(1)})
                print(f"\n{render_markdown(report)}\n")
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
        if _COCKPIT:
            _COCKPIT.stop()
            _COCKPIT = None
        _save_history(data_dir)
        _persist_session(user, messages, tools.memory, model)
