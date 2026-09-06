"""
Track B (discretionary) write API — Phase 8 work item 4.

Track B is industry-level narrative overreaction: low frequency, judgment-dependent,
impossible to backtest. It cannot be measured statistically the way Track A is, so what
this module enforces instead is *discipline*:

1. A discretionary ledger row cannot exist without a linked thesis whose falsifier is
   non-empty. You do not get to take the position without writing down what would prove
   you wrong.
2. The falsifier is immutable once the position opens. Edits before entry are recorded in
   an append-only `thesis_revisions` table so the original wording always survives.

Rule 2 is the entire anti-value-trap mechanism. After entry the operator will find
reasons; the defence is that the original text cannot move. Both rules are enforced here
*and* as CHECK constraints in build_db.py, so a direct sqlite write can't route around
them either.

Track A rows are written by build_signals.py, not here. Nothing in this module may write
a mechanism row, and nothing in build_signals.py may write a discretionary one.

Usage is operator-driven (there is no collector for a thesis — it is authored by hand):
    from ledger import create_thesis, open_discretionary_trade
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timezone

TRACK_MECHANISM = "mechanism"
TRACK_DISCRETIONARY = "discretionary"

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

# Cost assumption for Track B. Deliberately duplicated rather than shared with Track A's
# constant: the tracks must stay separable, and a months-long discretionary hold has a
# different cost profile from a 5-day mechanism trade even where the statutory components
# match. Same conservative flat basis for now (§8 requires net return always be computed).
COST_PCT_ROUNDTRIP = 0.30    # STT + stamp + exchange + GST
SLIPPAGE_PCT_ROUNDTRIP = 0.20
TOTAL_COSTS_PCT = COST_PCT_ROUNDTRIP + SLIPPAGE_PCT_ROUNDTRIP

PIPELINE_VERSION = "0.1.0"
_SOURCE = "manual"
_SOURCE_FILE = "operator_entry"


class LedgerError(Exception):
    """Raised when a write would violate a Track B discipline rule."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_thesis(
    conn: sqlite3.Connection,
    *,
    subject: str,
    claim: str,
    counter_claim: str,
    falsifier: str,
    horizon_months: int | None = None,
    review_date: str | None = None,
    created_date: str | None = None,
    thesis_id: str | None = None,
) -> str:
    """Record a Track B thesis. `falsifier` must be non-empty — the observable fact that
    would prove the thesis wrong. Returns the thesis id."""
    if not falsifier or not falsifier.strip():
        raise LedgerError("falsifier is required and cannot be empty — a thesis without a falsifier is not a thesis")
    if not subject or not subject.strip():
        raise LedgerError("subject is required")

    thesis_id = thesis_id or f"thesis:{uuid.uuid4().hex[:12]}"
    created = created_date or date.today().isoformat()
    now = _now()

    conn.execute(
        """
        INSERT INTO thesis (id, subject, created_date, claim, counter_claim, falsifier,
                            horizon_months, review_date, status, outcome, falsifier_hit,
                            source, source_file, ingested_at, pipeline_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
        """,
        (thesis_id, subject.strip(), created, claim, counter_claim, falsifier.strip(),
         horizon_months, review_date, STATUS_OPEN, _SOURCE, _SOURCE_FILE, now, PIPELINE_VERSION),
    )
    return thesis_id


def position_is_open(conn: sqlite3.Connection, thesis_id: str) -> bool:
    """True once any discretionary ledger row for this thesis has an entry_date."""
    row = conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE thesis_id = ? AND entry_date IS NOT NULL", (thesis_id,)
    ).fetchone()
    return bool(row and row[0])


def revise_falsifier(conn: sqlite3.Connection, thesis_id: str, new_falsifier: str, *, reason: str | None = None) -> None:
    """Edit a falsifier *before* the position opens, preserving the original wording in
    thesis_revisions. Refuses outright once a position is open — that immutability is the
    point of the mechanism, not a formality."""
    if not new_falsifier or not new_falsifier.strip():
        raise LedgerError("falsifier cannot be revised to empty")

    row = conn.execute("SELECT falsifier FROM thesis WHERE id = ?", (thesis_id,)).fetchone()
    if row is None:
        raise LedgerError(f"no such thesis: {thesis_id}")
    previous = row[0]

    if position_is_open(conn, thesis_id):
        raise LedgerError(
            f"falsifier is immutable once the position is open (thesis {thesis_id}). "
            "The original wording is what protects you from talking yourself out of it."
        )

    now = _now()
    conn.execute(
        """
        INSERT INTO thesis_revisions (revision_id, thesis_id, field, previous_value, new_value,
                                       revised_at, reason, source, source_file, ingested_at, pipeline_version)
        VALUES (?, ?, 'falsifier', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (f"rev:{uuid.uuid4().hex[:12]}", thesis_id, previous, new_falsifier.strip(),
         now, reason, _SOURCE, _SOURCE_FILE, now, PIPELINE_VERSION),
    )
    conn.execute("UPDATE thesis SET falsifier = ? WHERE id = ?", (new_falsifier.strip(), thesis_id))


def open_discretionary_trade(
    conn: sqlite3.Connection,
    *,
    thesis_id: str,
    direction: str,
    entry_date: str,
    entry_price: float,
    mode: str = "paper",
    notes: str | None = None,
    ledger_id: str | None = None,
) -> str:
    """Open a Track B paper/live position against a thesis. Refuses without a thesis
    carrying a non-empty falsifier (enforced here and by a CHECK in the schema)."""
    row = conn.execute("SELECT falsifier FROM thesis WHERE id = ?", (thesis_id,)).fetchone()
    if row is None:
        raise LedgerError(
            f"cannot open a discretionary position without a thesis (no such thesis: {thesis_id})"
        )
    if not row[0] or not row[0].strip():
        raise LedgerError(
            f"thesis {thesis_id} has an empty falsifier — write down what would prove you wrong first"
        )
    if direction not in ("long", "short"):
        raise LedgerError(f"direction must be 'long' or 'short', got {direction!r}")

    ledger_id = ledger_id or f"ledger:discretionary:{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO ledger (ledger_id, signal_id, thesis_id, track, mode, direction,
                            entry_date, entry_price, notes, updated_at, pipeline_version)
        VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ledger_id, thesis_id, TRACK_DISCRETIONARY, mode, direction, entry_date,
         entry_price, notes, _now(), PIPELINE_VERSION),
    )
    return ledger_id


def close_discretionary_trade(
    conn: sqlite3.Connection,
    ledger_id: str,
    *,
    exit_date: str,
    exit_price: float,
    exit_reason: str = "manual",
) -> None:
    """Close a Track B position, computing net return after costs (§8: net return must
    always be computed)."""
    row = conn.execute(
        "SELECT direction, entry_date, entry_price, track FROM ledger WHERE ledger_id = ?", (ledger_id,)
    ).fetchone()
    if row is None:
        raise LedgerError(f"no such ledger row: {ledger_id}")
    direction, entry_date, entry_price, track = row
    if track != TRACK_DISCRETIONARY:
        raise LedgerError(f"{ledger_id} is a {track} row — close it through its own track's pipeline")
    if entry_price is None:
        raise LedgerError(f"{ledger_id} has no entry price to close against")

    if direction == "long":
        gross = (exit_price / entry_price - 1.0) * 100.0
    else:
        gross = (entry_price / exit_price - 1.0) * 100.0
    net = gross - TOTAL_COSTS_PCT
    holding_days = (date.fromisoformat(exit_date) - date.fromisoformat(entry_date)).days

    conn.execute(
        """
        UPDATE ledger SET exit_date = ?, exit_price = ?, exit_reason = ?,
                          gross_return_pct = ?, costs_pct = ?, net_return_pct = ?,
                          holding_days = ?, updated_at = ?
        WHERE ledger_id = ?
        """,
        (exit_date, exit_price, exit_reason, gross, TOTAL_COSTS_PCT, net,
         holding_days, _now(), ledger_id),
    )


def theses_past_review(conn: sqlite3.Connection, as_of: str | None = None) -> list[dict]:
    """Theses whose review_date has passed and which still have no outcome recorded."""
    as_of = as_of or date.today().isoformat()
    try:
        rows = conn.execute(
            """
            SELECT id, subject, created_date, claim, counter_claim, falsifier,
                   horizon_months, review_date, status
            FROM thesis
            WHERE review_date IS NOT NULL AND review_date <= ? AND outcome IS NULL
            ORDER BY review_date
            """,
            (as_of,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    keys = ["id", "subject", "created_date", "claim", "counter_claim", "falsifier",
            "horizon_months", "review_date", "status"]
    return [dict(zip(keys, r)) for r in rows]
