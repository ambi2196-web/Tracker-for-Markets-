import tempfile
import unittest
from pathlib import Path

from parse import fo_ban


def _write(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    f.write(content)
    f.close()
    return Path(f.name)


class TestFoBanParser(unittest.TestCase):
    def test_nil_day(self):
        path = _write("Securities in Ban For Trade Date 03-AUG-2026: NIL")
        snap = fo_ban.parse_file(path)
        self.assertEqual(snap.symbols, frozenset())

    def test_single_symbol(self):
        path = _write("Securities in Ban For Trade Date 23-JUL-2026:\n1,KAYNES")
        snap = fo_ban.parse_file(path)
        self.assertEqual(snap.symbols, frozenset({"KAYNES"}))

    def test_multi_symbol_newline_separated(self):
        path = _write("Securities in Ban For Trade Date 07-AUG-2026:\n1,BANDHANBNK\n2,LICI")
        snap = fo_ban.parse_file(path)
        self.assertEqual(snap.symbols, frozenset({"BANDHANBNK", "LICI"}))

    def test_malformed_entry_raises_rather_than_guessing(self):
        path = _write("Securities in Ban For Trade Date 07-AUG-2026:\nBANDHANBNK,LICI")
        with self.assertRaises(fo_ban.ParseError):
            fo_ban.parse_file(path)

    def test_bad_header_raises(self):
        path = _write("Totally different format\nNIL")
        with self.assertRaises(fo_ban.ParseError):
            fo_ban.parse_file(path)

    def test_date_parsed_correctly(self):
        path = _write("Securities in Ban For Trade Date 07-AUG-2026: NIL")
        snap = fo_ban.parse_file(path)
        self.assertEqual(snap.date.isoformat(), "2026-08-07")


if __name__ == "__main__":
    unittest.main()
