import json
import sqlite3
import unittest
from datetime import date

from build_db import create_schema
from signals import ipo_lockin


def seed(conn, prices, ipo_events):
    create_schema(conn)
    conn.executemany(
        "INSERT INTO prices (symbol,date,open,high,low,close,volume,delivery_pct,source,"
        "source_file,ingested_at,pipeline_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(s, d, o, h, l, c, v, 50.0, "test", "test", "now", "0.1.0") for (s, d, o, h, l, c, v) in prices],
    )
    conn.executemany(
        "INSERT INTO events (event_id,symbol,event_type,announce_date,effective_date,detail,"
        "source_file,ingested_at,pipeline_version) VALUES (?,?,'ipo_lockin_unlock',NULL,?,?,?,?,?)",
        [(eid, s, d, json.dumps(detail), "test", "now", "0.1.0") for (eid, s, d, detail) in ipo_events],
    )
    conn.commit()


def flat_prices(symbol, start_day, n, price=100.0, month=1):
    return [(symbol, f"2026-{month:02d}-{d:02d}", price, price, price, price, 1000) for d in range(start_day, start_day + n)]


class TestIpoLockinSignals(unittest.TestCase):
    def test_watching_when_unlock_date_far_in_the_future(self):
        # Archive only reaches Jan 5; unlock date is Jan 30 — 25 calendar days out, well
        # beyond the 10-day watching threshold.
        conn = sqlite3.connect(":memory:")
        prices = flat_prices("NEWCO", 1, 5)
        seed(conn, prices, [("evt1", "NEWCO", "2026-01-30", {"tranche": "anchor_30d", "unlock_pct": 50})])
        signals, ledger = ipo_lockin.build_signals(conn)
        self.assertEqual(signals[0].state, "watching")
        self.assertIsNone(signals[0].window_start)
        self.assertIsNone(signals[0].window_end)
        self.assertEqual(ledger, [])

    def test_full_lifecycle_window_start_is_before_unlock_date(self):
        conn = sqlite3.connect(":memory:")
        prices = flat_prices("NEWCO", 1, 31)  # Jan 1 .. Jan 31, flat price, no gaps
        seed(conn, prices, [("evt1", "NEWCO", "2026-01-15", {"tranche": "anchor_30d", "unlock_pct": 50})])
        signals, ledger = ipo_lockin.build_signals(conn)
        sig = signals[0]
        self.assertEqual(sig.state, "closed")
        # window_start = T - 5 trading days = Jan 10 (flat calendar, no weekends in this fixture)
        self.assertEqual(sig.window_start, "2026-01-10")
        # window_end = T + 10 trading days = Jan 25
        self.assertEqual(sig.window_end, "2026-01-25")
        # entry = T + 1 trading day = Jan 16
        self.assertEqual(sig.entry_date, "2026-01-16")
        self.assertEqual(len(ledger), 2)
        self.assertEqual({l.direction for l in ledger}, {"long", "short"})
        for l in ledger:
            self.assertEqual(l.exit_reason, "window_end")

    def test_watch_metric_computed_from_pre_unlock_sessions_only(self):
        conn = sqlite3.connect(":memory:")
        prices = flat_prices("NEWCO", 1, 31, price=100.0)
        # Bump unlock-day volume far above the flat 1000 baseline
        prices = [p if p[1] != "2026-01-15" else ("NEWCO", "2026-01-15", 100, 100, 100, 100, 50000) for p in prices]
        seed(conn, prices, [("evt1", "NEWCO", "2026-01-15", {"tranche": "anchor_30d", "unlock_pct": 50})])
        signals, ledger = ipo_lockin.build_signals(conn)
        fp = signals[0].filters_passed
        self.assertEqual(fp["unlock_day_volume"], 50000)
        self.assertEqual(fp["adv_20_volume"], 1000.0)
        self.assertAlmostEqual(fp["volume_vs_adv_ratio"], 50.0)

    def test_no_invalidation_detection_yet_signal_still_resolves(self):
        # §7.2 invalidation (lock-in extended/waived, block deal) isn't automatically
        # detected in v1 — confirm the signal still resolves normally rather than getting
        # stuck, and is never marked invalidated (since nothing can trigger it yet).
        conn = sqlite3.connect(":memory:")
        prices = flat_prices("NEWCO", 1, 31)
        seed(conn, prices, [("evt1", "NEWCO", "2026-01-15", {"tranche": "anchor_30d", "unlock_pct": 50})])
        signals, ledger = ipo_lockin.build_signals(conn)
        self.assertNotEqual(signals[0].state, "invalidated")
        self.assertIsNone(signals[0].invalidation_reason)


if __name__ == "__main__":
    unittest.main()
