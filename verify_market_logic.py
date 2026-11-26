"""Deterministic checks for provider sessions and application updates."""

from unittest import TestCase, main as unittest_main
from unittest.mock import patch

import main
from market_quotes import quote_time, session_from_metadata


class TestMarketHours(TestCase):
    def test_each_provider_exposes_its_own_session(self):
        now = quote_time(1_760_000_000)
        regular = {"currentTradingPeriod": {"regular": {"start": 1_759_999_999, "end": 1_760_000_001}}}
        self.assertEqual(session_from_metadata(regular, now), "REGULAR")
        self.assertEqual(session_from_metadata({"marketState": "CLOSED"}, now), "CLOSED")
        self.assertEqual(session_from_metadata({}, now), "")


class TestUpdateManager(TestCase):
    def test_numeric_version_comparison(self):
        manager = main.UpdateManager("1.0.99", main.GITHUB_REPO)
        url = f"https://github.com/{main.GITHUB_REPO}/releases/download/v1.0.100/PiyasaWidget_Setup.exe"
        with patch.object(main.requests, "get") as get:
            response = get.return_value
            response.status_code = 200
            response.json.return_value = {"tag_name": "v1.0.100", "assets": [
                {"name": "PiyasaWidget_Setup.exe", "browser_download_url": url},
            ]}
            self.assertEqual(manager.check_for_updates(), (True, "1.0.100", url))
            get.assert_called_once_with(manager.api_url, timeout=(5, 15))

    def test_network_error_is_distinct_from_up_to_date(self):
        manager = main.UpdateManager("1.0.99", main.GITHUB_REPO)
        with patch.object(main.requests, "get", side_effect=main.requests.ConnectionError):
            with self.assertRaisesRegex(RuntimeError, "bilgisi alınamadı"):
                manager.check_for_updates()


if __name__ == "__main__":
    unittest_main(verbosity=2)
