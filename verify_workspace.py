"""Exercise the optional native detail window against isolated app storage."""

import json
import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class TestDesktopWorkspace(unittest.TestCase):
    def setUp(self):
        try:
            probe = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        probe.destroy()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.records = self.directory / "transactions.json"
        self.records.write_text(json.dumps([
            {"date": "12-08-2025", "amount_g": 20, "total_tl": 800},
            {"date": "11-09-2025", "instrument_key": "thyao", "instrument_label": "THYAO",
             "quantity": 10, "total_tl": 2800, "action": "buy", "currency": "TL"},
        ]), encoding="utf-8")
        self.before = self.records.read_bytes()
        patcher = patch.object(main, "APP_DIR", os.fspath(self.directory))
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch.object(main.PiyasaWidget, "make_toolwindow"), patch.object(main.PiyasaWidget, "veri_dongusu"):
            self.app = main.PiyasaWidget()
        self.addCleanup(self.app.history_db.conn.close)
        self.addCleanup(self.app.root.destroy)
        self.app.root.withdraw()
        self.app.guncelle_arayuz(main.market_data_for_save(
            {"thyao": 310, "gumus_tl": 56}, "26.11 17:35", {"gumus_tl": main.SILVER_SPOT_SYMBOL}
        ))

    def test_navigation_and_quotes_keep_legacy_cost_and_do_not_write_ledger(self):
        view = self.app.open_workspace()
        self.app.root.update_idletasks()
        self.assertEqual(view.view, "overview")
        self.assertIn("26.11 17:35", view.status_label.cget("text"))
        self.assertEqual(self.app.tm.get_average_cost("gumus_tl"), 40)
        overview = view.holdings["overview"]
        before_quote = overview.item("thyao", "values")
        self.assertEqual(before_quote[2], "₺280,00 / adet")
        self.assertEqual(overview.item("gumus_tl", "values")[2], "₺40,00 / g")

        self.app.guncelle_arayuz(main.market_data_for_save({"thyao": 325}, "26.11 17:36"))
        after_quote = overview.item("thyao", "values")
        self.assertEqual(after_quote[2], before_quote[2])
        self.assertNotEqual(after_quote[3], before_quote[3])
        self.assertIn("17:36", view.status_label.cget("text"))
        for screen in ("markets", "portfolio", "trades", "settings", "overview"):
            view.navigation[screen].invoke()
            self.assertEqual(view.view, screen)
        self.assertIs(self.app.open_workspace(), view)
        view.destroy()
        self.assertTrue(self.app.root.winfo_exists())
        self.assertEqual(self.records.read_bytes(), self.before)

    def test_native_edit_and_delete_refresh_positions_and_transaction_table(self):
        view = self.app.open_workspace("trades")
        self.assertEqual({row["source_index"] for row in view.trade_table.rows}, {0, 1})
        view.trade_search.set("THYAO")
        self.assertEqual([row["source_index"] for row in view.trade_table.rows], [1])
        view._edit_trade("1")
        dialog = self.app.portfolio_dialog
        self.assertIs(dialog.master, view)
        self.assertEqual(dialog.edit_index, 1)
        dialog.entry_total.delete(0, "end")
        dialog.entry_total.insert(0, "3000")
        dialog.save()
        self.assertEqual(self.app.tm.get_average_cost("thyao"), 300)
        self.assertEqual(main.TransactionManager(os.fspath(self.records)).get_average_cost("thyao"), 300)
        self.assertEqual([row["source_index"] for row in view.trade_table.rows], [1])
        with patch.object(main.messagebox, "askyesno", return_value=True):
            view._delete_trade("1")
        self.assertEqual(view.trade_table.rows, [])
        self.assertIsNone(self.app.tm.get_average_cost("thyao"))
        self.assertEqual(self.app.tm.get_average_cost("gumus_tl"), 40)

    def test_detail_graph_retains_exact_widget_period(self):
        view = self.app.open_workspace("markets", "thyao", period=6 * 3600)
        chart = view.charts["markets"][1]
        self.assertEqual(chart.period.get(), 6 * 3600)
        self.assertEqual(chart.instrument["key"], "thyao")
        view.show_view("overview")
        self.assertEqual(view.charts["overview"][1].period_choice.get(), "6 sa")

    def test_portfolio_counts_closed_sales_and_shows_weight(self):
        self.app.tm.save({
            "date": "12-09-2025", "instrument_key": "thyao", "instrument_label": "THYAO",
            "quantity": 10, "total_tl": 3100, "action": "sell", "currency": "TL",
        })
        view = self.app.open_workspace("portfolio")
        self.assertEqual(view.metrics["portfolio"][3][0].cget("text"), "+₺300,00")
        self.assertFalse(view.holdings["portfolio"].exists("thyao"))
        self.assertEqual(view.holdings["portfolio"].set("gumus_tl", "weight"), "%100,0")

    def test_transaction_asset_and_date_filter_can_be_combined(self):
        view = self.app.open_workspace("trades")
        view.trade_asset.set("THYAO")
        view.trade_start.set("01-09-2025")
        view.trade_end.set("30-09-2025")
        view._apply_trade_dates()
        self.assertEqual([row["asset"] for row in view.trade_table.rows], ["THYAO"])

        view.trade_end.set("31-02-2025")
        view._apply_trade_dates()
        self.assertIn("GG-AA-YYYY", view.trade_date_message.cget("text"))
        self.assertEqual([row["asset"] for row in view.trade_table.rows], ["THYAO"])

    def test_unreadable_ledger_is_not_shown_as_zero_portfolio(self):
        self.records.write_text('{"incomplete":true}', encoding="utf-8")
        self.app.tm = main.TransactionManager(os.fspath(self.records))
        view = self.app.open_workspace("portfolio")
        self.assertIn("okunamadı", view.status_label.cget("text"))
        self.assertEqual(view.metrics["portfolio"][0][0].cget("text"), "—")
        self.assertEqual(view.metrics["portfolio"][3][0].cget("text"), "—")


if __name__ == "__main__":
    unittest.main()
