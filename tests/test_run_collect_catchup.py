"""
Regression coverage for the self-healing catch-up sweep added after a real incident:
the scheduled task silently didn't fire for three consecutive days (machine was off)
and the gap sat unnoticed until manually backfilled. run_collect.py's default ("today",
no --date) invocation now also sweeps recent days for gaps.
"""
import unittest
from datetime import date
from unittest import mock

import run_collect


class TestCatchUpSweep(unittest.TestCase):
    def test_catch_up_skips_non_trading_days(self):
        calls = []
        with mock.patch("run_collect.run", side_effect=lambda d, **kw: calls.append(d) or 0), \
             mock.patch("run_collect.calendar.is_trading_day", side_effect=lambda d: d.weekday() < 5):
            run_collect.run_catch_up(date(2026, 8, 17), 7, dry_run=False, update_health=False)
        # Aug 17 2026 is a Monday; the 7 days before it are Aug 10-16, weekend (15,16) excluded
        self.assertEqual(calls, [date(2026, 8, d) for d in (10, 11, 12, 13, 14)])

    def test_catch_up_processes_oldest_first(self):
        calls = []
        with mock.patch("run_collect.run", side_effect=lambda d, **kw: calls.append(d) or 0), \
             mock.patch("run_collect.calendar.is_trading_day", return_value=True):
            run_collect.run_catch_up(date(2026, 8, 5), 3, dry_run=False, update_health=False)
        self.assertEqual(calls, sorted(calls))

    def test_catch_up_propagates_failure_exit_code(self):
        with mock.patch("run_collect.run", return_value=1), \
             mock.patch("run_collect.calendar.is_trading_day", return_value=True):
            result = run_collect.run_catch_up(date(2026, 8, 5), 2, dry_run=False, update_health=False)
        self.assertEqual(result, 1)

    def test_default_invocation_enables_seven_day_catchup(self):
        with mock.patch("run_collect.run", return_value=0) as mock_run, \
             mock.patch("run_collect.run_catch_up", return_value=0) as mock_catchup, \
             mock.patch("sys.argv", ["run_collect.py"]):
            run_collect.main()
        mock_catchup.assert_called_once()
        self.assertEqual(mock_catchup.call_args.args[1], 7)

    def test_explicit_date_disables_catchup_by_default(self):
        with mock.patch("run_collect.run", return_value=0), \
             mock.patch("run_collect.run_catch_up") as mock_catchup, \
             mock.patch("sys.argv", ["run_collect.py", "--date", "2026-08-03"]):
            run_collect.main()
        mock_catchup.assert_not_called()

    def test_explicit_catchup_days_overrides_default_even_with_date(self):
        with mock.patch("run_collect.run", return_value=0), \
             mock.patch("run_collect.run_catch_up", return_value=0) as mock_catchup, \
             mock.patch("sys.argv", ["run_collect.py", "--date", "2026-08-03", "--catch-up-days", "3"]):
            run_collect.main()
        mock_catchup.assert_called_once()
        self.assertEqual(mock_catchup.call_args.args[1], 3)


if __name__ == "__main__":
    unittest.main()
