"""Regression tests for the native compact trade form."""

import tempfile
from pathlib import Path
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from main import PortfolioManagerDialog, TransactionManager, default_watchlist


class TestQuickTradeForm(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        self.addCleanup(self.root.destroy)
        self.root.withdraw()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.filename = str(Path(temp.name) / "transactions.json")
        self.manager = TransactionManager(self.filename)
        self.saves = []

    def open(self):
        return PortfolioManagerDialog(self.root, self.manager, lambda: self.saves.append(True), 42.0,
                                      default_watchlist(), quick=True)

    def wait_for_preview(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(.01)
        self.fail("Trade preview did not reach its expected state")

    def test_unit_price_preview_and_total_switch_save_the_same_transaction(self):
        self.manager.save({"instrument_key": "gumus_tl", "action": "buy", "quantity": 2, "total_tl": 200})
        form = self.open()
        self.assertFalse(hasattr(form, "tree"))
        self.assertEqual(form.var_price_mode.get(), "Birim fiyat")
        form.entry_amount.insert(0, "2")
        form.entry_total.insert(0, "140")
        self.wait_for_preview(lambda: form.preview_total.get() == "₺280,00")
        self.assertEqual(form.preview_cost.get(), "₺120,00 / g")
        self.assertEqual(self.manager.get_average_cost("gumus_tl"), 100.0)

        form.var_price_mode.set("Toplam tutar")
        form._on_price_mode()
        self.assertEqual(float(form.entry_total.get()), 280.0)
        form.save()
        self.assertFalse(form.winfo_exists())
        self.assertEqual(self.saves, [True])
        reopened = TransactionManager(self.filename)
        self.assertEqual(reopened.transactions[-1]["total_tl"], 280.0)
        self.assertEqual(reopened.get_average_cost("gumus_tl"), 120.0)

    def test_edit_keeps_removed_asset_and_precise_saved_usd_amount(self):
        self.manager.save({
            "instrument_key": "retired", "instrument_label": "Eski Hisse", "action": "buy",
            "quantity": 1.1256789, "currency": "USD", "fx_rate": 30.8934567,
            "total_usd": 8.1234597, "total_tl": 250.95677891,
        })
        form = self.open()
        form.start_edit("0")
        self.assertEqual(form.var_price_mode.get(), "Toplam tutar")
        self.assertEqual(float(form.entry_rate.get()), 30.8934567)
        form.entry_date.delete(0, "end")
        form.entry_date.insert(0, "01-01-2026")
        form.save()
        saved = TransactionManager(self.filename).transactions[0]
        self.assertEqual(saved["instrument_key"], "retired")
        self.assertEqual(saved["quantity"], 1.1256789)
        self.assertEqual(saved["total_usd"], 8.1234597)
        self.assertEqual(saved["total_tl"], 250.95677891)

    def test_preview_rejects_oversale_before_saving(self):
        form = self.open()
        form.var_action.set("sell")
        form.entry_amount.insert(0, "5")
        form.entry_total.insert(0, "10")
        self.wait_for_preview(lambda: bool(form.preview_error.get()))
        self.assertIn("mevcut miktar", form.preview_error.get())
        with patch("main.messagebox.showerror") as error:
            form.save()
        self.assertTrue(form.winfo_exists())
        error.assert_called_once()
        self.assertEqual(self.manager.transactions, [])


if __name__ == "__main__":
    unittest.main()
