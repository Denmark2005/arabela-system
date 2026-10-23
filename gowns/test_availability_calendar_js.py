"""static/js/availability-calendar.js (the New Booking event-date calendar): its small pure helpers, run under Node with a bare
stand-in for the page. The drawing itself is checked in a real browser; the server's answers are tested in
arabela_admin/test_new_booking.py (EventDateCalendarTests)."""
import json
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

CAL_JS = Path(settings.BASE_DIR) / "static" / "js" / "availability-calendar.js"

NODE_SCRIPT = r"""
const vm = require('vm'), fs = require('fs');
const ctx = { window: {}, document: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const cal = ctx.window.arabelaAvailabilityCalendar;
const out = {};
out.api = Object.keys(cal).sort();
const oct = cal.monthCells('2026-10');
out.october = { blanks: oct.blanks, count: oct.days.length, first: oct.days[0], last: oct.days[oct.days.length - 1] };
out.february = [cal.monthCells('2026-02').days.length, cal.monthCells('2028-02').days.length];       // 28 and a leap year's 29
out.firstWeekdays = ['2026-11', '2027-01', '2026-03'].map((m) => cal.monthCells(m).blanks);           // Sunday=0: Nov 1 2026 is a Sunday
out.paging = [cal.shiftMonth('2026-12', 1), cal.shiftMonth('2026-01', -1), cal.shiftMonth('2026-10', 0), cal.shiftMonth('2026-03', 12)];
out.labels = [cal.monthLabel('2026-10'), cal.longDay('2026-10-23'), cal.shortDay('2026-10-23')];
out.free = cal.describe('2026-10-23', { ok: true, pickup: '2026-10-21', returnDate: '2026-10-25', restUntil: '2026-10-30' });
out.taken = cal.describe('2026-10-23', { ok: false, reason: 'booked', why: 'Lorna Reyes (RSV-2026-0007) has this gown Oct 20 – Oct 26' });
out.takenNoWhy = cal.describe('2026-10-23', { ok: false });
out.unknown = cal.describe('2026-10-23', null);
console.log(JSON.stringify(out));
"""


class AvailabilityCalendarHelperTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.result = None
        cls.node_error = ""
        node = shutil.which("node")
        if node:
            run = subprocess.run([node, "-e", NODE_SCRIPT, str(CAL_JS)], capture_output=True, text=True, timeout=60, encoding="utf-8")
            if run.returncode == 0:
                cls.result = json.loads(run.stdout.strip().splitlines()[-1])
            else:
                cls.node_error = run.stderr

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("Node is not installed, so the calendar helpers cannot be run here")
        self.assertIsNotNone(self.result, self.node_error)
        self.r = self.result

    def test_what_the_page_can_call(self):
        self.assertEqual(self.r["api"], ["close", "describe", "isOpen", "longDay", "monthCells", "monthLabel", "open", "shiftMonth", "shortDay"])

    def test_a_month_starts_on_the_right_weekday_and_has_the_right_length(self):
        self.assertEqual(self.r["october"], {"blanks": 4, "count": 31, "first": "2026-10-01", "last": "2026-10-31"})   # Oct 1 2026 is a Thursday
        self.assertEqual(self.r["february"], [28, 29])
        self.assertEqual(self.r["firstWeekdays"], [0, 5, 0])

    def test_paging_crosses_the_year(self):
        self.assertEqual(self.r["paging"], ["2027-01", "2025-12", "2026-10", "2027-03"])

    def test_dates_are_written_without_the_browsers_time_zone_moving_a_day(self):
        self.assertEqual(self.r["labels"], ["October 2026", "Fri, Oct 23, 2026", "Oct 23"])

    def test_what_the_line_under_the_calendar_says(self):
        self.assertEqual(self.r["free"]["kind"], "ok")
        self.assertEqual(self.r["free"]["text"], "Fri, Oct 23, 2026: available. Pick-up Oct 21, event Oct 23, return Oct 25. "
                                                  "After that the gown rests until Oct 30.")
        self.assertEqual(self.r["taken"]["kind"], "no")
        self.assertIn("RSV-2026-0007", self.r["taken"]["text"])
        self.assertEqual(self.r["takenNoWhy"]["text"], "Fri, Oct 23, 2026 — not available")
        self.assertEqual(self.r["unknown"]["kind"], "unknown")
