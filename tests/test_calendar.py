import unittest
from datetime import date
from app.models.loan import PaymentFrequency
from app.services.loan_service import frequency_delta

class CalendarTests(unittest.TestCase):
    def test_monthly_keeps_original_day(self):
        start = date(2026, 10, 7)
        self.assertEqual([frequency_delta(start, PaymentFrequency.monthly, i) for i in range(6)], [date(2026,10,7),date(2026,11,7),date(2026,12,7),date(2027,1,7),date(2027,2,7),date(2027,3,7)])

    def test_short_month_does_not_shift_later_months(self):
        for year, feb_day in [(2026,28),(2028,29)]:
            start = date(year,1,31)
            self.assertEqual(frequency_delta(start,PaymentFrequency.monthly,1),date(year,2,feb_day))
            self.assertEqual(frequency_delta(start,PaymentFrequency.monthly,2),date(year,3,31))

    def test_other_frequencies_unchanged(self):
        start = date(2026,12,31)
        for frequency, expected in [(PaymentFrequency.daily,date(2027,1,1)),(PaymentFrequency.weekly,date(2027,1,7)),(PaymentFrequency.biweekly,date(2027,1,14))]:
            self.assertEqual(frequency_delta(start,frequency,1),expected)