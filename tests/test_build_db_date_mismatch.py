import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import build_db


def _write_bhavcopy(dirpath: Path, filename: str, content_date: str) -> Path:
    path = dirpath / filename
    path.write_text(
        "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
        "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
        f"RELIANCE, EQ, {content_date}, 100, 101, 102, 99, 101, 101, 100.5, 1000, 10, 50, 500, 50.0\n"
    )
    return path


class TestMislabeledFileDetection(unittest.TestCase):
    def test_filename_date_parsing(self):
        self.assertEqual(
            build_db._filename_date_ddmmyyyy(Path("sec_bhavdata_full_26062026.csv")),
            date(2026, 6, 26),
        )

    def test_matching_date_ingests_normally(self):
        # ingest_bhavcopy_file computes source_file via path.relative_to(DATA_ROOT), so the
        # fixture must live under the real DATA_ROOT, not the system temp dir.
        with tempfile.TemporaryDirectory(dir=build_db.DATA_ROOT) as d:
            path = _write_bhavcopy(Path(d), "sec_bhavdata_full_25062026.csv", "25-Jun-2026")
            conn = sqlite3.connect(":memory:")
            build_db.create_schema(conn)
            result = build_db.ingest_bhavcopy_file(conn, path, now_iso="now")
            self.assertIsNotNone(result)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0], 1)

    def test_mismatched_date_is_skipped_not_silently_ingested(self):
        # The real bug found in Phase 4: NSE re-served 2026-06-25's content under the
        # 2026-06-26 filename. Ingesting it would corrupt source_file provenance for the
        # real date without adding any new information.
        with tempfile.TemporaryDirectory() as d:
            path = _write_bhavcopy(Path(d), "sec_bhavdata_full_26062026.csv", "25-Jun-2026")
            conn = sqlite3.connect(":memory:")
            build_db.create_schema(conn)
            result = build_db.ingest_bhavcopy_file(conn, path, now_iso="now")
            self.assertIsNone(result)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0], 0)

    def test_genuine_file_not_overwritten_by_mislabeled_duplicate(self):
        with tempfile.TemporaryDirectory(dir=build_db.DATA_ROOT) as d:
            genuine = _write_bhavcopy(Path(d), "sec_bhavdata_full_25062026.csv", "25-Jun-2026")
            mislabeled = _write_bhavcopy(Path(d), "sec_bhavdata_full_26062026.csv", "25-Jun-2026")
            conn = sqlite3.connect(":memory:")
            build_db.create_schema(conn)
            build_db.ingest_bhavcopy_file(conn, genuine, now_iso="now")
            build_db.ingest_bhavcopy_file(conn, mislabeled, now_iso="later")
            row = conn.execute("SELECT source_file FROM prices WHERE date='2026-06-25'").fetchone()
            self.assertIn("25062026", row[0])


if __name__ == "__main__":
    unittest.main()
