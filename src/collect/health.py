"""
Health record for each collector source.

Stored as JSON at data/health.json in Phase 1 (no database yet — that's Phase 2's
build_db.py, which will ingest this file into the `health` table verbatim; field names
match that schema exactly so the ingest is a straight copy).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HEALTH_PATH = Path(__file__).resolve().parents[2] / "data" / "health.json"


def _load() -> dict[str, Any]:
    if not HEALTH_PATH.exists():
        return {}
    with HEALTH_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict[str, Any]) -> None:
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = HEALTH_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp_path.replace(HEALTH_PATH)  # atomic on both POSIX and Windows (NTFS)


def record_attempt(source: str, now_iso: str) -> None:
    data = _load()
    row = data.setdefault(
        source, {"last_attempt": None, "last_success": None, "consecutive_failures": 0, "last_error": None}
    )
    row["last_attempt"] = now_iso
    _save(data)


def record_success(source: str, now_iso: str) -> None:
    data = _load()
    row = data.setdefault(
        source, {"last_attempt": None, "last_success": None, "consecutive_failures": 0, "last_error": None}
    )
    row["last_attempt"] = now_iso
    row["last_success"] = now_iso
    row["consecutive_failures"] = 0
    row["last_error"] = None
    _save(data)


def record_failure(source: str, now_iso: str, error: str) -> None:
    data = _load()
    row = data.setdefault(
        source, {"last_attempt": None, "last_success": None, "consecutive_failures": 0, "last_error": None}
    )
    row["last_attempt"] = now_iso
    row["consecutive_failures"] = row.get("consecutive_failures", 0) + 1
    row["last_error"] = error
    _save(data)


def record_no_op(source: str, now_iso: str, reason: str) -> None:
    """Weekend/holiday: touch last_attempt only, per SIGNAL_TRACKER_REQUIREMENTS.md §6.
    Does not count as a failure and does not update last_success (nothing was fetched)."""
    data = _load()
    row = data.setdefault(
        source, {"last_attempt": None, "last_success": None, "consecutive_failures": 0, "last_error": None}
    )
    row["last_attempt"] = now_iso
    row["last_no_op_reason"] = reason
    _save(data)
