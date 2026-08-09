import unittest
from datetime import date

from signals.calendar import add_trading_days, is_known_trading_day, sessions_up_to

DAYS = [date(2026, 1, d) for d in [5, 6, 7, 8, 9, 12, 13, 14]]  # 5-9 then 12-14 (weekend skip)


class TestTradingCalendar(unittest.TestCase):
    def test_add_trading_days_basic(self):
        self.assertEqual(add_trading_days(DAYS, date(2026, 1, 5), 1), date(2026, 1, 6))
        self.assertEqual(add_trading_days(DAYS, date(2026, 1, 5), 5), date(2026, 1, 12))

    def test_add_trading_days_from_a_date_not_in_the_list(self):
        # from_date itself need not be a trading day (e.g. a weekend or holiday)
        self.assertEqual(add_trading_days(DAYS, date(2026, 1, 10), 1), date(2026, 1, 12))

    def test_add_trading_days_returns_none_when_archive_too_short(self):
        self.assertIsNone(add_trading_days(DAYS, date(2026, 1, 13), 5))

    def test_sessions_up_to_respects_lookback_and_boundary(self):
        self.assertEqual(sessions_up_to(DAYS, date(2026, 1, 9), 3), [date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9)])
        # shorter than lookback near the start of the archive
        self.assertEqual(sessions_up_to(DAYS, date(2026, 1, 6), 20), [date(2026, 1, 5), date(2026, 1, 6)])

    def test_sessions_up_to_never_includes_future_dates(self):
        sessions = sessions_up_to(DAYS, date(2026, 1, 7), 10)
        self.assertTrue(all(d <= date(2026, 1, 7) for d in sessions))

    def test_is_known_trading_day(self):
        self.assertTrue(is_known_trading_day(DAYS, date(2026, 1, 8)))
        self.assertFalse(is_known_trading_day(DAYS, date(2026, 1, 10)))  # weekend gap


if __name__ == "__main__":
    unittest.main()
