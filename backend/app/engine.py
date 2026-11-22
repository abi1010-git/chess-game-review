"""Stockfish over UCI, behind a small interface the analysis code (and tests) can swap out."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import chess
import chess.engine

from . import config


@dataclass
class Line:
    """One engine line. Scores are from the point of view of the side to move."""
    cp: int | None
    mate: int | None
    pv: list[str] = field(default_factory=list)  # UCI moves

    @property
    def move(self) -> str | None:
        return self.pv[0] if self.pv else None


class Engine(Protocol):
    name: str

    def analyse(self, board: chess.Board, nodes: int, multipv: int = 1,
                root_moves: list[str] | None = None) -> list[Line]: ...

    def close(self) -> None: ...


class EngineUnavailable(RuntimeError):
    pass


def find_stockfish() -> str:
    if config.STOCKFISH_PATH:
        if not Path(config.STOCKFISH_PATH).exists():
            raise EngineUnavailable(f"STOCKFISH_PATH points to a missing file: {config.STOCKFISH_PATH}")
        return config.STOCKFISH_PATH
    found = shutil.which("stockfish")
    if found:
        return found
    if config.ENGINE_DIR.exists():
        for p in sorted(config.ENGINE_DIR.glob("stockfish*")):
            if p.is_file():
                return str(p)
    raise EngineUnavailable("Stockfish was not found. Set STOCKFISH_PATH or put the binary in backend/engines/.")


class StockfishEngine:
    def __init__(self, path: str | None = None, threads: int = 1, hash_mb: int = 64):
        self.path = path or find_stockfish()
        try:
            self._e = chess.engine.SimpleEngine.popen_uci(self.path)
        except (OSError, chess.engine.EngineError) as e:
            raise EngineUnavailable(f"Could not start Stockfish at {self.path}: {e}") from e
        self.name = self._e.id.get("name", "unknown engine")
        self._e.configure({"Threads": threads, "Hash": hash_mb})

    def analyse(self, board, nodes, multipv=1, root_moves=None):
        kwargs = {}
        if root_moves:
            kwargs["root_moves"] = [chess.Move.from_uci(m) for m in root_moves]
        infos = self._e.analyse(board, chess.engine.Limit(nodes=nodes), multipv=multipv, game=object(), **kwargs)
        if isinstance(infos, dict):
            infos = [infos]
        lines = []
        for info in infos:
            score = info.get("score")
            if score is None:
                continue
            pov = score.pov(board.turn)
            lines.append(Line(cp=pov.score(), mate=pov.mate(), pv=[m.uci() for m in info.get("pv", [])]))
        return lines

    def close(self):
        try:
            self._e.quit()
        except Exception:
            pass
