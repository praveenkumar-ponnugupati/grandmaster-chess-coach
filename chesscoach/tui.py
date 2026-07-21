"""Terminal UI moments — pure stdlib ANSI, no widget framework.

Three pieces, all preserving the linear texting-a-mentor REPL:
- Cockpit: a pinned one-line status bar above the scrolling chat
  (DECSTBM scroll region), showing who's coached, form, memory state,
  and live activity while tools run.
- replay: full-screen game replay on the alternate screen buffer —
  ←/→ steps moves, evals and blunder annotations from the cached
  analysis, q returns to the chat exactly as it was.
- drill: full-screen tactics quiz over the thrown-away wins; results
  are remembered in Supermemory.
"""
from __future__ import annotations

import io
import json
import shutil
import signal
import sys
import termios
import tty
from pathlib import Path

import chess
import chess.pgn

from .board import eval_bar, render_board

GOLD, DIM, OFF = "\033[1;33m", "\033[2m", "\033[0m"
GREEN, RED = "\033[32m", "\033[1;31m"
ALT_ON, ALT_OFF = "\033[?1049h", "\033[?1049l"


# ── Cockpit: pinned header over a scroll region ────────────────────────────

class Cockpit:
    """Row 1 becomes a sticky status bar; the chat scrolls in rows 2..h."""

    def __init__(self, summary: str, enabled: bool | None = None):
        self.summary = summary
        self.activity = ""
        self.enabled = sys.stdout.isatty() if enabled is None else enabled

    def start(self) -> None:
        if not self.enabled:
            return
        h = shutil.get_terminal_size().lines
        # region excludes row 1; DECSTBM homes the cursor, so re-park at bottom
        print(f"\033[2;{h}r\033[{h};1H", end="", flush=True)
        try:
            signal.signal(signal.SIGWINCH, self._on_resize)
        except (ValueError, OSError):
            pass  # non-main thread or exotic platform — resize just degrades
        self.draw()

    def _on_resize(self, *_ignored) -> None:
        h = shutil.get_terminal_size().lines
        print(f"\0337\033[2;{h}r", end="", flush=True)
        self.draw()
        print("\0338", end="", flush=True)

    def draw(self) -> None:
        if not self.enabled:
            return
        w = shutil.get_terminal_size().columns
        act = f" · {self.activity}" if self.activity else ""
        text = f" ♞ {self.summary}{act}"[: max(0, w - 1)]
        # inverse gold bar; save/restore cursor so the chat is undisturbed
        print(f"\0337\033[1;1H\033[2K\033[7;33m{text.ljust(w)}\033[0m\0338",
              end="", flush=True)

    def update(self, activity: str | None) -> None:
        self.activity = activity or ""
        self.draw()

    def after_clear(self) -> None:
        """`clear` wipes row 1 too — redraw and park below the header."""
        if self.enabled:
            print("\033[2;1H", end="", flush=True)
            self.draw()

    def stop(self) -> None:
        if not self.enabled:
            return
        try:
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
        print("\033[r", end="", flush=True)  # full-screen scrolling again


# ── shared: join cached engine analyses with parsed archive games ─────────

def analyzed_games(parsed: list[dict], data_dir: Path) -> list[dict]:
    """Newest-first [{parsed game + 'analysis'}] for games the engine has
    already looked at (no engine is ever started here)."""
    out = []
    for p in parsed:
        f = data_dir / "analysis" / f"{p['uuid']}.json"
        if p["uuid"] and f.exists():
            out.append({**p, "analysis": json.loads(f.read_text())})
    return out


def _read_key(fd) -> str:
    ch = sys.stdin.read(1)
    if ch != "\x1b":
        return ch
    seq = sys.stdin.read(2)
    return {"[C": "RIGHT", "[D": "LEFT", "[H": "HOME", "[F": "END",
            "[A": "HOME", "[B": "END"}.get(seq, "ESC")


def _frame(lines: list[str]) -> None:
    print("\033[2J\033[H" + "\n".join(lines), end="", flush=True)


# ── replay: step through a game on the alternate screen ────────────────────

def replay(game: dict, user: str) -> None:
    """Full-screen replay of one analyzed game. Blocking; restores the
    chat screen on exit."""
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        print("replay needs an interactive terminal")
        return
    pgn = chess.pgn.read_game(io.StringIO(game["pgn"]))
    if pgn is None:
        print("couldn't parse this game's PGN")
        return
    board = pgn.board()
    sans, moves, fens = [], [], [board.fen()]
    for mv in pgn.mainline_moves():
        sans.append(board.san(mv))
        moves.append(mv)
        board.push(mv)
        fens.append(board.fen())
    notes = {m["ply"]: m for m in game["analysis"]["moves"]}
    user_is_white = game["color"] == "white"
    flip = not user_is_white
    title = (f"{user} ({game['color']}) vs {game['opponent']} · "
             f"{game['time_class']} · {game['result']}")
    i = len(sans)  # open at the final position
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    print(ALT_ON, end="", flush=True)
    try:
        tty.setcbreak(fd)
        while True:
            lines = [f"{GOLD} ♞ replay · {title}{OFF}", ""]
            move_no = (i + 1) // 2
            whose = "White" if i % 2 == 1 else "Black"
            head = (f" move {move_no} · {whose}: {sans[i - 1]}" if i
                    else " starting position")
            note = notes.get(i)
            # highlights via UCI: square-parseable on any position, unlike
            # SAN which only parses on the pre-move board
            played_uci = moves[i - 1].uci() if i else None
            best_uci = None
            if note and note["best_san"]:
                try:
                    best_uci = chess.Board(fens[i - 1]).parse_san(
                        note["best_san"]).uci()
                except ValueError:
                    pass
            lines.append(f"{DIM}{head}{OFF}  {DIM}({i}/{len(sans)}){OFF}")
            lines.append("")
            lines += [ln for ln in
                      render_board(fens[i], best=best_uci, played=played_uci,
                                   flip=flip).splitlines()
                      if "■" not in ln]  # annotation line below replaces legend
            if note:
                cp = note["eval_after"] if user_is_white else -note["eval_after"]
                lines.append(eval_bar(cp))
                if note["class"]:
                    tone = RED if note["class"] == "blunder" else DIM
                    lines.append(f" {tone}{note['class'].upper()}{OFF} — "
                                 f"played {note['san']}, best was "
                                 f"{GREEN}{note['best_san'] or '?'}{OFF} "
                                 f"(lost {note['cp_loss']} cp)")
                else:
                    lines.append(f" {DIM}fine — {note['san']}{OFF}")
            lines.append("")
            lines.append(f"{DIM} ← back · → next · ↑ start · ↓ end · q quit{OFF}")
            _frame(lines)
            key = _read_key(fd)
            if key in ("q", "Q", "ESC"):
                return
            if key == "LEFT":
                i = max(0, i - 1)
            elif key == "RIGHT":
                i = min(len(sans), i + 1)
            elif key == "HOME":
                i = 0
            elif key == "END":
                i = len(sans)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print(ALT_OFF, end="", flush=True)


# ── drill: tactics quiz over thrown-away wins ─────────────────────────────

def drill(games: list[dict], user: str, memory, winning_eval: int = 150,
          max_spots: int = 8) -> None:
    """Full-screen quiz: find the move you missed. Results go to memory."""
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        print("drill needs an interactive terminal")
        return
    spots = []
    for g in games:
        for m in g["analysis"]["moves"]:
            if (m["class"] == "blunder" and m["best_san"]
                    and m["best_san"] != m["san"]
                    and m["eval_before"] >= winning_eval):
                spots.append((m, g))
    spots.sort(key=lambda t: -t[0]["cp_loss"])
    spots = spots[:max_spots]
    if not spots:
        print("no thrown-away wins in the analyzed games — nice, nothing to drill")
        return
    solved, results = 0, []
    print(ALT_ON, end="", flush=True)
    try:
        for k, (m, g) in enumerate(spots, 1):
            flip = g["color"] == "black"
            board = chess.Board(m["fen_before"])
            best_mv = board.parse_san(m["best_san"])
            tries = 0
            feedback = ""
            while True:
                lines = [f"{GOLD} ♞ drill · position {k}/{len(spots)} · "
                         f"solved {solved}{OFF}", ""]
                lines.append(f" {'White' if board.turn else 'Black'} to move — "
                             f"you were winning here. Find the move you missed.")
                lines.append("")
                lines += render_board(m["fen_before"], flip=flip).splitlines()
                if feedback:
                    lines.append(feedback)
                lines.append("")
                lines.append(f"{DIM} type a move (like Nf3, exd5, O-O) · "
                             f"enter=skip · q=quit{OFF}")
                _frame(lines)
                try:
                    answer = input("\n your move › ").strip()
                except (EOFError, KeyboardInterrupt):
                    answer = "q"
                if answer.lower() == "q":
                    raise StopIteration
                if not answer:
                    results.append((m, g, "skipped"))
                    break
                try:
                    guess = board.parse_san(answer)
                except ValueError:
                    feedback = f" {DIM}'{answer}' isn't a legal move here{OFF}"
                    continue
                if guess == best_mv:
                    solved += 1
                    results.append((m, g, "solved"))
                    _frame([f"{GREEN} ✓ {m['best_san']} — exactly. "
                            f"(you played {m['san']} in the game, "
                            f"−{m['cp_loss'] / 100:.1f} pawns){OFF}",
                            f"{DIM} any key for the next one …{OFF}"])
                    _wait_any_key()
                    break
                tries += 1
                if tries >= 2:
                    results.append((m, g, "failed"))
                    _frame([f"{RED} ✗ the winning idea was "
                            f"{GREEN}{m['best_san']}{OFF}{RED} — you tried "
                            f"{answer}.{OFF}",
                            f" {DIM}game: {g['url']}{OFF}",
                            f"{DIM} any key for the next one …{OFF}"])
                    _wait_any_key()
                    break
                feedback = (f" {RED}not {answer} — look again "
                            f"(1 try left){OFF}")
    except StopIteration:
        pass
    finally:
        print(ALT_OFF, end="", flush=True)
    attempted = [r for r in results if r[2] != "skipped"]
    print(f"\ndrill done: {solved}/{len(attempted)} solved"
          + (f" ({len(spots) - len(results)} unseen)" if len(results) < len(spots)
             else ""))
    if attempted and memory and memory.enabled:
        failed = [f"{r[0]['best_san']} (game {r[1]['url']})"
                  for r in results if r[2] == "failed"]
        note = (f"Tactics drill: solved {solved}/{len(attempted)} "
                f"thrown-away-win positions.")
        if failed:
            note += " Still missing: " + "; ".join(failed) + " — drill these again."
        memory.remember_note(user, note)
        print("(drill results remembered)")


def _wait_any_key() -> None:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
