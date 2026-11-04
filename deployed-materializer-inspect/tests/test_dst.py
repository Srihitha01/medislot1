import unittest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

def candidates(naive, zone):
    found=[]
    for fold in (0,1):
        aware=naive.replace(tzinfo=zone,fold=fold)
        back=aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if back==naive and all(x.utcoffset()!=aware.utcoffset() for x in found):
            found.append(aware)
    return found

class DSTTests(unittest.TestCase):
    def test_spring_forward_nonexistent_time_is_skipped(self):
        zone=ZoneInfo("America/New_York")
        self.assertEqual(candidates(datetime(2026,3,8,2,30),zone),[])

    def test_fall_back_repeated_time_has_two_distinct_offsets(self):
        zone=ZoneInfo("America/New_York")
        values=candidates(datetime(2026,11,1,1,30),zone)
        self.assertEqual(len(values),2)
        self.assertNotEqual(values[0].utcoffset(),values[1].utcoffset())

    def test_fall_back_slots_have_distinct_utc_ids(self):
        zone=ZoneInfo("America/New_York")
        values=candidates(datetime(2026,11,1,1,30),zone)
        starts={v.astimezone(timezone.utc).isoformat() for v in values}
        self.assertEqual(len(starts),2)

if __name__=="__main__": unittest.main()
