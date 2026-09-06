#!/usr/bin/env python3
"""
Phase 2 — parse layer and database.

Parses raw bhavcopy files (data/raw/bhavcopy/) into the `prices` table and mirrors
data/health.json into the `health` table. Nothing else — events/signals/ledger/dashboard
are later phases.

Modes:
    --live                 Ingest the full raw archive into data/tracker.db (upsert; safe to
                            run repeatedly, e.g. once per day after collection).
    --rebuild              Delete data/tracker.db first, then do exactly what --live does.
                            This is what proves the rebuild-equivalence acceptance test:
                            since ingestion is a deterministic upsert keyed on (symbol, date),
                            a from-scratch rebuild and years of incremental --live runs must
                            produce the same `prices` content (ingested_at excluded — that
                            column timestamps the parse run, not the underlying data).
    --replay FROM TO       Read only the raw archive for dates in [FROM, TO], write to a
                            scratch database (--out, default data/scratch_replay.db), and
                            never touch data/tracker.db. The primary debugging tool: replay
                            any date range with no network involved.
    --dry-run              Parse and validate everything for the selected mode, print a
                            summary, write nothing.

Usage:
    python src/build_db.py --live
    python src/build_db.py --rebuild
    python src/build_db.py --replay 2026-08-01 2026-08-31
    python src/build_db.py --live --dry-run
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from parse import bhavcopy  # noqa: E402

DATA_ROOT = REPO_ROOT / "data"
DEFAULT_DB_PATH = DATA_ROOT / "tracker.db"
DEFAULT_REPLAY_DB_PATH = DATA_ROOT / "scratch_replay.db"
HEALTH_JSON_PATH = DATA_ROOT / "health.json"

# Bumped whenever bhavcopy parsing/mapping logic changes, so a signal from February that
# behaves differently from one in June can be traced to a code change vs a market change.
PIPELINE_VERSION = "0.1.0"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    delivery_pct REAL,
    source TEXT NOT NULL,
    source_file TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS index_prices (
    index_name TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL,
    source TEXT NOT NULL,
    source_file TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    PRIMARY KEY (index_name, date)
);

CREATE TABLE IF NOT EXISTS health (
    source TEXT PRIMARY KEY,
    last_attempt TEXT,
    last_success TEXT,
    consecutive_failures INTEGER,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    announce_date TEXT,
    effective_date TEXT NOT NULL,
    detail TEXT,
    source_file TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    state TEXT NOT NULL,
    armed_date TEXT,
    window_start TEXT,
    window_end TEXT,
    entry_date TEXT,
    entry_ref_price REAL,
    stop_price REAL,
    invalidation_reason TEXT,
    filters_passed TEXT,
    updated_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thesis (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    created_date TEXT NOT NULL,
    claim TEXT NOT NULL,
    counter_claim TEXT NOT NULL,
    falsifier TEXT NOT NULL,
    horizon_months INTEGER,
    review_date TEXT,
    status TEXT NOT NULL,
    outcome TEXT,
    falsifier_hit INTEGER,
    source TEXT NOT NULL,
    source_file TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    CHECK (length(trim(falsifier)) > 0)
);

CREATE TABLE IF NOT EXISTS thesis_revisions (
    revision_id TEXT PRIMARY KEY,
    thesis_id TEXT NOT NULL REFERENCES thesis(id),
    field TEXT NOT NULL,
    previous_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    revised_at TEXT NOT NULL,
    reason TEXT,
    source TEXT NOT NULL,
    source_file TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sector_map (
    symbol TEXT NOT NULL,
    sector TEXT NOT NULL,
    company_name TEXT,
    source TEXT NOT NULL,
    source_file TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    PRIMARY KEY (symbol)
);

CREATE TABLE IF NOT EXISTS research_prompts (
    prompt_id TEXT PRIMARY KEY,
    sector TEXT NOT NULL,
    detected_date TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    spread REAL NOT NULL,
    n_constituents INTEGER,
    median_constituent_return REAL,
    index_return REAL,
    breadth REAL,
    status TEXT NOT NULL,
    notes TEXT,
    source TEXT NOT NULL,
    source_file TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    CHECK (status IN ('open', 'investigated', 'dismissed'))
);
"""

# Kept separate from SCHEMA_SQL so the Phase 8 migration can recreate exactly this one
# table without re-running the whole script mid-rename.
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS ledger (
    ledger_id TEXT PRIMARY KEY,
    signal_id TEXT REFERENCES signals(signal_id),
    thesis_id TEXT REFERENCES thesis(id),
    track TEXT NOT NULL DEFAULT 'mechanism',
    mode TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_date TEXT,
    entry_price REAL,
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    gross_return_pct REAL,
    costs_pct REAL,
    net_return_pct REAL,
    holding_days INTEGER,
    notes TEXT,
    updated_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    CHECK (track IN ('mechanism', 'discretionary')),
    -- Exactly one linkage per track. A mechanism row comes from a signal; a
    -- discretionary row comes from a thesis and has no signal at all. Enforced here as
    -- well as in src/ledger.py so it holds even against a direct sqlite write.
    CHECK (
        (track = 'mechanism'     AND signal_id IS NOT NULL AND thesis_id IS NULL)
     OR (track = 'discretionary' AND thesis_id IS NOT NULL AND signal_id IS NULL)
    )
);
"""

# SQLite cannot ALTER a NOT NULL away, so widening `ledger` (signal_id becomes nullable,
# plus new track/thesis_id columns) needs a table rebuild. Mechanism rows are derived and
# could be regenerated, but discretionary rows never can be — they are operator-authored
# source data, not derived from raw — so this migration copies rather than drops.
_LEDGER_MIGRATION_COLUMNS = [
    "ledger_id", "signal_id", "mode", "direction", "entry_date", "entry_price",
    "exit_date", "exit_price", "exit_reason", "gross_return_pct", "costs_pct",
    "net_return_pct", "holding_days", "notes", "updated_at", "pipeline_version",
]


def migrate_schema(conn: sqlite3.Connection) -> bool:
    """Bring a pre-Phase-8 ledger up to the track-aware shape, preserving every row.
    Returns True if a migration actually ran. No-op on a fresh or already-migrated db."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(ledger)")}
    if not existing or {"track", "thesis_id"} <= existing:
        return False

    cols = ", ".join(_LEDGER_MIGRATION_COLUMNS)
    conn.execute("ALTER TABLE ledger RENAME TO ledger_pre_phase8")
    conn.execute(LEDGER_DDL.strip().rstrip(";"))
    conn.execute(
        f"INSERT INTO ledger ({cols}, track, thesis_id) "
        f"SELECT {cols}, 'mechanism', NULL FROM ledger_pre_phase8"
    )
    conn.execute("DROP TABLE ledger_pre_phase8")
    return True


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL + LEDGER_DDL)
    migrate_schema(conn)


def discover_bhavcopy_files(data_root: Path) -> list[Path]:
    return sorted((data_root / "raw" / "bhavcopy").glob("*/*/*.csv"))


def _filename_date_ddmmyyyy(path: Path) -> date | None:
    digits = "".join(ch for ch in path.stem if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[-8:], "%d%m%Y").date()
    except ValueError:
        return None


def ingest_bhavcopy_file(conn: sqlite3.Connection, path: Path, *, now_iso: str) -> bhavcopy.ParseResult | None:
    """Returns None (and ingests nothing) if the file's own DATE1 content disagrees with
    its filename — NSE's bhavcopy archive endpoint has been observed silently serving the
    prior trading day's file under a holiday's filename instead of a clean 404 (found via
    the byte-identical 2026-06-25/2026-06-26 files during Phase 4). Ingesting it anyway
    would corrupt source_file provenance for the genuine date without adding any real
    data — the correctly-named file for the actual date already covers it."""
    result = bhavcopy.parse_file(path)
    if result.rows:
        content_date = date.fromisoformat(result.rows[0].date)
        filename_date = _filename_date_ddmmyyyy(path)
        if filename_date is not None and content_date != filename_date:
            print(f"WARNING: skipping {path} — filename implies {filename_date} but content is "
                  f"dated {content_date} (NSE likely re-served a prior day's file for a holiday)")
            return None

    source_file = path.relative_to(DATA_ROOT).as_posix()
    conn.executemany(
        """
        INSERT INTO prices
            (symbol, date, open, high, low, close, volume, delivery_pct, source,
             source_file, ingested_at, pipeline_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
            volume=excluded.volume, delivery_pct=excluded.delivery_pct, source=excluded.source,
            source_file=excluded.source_file, ingested_at=excluded.ingested_at,
            pipeline_version=excluded.pipeline_version
        """,
        [
            (r.symbol, r.date, r.open, r.high, r.low, r.close, r.volume, r.delivery_pct,
             r.source, source_file, now_iso, PIPELINE_VERSION)
            for r in result.rows
        ],
    )
    return result


def ingest_health(conn: sqlite3.Connection, health_json_path: Path) -> int:
    if not health_json_path.exists():
        return 0
    with health_json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    rows = [
        (source, row.get("last_attempt"), row.get("last_success"),
         row.get("consecutive_failures", 0), row.get("last_error"))
        for source, row in data.items()
    ]
    conn.executemany(
        """
        INSERT INTO health (source, last_attempt, last_success, consecutive_failures, last_error)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            last_attempt=excluded.last_attempt, last_success=excluded.last_success,
            consecutive_failures=excluded.consecutive_failures, last_error=excluded.last_error
        """,
        rows,
    )
    return len(rows)


def _file_date(path: Path) -> date:
    """Trade date for a bhavcopy raw file, read from its own content (DATE1), not the
    filename — the filename is a convenience, DATE1 is the authoritative source."""
    result = bhavcopy.parse_file(path)
    if not result.rows:
        # File exists but had zero EQ rows (shouldn't happen for bhavcopy) — fall back
        # to filename's DDMMYYYY so replay range filtering doesn't crash on an edge case.
        digits = "".join(ch for ch in path.stem if ch.isdigit())[-8:]
        return datetime.strptime(digits, "%d%m%Y").date()
    return date.fromisoformat(result.rows[0].date)


def run_live_or_rebuild(*, rebuild: bool, dry_run: bool) -> int:
    files = discover_bhavcopy_files(DATA_ROOT)
    if dry_run:
        total_eq = 0
        for path in files:
            result = bhavcopy.parse_file(path)
            total_eq += len(result.rows)
            print(f"[dry-run] {path.relative_to(DATA_ROOT)}: {len(result.rows)} EQ rows "
                  f"of {result.total_rows} total, series={result.series_counts}")
        print(f"[dry-run] {len(files)} files, {total_eq} EQ rows total. Nothing written.")
        return 0

    if rebuild and DEFAULT_DB_PATH.exists():
        DEFAULT_DB_PATH.unlink()
        print(f"[rebuild] deleted {DEFAULT_DB_PATH}")

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    try:
        create_schema(conn)
        total_rows = 0
        for path in files:
            result = ingest_bhavcopy_file(conn, path, now_iso=now_iso)
            if result is None:
                continue
            total_rows += len(result.rows)
            print(f"ingested {path.relative_to(DATA_ROOT)}: {len(result.rows)} rows")
        n_health = ingest_health(conn, HEALTH_JSON_PATH)
        conn.commit()
        print(f"done: {len(files)} bhavcopy files, {total_rows} price rows, "
              f"{n_health} health rows -> {DEFAULT_DB_PATH}")
    finally:
        conn.close()
    return 0


def run_replay(from_date: date, to_date: date, *, out_path: Path, dry_run: bool) -> int:
    files = [p for p in discover_bhavcopy_files(DATA_ROOT) if from_date <= _file_date(p) <= to_date]

    if dry_run:
        total_eq = sum(len(bhavcopy.parse_file(p).rows) for p in files)
        print(f"[dry-run][replay {from_date}..{to_date}] {len(files)} files, {total_eq} EQ rows. "
              f"Nothing written.")
        return 0

    if out_path.exists():
        out_path.unlink()

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(out_path)
    try:
        create_schema(conn)
        total_rows = 0
        for path in files:
            result = ingest_bhavcopy_file(conn, path, now_iso=now_iso)
            if result is None:
                continue
            total_rows += len(result.rows)
        conn.commit()
        print(f"[replay {from_date}..{to_date}] {len(files)} files, {total_rows} price rows "
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
    parser.add_argument("--out", type=str, default=str(DEFAULT_REPLAY_DB_PATH),
                        help="output db path for --replay (default: data/scratch_replay.db)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.replay:
        from_date, to_date = (date.fromisoformat(d) for d in args.replay)
        return run_replay(from_date, to_date, out_path=Path(args.out), dry_run=args.dry_run)
    return run_live_or_rebuild(rebuild=args.rebuild, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
