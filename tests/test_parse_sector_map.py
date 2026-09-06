import sqlite3
import tempfile
import unittest
from pathlib import Path

import dispersion
from build_db import create_schema
from parse import sector_map


def _write(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    f.write(content)
    f.close()
    return Path(f.name)


HEADER = "Company Name,Industry,Symbol,Series,ISIN Code\n"


class TestSectorMapParser(unittest.TestCase):
    def test_parses_symbol_and_industry(self):
        path = _write(HEADER + "Axis Bank Ltd.,Financial Services,AXISBANK,EQ,INE238A01034\n")
        rows = sector_map.parse_file(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].symbol, "AXISBANK")
        self.assertEqual(rows[0].sector, "Financial Services")
        self.assertEqual(rows[0].company_name, "Axis Bank Ltd.")

    def test_industry_is_used_not_index_membership(self):
        # ASHOKLEY sits in Nifty Auto but NSE classifies it Capital Goods; the published
        # Industry field is the grouping we key on.
        path = _write(HEADER + "Ashok Leyland Ltd.,Capital Goods,ASHOKLEY,EQ,INE208A01029\n")
        self.assertEqual(sector_map.parse_file(path)[0].sector, "Capital Goods")

    def test_rows_missing_symbol_or_industry_are_skipped(self):
        path = _write(HEADER + ",Financial Services,,EQ,X\nAxis,Financial Services,AXISBANK,EQ,Y\n")
        self.assertEqual(len(sector_map.parse_file(path)), 1)

    def test_missing_required_column_raises(self):
        path = _write("Company Name,Symbol,Series\nAxis,AXISBANK,EQ\n")
        with self.assertRaises(sector_map.ParseError):
            sector_map.parse_file(path)

    def test_header_only_file_returns_empty(self):
        self.assertEqual(sector_map.parse_file(_write(HEADER)), [])


class TestDispersionThresholds(unittest.TestCase):
    def _conn_with(self, sector_rows, price_rows, index_rows):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        conn.executemany(
            "INSERT INTO sector_map (symbol,sector,company_name,source,source_file,ingested_at,pipeline_version)"
            " VALUES (?,?,NULL,'test','t','n','0')", sector_rows)
        conn.executemany(
            "INSERT INTO prices (symbol,date,open,high,low,close,volume,delivery_pct,source,source_file,"
            "ingested_at,pipeline_version) VALUES (?,?,?,?,?,?,1000,50,'test','t','n','0')", price_rows)
        conn.executemany(
            "INSERT INTO index_prices (index_name,date,close,source,source_file,ingested_at,pipeline_version)"
            " VALUES ('NIFTY50',?,?,'test','t','n','0')", index_rows)
        conn.commit()
        return conn

    def test_fires_when_sector_median_far_below_index_with_enough_constituents(self):
        symbols = [f"S{i}" for i in range(6)]
        sectors = [(s, "Chemicals") for s in symbols]
        prices = []
        for s in symbols:
            prices.append((s, "2026-01-01", 100, 100, 100, 100))
            prices.append((s, "2026-04-01", 70, 70, 70, 70))   # -30%
        conn = self._conn_with(sectors, prices, [("2026-01-01", 100.0), ("2026-04-01", 100.0)])
        result = {d.sector: d for d in dispersion.compute(conn, lookback=90)}
        self.assertTrue(result["Chemicals"].fires)
        self.assertAlmostEqual(result["Chemicals"].spread, -30.0, places=4)
        conn.close()

    def test_does_not_fire_below_min_constituents(self):
        symbols = [f"S{i}" for i in range(dispersion.MIN_CONSTITUENTS - 1)]
        sectors = [(s, "Chemicals") for s in symbols]
        prices = []
        for s in symbols:
            prices.append((s, "2026-01-01", 100, 100, 100, 100))
            prices.append((s, "2026-04-01", 50, 50, 50, 50))
        conn = self._conn_with(sectors, prices, [("2026-01-01", 100.0), ("2026-04-01", 100.0)])
        result = {d.sector: d for d in dispersion.compute(conn, lookback=90)}
        self.assertFalse(result["Chemicals"].fires)
        conn.close()

    def test_does_not_fire_when_spread_is_shallower_than_trigger(self):
        symbols = [f"S{i}" for i in range(6)]
        sectors = [(s, "Chemicals") for s in symbols]
        prices = []
        for s in symbols:
            prices.append((s, "2026-01-01", 100, 100, 100, 100))
            prices.append((s, "2026-04-01", 95, 95, 95, 95))   # -5%, well short of -20pp
        conn = self._conn_with(sectors, prices, [("2026-01-01", 100.0), ("2026-04-01", 100.0)])
        result = {d.sector: d for d in dispersion.compute(conn, lookback=90)}
        self.assertFalse(result["Chemicals"].fires)
        conn.close()

    def test_median_not_mean_so_one_collapse_does_not_manufacture_a_signal(self):
        symbols = [f"S{i}" for i in range(6)]
        sectors = [(s, "Chemicals") for s in symbols]
        prices = []
        for i, s in enumerate(symbols):
            prices.append((s, "2026-01-01", 100, 100, 100, 100))
            end = 1 if i == 0 else 100   # one constituent down 99%, rest flat
            prices.append((s, "2026-04-01", end, end, end, end))
        conn = self._conn_with(sectors, prices, [("2026-01-01", 100.0), ("2026-04-01", 100.0)])
        result = {d.sector: d for d in dispersion.compute(conn, lookback=90)}
        self.assertFalse(result["Chemicals"].fires)
        self.assertAlmostEqual(result["Chemicals"].median_return, 0.0, places=4)
        conn.close()


if __name__ == "__main__":
    unittest.main()
