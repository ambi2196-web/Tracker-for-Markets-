#!/usr/bin/env python3
"""
Phase 3 — F&O ban list event detection. Extended in Phase 6 to also generate IPO
lock-in unlock events (SIGNAL_TRACKER_REQUIREMENTS.md §7.2) from the manually-maintained
data/seed/ipo_listings.csv — a direct derivation, not a detection, since those dates are
already known in advance; see src/events/ipo_lockin.py.

Parses the raw fo_ban archive into BanSnapshots (one per day actually on disk — gaps in
the archive, whatever their cause, are simply absent), diffs consecutive snapshots, and
writes fo_ban_entry / fo_ban_exit rows into the `events` table. Read-only with respect to
trading: this phase produces events, not signals or positions.

Modes mirror build_db.py:
    --live       Process the full fo_ban archive into data/tracker.db (upsert; safe to
                 rerun).
    --rebuild    Delete data/tracker.db's event rows for this source first, then --live.
    --replay FROM TO
                 Process only archived days in [FROM, TO] into a scratch database.
                 Caveat: whatever is already banned on the first snapshot inside the
                 window produces no entry event (nothing to diff against) even if it
                 truly entered ban before the window — this is the same left-censoring
                 behavior as the very start of the full archive, just relocated to the
                 window boundary. Not a bug; a real limit of windowed replay.
    --dry-run    Parse and detect, print a summary, write nothing.

Usage:
    python src/build_events.py --live
    python src/build_events.py --rebuild
    python src/build_events.py --replay 2026-06-01 2026-06-30
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from events import fo_ban as fo_ban_events  # noqa: E402
from events import ipo_lockin as ipo_events  # noqa: E402
from parse import fo_ban as fo_ban_parse  # noqa: E402
from parse import ipo_listings  # noqa: E402
from build_db import create_schema  # noqa: E402

DATA_ROOT = REPO_ROOT / "data"
DEFAULT_DB_PATH = DATA_ROOT / "tracker.db"
DEFAULT_REPLAY_DB_PATH = DATA_ROOT / "scratch_replay_events.db"
IPO_LISTINGS_PATH = DATA_ROOT / "seed" / "ipo_listings.csv"

PIPELINE_VERSION = "0.1.0"
SOURCE_EVENT_TYPES = (fo_ban_events.EVENT_TYPE_ENTRY, fo_ban_events.EVENT_TYPE_EXIT)


def discover_fo_ban_files(data_root: Path) -> list[Path]:
    return sorted((data_root / "raw" / "fo_ban").glob("*/*/*.csv"))


def load_snapshots(files: list[Path]) -> list[tuple[fo_ban_parse.BanSnapshot, str]]:
    """Parse every file, pair with its repo-relative path, sorted by the date *inside*
    the file (not the filename) — consistent with build_db.py's bhavcopy handling.

    A file that fails to parse (e.g. a real expiry-day layout anomaly) is skipped with a
    loud warning rather than aborting the whole run — it is simply absent from the
    resulting list, which detect() already treats safely as an unobserved gap rather
    than a false exit+re-entry."""
    pairs = []
    for path in files:
        try:
            snap = fo_ban_parse.parse_file(path)
        except fo_ban_parse.ParseError as exc:
            print(f"WARNING: skipping unparseable ban file, treated as a gap: {exc}")
            continue
        pairs.append((snap, path.relative_to(DATA_ROOT).as_posix()))
    pairs.sort(key=lambda p: p[0].date)
    return pairs


def generate_ipo_events() -> list[ipo_events.IpoLockinEvent]:
    listings = ipo_listings.parse_file(IPO_LISTINGS_PATH)
    source_file = IPO_LISTINGS_PATH.relative_to(DATA_ROOT).as_posix()
    return ipo_events.generate_events(listings, source_file)


def write_events(conn: sqlite3.Connection, evts, *, now_iso: str) -> None:
    import json
    conn.executemany(
        """
        INSERT INTO events (event_id, symbol, event_type, announce_date, effective_date,
                             detail, source_file, ingested_at, pipeline_version)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            detail=excluded.detail, source_file=excluded.source_file,
            ingested_at=excluded.ingested_at, pipeline_version=excluded.pipeline_version
        """,
        [
            (e.event_id, e.symbol, e.event_type, e.effective_date, json.dumps(e.detail),
             e.source_file, now_iso, PIPELINE_VERSION)
            for e in evts
        ],
    )


def run_live_or_rebuild(*, rebuild: bool, dry_run: bool) -> int:
    files = discover_fo_ban_files(DATA_ROOT)
    pairs = load_snapshots(files)
    ban_evts = fo_ban_events.detect(pairs)
    ipo_evts = generate_ipo_events()
    evts = [*ban_evts, *ipo_evts]

    if dry_run:
        n_days_nil = sum(1 for snap, _ in pairs if not snap.symbols)
        print(f"[dry-run] {len(pairs)} archived fo_ban days ({n_days_nil} NIL), "
              f"{len(ban_evts)} ban events "
              f"({sum(1 for e in ban_evts if e.event_type == 'fo_ban_entry')} entries, "
              f"{sum(1 for e in ban_evts if e.event_type == 'fo_ban_exit')} exits), "
              f"{len(ipo_evts)} ipo_lockin_unlock events. Nothing written.")
        return 0

    if rebuild and DEFAULT_DB_PATH.exists():
        conn = sqlite3.connect(DEFAULT_DB_PATH)
        conn.execute("DELETE FROM events WHERE event_type IN (?, ?, ?)", (*SOURCE_EVENT_TYPES, ipo_events.EVENT_TYPE))
        conn.commit()
        conn.close()
        print("[rebuild] cleared existing fo_ban_entry/fo_ban_exit/ipo_lockin_unlock rows from events")

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    try:
        create_schema(conn)
        write_events(conn, evts, now_iso=now_iso)
        conn.commit()
        print(f"done: {len(pairs)} archived fo_ban days, {len(ban_evts)} ban events, "
              f"{len(ipo_evts)} ipo_lockin_unlock events -> {DEFAULT_DB_PATH}")
    finally:
        conn.close()
    return 0


def run_replay(from_date: date, to_date: date, *, out_path: Path, dry_run: bool) -> int:
    files = discover_fo_ban_files(DATA_ROOT)
    pairs = [(snap, sf) for snap, sf in load_snapshots(files) if from_date <= snap.date <= to_date]
    ban_evts = fo_ban_events.detect(pairs)
    ipo_evts = [e for e in generate_ipo_events() if from_date <= date.fromisoformat(e.effective_date) <= to_date]
    evts = [*ban_evts, *ipo_evts]

    if dry_run:
        print(f"[dry-run][replay {from_date}..{to_date}] {len(pairs)} fo_ban days, "
              f"{len(ban_evts)} ban events, {len(ipo_evts)} ipo_lockin_unlock events. Nothing written.")
        return 0

    if out_path.exists():
        out_path.unlink()

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(out_path)
    try:
        create_schema(conn)
        write_events(conn, evts, now_iso=now_iso)
        conn.commit()
        print(f"[replay {from_date}..{to_date}] {len(pairs)} fo_ban days, {len(evts)} events total "
              f"-> {out_path} (data/tracker.db untouched)")
    finally:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--rebuild", action="store_true")
    mode.add_argument("--replay", nargs=2, metavar=("FROM", "TO"))
    parser.add_argument("--out", type=str, default=str(DEFAULT_REPLAY_DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.replay:
        from_date, to_date = (date.fromisoformat(d) for d in args.replay)
        return run_replay(from_date, to_date, out_path=Path(args.out), dry_run=args.dry_run)
    return run_live_or_rebuild(rebuild=args.rebuild, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
