from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CollectOutcome:
    source: str
    status: str  # "success" | "already_archived" | "no_op" | "failed"
    detail: str
    url: str | None = None
    dest_path: str | None = None
    bytes_written: int | None = None
    elapsed_seconds: float | None = None
