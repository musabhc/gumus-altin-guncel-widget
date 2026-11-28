import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from piyasa import widget as widget_module
from piyasa.market import cache, history, instruments, quotes, series, watchlist
from piyasa.portfolio import transactions


class FakeTicker:
    def __init__(self, price):
        self.fast_info = {"last_price": price}
        self.info = {}


class FakeTickers:
    def __init__(self, prices):
        self.tickers = {symbol: FakeTicker(price) for symbol, price in prices.items()}


class TestDynamicMarketPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.widget = widget_module.PiyasaWidget.__new__(widget_module.PiyasaWidget)
        self.widget.watchlist = instruments.default_watchlist()
        self.widget.market_data_path = os.path.join(self.tmp.name, "market_data.json")
        self.widget._fetch_lock = threading.Lock()
        self.widget.root = MagicMock()
        self.widget.root.after.side_effect = lambda _delay, callback: callback()
        self.widget.var_market_status = MagicMock()
        self.widget.var_market_label = MagicMock()
        self.widget.lbl_market_status = MagicMock()
        self.widget.var_time = MagicMock()
        self.widget.price_vars = {item["key"]: MagicMock() for item in self.widget.watchlist}
        self.widget.var_portfolio = MagicMock()
        self.widget.var_profit = MagicMock()
        self.widget.lbl_profit = MagicMock()
        self.widget.tm = MagicMock()
        self.widget.tm.get_summary.return_value = (1000, 10)
        self.widget.tm.get_portfolio_summary.return_value = {
            "total_value_tl": 0,
            "total_cost_tl": 0,
            "profit_tl": 0,
            "profit_pct": 0,
            "rows": [],
        }
        self.widget.history_db = MagicMock()
        self.widget.color_success = "#2ecc71"
        self.widget.color_danger = "#e74c3c"
        self.widget.color_text_dim = "#666666"
        self.widget.color_success_bg = "#10261a"
        self.widget.color_danger_bg = "#2a1214"
        self.widget.color_card_alt = "#10161d"

    def test_legacy_market_data_is_loaded_as_prices_map(self):
        data = {
            "ons_gumus": 30.5,
            "gram_gumus_tl": 35.0,
            "gram_altin_tl": 2500.0,
            "dolar": 36.5,
            "timestamp": "Test Time",
        }

        self.widget.save_last_data(data)
        loaded = self.widget.load_last_data()

        self.assertEqual(loaded["prices"]["gumus_ons"], 30.5)
        self.assertEqual(loaded["prices"]["gumus_tl"], 35.0)
        self.assertEqual(loaded["prices"]["altin_tl"], 2500.0)
        self.assertEqual(loaded["prices"]["dolar"], 36.5)
        self.assertEqual(loaded["timestamp"], "Test Time")

    def test_offline_market_uses_cached_thyao_data(self):
        self.widget.save_last_data({
            "prices": {
                "gumus_ons": 30.0,
                "gumus_tl": 40.0,
                "altin_tl": 2500.0,
                "dolar": 36.0,
                "thyao": 300.0,
            },
            "timestamp": "Closed Time",
        })
        with patch.object(self.widget, "_fetch_raw_prices", return_value={}), patch.object(self.widget, "_fetch_spot_silver_price", return_value=0):
            self.widget.veri_getir()

        self.widget.price_vars["thyao"].set.assert_called_with("₺300,00")
        self.widget.var_market_label.set.assert_called_with("Son kayıtlar")
        self.widget.var_time.set.assert_called_with("Closed Time")
        self.assertEqual(self.widget.workspace_quote_status("thyao", compact=True), "Son kayıt")

    def test_open_market_fetch_saves_thyao_and_dynamic_history(self):
        prices = {
            "GC=F": 2500.0,
            "TRY=X": 40.0,
            "THYAO.IS": 300.0,
        }

        with (
            patch.object(widget_module.yf, "Tickers", return_value=FakeTickers(prices)),
            patch.object(self.widget, "_fetch_spot_silver_price", return_value=30.0),
        ):
            self.widget.veri_getir()

        loaded = self.widget.load_last_data()
        self.assertEqual(loaded["prices"]["thyao"], 300.0)
        self.assertEqual(loaded["prices"]["gumus_ons"], 30.0)
        self.assertAlmostEqual(loaded["prices"]["gumus_tl"], (30.0 * 40.0) / instruments.TROY_OUNCE_GRAMS)
        self.assertEqual(loaded["sources"]["gumus_ons"], instruments.SILVER_SPOT_SYMBOL)
        self.widget.price_vars["thyao"].set.assert_called_with("₺300,00")

        inserted_prices = {key: value for call in self.widget.history_db.insert_prices.call_args_list
                           for key, value in call.args[0].items()}
        self.assertEqual(inserted_prices["thyao"], 300.0)

    def test_partial_refresh_keeps_old_timestamp_and_marks_old_fx(self):
        old = "2026-11-24T09:00:00+00:00"
        self.widget.save_last_data(cache.market_data_for_save(
            {"thyao": 300, "dolar": 40}, sources={"thyao": "THYAO.IS", "dolar": "TRY=X"},
            quotes={"thyao": {"observed_at": old, "price_at": old, "provider": "Yahoo Finance", "state": "received"}},
        ))
        with (
            patch.object(self.widget, "_fetch_raw_prices", return_value={}),
            patch.object(self.widget, "_fetch_spot_silver_price", return_value=32.0),
        ):
            self.widget.veri_getir()

        saved = self.widget.load_last_data()
        self.assertEqual(saved["quotes"]["thyao"]["price_at"], old)
        self.assertEqual(saved["quotes"]["thyao"]["state"], "cached")
        self.assertEqual(saved["quotes"]["gumus_tl"]["state"], "mixed")
        self.assertIn("24.11 12:00", self.widget.workspace_quote_status("thyao"))
        self.assertIn("Eski kur", self.widget.workspace_quote_status("gumus_tl"))
        inserted = {key for call in self.widget.history_db.insert_prices.call_args_list for key in call.args[0]}
        self.assertEqual(inserted, {"gumus_ons"})

    def test_unchanged_prices_without_provider_time_do_not_extend_history(self):
        self.widget.history_db = history.MarketHistoryDB(os.path.join(self.tmp.name, "history.db"))
        self.addCleanup(self.widget.history_db.conn.close)
        first = datetime(2026, 11, 27, 14, tzinfo=timezone.utc)
        next_day = datetime(2026, 11, 28, 9, tzinfo=timezone.utc)
        monday = datetime(2026, 11, 30, 9, tzinfo=timezone.utc)
        thyao = {"THYAO.IS": 300.0}
        with (
            patch.object(self.widget, "_fetch_raw_prices", return_value=thyao) as fetch,
            patch.object(self.widget, "_fetch_spot_silver_price", return_value=0),
            patch.object(widget_module, "datetime", wraps=datetime) as clock,
        ):
            clock.now.side_effect = (first, next_day, monday)
            self.widget.veri_getir()
            self.widget.veri_getir()
            self.assertEqual(self.widget.load_last_data()["quotes"]["thyao"]["observed_at"], first.isoformat())
            fetch.return_value = {"THYAO.IS": 305.0}
            self.widget.veri_getir()
        rows = self.widget.history_db.get_all_history("thyao")
        self.assertEqual(rows, [
            (series.normalize_history_timestamp(first), 300.0),
            (series.normalize_history_timestamp(monday), 305.0),
        ])

    def test_repeated_timed_close_is_retained_and_labelled(self):
        self.widget.history_db = history.MarketHistoryDB(os.path.join(self.tmp.name, "history.db"))
        self.addCleanup(self.widget.history_db.conn.close)
        close = "2026-11-27T15:00:00+00:00"

        def fetch(_symbols, *, metadata):
            metadata["THYAO.IS"] = quotes.observation("Yahoo Finance", close, price_at=close, market_state="CLOSED")
            return {"THYAO.IS": 300.0}

        with (
            patch.object(self.widget, "_fetch_raw_prices", side_effect=fetch),
            patch.object(self.widget, "_fetch_spot_silver_price", return_value=0),
        ):
            self.widget.veri_getir()
            self.widget.veri_getir()
        self.assertEqual(len(self.widget.history_db.get_all_history("thyao")), 1)
        self.assertTrue(self.widget.workspace_quote_status("thyao", compact=True).startswith("Kapalı · "))

    def test_usd_priced_watchlist_items_require_dollar_rate(self):
        widget = widget_module.PiyasaWidget.__new__(widget_module.PiyasaWidget)
        widget.watchlist = [{
            "key": "aapl",
            "label": "AAPL",
            "symbol": "AAPL",
            "currency": "$",
            "decimals": 2,
            "color": "#2196f3",
            "source": "direct",
        }]

        self.assertIn("TRY=X", widget._required_yahoo_symbols())
        self.assertNotIn(
            instruments.SILVER_SPOT_SYMBOL,
            self.widget._required_yahoo_symbols()
        )

    def test_previous_close_is_not_presented_as_a_live_quote(self):
        ticker = MagicMock()
        ticker.fast_info = {"previous_close": 71.0}
        ticker.info = {"previousClose": 71.0}

        self.assertEqual(self.widget._extract_price(ticker), 0)

    def test_spot_silver_endpoint_is_validated(self):
        response = MagicMock()
        response.json.return_value = {
            "symbol": "XAG",
            "currency": "USD",
            "price": 70.35,
        }
        with patch.object(widget_module.requests, "get", return_value=response) as get:
            price = self.widget._fetch_spot_silver_price()

        self.assertEqual(price, 70.35)
        get.assert_called_once_with(
            instruments.SILVER_SPOT_API_URL,
            headers={"User-Agent": "Disa-Finans-Widget/1.0"},
            timeout=10
        )

    def test_spot_gram_price_can_use_last_known_dollar_rate(self):
        prices, fresh = self.widget._resolve_watchlist_prices(
            {instruments.SILVER_SPOT_SYMBOL: 70.0},
            {"dolar": 40.0}
        )

        self.assertIn("gumus_tl", fresh)
        self.assertAlmostEqual(
            prices["gumus_tl"],
            (70.0 * 40.0) / instruments.TROY_OUNCE_GRAMS
        )

    def test_legacy_futures_cache_is_not_reused_for_spot_rows(self):
        cached = cache.market_data_for_save({
            "gumus_ons": 71.0,
            "gumus_tl": 103.0,
            "thyao": 300.0,
        })

        compatible = self.widget._compatible_cached_data(cached)

        self.assertNotIn("gumus_ons", compatible["prices"])
        self.assertNotIn("gumus_tl", compatible["prices"])
        self.assertEqual(compatible["prices"]["thyao"], 300.0)

    def test_legacy_history_is_migrated_to_dynamic_table(self):
        db_path = os.path.join(self.tmp.name, "history.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE market_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ons_gumus REAL,
                gram_gumus_tl REAL,
                gram_altin_tl REAL,
                dolar REAL
            )
        """)
        conn.execute(
            "INSERT INTO market_history (timestamp, ons_gumus, gram_gumus_tl, gram_altin_tl, dolar) VALUES (?, ?, ?, ?, ?)",
            ("2026-01-01T10:00:00", 30.0, 40.0, 2500.0, 36.0),
        )
        conn.commit()
        conn.close()

        db = history.MarketHistoryDB(db_path)
        try:
            stats = db.get_stats("gumus_tl")
            self.assertEqual(stats[3], 1)
            self.assertEqual(stats[0], 40.0)
        finally:
            db.conn.close()

    def test_silver_history_can_be_filtered_by_quote_source(self):
        db_path = os.path.join(self.tmp.name, "source_history.db")
        db = history.MarketHistoryDB(db_path)
        try:
            futures = [{
                "key": "gumus_ons",
                "label": "Gümüş ONS",
                "symbol": instruments.LEGACY_SILVER_FUTURES_SYMBOL,
            }]
            spot = [{
                "key": "gumus_ons",
                "label": "Gümüş ONS",
                "symbol": instruments.SILVER_SPOT_SYMBOL,
            }]
            now = widget_module.datetime.now()
            db.insert_prices(
                {"gumus_ons": 71.0},
                futures,
                timestamp=(now - widget_module.timedelta(minutes=1)).isoformat()
            )
            db.insert_prices(
                {"gumus_ons": 70.0},
                spot,
                timestamp=now.isoformat()
            )

            rows = db.get_history(
                "gumus_ons", days=1, source_symbol=instruments.SILVER_SPOT_SYMBOL
            )

            self.assertEqual([row[1] for row in rows], [70.0])
        finally:
            db.conn.close()


class TestHistoricalChartInfrastructure(unittest.TestCase):
    def test_live_chart_recovers_prices_before_legacy_flat_tail(self):
        widget = widget_module.PiyasaWidget.__new__(widget_module.PiyasaWidget)
        widget.watchlist = instruments.default_watchlist()
        with tempfile.TemporaryDirectory() as directory:
            db = history.MarketHistoryDB(os.path.join(directory, "history.db"))
            widget.history_db = db
            rows = [
                ("2026-11-27T17:40:00", 295.0),
                ("2026-11-27T17:45:00", 295.0),
                ("2026-11-27T17:50:00", 300.0),
                ("2026-11-28T10:00:00", 300.0),
                ("2026-11-29T10:00:00", 300.0),
            ]
            try:
                for stamp, value in rows:
                    db.insert_prices({"thyao": value}, widget.watchlist, timestamp=stamp)
                series_rows, source, interval = widget._cached_history_series("thyao", 1800)
                self.assertEqual(series_rows, [rows[0], rows[2]])
                self.assertEqual((source, interval), ("THYAO.IS", "live"))
                self.assertEqual(db.get_all_history("thyao"), rows)
            finally:
                db.conn.close()

    def test_default_widget_geometry_matches_reference_desktop(self):
        self.assertEqual(
            widget_module.PiyasaWidget.calculate_default_geometry(1920, 1080),
            (368, 536, 1522, 50),
        )

    def test_chart_periods_cover_all_requested_windows(self):
        self.assertEqual(
            widget_module.PiyasaWidget.CHART_PERIOD_OPTIONS,
            (
                ("30 dakika", 30 * 60),
                ("1 saat", 60 * 60),
                ("6 saat", 6 * 60 * 60),
                ("12 saat", 12 * 60 * 60),
                ("24 saat", 24 * 60 * 60),
                ("7 gün", 7 * 24 * 60 * 60),
                ("30 gün", 30 * 24 * 60 * 60),
                ("Tümü", 0),
            ),
        )

    def test_zoom_and_pan_ranges_are_clamped_and_preserve_span(self):
        zoomed = series.calculate_zoomed_range(
            0.0,
            1.0,
            True,
            anchor=0.25,
        )
        self.assertAlmostEqual(zoomed[0], 0.0875)
        self.assertAlmostEqual(zoomed[1], 0.7375)
        self.assertAlmostEqual(zoomed[1] - zoomed[0], 0.65)

        self.assertEqual(
            series.calculate_panned_range(0.2, 0.6, -1.0),
            (0.0, 0.4),
        )
        right_clamped = series.calculate_panned_range(0.2, 0.6, 1.0)
        self.assertAlmostEqual(right_clamped[0], 0.6)
        self.assertAlmostEqual(right_clamped[1], 1.0)
        self.assertAlmostEqual(right_clamped[1] - right_clamped[0], 0.4)

    def test_downsampling_keeps_endpoints_and_local_extrema(self):
        rows = [
            (f"2026-01-01T00:{index:02d}:00", value)
            for index, value in enumerate(
                (10, 11, 12, 99, -25, 13, 14, 15, 16, 17, 18)
            )
        ]

        sampled = series.downsample_history_rows(rows, 6)

        self.assertLessEqual(len(sampled), 6)
        self.assertEqual(sampled[0], rows[0])
        self.assertEqual(sampled[-1], rows[-1])
        self.assertIn(rows[3], sampled)
        self.assertIn(rows[4], sampled)
        self.assertEqual(
            [timestamp for timestamp, _price in sampled],
            sorted(timestamp for timestamp, _price in sampled),
        )

    def test_history_rows_are_interval_aware_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = history.MarketHistoryDB(os.path.join(tmp, "history.db"))
            instrument = {
                "key": "thyao",
                "label": "THYAO",
                "symbol": "THYAO.IS",
            }
            now = widget_module.datetime.now().replace(microsecond=0)
            rows = [
                ((now - widget_module.timedelta(hours=2)).isoformat(), 285.0),
                ((now - widget_module.timedelta(minutes=50)).isoformat(), 290.0),
                ((now - widget_module.timedelta(minutes=10)).isoformat(), 295.0),
            ]
            try:
                self.assertEqual(
                    db.insert_history_rows(
                        instrument,
                        rows,
                        "5m",
                        source_symbol="THYAO.IS",
                    ),
                    3,
                )
                self.assertEqual(
                    db.insert_history_rows(
                        instrument,
                        rows,
                        "5m",
                        source_symbol="THYAO.IS",
                    ),
                    0,
                )
                db.insert_history_rows(
                    instrument,
                    [(rows[-1][0], 999.0)],
                    "1d",
                    source_symbol="THYAO.IS",
                )

                recent = db.get_history(
                    "thyao",
                    hours=1,
                    source_symbol=("THYAO.IS", "UNUSED"),
                    intervals=("5m",),
                )
                daily = db.get_all_history(
                    "thyao",
                    source_symbol="THYAO.IS",
                    intervals=("1d",),
                )

                self.assertEqual([price for _timestamp, price in recent], [290.0, 295.0])
                self.assertEqual([price for _timestamp, price in daily], [999.0])
                count = db.conn.execute(
                    "SELECT COUNT(*) FROM market_price_history "
                    "WHERE instrument_key = ? AND interval = ?",
                    ("thyao", "5m"),
                ).fetchone()[0]
                self.assertEqual(count, 3)
            finally:
                db.conn.close()

    def test_metal_history_uses_latest_prior_fx_and_rejects_stale_fx(self):
        base_rows = [
            ("2026-01-01T10:30:00", instruments.TROY_OUNCE_GRAMS),
            ("2026-01-01T11:30:00", 2 * instruments.TROY_OUNCE_GRAMS),
            ("2026-01-06T12:00:00", instruments.TROY_OUNCE_GRAMS),
        ]
        fx_rows = [
            ("2026-01-01T10:00:00", 40.0),
            ("2026-01-01T11:00:00", 42.0),
        ]

        converted = widget_module.PiyasaWidget._combine_history_with_fx(
            base_rows,
            fx_rows,
        )

        self.assertEqual(
            [timestamp for timestamp, _price in converted],
            ["2026-01-01T10:30:00", "2026-01-01T11:30:00"],
        )
        self.assertAlmostEqual(converted[0][1], 40.0)
        self.assertAlmostEqual(converted[1][1], 84.0)

    def test_intraday_fx_conversion_rejects_a_stale_quote(self):
        converted = widget_module.PiyasaWidget._combine_history_with_fx(
            [
                ("2026-01-01T10:10:00", instruments.TROY_OUNCE_GRAMS),
                ("2026-01-01T10:20:00", instruments.TROY_OUNCE_GRAMS),
            ],
            [("2026-01-01T10:00:00", 40.0)],
            "5m",
        )

        self.assertEqual(converted, [("2026-01-01T10:10:00", 40.0)])

    def test_latest_anchored_window_works_for_a_closed_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = history.MarketHistoryDB(os.path.join(tmp, "history.db"))
            instrument = {
                "key": "thyao",
                "label": "THYAO",
                "symbol": "THYAO.IS",
            }
            try:
                db.insert_history_rows(
                    instrument,
                    [
                        ("2020-01-03T16:00:00", 10.0),
                        ("2020-01-03T16:30:00", 11.0),
                        ("2020-01-03T17:00:00", 12.0),
                    ],
                    "5m",
                )

                wall_clock = db.get_history(
                    "thyao", hours=1, intervals=("5m",)
                )
                latest_session = db.get_history(
                    "thyao",
                    hours=1,
                    intervals=("5m",),
                    anchor_to_latest=True,
                )

                self.assertEqual(wall_clock, [])
                self.assertEqual(
                    [price for _timestamp, price in latest_session],
                    [10.0, 11.0, 12.0],
                )
            finally:
                db.conn.close()

    def test_silver_chart_prefers_proxy_bars_without_mixing_spot_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            widget = widget_module.PiyasaWidget.__new__(widget_module.PiyasaWidget)
            widget.watchlist = instruments.default_watchlist()
            widget.history_db = history.MarketHistoryDB(
                os.path.join(tmp, "history.db")
            )
            silver = next(
                item for item in widget.watchlist
                if item["key"] == "gumus_ons"
            )
            now = widget_module.datetime.now().replace(microsecond=0)
            try:
                widget.history_db.insert_history_rows(
                    silver,
                    [
                        ((now - widget_module.timedelta(minutes=10)).isoformat(), 60.0),
                        (now.isoformat(), 61.0),
                    ],
                    "5m",
                    source_symbol=instruments.SILVER_HISTORY_PROXY_SYMBOL,
                )
                widget.history_db.insert_prices(
                    {"gumus_ons": 80.0},
                    [silver],
                    timestamp=now.isoformat(),
                )

                rows, source, interval = widget._cached_history_series(
                    "gumus_ons", 24 * 60 * 60
                )

                self.assertEqual([price for _timestamp, price in rows], [60.0, 61.0])
                self.assertEqual(source, instruments.SILVER_HISTORY_PROXY_SYMBOL)
                self.assertEqual(interval, "5m")
            finally:
                widget.history_db.conn.close()

    def test_valid_long_term_trend_is_not_removed_as_an_outlier(self):
        values = [float(value) for value in range(1, 301)]
        timestamps = [f"2026-01-{(index % 28) + 1:02d}" for index in range(300)]

        filtered, filtered_timestamps = widget_module.PiyasaWidget._filter_outliers(
            values, timestamps
        )

        self.assertEqual(filtered, values)
        self.assertEqual(filtered_timestamps, timestamps)

    def test_yahoo_history_uses_adjusted_prices(self):
        widget = widget_module.PiyasaWidget.__new__(widget_module.PiyasaWidget)
        ticker = MagicMock()
        ticker.history.return_value = None
        with patch.object(widget_module.yf, "Ticker", return_value=ticker):
            self.assertEqual(widget._download_yahoo_history("THYAO.IS", "1d"), [])

        self.assertTrue(ticker.history.call_args.kwargs["auto_adjust"])
        self.assertEqual(ticker.history.call_args.kwargs["period"], "max")

    def test_history_sync_persists_provider_bars_and_marks_profile_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            widget = widget_module.PiyasaWidget.__new__(widget_module.PiyasaWidget)
            instrument = next(
                item for item in instruments.default_watchlist()
                if item["key"] == "thyao"
            )
            widget.watchlist = [instrument]
            widget.history_db = history.MarketHistoryDB(
                os.path.join(tmp, "history.db")
            )
            widget._stop_event = threading.Event()
            provider_rows = [
                ("2026-10-05T10:00:00", 290.0),
                ("2026-10-05T10:05:00", 291.0),
            ]
            widget._download_yahoo_history = MagicMock(
                return_value=provider_rows
            )
            try:
                inserted = widget._sync_history_profile(
                    "5m", widget_module.timedelta(minutes=30)
                )

                self.assertEqual(inserted, 2)
                self.assertEqual(
                    widget.history_db.get_all_history(
                        "thyao",
                        source_symbol="THYAO.IS",
                        intervals=("5m",),
                    ),
                    provider_rows,
                )
                self.assertFalse(
                    widget.history_db.history_sync_due(
                        "thyao",
                        "THYAO.IS",
                        "5m",
                        widget_module.timedelta(minutes=30),
                    )
                )
            finally:
                widget.history_db.conn.close()


class TestMultiAssetPortfolio(unittest.TestCase):
    def test_legacy_transactions_are_read_as_silver_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "transactions.json")
            manager = transactions.TransactionManager(path)
            manager.save({
                "date": "01-01-2026",
                "amount_g": 10,
                "total_tl": 1000,
                "currency": "TL",
            })

            summary = manager.get_portfolio_summary({"gumus_tl": 120}, instruments.default_watchlist())

            self.assertEqual(summary["rows"][0]["key"], "gumus_tl")
            self.assertEqual(summary["rows"][0]["quantity"], 10)
            self.assertEqual(summary["total_value_tl"], 1200)
            self.assertEqual(summary["profit_tl"], 200)

    def test_thyao_buy_is_included_in_total_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = transactions.TransactionManager(os.path.join(tmp, "transactions.json"))
            manager.save({
                "date": "01-01-2026",
                "instrument_key": "thyao",
                "instrument_label": "THYAO",
                "action": "buy",
                "quantity": 15,
                "currency": "TL",
                "total_tl": 4200,
            })

            summary = manager.get_portfolio_summary({"thyao": 300}, instruments.default_watchlist())

            self.assertEqual(summary["total_value_tl"], 4500)
            self.assertEqual(summary["rows"][0]["label"], "THYAO")
            self.assertEqual(summary["profit_tl"], 300)

    def test_multi_asset_summary_converts_usd_assets_to_tl(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = transactions.TransactionManager(os.path.join(tmp, "transactions.json"))
            watchlist = instruments.default_watchlist() + [{
                "key": "aapl",
                "label": "AAPL",
                "symbol": "AAPL",
                "currency": "$",
                "decimals": 2,
                "color": "#2196f3",
                "source": "direct",
            }]
            manager.save({"date": "01-01-2026", "instrument_key": "gumus_tl", "instrument_label": "Gümüş TL", "action": "buy", "quantity": 10, "currency": "TL", "total_tl": 1000})
            manager.save({"date": "01-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "buy", "quantity": 2, "currency": "TL", "total_tl": 500})
            manager.save({"date": "01-01-2026", "instrument_key": "aapl", "instrument_label": "AAPL", "action": "buy", "quantity": 1, "currency": "USD", "total_usd": 100, "fx_rate": 30, "total_tl": 3000})

            summary = manager.get_portfolio_summary({"gumus_tl": 120, "thyao": 300, "aapl": 110, "dolar": 40}, watchlist)

            self.assertEqual(summary["total_value_tl"], 1200 + 600 + 4400)
            self.assertEqual(summary["profit_tl"], (1200 + 600 + 4400) - 4500)

    def test_sell_reduces_quantity_and_rejects_oversell(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = transactions.TransactionManager(os.path.join(tmp, "transactions.json"))
            manager.save({"date": "01-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "buy", "quantity": 10, "currency": "TL", "total_tl": 1000})
            manager.save({"date": "02-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "sell", "quantity": 4, "currency": "TL", "total_tl": 600})

            summary = manager.get_portfolio_summary({"thyao": 120}, instruments.default_watchlist())

            self.assertEqual(summary["rows"][0]["quantity"], 6)
            self.assertEqual(summary["rows"][0]["cost_basis_tl"], 600)
            with self.assertRaises(ValueError):
                manager.save({"date": "03-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "sell", "quantity": 7, "currency": "TL", "total_tl": 700})

    def test_replace_updates_transaction_and_validates_sell_quantity(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = transactions.TransactionManager(os.path.join(tmp, "transactions.json"))
            manager.save({"date": "01-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "buy", "quantity": 10, "currency": "TL", "total_tl": 1000})
            manager.save({"date": "02-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "sell", "quantity": 2, "currency": "TL", "total_tl": 300})

            manager.replace(0, {"date": "01-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "buy", "quantity": 12, "currency": "TL", "total_tl": 1200})
            summary = manager.get_portfolio_summary({"thyao": 150}, instruments.default_watchlist())

            self.assertEqual(summary["rows"][0]["quantity"], 10)
            with self.assertRaises(ValueError):
                manager.replace(1, {"date": "02-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "sell", "quantity": 13, "currency": "TL", "total_tl": 1300})

    def test_missing_price_keeps_asset_row_but_counts_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = transactions.TransactionManager(os.path.join(tmp, "transactions.json"))
            manager.save({"date": "01-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "buy", "quantity": 5, "currency": "TL", "total_tl": 1000})

            summary = manager.get_portfolio_summary({}, instruments.default_watchlist())

            self.assertEqual(summary["total_value_tl"], 0)
            self.assertFalse(summary["rows"][0]["has_price"])
            self.assertEqual(summary["profit_tl"], 0)

    def test_average_cost_survives_price_refresh_and_retains_historical_fx(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "transactions.json")
            manager = transactions.TransactionManager(path)
            manager.save_many([
                {"instrument_key": "thyao", "quantity": 10, "total_tl": 1000},
                {"instrument_key": "thyao", "quantity": 10, "total_tl": 2000},
                {"amount_g": 5, "total_tl": 250},
                {"instrument_key": "gumus_tl", "quantity": 5, "total_tl": 350},
                {"instrument_key": "aapl", "quantity": 2, "currency": "USD",
                 "total_usd": 100, "fx_rate": 40},
            ])
            manager = transactions.TransactionManager(path)
            with patch.object(
                manager, "_calculate_positions", wraps=manager._calculate_positions
            ) as calculate:
                for prices in (
                    {"thyao": 180, "gumus_tl": 65, "dolar": 41},
                    {"thyao": 220, "gumus_tl": 70, "dolar": 45},
                ):
                    summary = manager.get_portfolio_summary(prices, instruments.default_watchlist())
                    rows = {row["key"]: row for row in summary["rows"]}
                    self.assertEqual(rows["thyao"]["average_cost_tl"], 150)
                    self.assertEqual(rows["gumus_tl"]["average_cost_tl"], 60)
                    self.assertEqual(rows["aapl"]["average_cost_tl"], 2000)
                calculate.assert_called_once()
            self.assertEqual(summary["profit_tl"], 1500)

    def test_average_cost_handles_partial_sale_edits_and_closed_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = transactions.TransactionManager(os.path.join(tmp, "transactions.json"))
            manager.save_many([
                {"instrument_key": "thyao", "quantity": 10, "total_tl": 1000},
                {"instrument_key": "thyao", "quantity": 10, "total_tl": 2000},
                {"instrument_key": "thyao", "action": "sell", "quantity": 4, "total_tl": 1200},
            ])
            self.assertEqual(manager.get_average_cost("thyao"), 150)
            self.assertEqual(manager.get_positions()["thyao"]["realized_profit_tl"], 600)

            manager.replace(1, {"instrument_key": "thyao", "quantity": 10, "total_tl": 3000})
            self.assertEqual(manager.get_average_cost("thyao"), 200)
            manager.delete(1)
            self.assertEqual(manager.get_average_cost("thyao"), 100)

            manager.save({"instrument_key": "thyao", "action": "sell", "quantity": 6, "total_tl": 1800})
            self.assertIsNone(manager.get_average_cost("thyao"))
            manager.save({"instrument_key": "thyao", "quantity": 2, "total_tl": 600})
            self.assertEqual(manager.get_average_cost("thyao"), 300)

    def test_invalid_batch_or_failed_write_leaves_ledger_and_cost_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "transactions.json")
            manager = transactions.TransactionManager(path)
            manager.save({"instrument_key": "thyao", "quantity": 10, "total_tl": 1000})
            second_buy = {"instrument_key": "thyao", "quantity": 10, "total_tl": 2000}

            with self.assertRaises(ValueError):
                manager.save_many([
                    second_buy,
                    {"instrument_key": "thyao", "action": "sell", "quantity": 30, "total_tl": 3000},
                ])
            self.assertEqual(manager.get_average_cost("thyao"), 100)

            with patch.object(widget_module.os, "replace", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    manager.save(second_buy)
            self.assertEqual(manager.get_position_quantity("thyao"), 10)
            self.assertEqual(manager.get_average_cost("thyao"), 100)
            self.assertEqual(transactions.TransactionManager(path).transactions, manager.transactions)

    def test_edit_and_delete_validate_later_sales_in_ledger_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "transactions.json")
            manager = transactions.TransactionManager(path)
            first_buy = {"instrument_key": "thyao", "quantity": 5, "total_tl": 500}
            sale = {"instrument_key": "thyao", "action": "sell", "quantity": 4, "total_tl": 600}
            later_buy = {"instrument_key": "thyao", "quantity": 10, "total_tl": 2000}
            manager.save_many([first_buy, sale, later_buy])

            smaller_sale = dict(sale, quantity=3, total_tl=450)
            manager.replace(1, smaller_sale)
            expected = [first_buy, smaller_sale, later_buy]
            self.assertEqual(manager.get_position_quantity("thyao"), 12)
            self.assertAlmostEqual(manager.get_average_cost("thyao"), 2200 / 12)

            with self.assertRaises(ValueError):
                manager.replace(1, dict(sale, quantity=6))
            with self.assertRaises(ValueError):
                manager.replace(0, dict(first_buy, quantity=2))
            with self.assertRaises(ValueError):
                manager.delete(0)

            self.assertEqual(manager.transactions, expected)
            self.assertEqual(transactions.TransactionManager(path).transactions, expected)
            self.assertEqual(manager.get_position_quantity("thyao"), 12)
            self.assertAlmostEqual(manager.get_average_cost("thyao"), 2200 / 12)

    def test_legacy_oversold_ledger_can_be_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "transactions.json")
            original_buy = {"quantity": 5, "total_tl": 500}
            legacy_sale = {"action": "sell", "quantity": 7, "total_tl": 840}
            with open(path, "w", encoding="utf-8") as file:
                json.dump([original_buy, legacy_sale], file)

            manager = transactions.TransactionManager(path)
            self.assertIsNone(manager.get_average_cost("gumus_tl"))
            manager.replace(0, dict(original_buy, quantity=6, total_tl=600))
            manager.replace(1, dict(legacy_sale, quantity=4, total_tl=480))
            self.assertEqual(manager.get_position_quantity("gumus_tl"), 2)
            self.assertEqual(manager.get_average_cost("gumus_tl"), 100)


class TestWatchlistManager(unittest.TestCase):
    def test_hidden_usd_position_stays_valued_and_can_be_restored_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "watchlist.json")
            manager = watchlist.WatchlistManager(path)
            apple = manager.add("Apple", "AAPL", "$")
            portfolio = transactions.TransactionManager(os.path.join(tmp, "transactions.json"))
            portfolio.save({"instrument_key": apple["key"], "quantity": 1, "total_tl": 4000})
            manager.remove(apple["key"])

            widget = widget_module.PiyasaWidget.__new__(widget_module.PiyasaWidget)
            widget.watchlist_manager = watchlist.WatchlistManager(path)
            widget.watchlist = widget.watchlist_manager.instruments
            widget.tm = portfolio
            self.assertFalse(widget.watchlist_manager.has_symbol("AAPL"))
            self.assertIn("AAPL", widget._required_yahoo_symbols())
            instruments = widget.workspace_instruments()
            summary = portfolio.get_portfolio_summary({apple["key"]: 120, "dolar": 40}, instruments)
            self.assertEqual(summary["total_value_tl"], 4800)

            widget.watchlist_manager.restore(apple["key"])
            self.assertIn(apple, watchlist.WatchlistManager(path).instruments)

    def test_defaults_include_thyao_and_user_symbols_are_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "watchlist.json")
            manager = watchlist.WatchlistManager(path)

            self.assertTrue(any(item["symbol"] == "THYAO.IS" for item in manager.instruments))
            manager.add("Apple", "AAPL", "$")
            self.assertTrue(manager.has_symbol("AAPL"))

            with self.assertRaises(ValueError):
                manager.add("Apple Again", "AAPL", "$")

            manager_reloaded = watchlist.WatchlistManager(path)
            self.assertTrue(manager_reloaded.has_symbol("AAPL"))

    def test_legacy_builtin_silver_symbols_migrate_to_spot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "watchlist.json")
            manager = watchlist.WatchlistManager(path)
            for instrument in manager.instruments:
                if instrument["key"] in instruments.SILVER_SPOT_KEYS:
                    instrument["symbol"] = instruments.LEGACY_SILVER_FUTURES_SYMBOL
                    instrument["source"] = (
                        "direct" if instrument["key"] == "gumus_ons" else "metal_try"
                    )
            manager.save_all()

            migrated = watchlist.WatchlistManager(path)
            silver = {
                item["key"]: item
                for item in migrated.instruments
                if item["key"] in instruments.SILVER_SPOT_KEYS
            }

            self.assertEqual(silver["gumus_ons"]["symbol"], instruments.SILVER_SPOT_SYMBOL)
            self.assertEqual(silver["gumus_ons"]["source"], "silver_spot")
            self.assertEqual(silver["gumus_tl"]["symbol"], instruments.SILVER_SPOT_SYMBOL)
            self.assertEqual(silver["gumus_tl"]["source"], "silver_spot_try")
            self.assertEqual(
                watchlist.WatchlistManager(path).instruments,
                migrated.instruments
            )

    def test_reordered_rows_are_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "watchlist.json")
            manager = watchlist.WatchlistManager(path)
            reversed_keys = [item["key"] for item in reversed(manager.instruments)]

            self.assertTrue(manager.reorder(reversed_keys))

            reloaded = watchlist.WatchlistManager(path)
            self.assertEqual(
                [item["key"] for item in reloaded.instruments],
                reversed_keys
            )

    def test_reorder_rejects_non_permutations_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = watchlist.WatchlistManager(os.path.join(tmp, "watchlist.json"))
            original = [item["key"] for item in manager.instruments]

            with self.assertRaises(ValueError):
                manager.reorder(original[:-1])
            with self.assertRaises(ValueError):
                manager.reorder(original[:-1] + [original[0]])

            self.assertEqual([item["key"] for item in manager.instruments], original)

    def test_drag_order_uses_row_midpoints(self):
        keys = ["a", "b", "c", "d"]
        midpoints = {"a": 10, "b": 20, "d": 40}

        self.assertEqual(
            instruments.calculate_reordered_keys(keys, "c", 0, midpoints),
            ["c", "a", "b", "d"]
        )
        self.assertEqual(
            instruments.calculate_reordered_keys(keys, "c", 25, midpoints),
            ["a", "b", "c", "d"]
        )
        self.assertEqual(
            instruments.calculate_reordered_keys(keys, "c", 100, midpoints),
            ["a", "b", "d", "c"]
        )


if __name__ == "__main__":
    unittest.main()
