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
                # Tk's after queue outlives the Python callbacks destroyed with
                # each test window. Cancel it before discarding the interpreter.
                for after_id in widget.tk.call("after", "info"):
                    widget.after_cancel(after_id)
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

        main.enable_high_dpi()
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

        self.assertEqual(self.app.ROW_DRAG_HOLD_MS, 350)
        with patch.object(
            self.app.root,
            "after",
            wraps=self.app.root.after,
        ) as schedule:
            self.assertEqual(self.app._on_price_row_press(click, selected_key), "break")
        self.assertEqual(schedule.call_args.args[0], 350)
        self.assertIsNotNone(self.app._row_hold_after_id)
        self.assertEqual(
            self.app._on_price_row_release(click, selected_key),
            "break",
        )

        self.assertEqual(self.app.chart_var.get(), selected_key)
        self.assertEqual(self.app.current_page, 1)
        self.assertIsNone(self.app._row_hold_after_id)
        self.assertIsNone(self.app._row_press_key)

    def test_price_row_early_motion_starts_reordering_without_moving_window(self):
        self._require_tk()
        self._build_piyasa_widget()

        selected_key = self.app.watchlist[-1]["key"]
        initial_chart_key = self.app.chart_var.get()
        press = SimpleNamespace(x_root=120, y_root=240)
        moved = SimpleNamespace(x_root=127, y_root=240)

        original_geometry = self.app.root.geometry()
        self.assertEqual(self.app._on_price_row_press(press, selected_key), "break")
        self.assertEqual(self.app._on_price_row_motion(moved, selected_key), "break")
        self.assertEqual(self.app._row_drag_key, selected_key)
        self.assertEqual(self.app._on_price_row_release(moved, selected_key), "break")
        self.assertEqual(self.app.root.geometry(), original_geometry)

        self.assertEqual(self.app.chart_var.get(), initial_chart_key)
        self.assertEqual(self.app.current_page, 0)

    def test_price_row_long_press_reorder_persists(self):
        self._require_tk()
        self._build_piyasa_widget()

        original_order = [item["key"] for item in self.app.watchlist]
        dragged_key = original_order[0]
        final_order = original_order[1:] + [dragged_key]
        press = SimpleNamespace(x_root=120, y_root=240)

        self.assertEqual(self.app._on_price_row_press(press, dragged_key), "break")
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

    def test_chart_controls_expose_all_periods_and_interactions(self):
        self._require_tk()
        self._build_piyasa_widget()

        rendered_options = tuple(
            (button.cget("text"), value)
            for value, button in self.app.chart_period_control.buttons.items()
        )
        self.assertEqual(rendered_options, self.app.CHART_PERIOD_OPTIONS)
        self.assertEqual(len(rendered_options), 8)
        self.assertEqual(
            [
                (int(button.grid_info()["row"]), int(button.grid_info()["column"]))
                for button in self.app.chart_period_control.buttons.values()
            ],
            [
                (0, 0), (0, 1), (0, 2), (0, 3),
                (1, 0), (1, 1), (1, 2), (1, 3),
            ],
        )

        for sequence in (
            "<MouseWheel>",
            "<Button-4>",
            "<Button-5>",
            "<ButtonPress-1>",
            "<B1-Motion>",
            "<ButtonRelease-1>",
            "<Double-Button-1>",
        ):
            with self.subTest(sequence=sequence):
                self.assertTrue(self.app.chart_canvas.bind(sequence))

        self.app._chart_full_values = list(range(20))
        self.app._chart_plot_bounds = (20, 10, 220, 180)
        with patch.object(self.app, "_draw_chart") as draw_chart:
            wheel = SimpleNamespace(delta=120, x=120)
            self.assertEqual(self.app._on_chart_mousewheel(wheel), "break")
            self.assertAlmostEqual(self.app._chart_view_start, 0.175)
            self.assertAlmostEqual(self.app._chart_view_end, 0.825)

            self.assertEqual(
                self.app._on_chart_pan_start(SimpleNamespace(x=120)),
                "break",
            )
            before_pan = (
                self.app._chart_view_start,
                self.app._chart_view_end,
            )
            self.assertEqual(
                self.app._on_chart_pan_motion(SimpleNamespace(x=150)),
                "break",
            )
            self.assertNotEqual(
                (self.app._chart_view_start, self.app._chart_view_end),
                before_pan,
            )
            self.assertEqual(self.app._on_chart_pan_end(), "break")
            self.assertIsNone(self.app._chart_pan_origin)

            self.assertEqual(
                self.app.reset_chart_view(SimpleNamespace()),
                "break",
            )
            self.assertEqual(
                (self.app._chart_view_start, self.app._chart_view_end),
                (0.0, 1.0),
            )
            self.assertGreaterEqual(draw_chart.call_count, 3)

    def test_settings_menu_is_a_themeable_popup_with_preserved_actions(self):
        self._require_tk()
        self._build_piyasa_widget()

        self.assertIsInstance(self.app.settings_menu, main.ThemedPopupMenu)
        self.assertIsInstance(self.app.settings_menu, tk.Toplevel)
        self.assertEqual(
            [
                (item["type"], item["label"])
                for item in self.app.settings_menu.items
            ],
            [
                ("command", "Portföyü yönet"),
                ("command", "İşlemleri içe aktar"),
                ("separator", None),
                ("check", "Windows ile başlat"),
                ("check", "Her zaman üstte"),
                ("separator", None),
                ("choice", "Mini grafik zaman aralığı"),
                ("separator", None),
                ("command", "İzlenenleri düzenle"),
                ("command", "Güncellemeleri kontrol et"),
                ("separator", None),
                ("command", "Kapat"),
            ],
        )
        self.assertEqual(
            self.app.settings_menu.cget("bg"),
            main.THEME["border_strong"],
        )
        self.assertEqual(
            self.app.settings_menu.body.cget("bg"),
            main.THEME["surface_alt"],
        )

    def test_all_price_row_surfaces_capture_drag_events(self):
        self._require_tk()
        self._build_piyasa_widget()
        self.app.root.deiconify()
        self.app.root.update()
        key = self.app.watchlist[0]["key"]
        row = self.app.price_row_widgets[key]
        window_events = []
        for sequence in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.app.root.bind(sequence, lambda event: window_events.append(event.type), add="+")
        original_position = (self.app.root.winfo_x(), self.app.root.winfo_y())
        for target in (row, *row.winfo_children()):
            with self.subTest(target=target.winfo_class()):
                x, y = target.winfo_rootx() + 2, target.winfo_rooty() + 2
                target.event_generate("<ButtonPress-1>", x=2, y=2, rootx=x, rooty=y)
                target.event_generate("<B1-Motion>", x=10, y=2, rootx=x + 8, rooty=y)
                self.assertEqual(self.app._row_drag_key, key)
                target.event_generate("<ButtonRelease-1>", x=10, y=2, rootx=x + 8, rooty=y)
                self.assertIsNone(self.app._row_drag_key)
                self.assertEqual((self.app.root.winfo_x(), self.app.root.winfo_y()), original_position)
        self.assertEqual(window_events, [])
        original_order = [item["key"] for item in self.app.watchlist]
        last_row = self.app.price_row_widgets[original_order[-1]]
        target = self.app.price_spark_canvases[key]
        x, y = target.winfo_rootx() + 2, target.winfo_rooty() + 2
        end_y = last_row.winfo_rooty() + last_row.winfo_height() + 5
        target.event_generate("<ButtonPress-1>", x=2, y=2, rootx=x, rooty=y)
        target.event_generate("<B1-Motion>", x=2, y=end_y-y, rootx=x, rooty=end_y)
        target.event_generate("<ButtonRelease-1>", x=2, y=end_y-y, rootx=x, rooty=end_y)
        expected = original_order[1:] + [key]
        self.assertEqual([item["key"] for item in self.app.watchlist], expected)
        reloaded = main.WatchlistManager(self.app.watchlist_manager.filepath)
        self.assertEqual([item["key"] for item in reloaded.instruments], expected)
        self.assertEqual((self.app.root.winfo_x(), self.app.root.winfo_y()), original_position)

    def test_mini_chart_period_applies_to_line_and_change_and_survives_reload(self):
        self._require_tk()
        self._build_piyasa_widget()
        self.assertEqual(self.app.sparkline_period.get(), 86400)
        main_chart_period = self.app.chart_period.get()
        for _label, seconds in self.app.CHART_PERIOD_OPTIONS:
            self.app.sparkline_period_control.select(seconds)
            with patch.object(self.app, "_cached_history_series", return_value=(
                [("2026-10-23 12:00:00", 100), ("2026-10-24 12:00:00", 125)], "test", "5m"
            )) as history:
                self.assertEqual(self.app._get_sparkline_values("thyao", 125), [100, 125])
                self.assertEqual(self.app._get_change_pct("thyao", 125), 25)
                self.assertEqual([call.args for call in history.call_args_list],
                                 [("thyao", seconds), ("thyao", seconds)])
            self.assertEqual(self.app._load_preferences()["sparkline_period"], seconds)
        self.assertEqual(self.app.chart_period.get(), main_chart_period)
        self.assert_temp_path(self.app.preferences_path)
        with open(self.app.preferences_path, "w", encoding="utf-8") as target:
            json.dump({"sparkline_period": 999}, target)
        self.assertEqual(self.app._load_preferences()["sparkline_period"], 86400)

    def test_header_alignment_and_footer_icons(self):
        self._require_tk()
        self._build_piyasa_widget()
        self.app.root.deiconify()
        self.app.root.update()
        for width in (368, 480):
            self.app.root.geometry(f"{width}x600")
            self.app.root.update()
            widgets = (self.app.btn_prev, self.app.lbl_time, self.app.btn_next,
                       self.app.lbl_market_status, self.app.lbl_market_label)
            centers = [widget.winfo_rooty() + widget.winfo_height() / 2 for widget in widgets]
            self.assertLessEqual(max(centers) - min(centers), 1)
        self.assertEqual(set(self.app.footer_buttons), {"settings", "refresh"})
        for button in self.app.footer_buttons.values():
            self.assertIsNone(button.label)
            self.assertEqual(int(button.cget("width")), 32)
            self.assertEqual([button.type(item) for item in button.find_withtag("icon")], ["image"])
        self.app.root.attributes("-topmost", True)
        self.app.open_settings()
        self.app.root.update()
        self.assertTrue(self.app.settings_menu.winfo_viewable())
        self.assertTrue(self.app.settings_menu.attributes("-topmost"))

    def test_navigation_clicks_and_pointer_jitter_never_move_the_window(self):
        self._require_tk()
        self._build_piyasa_widget()
        self.app.root.geometry("368x600+60+60")
        self.app.root.deiconify()
        self.app.root.update()
        initial_position = (self.app.root.winfo_x(), self.app.root.winfo_y())
        # First move the window normally, to leave the old code's stale local
        # coordinates behind before reproducing the navigation gesture.
        frame = self.app.frame
        x, y = frame.winfo_rootx() + 3, frame.winfo_rooty() + 3
        frame.event_generate("<ButtonPress-1>", x=3, y=3, rootx=x, rooty=y)
        frame.event_generate("<B1-Motion>", x=27, y=19, rootx=x+24, rooty=y+16)
        frame.event_generate("<ButtonRelease-1>", x=27, y=19, rootx=x+24, rooty=y+16)
        self.app.root.update()
        expected_position = (initial_position[0]+24, initial_position[1]+16)
        self.assertEqual((self.app.root.winfo_x(), self.app.root.winfo_y()), expected_position)
        for jitter in (0, 2, -2):
            for button, page in ((self.app.btn_next, 1), (self.app.btn_next, 2),
                                 (self.app.btn_prev, 1), (self.app.btn_prev, 0)):
                x, y = button.winfo_rootx()+14, button.winfo_rooty()+14
                button.event_generate("<ButtonPress-1>", x=14, y=14, rootx=x, rooty=y)
                if jitter:
                    button.event_generate("<B1-Motion>", x=14+jitter, y=14,
                                          rootx=x+jitter, rooty=y)
                button.event_generate("<ButtonRelease-1>", x=14+jitter, y=14,
                                      rootx=x+jitter, rooty=y)
                self.app.root.update()
                self.assertEqual(self.app.current_page, page)
                self.assertEqual((self.app.root.winfo_x(), self.app.root.winfo_y()), expected_position)
                self.assertIsNone(self.app._window_move_origin)
        # Dragging off a button cancels activation and still cannot move the app.
        button = self.app.btn_next
        x, y = button.winfo_rootx()+14, button.winfo_rooty()+14
        button.event_generate("<ButtonPress-1>", x=14, y=14, rootx=x, rooty=y)
        button.event_generate("<B1-Motion>", x=34, y=14, rootx=x+20, rooty=y)
        button.event_generate("<ButtonRelease-1>", x=14, y=14, rootx=x, rooty=y)
        self.assertEqual(self.app.current_page, 0)
        self.assertEqual((self.app.root.winfo_x(), self.app.root.winfo_y()), expected_position)

    def test_portfolio_opens_in_front_with_either_widget_topmost_setting(self):
        self._require_tk()
        self._build_piyasa_widget()
        self.app.root.geometry("368x600+80+80")
        self.app.root.deiconify()
        self.app.root.update()
        for topmost in (False, True):
            self.app.root.attributes("-topmost", topmost)
            self.app.open_settings()
            self.app.root.update()
            manage_row = self.app.settings_menu.items[0]["row"]
            manage_row.event_generate("<ButtonRelease-1>", x=10, y=10)
            self.dialog = self.app.portfolio_dialog
            self.dialog.geometry("1120x760+80+80")
            self.app.root.update()
            self.assertFalse(self.app.settings_menu.is_visible())
            self.assertEqual(bool(self.dialog.attributes("-topmost")), topmost)
            self.assertEqual(self.dialog.focus_displayof().winfo_toplevel(), self.dialog)
            covering = self.app.root.winfo_containing(100, 160)
            self.assertIsNotNone(covering)
            self.assertEqual(covering.winfo_toplevel(), self.dialog)
            self.dialog.destroy()
            self.dialog = None

    def test_curves_and_icons_have_antialiased_edges_at_different_sizes(self):
        from rendering import render_icon, render_chart_line

        for size in (24, 32, 48):
            for icon in ("settings", "refresh", "previous", "next", "edit", "delete"):
                artwork = render_icon(icon, size, "#FFFFFF")
                self.assertEqual(artwork.size, (size, size))
                alpha = set(artwork.getchannel("A").getdata())
                self.assertTrue(any(0 < value < 255 for value in alpha))
        curve = render_chart_line(62, 22, [(1, 19), (23.25, 2.5), (60, 14)], "#FFFFFF")
        self.assertTrue(any(0 < value < 255 for value in curve.getchannel("A").getdata()))

    def test_transaction_headers_cycle_sort_and_preserve_record_identity(self):
        self._require_tk()
        self.root = tk.Tk()
        self.root.withdraw()
        manager = main.TransactionManager(os.path.join(self.temp_dir.name, "transactions.json"))
        records = [
            ("31-12-2025", "thyao", "buy", 10, 100),
            ("02-01-2026", "thyao", "buy", 2, 900),
            ("15-01-2026", "gumus_tl", "buy", 100, 20),
            ("02-01-2026", "thyao", "sell", 2, 500),
        ]
        for date, key, action, quantity, total in records:
            manager.save({"date": date, "instrument_key": key, "action": action,
                          "quantity": quantity, "currency": "TL", "total_tl": total})
        self.dialog = main.PortfolioManagerDialog(self.root, manager, lambda: None, 40, main.default_watchlist())
        self.dialog.withdraw()
        self.assertTrue(self.dialog.overrideredirect())
        original = copy.deepcopy(manager.transactions)
        expected = {"date": ([0, 1, 3, 2], [2, 1, 3, 0]),
                    "action": ([0, 1, 2, 3], [3, 0, 1, 2]),
                    "asset": ([2, 0, 1, 3], [0, 1, 3, 2]),
                    "amount": ([1, 3, 0, 2], [2, 0, 1, 3]),
                    "total": ([2, 0, 3, 1], [1, 3, 0, 2])}
        for column, (ascending, descending) in expected.items():
            for order in (ascending, descending, [0, 1, 2, 3]):
                self.dialog.tree.heading_buttons[column].invoke()
                self.assertEqual([row["source_index"] for row in self.dialog.tree.rows], order)
        self.dialog.tree.heading_buttons["amount"].invoke()
        self.dialog.tree.heading_buttons["total"].invoke()
        self.assertEqual(self.dialog.sort_direction, 1)
        self.dialog.start_edit(str(self.dialog.tree.rows[0]["source_index"]))
        self.assertEqual(self.dialog.edit_index, 2)
        self.assertEqual(self.dialog.var_instrument.get(), "Gümüş TL")
        self.dialog.cancel_edit()
        self.dialog.var_asset_filter.set("THYAO")
        self.dialog.load_list()
        self.assertEqual([row["source_index"] for row in self.dialog.tree.rows], [0, 3, 1])
        self.dialog.tree.heading_buttons["total"].invoke()
        self.assertEqual([row["source_index"] for row in self.dialog.tree.rows], [1, 3, 0])
        self.dialog.tree.heading_buttons["total"].invoke()
        self.assertEqual([row["source_index"] for row in self.dialog.tree.rows], [0, 1, 3])
        self.assertEqual(manager.transactions, original)

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
