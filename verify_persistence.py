import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main


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
        self.widget = main.PiyasaWidget.__new__(main.PiyasaWidget)
        self.widget.watchlist = main.default_watchlist()
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

    def tearDown(self):
        self.tmp.cleanup()

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

    def test_closed_market_uses_cached_thyao_data(self):
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
        self.widget.is_market_closed = MagicMock(return_value=True)

        self.widget.veri_getir()

        self.widget.price_vars["thyao"].set.assert_called_with("₺300.00")
        self.widget.var_market_label.set.assert_called_with("Piyasa Kapalı")
        self.widget.var_time.set.assert_called_with("Son: Closed Time")

    def test_open_market_fetch_saves_thyao_and_dynamic_history(self):
        self.widget.is_market_closed = MagicMock(return_value=False)
        prices = {
            "SI=F": 30.0,
            "GC=F": 2500.0,
            "TRY=X": 40.0,
            "THYAO.IS": 300.0,
        }

        with patch.object(main.yf, "Tickers", return_value=FakeTickers(prices)):
            self.widget.veri_getir()

        loaded = self.widget.load_last_data()
        self.assertEqual(loaded["prices"]["thyao"], 300.0)
        self.assertAlmostEqual(loaded["prices"]["gumus_tl"], (30.0 * 40.0) / main.TROY_OUNCE_GRAMS)
        self.widget.price_vars["thyao"].set.assert_called_with("₺300.00")

        inserted_prices = self.widget.history_db.insert_prices.call_args.args[0]
        self.assertEqual(inserted_prices["thyao"], 300.0)

    def test_usd_priced_watchlist_items_require_dollar_rate(self):
        widget = main.PiyasaWidget.__new__(main.PiyasaWidget)
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

        db = main.MarketHistoryDB(db_path)
        try:
            stats = db.get_stats("gumus_tl")
            self.assertEqual(stats[3], 1)
            self.assertEqual(stats[0], 40.0)
        finally:
            db.conn.close()


class TestMultiAssetPortfolio(unittest.TestCase):
    def test_legacy_transactions_are_read_as_silver_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "transactions.json")
            manager = main.TransactionManager(path)
            manager.save({
                "date": "01-01-2026",
                "amount_g": 10,
                "total_tl": 1000,
                "currency": "TL",
            })

            summary = manager.get_portfolio_summary({"gumus_tl": 120}, main.default_watchlist())

            self.assertEqual(summary["rows"][0]["key"], "gumus_tl")
            self.assertEqual(summary["rows"][0]["quantity"], 10)
            self.assertEqual(summary["total_value_tl"], 1200)
            self.assertEqual(summary["profit_tl"], 200)

    def test_thyao_buy_is_included_in_total_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = main.TransactionManager(os.path.join(tmp, "transactions.json"))
            manager.save({
                "date": "01-01-2026",
                "instrument_key": "thyao",
                "instrument_label": "THYAO",
                "action": "buy",
                "quantity": 15,
                "currency": "TL",
                "total_tl": 4200,
            })

            summary = manager.get_portfolio_summary({"thyao": 300}, main.default_watchlist())

            self.assertEqual(summary["total_value_tl"], 4500)
            self.assertEqual(summary["rows"][0]["label"], "THYAO")
            self.assertEqual(summary["profit_tl"], 300)

    def test_multi_asset_summary_converts_usd_assets_to_tl(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = main.TransactionManager(os.path.join(tmp, "transactions.json"))
            watchlist = main.default_watchlist() + [{
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
            manager = main.TransactionManager(os.path.join(tmp, "transactions.json"))
            manager.save({"date": "01-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "buy", "quantity": 10, "currency": "TL", "total_tl": 1000})
            manager.save({"date": "02-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "sell", "quantity": 4, "currency": "TL", "total_tl": 600})

            summary = manager.get_portfolio_summary({"thyao": 120}, main.default_watchlist())

            self.assertEqual(summary["rows"][0]["quantity"], 6)
            self.assertEqual(summary["rows"][0]["cost_basis_tl"], 600)
            with self.assertRaises(ValueError):
                manager.save({"date": "03-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "sell", "quantity": 7, "currency": "TL", "total_tl": 700})

    def test_replace_updates_transaction_and_validates_sell_quantity(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = main.TransactionManager(os.path.join(tmp, "transactions.json"))
            manager.save({"date": "01-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "buy", "quantity": 10, "currency": "TL", "total_tl": 1000})
            manager.save({"date": "02-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "sell", "quantity": 2, "currency": "TL", "total_tl": 300})

            manager.replace(0, {"date": "01-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "buy", "quantity": 12, "currency": "TL", "total_tl": 1200})
            summary = manager.get_portfolio_summary({"thyao": 150}, main.default_watchlist())

            self.assertEqual(summary["rows"][0]["quantity"], 10)
            with self.assertRaises(ValueError):
                manager.replace(1, {"date": "02-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "sell", "quantity": 13, "currency": "TL", "total_tl": 1300})

    def test_missing_price_keeps_asset_row_but_counts_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = main.TransactionManager(os.path.join(tmp, "transactions.json"))
            manager.save({"date": "01-01-2026", "instrument_key": "thyao", "instrument_label": "THYAO", "action": "buy", "quantity": 5, "currency": "TL", "total_tl": 1000})

            summary = manager.get_portfolio_summary({}, main.default_watchlist())

            self.assertEqual(summary["total_value_tl"], 0)
            self.assertFalse(summary["rows"][0]["has_price"])


class TestWatchlistManager(unittest.TestCase):
    def test_defaults_include_thyao_and_user_symbols_are_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "watchlist.json")
            manager = main.WatchlistManager(path)

            self.assertTrue(any(item["symbol"] == "THYAO.IS" for item in manager.instruments))
            manager.add("Apple", "AAPL", "$")
            self.assertTrue(manager.has_symbol("AAPL"))

            with self.assertRaises(ValueError):
                manager.add("Apple Again", "AAPL", "$")

            manager_reloaded = main.WatchlistManager(path)
            self.assertTrue(manager_reloaded.has_symbol("AAPL"))


if __name__ == "__main__":
    unittest.main()
