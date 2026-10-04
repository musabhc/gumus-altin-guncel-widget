"""Focused, isolated smoke checks for the Tk presentation layer.

The tests deliberately redirect ``main.APP_DIR`` to a temporary directory
before constructing application widgets.  They also disable the market-data
loop, so running this module cannot read, migrate, or overwrite the user's
live portfolio files.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


def _disabled_data_loop(_widget):
    """Replacement thread target used while constructing ``PiyasaWidget``."""


class IsolatedTkSmokeTest(unittest.TestCase):
    """Build real widgets while keeping all persistence in a disposable tree."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_app_dir = main.APP_DIR
        self.original_data_loop = main.PiyasaWidget.veri_dongusu
        self.app = None
        self.dialog = None
        self.root = None

        main.APP_DIR = self.temp_dir.name
        main.PiyasaWidget.veri_dongusu = _disabled_data_loop

        # Cleanups are LIFO: windows/database first, globals next, temporary
        # files last.  This order also applies when a test is skipped.
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self._restore_globals)
        self.addCleanup(self._cleanup_gui)

    def _restore_globals(self):
        main.PiyasaWidget.veri_dongusu = self.original_data_loop
        main.APP_DIR = self.original_app_dir

    @staticmethod
    def _destroy(widget):
        if widget is None:
            return
        try:
            if widget.winfo_exists():
                widget.destroy()
        except tk.TclError:
            pass

    def _cleanup_gui(self):
        if self.dialog is not None:
            self._destroy(self.dialog)

        if self.app is not None:
            stop_event = getattr(self.app, "_stop_event", None)
            if stop_event is not None:
                stop_event.set()

            update_thread = getattr(self.app, "update_thread", None)
            if update_thread is not None and update_thread.is_alive():
                update_thread.join(timeout=1)

            history_db = getattr(self.app, "history_db", None)
            connection = getattr(history_db, "conn", None)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

            self._destroy(getattr(self.app, "root", None))

        self._destroy(self.root)

    def _require_tk(self):
        """Skip cleanly when Tcl/Tk cannot connect to the current desktop."""

        probe = None
        try:
            probe = tk.Tk()
            probe.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk arayüzü bu ortamda kullanılamıyor: {exc}")
        finally:
            self._destroy(probe)

    def assert_temp_path(self, path):
        temp_root = os.path.realpath(self.temp_dir.name)
        candidate = os.path.realpath(path)
        self.assertEqual(os.path.commonpath((temp_root, candidate)), temp_root)

    def _build_piyasa_widget(self):
        # Assign before __init__ so cleanup can recover a partially built root.
        self.app = main.PiyasaWidget.__new__(main.PiyasaWidget)
        main.PiyasaWidget.__init__(self.app)
        self.app.root.withdraw()
        self.app.root.update_idletasks()
        return self.app

    def test_piyasa_widget_builds_hidden_with_required_state(self):
        self._require_tk()

        self._build_piyasa_widget()

        self.assertEqual(self.app.root.state(), "withdrawn")
        self.assertEqual(
            self.app.pages,
            [self.app.page_main, self.app.page_chart, self.app.page_stats],
        )
        self.assertEqual(len(self.app.pages), 3)
        self.assertEqual(self.app.current_page, 0)

        required_state = (
            "watchlist",
            "price_row_widgets",
            "chart_var",
            "chart_period",
            "chart_canvas",
            "chart_symbol_frame",
            "stats_canvas",
            "stats_frame",
            "_fetch_lock",
            "_stop_event",
        )
        for attribute in required_state:
            with self.subTest(attribute=attribute):
                self.assertTrue(hasattr(self.app, attribute), attribute)

        self.assertIsInstance(self.app.chart_var, tk.StringVar)
        self.assertIsInstance(self.app.chart_period, tk.IntVar)
        self.assertIsInstance(self.app.chart_canvas, tk.Canvas)
        self.assertGreaterEqual(len(self.app.watchlist), 1)
        self.assertEqual(
            set(self.app.price_row_widgets),
            {instrument["key"] for instrument in self.app.watchlist},
        )

        # The constructor is allowed to initialize persistence, but only under
        # the temporary APP_DIR selected in setUp.
        self.assert_temp_path(self.app.tm.filename)
        self.assert_temp_path(self.app.watchlist_manager.filepath)
        self.assert_temp_path(self.app.history_db.db_path)
        self.assertTrue(os.path.exists(self.app.history_db.db_path))

    def test_price_row_short_click_opens_selected_chart(self):
        self._require_tk()
        self._build_piyasa_widget()

        selected_key = self.app.watchlist[-1]["key"]
        click = SimpleNamespace(x_root=120, y_root=240)

        self.assertEqual(self.app.ROW_DRAG_HOLD_MS, 1000)
        with patch.object(
            self.app.root,
            "after",
            wraps=self.app.root.after,
        ) as schedule:
            self.assertIsNone(self.app._on_price_row_press(click, selected_key))
        self.assertEqual(schedule.call_args.args[0], 1000)
        self.assertIsNotNone(self.app._row_hold_after_id)
        self.assertEqual(
            self.app._on_price_row_release(click, selected_key),
            "break",
        )

        self.assertEqual(self.app.chart_var.get(), selected_key)
        self.assertEqual(self.app.current_page, 1)
        self.assertIsNone(self.app._row_hold_after_id)
        self.assertIsNone(self.app._row_press_key)

    def test_price_row_early_motion_does_not_open_chart(self):
        self._require_tk()
        self._build_piyasa_widget()

        selected_key = self.app.watchlist[-1]["key"]
        initial_chart_key = self.app.chart_var.get()
        press = SimpleNamespace(x_root=120, y_root=240)
        moved = SimpleNamespace(x_root=127, y_root=240)

        self.assertIsNone(self.app._on_price_row_press(press, selected_key))
        self.assertIsNone(self.app._on_price_row_motion(moved, selected_key))
        self.assertIsNone(self.app._row_press_key)
        self.assertIsNone(self.app._on_price_row_release(moved, selected_key))

        self.assertEqual(self.app.chart_var.get(), initial_chart_key)
        self.assertEqual(self.app.current_page, 0)

    def test_price_row_long_press_reorder_persists(self):
        self._require_tk()
        self._build_piyasa_widget()

        original_order = [item["key"] for item in self.app.watchlist]
        dragged_key = original_order[0]
        final_order = original_order[1:] + [dragged_key]
        press = SimpleNamespace(x_root=120, y_root=240)

        self.assertIsNone(self.app._on_price_row_press(press, dragged_key))
        self.app.root.after_cancel(self.app._row_hold_after_id)
        self.app._row_hold_after_id = None
        self.app._activate_price_row_drag(dragged_key)
        self.assertEqual(self.app._row_drag_key, dragged_key)

        self.app._row_drag_order = final_order
        self.assertEqual(
            self.app._on_price_row_release(press, dragged_key),
            "break",
        )

        self.assertEqual(
            [item["key"] for item in self.app.watchlist],
            final_order,
        )
        reloaded = main.WatchlistManager(self.app.watchlist_manager.filepath)
        self.assertEqual(
            [item["key"] for item in reloaded.instruments],
            final_order,
        )
        self.assertEqual(self.app.current_page, 0)

    def test_portfolio_dialog_search_filter_and_source_indices(self):
        self._require_tk()
        self.root = tk.Tk()
        self.root.withdraw()

        transaction_path = os.path.join(self.temp_dir.name, "transactions.json")
        manager = main.TransactionManager(transaction_path)
        instruments = main.default_watchlist() + [
            {
                "key": "aapl",
                "label": "Apple",
                "symbol": "AAPL",
                "currency": "$",
                "decimals": 2,
                "color": "#3B82F6",
                "source": "direct",
            }
        ]
        manager.save(
            {
                "date": "01-01-2026",
                "instrument_key": "gumus_tl",
                "instrument_label": "Gümüş TL",
                "action": "buy",
                "quantity": 12.5,
                "currency": "TL",
                "total_tl": 1000,
            }
        )
        manager.save(
            {
                "date": "02-01-2026",
                "instrument_key": "thyao",
                "instrument_label": "THYAO",
                "action": "buy",
                "quantity": 3,
                "currency": "TL",
                "total_tl": 900,
            }
        )
        manager.save(
            {
                "date": "03-01-2026",
                "instrument_key": "aapl",
                "instrument_label": "Apple",
                "action": "buy",
                "quantity": 1,
                "currency": "USD",
                "total_usd": 100,
                "fx_rate": 40,
                "total_tl": 4000,
            }
        )
        original_transactions = copy.deepcopy(manager.transactions)
        callback_calls = []

        self.dialog = main.PortfolioManagerDialog(
            self.root,
            manager,
            lambda: callback_calls.append(True),
            40.0,
            instruments,
        )
        self.dialog.withdraw()

        self.assert_temp_path(manager.filename)
        self.assertEqual(len(self.dialog.tree.rows), 3)
        self.assertEqual(
            [row["source_index"] for row in self.dialog.tree.rows],
            [0, 1, 2],
        )
        self.assertEqual(set(self.dialog.tree._row_widgets), {0, 1, 2})

        raw_filter_values = self.dialog.combo_asset_filter.cget("values")
        filter_values = (
            tuple(self.dialog.tk.splitlist(raw_filter_values))
            if isinstance(raw_filter_values, str)
            else tuple(raw_filter_values)
        )
        self.assertEqual(filter_values[0], self.dialog.ALL_ASSETS_LABEL)
        self.assertIn("Gümüş TL", filter_values)
        self.assertIn("THYAO", filter_values)
        self.assertIn("Apple", filter_values)

        # StringVar traces call load_list synchronously.  Calling it explicitly
        # keeps this assertion stable even if the UI later debounces typing.
        self.dialog.var_search.set("THYAO")
        self.dialog.load_list()
        self.assertEqual(
            [row["source_index"] for row in self.dialog.tree.rows],
            [1],
        )
        self.assertEqual(set(self.dialog.tree._row_widgets), {1})
        self.assertEqual(self.dialog.var_result_count.get(), "1 / 3 işlem")

        # Editing through the preserved source index proves that a filtered
        # display row does not accidentally address display position zero.
        source_index = self.dialog.tree.rows[0]["source_index"]
        self.dialog.start_edit(str(source_index))
        self.assertEqual(self.dialog.edit_index, 1)
        self.assertEqual(self.dialog.var_instrument.get(), "THYAO")
        self.dialog.cancel_edit()

        self.dialog.var_search.set("")
        self.dialog.var_asset_filter.set("Apple")
        self.dialog.load_list()
        self.assertEqual(
            [row["source_index"] for row in self.dialog.tree.rows],
            [2],
        )
        self.assertEqual(set(self.dialog.tree._row_widgets), {2})
        self.assertEqual(self.dialog.var_result_count.get(), "1 / 3 işlem")

        # The smoke test never invokes a delete callback.  Construction,
        # searching, filtering and edit-form population are all non-mutating.
        self.assertEqual(manager.transactions, original_transactions)
        self.assertEqual(callback_calls, [])

    def test_watchlist_dialog_builds_with_preserved_callbacks(self):
        self._require_tk()
        self.root = tk.Tk()
        self.root.withdraw()
        manager = main.WatchlistManager(
            os.path.join(self.temp_dir.name, "watchlist.json")
        )
        callback_calls = []
        self.dialog = main.WatchlistDialog(
            self.root,
            manager,
            lambda: callback_calls.append(True),
            lambda _symbol: 1.0,
        )
        self.dialog.withdraw()
        self.dialog.update_idletasks()

        self.assertEqual(
            set(self.dialog.tree.get_children()),
            {instrument["key"] for instrument in manager.instruments},
        )
        self.assertIsInstance(self.dialog.btn_add, main.HoverButton)
        self.assertEqual(self.dialog.btn_add.cget("state"), "normal")
        self.assertEqual(str(self.dialog.combo_currency.cget("state")), "readonly")
        self.dialog._finish_add("Apple", "AAPL", "$", None)
        self.assertTrue(manager.has_symbol("AAPL"))
        self.assertIn("aapl", self.dialog.tree.get_children())
        self.assertEqual(callback_calls, [True])
        self.assert_temp_path(manager.filepath)

    def test_portfolio_dialog_add_edit_delete_persists_in_temp_storage(self):
        self._require_tk()
        self.root = tk.Tk()
        self.root.withdraw()
        transaction_path = os.path.join(self.temp_dir.name, "transactions.json")
        manager = main.TransactionManager(transaction_path)
        manager.save(
            {
                "date": "01-01-2026",
                "instrument_key": "thyao",
                "instrument_label": "THYAO",
                "action": "buy",
                "quantity": 10,
                "currency": "TL",
                "total_tl": 3000,
            }
        )
        callback_calls = []
        self.dialog = main.PortfolioManagerDialog(
            self.root,
            manager,
            lambda: callback_calls.append(True),
            40.0,
            main.default_watchlist(),
        )
        self.dialog.withdraw()

        self.dialog.var_instrument.set("THYAO")
        self.dialog.update_amount_label()
        self.dialog.entry_date.delete(0, "end")
        self.dialog.entry_date.insert(0, "02-01-2026")
        self.dialog.entry_amount.insert(0, "2")
        self.dialog.entry_total.insert(0, "600")
        self.dialog.save()
        self.assertEqual(len(manager.transactions), 2)
        self.assertEqual(manager.normalize_transaction(manager.transactions[1])["quantity"], 2)
        self.assertEqual(callback_calls, [True])

        self.dialog.start_edit("1")
        self.dialog.entry_amount.delete(0, "end")
        self.dialog.entry_amount.insert(0, "3")
        self.dialog.entry_total.delete(0, "end")
        self.dialog.entry_total.insert(0, "900")
        self.dialog.save()
        self.assertEqual(len(manager.transactions), 2)
        self.assertEqual(manager.normalize_transaction(manager.transactions[1])["quantity"], 3)
        self.assertEqual(callback_calls, [True, True])

        with patch.object(main.tk.messagebox, "askyesno", return_value=True):
            self.dialog.delete_transaction("1")
        self.assertEqual(len(manager.transactions), 1)
        self.assertEqual(callback_calls, [True, True, True])
        with open(transaction_path, "r", encoding="utf-8") as transaction_file:
            saved = json.load(transaction_file)
        self.assertEqual(len(saved), 1)
        self.assertEqual(manager.normalize_transaction(saved[0])["quantity"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
