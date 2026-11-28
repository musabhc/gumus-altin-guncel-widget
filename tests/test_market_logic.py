"""Deterministic checks for provider sessions and application updates."""

from datetime import datetime, timedelta
from unittest import TestCase, main as unittest_main
from unittest.mock import patch

from piyasa import windows
from piyasa.market.quotes import quote_time, session_from_metadata
from piyasa.market.series import sample_chart_rows
from piyasa.ui.rendering import render_chart_line


class TestMarketHours(TestCase):
    def test_each_provider_exposes_its_own_session(self):
        now = quote_time(1_760_000_000)
        regular = {"currentTradingPeriod": {"regular": {"start": 1_759_999_999, "end": 1_760_000_001}}}
        self.assertEqual(session_from_metadata(regular, now), "REGULAR")
        self.assertEqual(session_from_metadata({"marketState": "CLOSED"}, now), "CLOSED")
        self.assertEqual(session_from_metadata({}, now), "")

    def test_chart_keeps_missing_intervals_blank_after_downsampling(self):
        start = datetime(2026, 11, 27, 17)
        friday = [(start + timedelta(minutes=5 * i), 300) for i in range(12)]
        monday = [(stamp + timedelta(days=3), price) for stamp, price in friday]
        sampled, breaks = sample_chart_rows(friday + monday, 8, "5m")
        split, = breaks
        self.assertEqual(sampled[split - 1:split + 1], [friday[-1], monday[0]])

        points = [(3, 8), (12, 8), (30, 8), (38, 8)]
        image = render_chart_line(44, 24, points, "#FFFFFF", fill_to=20, breaks=(2,))
        self.assertGreater(image.getpixel((7, 8))[3], 0)
        self.assertEqual(image.getpixel((21, 8))[3], 0)
        self.assertEqual(image.getpixel((21, 14))[3], 0)


class TestUpdateManager(TestCase):
    def test_numeric_version_comparison(self):
        manager = windows.UpdateManager("1.0.99", windows.GITHUB_REPO)
        url = f"https://github.com/{windows.GITHUB_REPO}/releases/download/v1.0.100/PiyasaWidget_Setup.exe"
        with patch.object(windows.requests, "get") as get:
            response = get.return_value
            response.status_code = 200
            response.json.return_value = {"tag_name": "v1.0.100", "assets": [
                {"name": "PiyasaWidget_Setup.exe", "browser_download_url": url},
            ]}
            self.assertEqual(manager.check_for_updates(), (True, "1.0.100", url))
            get.assert_called_once_with(manager.api_url, timeout=(5, 15))

    def test_network_error_is_distinct_from_up_to_date(self):
        manager = windows.UpdateManager("1.0.99", windows.GITHUB_REPO)
        with patch.object(windows.requests, "get", side_effect=windows.requests.ConnectionError):
            with self.assertRaisesRegex(RuntimeError, "bilgisi alınamadı"):
                manager.check_for_updates()


if __name__ == "__main__":
    unittest_main(verbosity=2)
