import unittest

from events.ipo_lockin import generate_events
from parse.ipo_listings import IpoListing


class TestIpoLockinEventGeneration(unittest.TestCase):
    def test_generates_one_event_per_populated_tranche(self):
        listing = IpoListing(
            symbol="NEWCO", listing_date="2026-01-01",
            anchor_lockin_30d="2026-01-31", anchor_lockin_90d="2026-04-01",
            preipo_lockin_180d="2026-06-30",
        )
        events = generate_events([listing], "seed/ipo_listings.csv")
        self.assertEqual(len(events), 3)
        self.assertEqual({e.effective_date for e in events}, {"2026-01-31", "2026-04-01", "2026-06-30"})
        tranches = {e.detail["tranche"] for e in events}
        self.assertEqual(tranches, {"anchor_30d", "anchor_90d", "preipo_180d"})

    def test_missing_tranche_produces_no_event(self):
        listing = IpoListing(
            symbol="NEWCO", listing_date="2026-01-01",
            anchor_lockin_30d=None, anchor_lockin_90d=None,
            preipo_lockin_180d="2026-06-30",
        )
        events = generate_events([listing], "seed/ipo_listings.csv")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].detail["tranche"], "preipo_180d")

    def test_unlock_percentages(self):
        listing = IpoListing(
            symbol="NEWCO", listing_date="2026-01-01",
            anchor_lockin_30d="2026-01-31", anchor_lockin_90d=None, preipo_lockin_180d=None,
        )
        events = generate_events([listing], "seed/ipo_listings.csv")
        self.assertEqual(events[0].detail["unlock_pct"], 50)

    def test_event_id_is_deterministic(self):
        listing = IpoListing(
            symbol="NEWCO", listing_date="2026-01-01",
            anchor_lockin_30d="2026-01-31", anchor_lockin_90d=None, preipo_lockin_180d=None,
        )
        events1 = generate_events([listing], "seed/ipo_listings.csv")
        events2 = generate_events([listing], "seed/ipo_listings.csv")
        self.assertEqual(events1[0].event_id, events2[0].event_id)


if __name__ == "__main__":
    unittest.main()
