"""
Phase 8 schema guards: the ledger migration must not lose rows, and every new table must
carry the standard provenance columns.
"""
import sqlite3
import unittest

import build_db
from build_db import create_schema, migrate_schema

PROVENANCE = {"source", "source_file", "ingested_at", "pipeline_version"}
NEW_PHASE8_TABLES = ["sector_map", "research_prompts", "thesis", "thesis_revisions"]

# The pre-Phase-8 ledger, verbatim, for migration testing.
LEGACY_LEDGER_DDL = """
CREATE TABLE ledger (
    ledger_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
    mode TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_date TEXT, entry_price REAL,
    exit_date TEXT, exit_price REAL,
    exit_reason TEXT,
    gross_return_pct REAL, costs_pct REAL, net_return_pct REAL,
    holding_days INTEGER, notes TEXT,
    updated_at TEXT NOT NULL, pipeline_version TEXT NOT NULL
);
"""


class TestProvenanceColumns(unittest.TestCase):
    def test_every_new_table_carries_provenance_columns(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        for table in NEW_PHASE8_TABLES:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            missing = PROVENANCE - cols
            self.assertEqual(missing, set(), f"{table} is missing provenance columns {sorted(missing)}")
        conn.close()


class TestLedgerMigration(unittest.TestCase):
    def _legacy_db(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(build_db.SCHEMA_SQL)  # everything except the new ledger
        conn.executescript(LEGACY_LEDGER_DDL)
        conn.execute(
            "INSERT INTO ledger (ledger_id,signal_id,mode,direction,entry_date,entry_price,"
            "exit_date,exit_price,exit_reason,gross_return_pct,costs_pct,net_return_pct,"
            "holding_days,notes,updated_at,pipeline_version) VALUES "
            "('l1','s1','paper','long','2026-01-06',100,'2026-01-12',110,'window_end',"
            "10.0,0.5,9.5,6,'note','n','0')"
        )
        conn.commit()
        return conn

    def test_migration_preserves_rows_and_tags_them_mechanism(self):
        conn = self._legacy_db()
        self.assertTrue(migrate_schema(conn))
        row = conn.execute(
            "SELECT ledger_id, signal_id, track, thesis_id, net_return_pct, notes FROM ledger"
        ).fetchone()
        self.assertEqual(row, ("l1", "s1", "mechanism", None, 9.5, "note"))
        conn.close()

    def test_migration_makes_signal_id_nullable(self):
        conn = self._legacy_db()
        migrate_schema(conn)
        notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(ledger)")}
        self.assertEqual(notnull["signal_id"], 0)
        conn.close()

    def test_migration_is_idempotent(self):
        conn = self._legacy_db()
        self.assertTrue(migrate_schema(conn))
        self.assertFalse(migrate_schema(conn))   # second run is a no-op
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 1)
        conn.close()

    def test_migration_drops_the_temporary_table(self):
        conn = self._legacy_db()
        migrate_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("ledger_pre_phase8", tables)
        conn.close()

    def test_fresh_database_needs_no_migration(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        self.assertFalse(migrate_schema(conn))
        conn.close()


if __name__ == "__main__":
    unittest.main()
