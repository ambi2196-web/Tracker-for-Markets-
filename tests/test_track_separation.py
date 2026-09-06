"""
Phase 8 acceptance guards for the Track A / Track B split.

The headline test here is `test_every_render_path_ledger_query_filters_by_track`: it
statically inspects every SQL literal in the render path and fails if one touches
`ledger` without constraining `track`. The rule it protects — no aggregate statistic may
span both tracks — is the entire reason Phase 8 exists, and it is the kind of rule that
decays silently when someone adds a query six months from now.
"""
import ast
import sqlite3
import unittest
from pathlib import Path

import dispersion
import ledger as ledger_api
import regime
import verdict
from build_db import create_schema

SRC = Path(__file__).resolve().parent.parent / "src"

# Every module that reads the ledger for presentation or scoring.
RENDER_PATH_MODULES = [
    SRC / "render_dashboard.py",
    SRC / "render_summary.py",
    SRC / "render_digest.py",
    SRC / "regime.py",
    SRC / "verdict.py",
]


def _sql_literals_touching_ledger(path: Path) -> list[str]:
    """String constants in the module that are SQL reading from `ledger`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            lowered = text.lower()
            if "from ledger" in lowered or "join ledger" in lowered:
                found.append(text)
    return found


class TestTrackFilterGuard(unittest.TestCase):
    def test_every_render_path_ledger_query_filters_by_track(self):
        offenders = []
        checked = 0
        for module in RENDER_PATH_MODULES:
            for sql in _sql_literals_touching_ledger(module):
                checked += 1
                if "track" not in sql.lower():
                    offenders.append(f"{module.name}: {' '.join(sql.split())[:110]}")
        self.assertTrue(checked, "guard found no ledger queries at all — it has stopped guarding anything")
        self.assertEqual(
            offenders, [],
            "these render-path queries read `ledger` without filtering by track — "
            "no aggregate may span mechanism and discretionary:\n  " + "\n  ".join(offenders),
        )


class TestTrackSeparationBehaviour(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        create_schema(self.conn)
        # One mechanism cohort trade...
        self.conn.execute(
            "INSERT INTO events (event_id,symbol,event_type,announce_date,effective_date,detail,"
            "source_file,ingested_at,pipeline_version) VALUES ('e1','AAA','fo_ban_exit',NULL,'2026-01-05','{}','t','n','0')"
        )
        self.conn.execute(
            "INSERT INTO signals (signal_id,event_id,symbol,signal_type,state,armed_date,window_start,"
            "window_end,entry_date,entry_ref_price,stop_price,invalidation_reason,filters_passed,"
            "updated_at,pipeline_version) VALUES ('s1','e1','AAA','fo_ban_exit','closed','2026-01-05',"
            "'2026-01-05','2026-01-12','2026-01-06',100,92,NULL,'{}','n','0')"
        )
        self.conn.execute(
            "INSERT INTO ledger (ledger_id,signal_id,thesis_id,track,mode,direction,entry_date,entry_price,"
            "exit_date,exit_price,exit_reason,gross_return_pct,costs_pct,net_return_pct,holding_days,"
            "updated_at,pipeline_version) VALUES ('l1','s1',NULL,'mechanism','paper','long','2026-01-06',100,"
            "'2026-01-12',110,'window_end',10.0,0.5,9.5,6,'n','0')"
        )
        # ...and one wildly different discretionary trade that must never be averaged in.
        tid = ledger_api.create_thesis(
            self.conn, subject="Widgets", claim="permanent impairment",
            counter_claim="temporary input shock", falsifier="input cost stays elevated past Q4",
            review_date="2026-06-01",
        )
        lid = ledger_api.open_discretionary_trade(
            self.conn, thesis_id=tid, direction="long", entry_date="2026-01-06", entry_price=100
        )
        ledger_api.close_discretionary_trade(
            self.conn, lid, exit_date="2026-07-06", exit_price=500, exit_reason="manual"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_verdict_cohorts_exclude_discretionary(self):
        cohorts = verdict.cohort_verdicts(self.conn)
        self.assertEqual(len(cohorts), 1)
        self.assertEqual(cohorts[0]["n"], 1)
        # The +400% discretionary trade would swamp this if tracks were pooled.
        self.assertAlmostEqual(cohorts[0]["mean_net"], 9.5, places=6)

    def test_regime_span_is_track_scoped(self):
        mech = regime.compute_regime(self.conn, track="mechanism")
        disc = regime.compute_regime(self.conn, track="discretionary")
        self.assertEqual(mech["end_date"], "2026-01-12")
        self.assertEqual(disc["end_date"], "2026-07-06")


class TestResearchPromptsNeverReachLedger(unittest.TestCase):
    """A research prompt says 'go read about this'. It is not a position and must never
    become one implicitly."""

    def test_writing_prompts_does_not_touch_ledger(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        before = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        d = dispersion.SectorDispersion(
            sector="Chemicals", n_constituents=12, median_return=-30.0,
            index_return=-2.0, spread=-28.0, breadth=0.6, fires=True,
        )
        written = dispersion.write_research_prompts(conn, [d], detected_date="2026-01-05")
        conn.commit()
        self.assertEqual(written, 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], before)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM research_prompts").fetchone()[0], 1)
        conn.close()

    def test_dispersion_module_has_no_ledger_write_path(self):
        source = (SRC / "dispersion.py").read_text(encoding="utf-8").lower()
        for forbidden in ("insert into ledger", "update ledger"):
            self.assertNotIn(forbidden, source)

    def test_prompts_are_idempotent_for_same_sector_and_date(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        d = dispersion.SectorDispersion("Chemicals", 12, -30.0, -2.0, -28.0, 0.6, True)
        dispersion.write_research_prompts(conn, [d], detected_date="2026-01-05")
        second = dispersion.write_research_prompts(conn, [d], detected_date="2026-01-05")
        self.assertEqual(second, 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM research_prompts").fetchone()[0], 1)
        conn.close()

    def test_non_firing_sector_writes_nothing(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        d = dispersion.SectorDispersion("Chemicals", 12, -3.0, -2.0, -1.0, 0.1, False)
        self.assertEqual(dispersion.write_research_prompts(conn, [d], detected_date="2026-01-05"), 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
