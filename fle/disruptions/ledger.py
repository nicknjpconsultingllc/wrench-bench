"""Append-only event ledger.

The ledger is the benchmark's ground truth: every armed, fired, or observed
event is recorded with the real ``game.tick`` at which it happened, plus the
exact entities affected. Scorers read the ledger; agents never do (a CI test
greps rendered prompts/observations for ledger contents to enforce this).
"""

import json
from pathlib import Path

from pydantic import BaseModel, Field


class LedgerEntry(BaseModel, frozen=True, extra="forbid"):
    # Real game.tick (never FLE's synthetic elapsed_ticks accumulator).
    tick: int
    # "armed" | "fired" | "report_fault" | "character_died" | ...
    event: str
    kind: str | None = None
    seed: int | None = None
    # Affected-entity manifest as returned by the Lua injection:
    # [{"name": ..., "position": {"x": ..., "y": ...}, ...}, ...]
    affected: list[dict] = Field(default_factory=list)
    detail: dict = Field(default_factory=dict)


class EventLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: LedgerEntry) -> None:
        with self.path.open("a") as f:
            f.write(entry.model_dump_json(exclude_none=True) + "\n")

    def read(self) -> list[LedgerEntry]:
        if not self.path.exists():
            return []
        with self.path.open() as f:
            return [LedgerEntry.model_validate(json.loads(line)) for line in f if line.strip()]

    def entries(self, event: str) -> list[LedgerEntry]:
        return [e for e in self.read() if e.event == event]

    def injections(self) -> list[LedgerEntry]:
        return self.entries("fired")
