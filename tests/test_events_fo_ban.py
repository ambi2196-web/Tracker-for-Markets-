import unittest
from datetime import date

from events.fo_ban import detect
from parse.fo_ban import BanSnapshot


class TestFoBanEventDetection(unittest.TestCase):
    def test_entry_and_exit_detected(self):
        snaps = [
            (BanSnapshot(date(2026, 1, 1), frozenset()), "d1.csv"),
            (BanSnapshot(date(2026, 1, 2), frozenset({"AAA"})), "d2.csv"),
            (BanSnapshot(date(2026, 1, 3), frozenset()), "d3.csv"),
        ]
        events = detect(snaps)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, "fo_ban_entry")
        self.assertEqual(events[0].symbol, "AAA")
        self.assertEqual(events[0].effective_date, "2026-01-02")
        self.assertEqual(events[1].event_type, "fo_ban_exit")
        self.assertEqual(events[1].effective_date, "2026-01-03")

    def test_continuous_ban_across_gap_produces_no_phantom_event(self):
        snaps = [
            (BanSnapshot(date(2026, 1, 1), frozenset({"AAA"})), "d1.csv"),
            # day 2 entirely absent — holiday or collection gap
            (BanSnapshot(date(2026, 1, 3), frozenset({"AAA"})), "d3.csv"),
        ]
        self.assertEqual(detect(snaps), [])

    def test_real_exit_spanning_a_gap_is_recorded_once_with_gap_size(self):
        snaps = [
            (BanSnapshot(date(2026, 1, 1), frozenset({"AAA"})), "d1.csv"),
            (BanSnapshot(date(2026, 1, 4), frozenset()), "d4.csv"),
        ]
        events = detect(snaps)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "fo_ban_exit")
        self.assertEqual(events[0].detail["gap_calendar_days"], 3)

    def test_first_snapshot_is_left_censored_no_fabricated_entry(self):
        snaps = [
            (BanSnapshot(date(2026, 1, 1), frozenset({"AAA"})), "d1.csv"),
            (BanSnapshot(date(2026, 1, 2), frozenset({"AAA"})), "d2.csv"),
        ]
        self.assertEqual(detect(snaps), [])

    def test_multiple_simultaneous_entries_and_exits(self):
        snaps = [
            (BanSnapshot(date(2026, 1, 1), frozenset({"AAA", "BBB"})), "d1.csv"),
            (BanSnapshot(date(2026, 1, 2), frozenset({"BBB", "CCC"})), "d2.csv"),  # AAA exits, CCC enters
        ]
        events = detect(snaps)
        types_symbols = {(e.event_type, e.symbol) for e in events}
        self.assertEqual(types_symbols, {("fo_ban_exit", "AAA"), ("fo_ban_entry", "CCC")})

    def test_event_id_is_deterministic_for_idempotent_upsert(self):
        snaps = [
            (BanSnapshot(date(2026, 1, 1), frozenset()), "d1.csv"),
            (BanSnapshot(date(2026, 1, 2), frozenset({"AAA"})), "d2.csv"),
        ]
        events1 = detect(snaps)
        events2 = detect(snaps)
        self.assertEqual(events1[0].event_id, events2[0].event_id)


if __name__ == "__main__":
    unittest.main()
