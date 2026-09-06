"""
Phase 8 work item 4 — the Track B discipline rules.

These are not schema formalities. Rule 1 (no position without a written falsifier) and
rule 2 (the falsifier is immutable once the position opens) are the entire anti-value-trap
mechanism: after entry the operator will find reasons, and the only defence is that the
original text cannot move. If these tests ever go soft, the mechanism is gone.
"""
import sqlite3
import unittest

import ledger as ledger_api
from build_db import create_schema


class TestThesisRequiredForDiscretionaryTrade(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        create_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_cannot_open_without_a_thesis(self):
        with self.assertRaises(ledger_api.LedgerError):
            ledger_api.open_discretionary_trade(
                self.conn, thesis_id="thesis:does-not-exist", direction="long",
                entry_date="2026-01-06", entry_price=100,
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 0)

    def test_cannot_create_thesis_with_empty_falsifier(self):
        for bad in ("", "   ", "\n"):
            with self.assertRaises(ledger_api.LedgerError):
                ledger_api.create_thesis(
                    self.conn, subject="X", claim="c", counter_claim="cc", falsifier=bad
                )

    def test_schema_check_blocks_empty_falsifier_even_on_a_direct_write(self):
        """Code enforcement is the first line; the CHECK constraint is what holds when
        someone writes to sqlite directly."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO thesis (id,subject,created_date,claim,counter_claim,falsifier,"
                "horizon_months,review_date,status,outcome,falsifier_hit,source,source_file,"
                "ingested_at,pipeline_version) VALUES ('t','s','2026-01-01','c','cc','   ',"
                "NULL,NULL,'open',NULL,NULL,'manual','x','n','0')"
            )

    def test_schema_check_blocks_discretionary_row_without_thesis(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO ledger (ledger_id,signal_id,thesis_id,track,mode,direction,"
                "updated_at,pipeline_version) VALUES ('l','s1',NULL,'discretionary','paper','long','n','0')"
            )

    def test_schema_check_blocks_mechanism_row_carrying_a_thesis(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO ledger (ledger_id,signal_id,thesis_id,track,mode,direction,"
                "updated_at,pipeline_version) VALUES ('l','s1','t1','mechanism','paper','long','n','0')"
            )

    def test_happy_path_opens_and_closes_with_net_after_costs(self):
        tid = ledger_api.create_thesis(
            self.conn, subject="Widgets", claim="market prices permanent impairment",
            counter_claim="input shock is temporary", falsifier="spot input cost still elevated at Q4 results",
        )
        lid = ledger_api.open_discretionary_trade(
            self.conn, thesis_id=tid, direction="long", entry_date="2026-01-06", entry_price=100
        )
        ledger_api.close_discretionary_trade(self.conn, lid, exit_date="2026-04-06", exit_price=120)
        row = self.conn.execute(
            "SELECT track, gross_return_pct, costs_pct, net_return_pct FROM ledger WHERE ledger_id=?", (lid,)
        ).fetchone()
        self.assertEqual(row[0], "discretionary")
        self.assertAlmostEqual(row[1], 20.0, places=6)
        self.assertAlmostEqual(row[3], 20.0 - ledger_api.TOTAL_COSTS_PCT, places=6)


class TestFalsifierImmutability(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        create_schema(self.conn)
        self.tid = ledger_api.create_thesis(
            self.conn, subject="Widgets", claim="permanent impairment",
            counter_claim="temporary input shock",
            falsifier="ORIGINAL: input cost still elevated at Q4 results",
        )

    def tearDown(self):
        self.conn.close()

    def test_revision_before_entry_preserves_original_text(self):
        ledger_api.revise_falsifier(
            self.conn, self.tid, "REVISED: input cost still elevated at Q3 results", reason="tightened"
        )
        current = self.conn.execute("SELECT falsifier FROM thesis WHERE id=?", (self.tid,)).fetchone()[0]
        self.assertTrue(current.startswith("REVISED:"))

        revisions = self.conn.execute(
            "SELECT field, previous_value, new_value FROM thesis_revisions WHERE thesis_id=?", (self.tid,)
        ).fetchall()
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0][0], "falsifier")
        # The whole point: the original wording survives verbatim.
        self.assertEqual(revisions[0][1], "ORIGINAL: input cost still elevated at Q4 results")

    def test_falsifier_is_immutable_once_position_opens(self):
        ledger_api.open_discretionary_trade(
            self.conn, thesis_id=self.tid, direction="long", entry_date="2026-01-06", entry_price=100
        )
        with self.assertRaises(ledger_api.LedgerError):
            ledger_api.revise_falsifier(self.conn, self.tid, "CONVENIENTLY WEAKER falsifier")

        unchanged = self.conn.execute("SELECT falsifier FROM thesis WHERE id=?", (self.tid,)).fetchone()[0]
        self.assertTrue(unchanged.startswith("ORIGINAL:"))

    def test_cannot_revise_to_empty(self):
        with self.assertRaises(ledger_api.LedgerError):
            ledger_api.revise_falsifier(self.conn, self.tid, "   ")

    def test_theses_past_review_surfaces_original_falsifier(self):
        ledger_api.create_thesis(
            self.conn, subject="Gadgets", claim="c", counter_claim="cc",
            falsifier="margin stays below 8% for two quarters", review_date="2026-01-01",
        )
        due = ledger_api.theses_past_review(self.conn, as_of="2026-06-01")
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["subject"], "Gadgets")
        self.assertIn("margin stays below 8%", due[0]["falsifier"])


if __name__ == "__main__":
    unittest.main()
