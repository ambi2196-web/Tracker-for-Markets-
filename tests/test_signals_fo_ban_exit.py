import sqlite3
import unittest

from build_db import create_schema
from signals import fo_ban_exit


def seed(conn, prices, events):
    create_schema(conn)
    conn.executemany(
        "INSERT INTO prices (symbol,date,open,high,low,close,volume,delivery_pct,source,"
        "source_file,ingested_at,pipeline_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(s, d, o, h, l, c, v, 50.0, "test", "test", "now", "0.1.0") for (s, d, o, h, l, c, v) in prices],
    )
    conn.executemany(
        "INSERT INTO events (event_id,symbol,event_type,announce_date,effective_date,detail,"
        "source_file,ingested_at,pipeline_version) VALUES (?,?,?,NULL,?,?,?,?,?)",
        [(f"{et}:{s}:{d}", s, et, d, "{}", "test", "now", "0.1.0") for (s, et, d) in events],
    )
    conn.commit()


def daily_prices(symbol, start_day, n, price=100.0):
    return [(symbol, f"2026-01-{d:02d}", price, price, price, price, 1000) for d in range(start_day, start_day + n)]


class TestFoBanExitStateMachine(unittest.TestCase):
    def test_pending_when_insufficient_future_data(self):
        # Only 4 days of price history after T (2026-01-05) — nowhere near the 5 trading
        # days needed for window_end, so it must stay unresolved regardless of whether
        # entry_date itself happened to resolve.
        conn = sqlite3.connect(":memory:")
        seed(conn, daily_prices("AAA", 5, 4), [("AAA", "fo_ban_exit", "2026-01-05")])
        signals, ledger = fo_ban_exit.build_signals(conn)
        self.assertIn(signals[0].state, ("armed", "open"))
        self.assertIsNone(signals[0].window_end)

    def test_full_lifecycle_closes_at_window_end(self):
        # Flat price throughout — neither direction's 8% stop is ever touched, so both
        # legs must resolve via window_end rather than stop.
        conn = sqlite3.connect(":memory:")
        prices = [("AAA", f"2026-01-{d:02d}", 100, 100, 100, 100, 1000) for d in range(5, 20)]
        seed(conn, prices, [("AAA", "fo_ban_exit", "2026-01-05")])
        signals, ledger = fo_ban_exit.build_signals(conn)
        self.assertEqual(signals[0].state, "closed")
        self.assertEqual(signals[0].entry_date, "2026-01-06")
        self.assertEqual(signals[0].entry_ref_price, 100)
        self.assertEqual(len(ledger), 2)
        self.assertEqual({l.direction for l in ledger}, {"long", "short"})
        for l in ledger:
            self.assertEqual(l.exit_reason, "window_end")

    def test_invalidated_before_entry_produces_zero_ledger_rows(self):
        conn = sqlite3.connect(":memory:")
        prices = daily_prices("BBB", 5, 15)
        seed(conn, prices, [("BBB", "fo_ban_exit", "2026-01-05"), ("BBB", "fo_ban_entry", "2026-01-06")])
        signals, ledger = fo_ban_exit.build_signals(conn)
        self.assertEqual(signals[0].state, "invalidated")
        self.assertIn("before_entry", signals[0].invalidation_reason)
        self.assertEqual(ledger, [])

    def test_invalidated_after_entry_produces_two_closed_ledger_rows(self):
        conn = sqlite3.connect(":memory:")
        prices = daily_prices("CCC", 5, 15)
        seed(conn, prices, [("CCC", "fo_ban_exit", "2026-01-05"), ("CCC", "fo_ban_entry", "2026-01-09")])
        signals, ledger = fo_ban_exit.build_signals(conn)
        self.assertEqual(signals[0].state, "invalidated")
        self.assertIn("after_entry", signals[0].invalidation_reason)
        self.assertEqual(len(ledger), 2)
        self.assertTrue(all(l.exit_reason == "invalidated" and l.exit_date is not None for l in ledger))

    def test_halt_on_window_end_falls_back_to_next_available_close(self):
        conn = sqlite3.connect(":memory:")
        dates = [5, 6, 7, 8, 9, 12, 13]
        prices = [("DDD", f"2026-01-{d:02d}", 100, 100, 100, 100, 1000) for d in dates if d != 12]
        prices += [("OTHER", f"2026-01-{d:02d}", 50, 50, 50, 50, 500) for d in dates]
        seed(conn, prices, [("DDD", "fo_ban_exit", "2026-01-05")])
        signals, ledger = fo_ban_exit.build_signals(conn)
        self.assertEqual(signals[0].window_end, "2026-01-12")
        self.assertEqual(signals[0].state, "closed")
        for l in ledger:
            self.assertEqual(l.exit_date, "2026-01-13")
            self.assertIn("no price on window_end", l.notes)

    def test_long_stop_triggers_before_window_end(self):
        conn = sqlite3.connect(":memory:")
        prices = [("EEE", f"2026-01-{d:02d}", 100, 100, 100, 100, 1000) for d in range(5, 20)]
        prices[1] = ("EEE", "2026-01-06", 100, 100, 100, 100, 1000)  # entry = 100, long stop = 92
        prices[3] = ("EEE", "2026-01-08", 90, 90, 90, 90, 1000)  # breaches long stop (<=92)
        seed(conn, prices, [("EEE", "fo_ban_exit", "2026-01-05")])
        signals, ledger = fo_ban_exit.build_signals(conn)
        long_row = next(l for l in ledger if l.direction == "long")
        self.assertEqual(long_row.exit_reason, "stop")
        self.assertEqual(long_row.exit_date, "2026-01-08")

    def test_cost_model_applied_net_equals_gross_minus_total_costs(self):
        conn = sqlite3.connect(":memory:")
        prices = [("FFF", f"2026-01-{d:02d}", 100, 100, 100, 100, 1000) for d in range(5, 20)]
        seed(conn, prices, [("FFF", "fo_ban_exit", "2026-01-05")])
        signals, ledger = fo_ban_exit.build_signals(conn)
        for l in ledger:
            self.assertAlmostEqual(l.net_return_pct, l.gross_return_pct - fo_ban_exit.TOTAL_COSTS_PCT, places=9)
            self.assertEqual(l.costs_pct, fo_ban_exit.TOTAL_COSTS_PCT)

    def test_lookahead_safety_adv_never_uses_future_prices(self):
        # Seed an extreme price on T+1 (after arming) that would massively skew ADV if
        # (incorrectly) included. If the computed ADV reflects it, lookahead has leaked.
        conn = sqlite3.connect(":memory:")
        prices = [("GGG", f"2026-01-{d:02d}", 100, 100, 100, 100, 1000) for d in range(1, 5)]
        prices.append(("GGG", "2026-01-05", 100, 100, 100, 100, 1000))  # T (arming date)
        prices.append(("GGG", "2026-01-06", 999999, 999999, 999999, 999999, 999999999))  # T+1, must be excluded
        for d in range(7, 20):
            prices.append(("GGG", f"2026-01-{d:02d}", 100, 100, 100, 100, 1000))
        seed(conn, prices, [("GGG", "fo_ban_exit", "2026-01-05")])
        signals, ledger = fo_ban_exit.build_signals(conn)  # would raise AssertionError if it leaked
        adv = signals[0].filters_passed["adv_20_rupees"]
        self.assertLess(adv, 1_000_000_000)  # far below what including the T+1 row would produce

    def test_symbol_with_no_price_at_entry_is_invalidated_not_stuck(self):
        conn = sqlite3.connect(":memory:")
        prices = daily_prices("OTHER", 5, 15, price=50.0)  # keeps the calendar populated
        seed(conn, prices, [("GHOST", "fo_ban_exit", "2026-01-05")])
        signals, ledger = fo_ban_exit.build_signals(conn)
        self.assertEqual(signals[0].state, "invalidated")
        self.assertEqual(signals[0].invalidation_reason, "no_price_data_at_entry")
        self.assertEqual(ledger, [])


if __name__ == "__main__":
    unittest.main()
