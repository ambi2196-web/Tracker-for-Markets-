"""
Phase 8 work item 2 — cohort verdicts, matched-window regime, and the hard rule that no
label may ever assert a proven edge.
"""
import re
import sqlite3
import unittest
from pathlib import Path

import regime
import verdict
from build_db import create_schema

SRC = Path(__file__).resolve().parent.parent / "src"


def _seed_prices_and_index(conn, dates_closes: list[tuple[str, float]]):
    for d, c in dates_closes:
        conn.execute(
            "INSERT INTO index_prices (index_name,date,close,source,source_file,ingested_at,pipeline_version)"
            " VALUES ('NIFTY50',?,?,'test','t','n','0')", (d, c)
        )


def _seed_trade(conn, *, sid, direction, entry, exit_, net, ledger_id, hold=5):
    conn.execute(
        "INSERT OR IGNORE INTO events (event_id,symbol,event_type,announce_date,effective_date,detail,"
        "source_file,ingested_at,pipeline_version) VALUES ('e1','AAA','fo_ban_exit',NULL,'2026-01-01','{}','t','n','0')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO signals (signal_id,event_id,symbol,signal_type,state,armed_date,window_start,"
        "window_end,entry_date,entry_ref_price,stop_price,invalidation_reason,filters_passed,updated_at,"
        "pipeline_version) VALUES (?,'e1','AAA','fo_ban_exit','closed','2026-01-01','2026-01-01',"
        "'2026-01-10',?,100,92,NULL,'{}','n','0')", (sid, entry)
    )
    conn.execute(
        "INSERT INTO ledger (ledger_id,signal_id,thesis_id,track,mode,direction,entry_date,entry_price,"
        "exit_date,exit_price,exit_reason,gross_return_pct,costs_pct,net_return_pct,holding_days,"
        "updated_at,pipeline_version) VALUES (?,?,NULL,'mechanism','paper',?,?,100,?,110,'window_end',"
        "?,0.5,?,?,'n','0')", (ledger_id, sid, direction, entry, exit_, net + 0.5, net, hold)
    )


class TestVerdictLabels(unittest.TestCase):
    def test_below_min_sample_is_insufficient_regardless_of_spread(self):
        self.assertEqual(verdict._label(verdict.MIN_SAMPLE - 1, 99.0), verdict.LABEL_INSUFFICIENT)
        self.assertEqual(verdict._label(0, -99.0), verdict.LABEL_INSUFFICIENT)

    def test_negative_or_zero_spread_is_no_edge(self):
        self.assertEqual(verdict._label(verdict.MIN_SAMPLE, -0.01), verdict.LABEL_NO_EDGE)
        self.assertEqual(verdict._label(verdict.MIN_SAMPLE, 0.0), verdict.LABEL_NO_EDGE)

    def test_positive_spread_at_min_sample_is_promising_not_proven(self):
        self.assertEqual(verdict._label(verdict.MIN_SAMPLE, 0.01), verdict.LABEL_PROMISING)
        self.assertEqual(verdict._label(500, 42.0), verdict.LABEL_PROMISING)

    def test_missing_regime_data_is_not_silently_called_no_edge(self):
        self.assertEqual(verdict._label(verdict.MIN_SAMPLE, None), verdict.LABEL_NO_REGIME_DATA)

    def test_min_sample_matches_the_spec_floor(self):
        self.assertEqual(verdict.MIN_SAMPLE, 20)


class TestNoLabelAssertsProvenEdge(unittest.TestCase):
    """The strongest statement this system is permitted to make is 'promising'."""

    FORBIDDEN = re.compile(r"\b(proven|validated|works|confirmed edge)\b", re.IGNORECASE)

    def test_no_verdict_constant_asserts_a_proven_edge(self):
        for label in (verdict.LABEL_INSUFFICIENT, verdict.LABEL_NO_EDGE,
                      verdict.LABEL_PROMISING, verdict.LABEL_NO_REGIME_DATA):
            match = self.FORBIDDEN.search(label)
            # "NOT PROVEN" is the one permitted use — it is a denial, not an assertion.
            if match:
                self.assertIn("NOT PROVEN", label.upper(),
                              f"verdict label {label!r} asserts a proven edge")

    def test_promising_label_carries_its_disclaimer(self):
        self.assertIn("NOT PROVEN", verdict.LABEL_PROMISING.upper())


class TestMatchedWindowRegime(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        create_schema(self.conn)
        # Index drifts down over the year but rises inside the trade's own window —
        # exactly the case where a global span misleads.
        _seed_prices_and_index(self.conn, [
            ("2026-01-01", 100.0), ("2026-01-06", 100.0), ("2026-01-12", 104.0),
            ("2026-12-31", 80.0),
        ])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_matched_window_uses_the_trades_own_window(self):
        r = regime.matched_window_return(self.conn, "2026-01-06", "2026-01-12")
        self.assertAlmostEqual(r, 4.0, places=6)

    def test_matched_window_returns_none_when_index_missing(self):
        empty = sqlite3.connect(":memory:")
        create_schema(empty)
        self.assertIsNone(regime.matched_window_return(empty, "2026-01-06", "2026-01-12"))
        empty.close()

    def test_mean_matched_window_skips_uncomputable_windows(self):
        mean = regime.mean_matched_window_return(
            self.conn, [("2026-01-06", "2026-01-12"), ("2030-01-01", "2030-02-01")]
        )
        self.assertAlmostEqual(mean, 4.0, places=6)

    def test_cohort_spread_uses_matched_not_global_window(self):
        # One trade, +5% net, over a window in which NIFTY rose 4%.
        _seed_trade(self.conn, sid="s1", direction="long", entry="2026-01-06",
                    exit_="2026-01-12", net=5.0, ledger_id="l1")
        self.conn.commit()
        c = verdict.cohort_verdicts(self.conn)[0]
        self.assertAlmostEqual(c["matched_nifty"], 4.0, places=6)
        self.assertAlmostEqual(c["spread"], 1.0, places=6)
        # Against the global span (100 -> 80, i.e. -20%) the spread would have been +25pp
        # — a fabricated edge. Guard that we did not do that.
        self.assertLess(c["spread"], 5.0)


class TestCohortStatistics(unittest.TestCase):
    def test_median_worst_and_holding_days_present_for_section_9_6(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        _seed_prices_and_index(conn, [("2026-01-06", 100.0), ("2026-01-12", 100.0)])
        for i, net in enumerate([1.0, 5.0, -3.0]):
            _seed_trade(conn, sid=f"s{i}", direction="long", entry="2026-01-06",
                        exit_="2026-01-12", net=net, ledger_id=f"l{i}", hold=6)
        conn.commit()
        c = verdict.cohort_verdicts(conn)[0]
        self.assertEqual(c["n"], 3)
        self.assertAlmostEqual(c["median_net"], 1.0, places=6)
        self.assertAlmostEqual(c["worst"], -3.0, places=6)
        self.assertAlmostEqual(c["mean_holding_days"], 6.0, places=6)
        conn.close()


if __name__ == "__main__":
    unittest.main()
