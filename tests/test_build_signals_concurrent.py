"""
PHASED_IMPLEMENTATION_PLAN.md Phase 6 acceptance test: "Both signal types run
concurrently without interference" and "Ledger correctly segregates performance by
type." Runs build_signals._build_all() against a single database seeded with both a
fo_ban_exit event and an ipo_lockin_unlock event for *different* symbols, and confirms
neither type's signals/ledger rows leak into or corrupt the other's.
"""
import json
import sqlite3
import unittest

import build_signals
from build_db import create_schema


def seed(conn, prices, ban_events, ipo_events):
    create_schema(conn)
    conn.executemany(
        "INSERT INTO prices (symbol,date,open,high,low,close,volume,delivery_pct,source,"
        "source_file,ingested_at,pipeline_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(s, d, o, h, l, c, v, 50.0, "test", "test", "now", "0.1.0") for (s, d, o, h, l, c, v) in prices],
    )
    conn.executemany(
        "INSERT INTO events (event_id,symbol,event_type,announce_date,effective_date,detail,"
        "source_file,ingested_at,pipeline_version) VALUES (?,?,?,NULL,?,?,?,?,?)",
        [(f"{et}:{s}:{d}", s, et, d, "{}", "test", "now", "0.1.0") for (s, et, d) in ban_events],
    )
    conn.executemany(
        "INSERT INTO events (event_id,symbol,event_type,announce_date,effective_date,detail,"
        "source_file,ingested_at,pipeline_version) VALUES (?,?,'ipo_lockin_unlock',NULL,?,?,?,?,?)",
        [(eid, s, d, json.dumps(detail), "test", "now", "0.1.0") for (eid, s, d, detail) in ipo_events],
    )
    conn.commit()


class TestConcurrentSignalTypes(unittest.TestCase):
    def test_both_types_produce_independent_correct_signals(self):
        conn = sqlite3.connect(":memory:")
        prices = (
            [("BANSYM", f"2026-01-{d:02d}", 100, 100, 100, 100, 1000) for d in range(1, 31)]
            + [("IPOSYM", f"2026-01-{d:02d}", 200, 200, 200, 200, 1000) for d in range(1, 31)]
        )
        seed(
            conn, prices,
            ban_events=[("BANSYM", "fo_ban_exit", "2026-01-05")],
            ipo_events=[("evt1", "IPOSYM", "2026-01-15", {"tranche": "anchor_30d", "unlock_pct": 50})],
        )
        signals, ledger = build_signals._build_all(conn)

        self.assertEqual(len(signals), 2)
        by_type = {s.signal_type: s for s in signals}
        self.assertEqual(set(by_type), {"fo_ban_exit", "ipo_lockin"})
        self.assertEqual(by_type["fo_ban_exit"].symbol, "BANSYM")
        self.assertEqual(by_type["ipo_lockin"].symbol, "IPOSYM")

        # No cross-contamination: each type's ledger rows reference only their own signal.
        ban_signal_id = by_type["fo_ban_exit"].signal_id
        ipo_signal_id = by_type["ipo_lockin"].signal_id
        ban_ledger = [l for l in ledger if l.signal_id == ban_signal_id]
        ipo_ledger = [l for l in ledger if l.signal_id == ipo_signal_id]
        self.assertEqual(len(ban_ledger), 2)
        self.assertEqual(len(ipo_ledger), 2)
        self.assertEqual(len(ledger), 4)  # nothing extra, nothing missing

    def test_write_and_query_segregates_by_type_in_the_database(self):
        conn = sqlite3.connect(":memory:")
        prices = (
            [("BANSYM", f"2026-01-{d:02d}", 100, 100, 100, 100, 1000) for d in range(1, 31)]
            + [("IPOSYM", f"2026-01-{d:02d}", 200, 200, 200, 200, 1000) for d in range(1, 31)]
        )
        seed(
            conn, prices,
            ban_events=[("BANSYM", "fo_ban_exit", "2026-01-05")],
            ipo_events=[("evt1", "IPOSYM", "2026-01-15", {"tranche": "anchor_30d", "unlock_pct": 50})],
        )
        signals, ledger = build_signals._build_all(conn)
        build_signals._write(conn, signals, ledger, now_iso="now")
        conn.commit()

        rows = conn.execute(
            """
            SELECT s.signal_type, COUNT(*) FROM ledger l
            JOIN signals s ON s.signal_id = l.signal_id
            GROUP BY s.signal_type ORDER BY s.signal_type
            """
        ).fetchall()
        self.assertEqual(rows, [("fo_ban_exit", 2), ("ipo_lockin", 2)])

        # A symbol belonging to one type must never appear under the other's rows.
        cross = conn.execute(
            "SELECT symbol FROM signals WHERE signal_type='fo_ban_exit' AND symbol='IPOSYM'"
        ).fetchall()
        self.assertEqual(cross, [])


if __name__ == "__main__":
    unittest.main()
