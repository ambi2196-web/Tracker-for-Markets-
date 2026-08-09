import tempfile
import unittest
from pathlib import Path

from parse import ipo_listings


def _write(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    f.write(content)
    f.close()
    return Path(f.name)


class TestIpoListingsParser(unittest.TestCase):
    def test_parses_full_row(self):
        path = _write(
            "symbol,listing_date,anchor_lockin_30d,anchor_lockin_90d,preipo_lockin_180d\n"
            "NEWCO,2026-01-01,2026-01-31,2026-04-01,2026-06-30\n"
        )
        rows = ipo_listings.parse_file(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].symbol, "NEWCO")
        self.assertEqual(rows[0].anchor_lockin_30d, "2026-01-31")

    def test_blank_unlock_columns_become_none(self):
        path = _write(
            "symbol,listing_date,anchor_lockin_30d,anchor_lockin_90d,preipo_lockin_180d\n"
            "NEWCO,2026-01-01,,,2026-06-30\n"
        )
        rows = ipo_listings.parse_file(path)
        self.assertIsNone(rows[0].anchor_lockin_30d)
        self.assertIsNone(rows[0].anchor_lockin_90d)
        self.assertEqual(rows[0].preipo_lockin_180d, "2026-06-30")

    def test_blank_symbol_row_skipped(self):
        path = _write(
            "symbol,listing_date,anchor_lockin_30d,anchor_lockin_90d,preipo_lockin_180d\n"
            ",2026-01-01,,,\n"
            "NEWCO,2026-01-01,,,2026-06-30\n"
        )
        rows = ipo_listings.parse_file(path)
        self.assertEqual(len(rows), 1)

    def test_missing_file_returns_empty_not_error(self):
        self.assertEqual(ipo_listings.parse_file(Path("does_not_exist.csv")), [])

    def test_missing_column_raises(self):
        path = _write("symbol,listing_date\nNEWCO,2026-01-01\n")
        with self.assertRaises(ipo_listings.ParseError):
            ipo_listings.parse_file(path)

    def test_header_only_file_returns_empty(self):
        path = _write("symbol,listing_date,anchor_lockin_30d,anchor_lockin_90d,preipo_lockin_180d\n")
        self.assertEqual(ipo_listings.parse_file(path), [])


if __name__ == "__main__":
    unittest.main()
