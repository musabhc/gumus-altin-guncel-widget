"""Deterministic boundary checks for the widget's market-hours rule."""

from datetime import datetime
from unittest import TestCase, main as unittest_main
from unittest.mock import patch

import main


class TestMarketHours(TestCase):
    CASES = (
        ("Friday evening", datetime(2026, 10, 2, 23, 30), False),
        ("Saturday before close", datetime(2026, 10, 3, 0, 59), False),
        ("Saturday at close", datetime(2026, 10, 3, 1, 0), True),
        ("Sunday", datetime(2026, 10, 4, 12, 0), True),
        ("Monday before open", datetime(2026, 10, 5, 1, 59), True),
        ("Monday at open", datetime(2026, 10, 5, 2, 0), False),
        ("Tuesday", datetime(2026, 10, 6, 12, 0), False),
    )

    def test_market_boundaries(self):
        widget = main.PiyasaWidget.__new__(main.PiyasaWidget)
        for label, current_time, expected_closed in self.CASES:
            with self.subTest(label=label), patch.object(main, "datetime") as mocked_datetime:
                mocked_datetime.now.return_value = current_time
                self.assertEqual(widget.is_market_closed(), expected_closed)


if __name__ == "__main__":
    unittest_main(verbosity=2)
