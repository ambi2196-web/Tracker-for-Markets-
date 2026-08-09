import tempfile
import unittest
from pathlib import Path

from parse import bhavcopy


def _write(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    f.write(content)
    f.close()
    return Path(f.name)


class TestBhavcopyParser(unittest.TestCase):
    def test_filters_to_eq_series_only(self):
        path = _write(
            "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
            "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
            "RELIANCE, EQ, 03-Aug-2026, 100, 101, 102, 99, 101, 101, 100.5, 1000, 10, 50, 500, 50.0\n"
            "1018GS2026, GS, 03-Aug-2026, 104, 104.4, 104.4, 104.4, 104.4, 104.4, 104.4, 22, 0.02, 6, 22, 100.0\n"
        )
        result = bhavcopy.parse_file(path)
        self.assertEqual(result.total_rows, 2)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].symbol, "RELIANCE")
        self.assertEqual(result.series_counts, {"EQ": 1, "GS": 1})

    def test_missing_required_column_raises(self):
        path = _write("SYMBOL, SERIES, DATE1\nRELIANCE, EQ, 03-Aug-2026\n")
        with self.assertRaises(bhavcopy.ParseError):
            bhavcopy.parse_file(path)

    def test_dash_and_blank_treated_as_none(self):
        path = _write(
            "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
            "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
            "RELIANCE, EQ, 03-Aug-2026, 100, 101, 102, 99, 101, 101, 100.5, 1000, 10, 50, -, \n"
        )
        result = bhavcopy.parse_file(path)
        self.assertIsNone(result.rows[0].delivery_pct)

    def test_date_conversion_to_iso(self):
        path = _write(
            "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, "
            "CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
            "RELIANCE, EQ, 25-Dec-2026, 100, 101, 102, 99, 101, 101, 100.5, 1000, 10, 50, 500, 50.0\n"
        )
        result = bhavcopy.parse_file(path)
        self.assertEqual(result.rows[0].date, "2026-12-25")

    def test_empty_file_raises(self):
        path = _write("")
        with self.assertRaises(bhavcopy.ParseError):
            bhavcopy.parse_file(path)


if __name__ == "__main__":
    unittest.main()
