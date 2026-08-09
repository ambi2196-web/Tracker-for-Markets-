import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import run_collect
from collect import health
from collect.outcome import CollectOutcome


class TestNoHealthFlag(unittest.TestCase):
    """Regression test for a real bug found in Phase 5: a manual `--date 2026-06-26`
    debugging run silently overwrote data/health.json with an unrelated permanent 404
    from months earlier, making the live pipeline look broken. --no-health must fully
    isolate ad-hoc runs from the shared health record."""

    def setUp(self):
        self._orig_path = health.HEALTH_PATH
        self._tmpdir = tempfile.TemporaryDirectory()
        health.HEALTH_PATH = Path(self._tmpdir.name) / "health.json"

    def tearDown(self):
        health.HEALTH_PATH = self._orig_path
        self._tmpdir.cleanup()

    def _mock_collectors(self, status="failed"):
        """Patch each collector's .collect() so no real network call is made — this test
        is about health.json isolation, not about NSE connectivity."""
        patches = []
        for mod in run_collect.COLLECTORS:
            p = mock.patch.object(mod, "collect", return_value=CollectOutcome(mod.SOURCE, status, "mocked"))
            p.start()
            patches.append(p)
        return patches

    def test_no_health_flag_leaves_health_json_untouched(self):
        health.record_success("bhavcopy", "genuine-baseline")
        baseline = json.loads(health.HEALTH_PATH.read_text())

        patches = self._mock_collectors()
        try:
            with mock.patch("run_collect.setup_logging", return_value=mock.MagicMock()), \
                 mock.patch("run_collect.NSEClient"):
                run_collect.run(date(2020, 1, 6), dry_run=False, update_health=False)
        finally:
            for p in patches:
                p.stop()

        after = json.loads(health.HEALTH_PATH.read_text())
        self.assertEqual(baseline, after)

    def test_default_behavior_still_updates_health(self):
        patches = self._mock_collectors()
        try:
            with mock.patch("run_collect.setup_logging", return_value=mock.MagicMock()), \
                 mock.patch("run_collect.NSEClient"):
                run_collect.run(date(2020, 1, 6), dry_run=False, update_health=True)
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(health.HEALTH_PATH.exists())


if __name__ == "__main__":
    unittest.main()
