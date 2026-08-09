import tempfile
import unittest
from pathlib import Path

from collect import health


class TestHealthTracking(unittest.TestCase):
    def setUp(self):
        self._orig_path = health.HEALTH_PATH
        self._tmpdir = tempfile.TemporaryDirectory()
        health.HEALTH_PATH = Path(self._tmpdir.name) / "health.json"

    def tearDown(self):
        health.HEALTH_PATH = self._orig_path
        self._tmpdir.cleanup()

    def _load(self):
        import json
        return json.loads(health.HEALTH_PATH.read_text())

    def test_success_clears_failure_streak(self):
        health.record_failure("bhavcopy", "t1", "boom")
        health.record_failure("bhavcopy", "t2", "boom again")
        self.assertEqual(self._load()["bhavcopy"]["consecutive_failures"], 2)
        health.record_success("bhavcopy", "t3")
        row = self._load()["bhavcopy"]
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertIsNone(row["last_error"])
        self.assertEqual(row["last_success"], "t3")

    def test_attempt_alone_does_not_touch_last_success(self):
        health.record_success("asm", "t1")
        health.record_attempt("asm", "t2")
        row = self._load()["asm"]
        self.assertEqual(row["last_success"], "t1")  # unchanged
        self.assertEqual(row["last_attempt"], "t2")

    def test_no_op_touches_attempt_only(self):
        health.record_success("fo_ban", "t1")
        health.record_no_op("fo_ban", "t2", reason="weekend_or_holiday")
        row = self._load()["fo_ban"]
        self.assertEqual(row["last_success"], "t1")
        self.assertEqual(row["last_attempt"], "t2")
        self.assertEqual(row["last_no_op_reason"], "weekend_or_holiday")

    def test_write_is_atomic_no_tmp_file_left_behind(self):
        health.record_success("bhavcopy", "t1")
        self.assertTrue(health.HEALTH_PATH.exists())
        self.assertFalse(health.HEALTH_PATH.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
