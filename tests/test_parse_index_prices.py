import tempfile
import unittest
from pathlib import Path

from parse import index_prices


def _write(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    f.write(content)
    f.close()
    return Path(f.name)


class TestIndexPricesParser(unittest.TestCase):
    def test_parses_rows(self):
        path = _write("Date,Close\n2026-01-01,24000.5\n2026-01-02,24100.25\n")
        rows = index_prices.parse_file(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].date, "2026-01-01")
        self.assertEqual(rows[0].close, 24000.5)

    def test_skips_blank_rows(self):
        path = _write("Date,Close\n2026-01-01,24000.5\n,\n2026-01-02,24100.25\n")
        rows = index_prices.parse_file(path)
        self.assertEqual(len(rows), 2)

    def test_missing_columns_raises(self):
        path = _write("Date,Open\n2026-01-01,24000.5\n")
        with self.assertRaises(index_prices.ParseError):
            index_prices.parse_file(path)


if __name__ == "__main__":
    unittest.main()
