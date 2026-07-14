"""Inline chess board — Unicode pieces on ANSI-colored squares.

Renders INTO the conversation stream (no widgets, no alt-screen) so the
REPL keeps feeling like texting a mentor. Pure stdlib + python-chess.

Palette discipline: board squares are grays; the only accents are one
green (the move you should have played) and one red (the move you did),
matching the coach's yellow-on-dark aesthetic.
"""
from __future__ import annotations

import sys

import chess

# Grays for the checkerboard, green = best move, red = played blunder
LIGHT_BG, DARK_BG = "\033[48;5;250m", "\033[48;5;240m"
BEST_LIGHT, BEST_DARK = "\033[48;5;77m", "\033[48;5;70m"
PLAYED_LIGHT, PLAYED_DARK = "\033[48;5;174m", "\033[48;5;131m"
WHITE_FG, BLACK_FG = "\033[38;5;231;1m", "\033[38;5;16m"
DIM, OFF = "\033[2m", "\033[0m"

# One filled glyph set for both sides (fg color tells them apart) —
# outline glyphs render inconsistently across terminal fonts
GLYPHS = {chess.PAWN: "♟", chess.KNIGHT: "♞", chess.BISHOP: "♝",
          chess.ROOK: "♜", chess.QUEEN: "♛", chess.KING: "♚"}

EVAL_BAR_WIDTH = 24


def _move_squares(board: chess.Board, move_str: str | None) -> set[int]:
    """from/to squares of a SAN or UCI move in this position; {} if unparseable."""
    if not move_str:
        return set()
    try:
        mv = board.parse_san(move_str)
    except ValueError:
        try:
            mv = chess.Move.from_uci(move_str.lower())
        except ValueError:
            return set()
    return {mv.from_square, mv.to_square}


def eval_bar(cp: int, color: bool = True) -> str:
    """White's winning chances as a horizontal bar: [███████░░░] +1.5"""
    share = 1.0 / (1.0 + 10 ** (-cp / 400.0))  # logistic, elo-style
    filled = round(share * EVAL_BAR_WIDTH)
    label = f"{cp / 100:+.1f}"
    if not color:
        return "[" + "#" * filled + "." * (EVAL_BAR_WIDTH - filled) + f"] {label}"
    return (f"{DIM}   [{OFF}" + "█" * filled
            + f"{DIM}" + "░" * (EVAL_BAR_WIDTH - filled) + f"]{OFF} {label}")


def render_board(fen: str, best: str | None = None, played: str | None = None,
                 eval_cp: int | None = None, color: bool | None = None) -> str:
    """The position as terminal art. `best` squares go green, `played` red;
    an eval bar rides underneath when an eval is known."""
    board = chess.Board(fen)
    if color is None:
        color = sys.stdout.isatty()
    if not color:  # piped/tests: plain text, same information
        txt = str(board)
        parts = [txt]
        if played:
            parts.append(f"played: {played}")
        if best:
            parts.append(f"best:   {best}")
        if eval_cp is not None:
            parts.append(eval_bar(eval_cp, color=False))
        return "\n".join(parts)

    best_sqs = _move_squares(board, best)
    played_sqs = _move_squares(board, played)
    lines = []
    for rank in range(7, -1, -1):
        cells = []
        for file in range(8):
            sq = chess.square(file, rank)
            light = (file + rank) % 2 == 1
            if sq in best_sqs:
                bg = BEST_LIGHT if light else BEST_DARK
            elif sq in played_sqs:
                bg = PLAYED_LIGHT if light else PLAYED_DARK
            else:
                bg = LIGHT_BG if light else DARK_BG
            piece = board.piece_at(sq)
            if piece:
                fg = WHITE_FG if piece.color == chess.WHITE else BLACK_FG
                cells.append(f"{bg}{fg} {GLYPHS[piece.piece_type]} {OFF}")
            else:
                cells.append(f"{bg}   {OFF}")
        lines.append(f"{DIM} {rank + 1} {OFF}" + "".join(cells))
    lines.append(f"{DIM}    a  b  c  d  e  f  g  h{OFF}")
    legend = []
    if played:
        legend.append(f"\033[38;5;131m■{OFF} played {played}")
    if best:
        legend.append(f"\033[38;5;70m■{OFF} best {best}")
    if legend:
        lines.append("   " + f"{DIM} · {OFF}".join(legend))
    if eval_cp is not None:
        lines.append(eval_bar(eval_cp))
    return "\n".join(lines)
