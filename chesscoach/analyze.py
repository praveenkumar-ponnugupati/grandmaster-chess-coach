"""Engine analysis of one player's moves in their chess.com games."""
from __future__ import annotations

import io
import json
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from .chesscom import _opening_name

# Centipawn-loss classification (chess.com/lichess-style buckets)
BLUNDER, MISTAKE, INACCURACY = 250, 100, 50
# Mates fold into a large-but-finite cp value so swings stay comparable
MATE_SCORE = 1500


def classify(cp_loss: int) -> str | None:
    if cp_loss >= BLUNDER:
        return "blunder"
    if cp_loss >= MISTAKE:
        return "mistake"
    if cp_loss >= INACCURACY:
        return "inaccuracy"
    return None


def analyze_game(game_json: dict, user: str, engine: chess.engine.SimpleEngine,
                 movetime: float, cache_dir: Path) -> dict | None:
    """Per-move cp loss for `user`'s moves. Cached by chess.com game uuid."""
    uuid = game_json.get("uuid", "")
    cache = cache_dir / f"{uuid}.json"
    if uuid and cache.exists():
        return json.loads(cache.read_text())

    pgn = game_json.get("pgn")
    if not pgn:
        return None
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        return None

    white = game.headers.get("White", "").lower()
    user_is_white = white == user.lower()
    user_color = chess.WHITE if user_is_white else chess.BLACK

    board = game.board()
    limit = chess.engine.Limit(time=movetime)
    moves = []
    info = engine.analyse(board, limit)

    for move in game.mainline_moves():
        mover = board.turn
        eval_before = info["score"].pov(mover).score(mate_score=MATE_SCORE)
        best_san = None
        if mover == user_color and info.get("pv"):
            best_san = board.san(info["pv"][0])
        san = board.san(move)
        fen_before = board.fen()
        ply = board.ply() + 1

        board.push(move)
        info = engine.analyse(board, limit)
        eval_after = info["score"].pov(mover).score(mate_score=MATE_SCORE)

        if mover == user_color:
            cp_loss = max(0, eval_before - eval_after)
            moves.append({
                "ply": ply,
                "san": san,
                "best_san": best_san,
                "cp_loss": cp_loss,
                "class": classify(cp_loss),
                "eval_before": eval_before,
                "eval_after": eval_after,
                "fen_before": fen_before,
            })

    result = {
        "uuid": uuid,
        "url": game_json.get("url", ""),
        "end_time": game_json.get("end_time", 0),
        "time_class": game_json.get("time_class", "?"),
        "user_is_white": user_is_white,
        "user_result": _user_result(game_json, user_is_white),
        "eco": game.headers.get("ECO", "?"),
        "opening": _opening_name(game.headers.get("ECOUrl", "")),
        "moves": moves,
        "acpl": round(sum(m["cp_loss"] for m in moves) / len(moves), 1) if moves else 0.0,
    }
    if uuid:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(result))
    return result


def _user_result(game_json: dict, user_is_white: bool) -> str:
    side = game_json.get("white" if user_is_white else "black", {})
    raw = side.get("result", "?")
    if raw == "win":
        return "win"
    if raw in ("agreed", "repetition", "stalemate", "insufficient",
               "50move", "timevsinsufficient"):
        return "draw"
    return "loss"


