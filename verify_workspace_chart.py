"""Regression coverage for the detailed desktop chart."""

from datetime import datetime, timedelta
from threading import Event
import time
import tkinter as tk
import unittest

from workspace_chart import DAY, WorkspaceChart


class TestWorkspaceChart(unittest.TestCase):
    base = datetime(2026, 1, 1)

    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk is unavailable: {error}")
        self.addCleanup(self.root.destroy)
        self.root.attributes("-alpha", 0.0)
        self.root.geometry("600x450")

    def chart(self, loader):
        chart = WorkspaceChart(self.root, loader)
        chart.pack(fill="both", expand=True)
        self.root.update()
        return chart

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(.01)
        self.fail("Chart did not reach its expected state")

    def test_longer_windows_use_daily_history_and_preserve_live_quote(self):
        calls = []
        rows = [(self.base + timedelta(days=index), 100 + index) for index in range(200)]

        def load(key, period):
            calls.append((key, period))
            return rows, "TEST.IS", "1d"

        chart = self.chart(load)
        chart.set_instrument(
            {"key": "test", "label": "Test", "currency": "₺"},
            price=301.25, quote_status="Son kayıt · 17:25",
        )
        self.wait_for(lambda: chart._plot is not None)
        self.assertEqual(chart.quote.cget("text"), "₺301,25")
        self.assertEqual(calls, [("test", 30 * DAY)])
        self.assertIn("Günlük", chart.source.cget("text"))
        self.assertIn("TEST.IS", chart.source.cget("text"))
        self.assertEqual(chart.quote_meta.cget("text"), "Son fiyat · Son kayıt · 17:25")

        chart.period_control.select(90 * DAY)
        self.wait_for(lambda: len(chart._series) == 91 and chart._plot is not None)
        self.assertEqual(calls[-1], ("test", 0))
        self.assertEqual(chart._series[0], (self.base + timedelta(days=109), 209.0))
        stats = chart.range_stats.cget("text")
        chart.set_quote(303.5)
        self.assertEqual(chart.quote.cget("text"), "₺303,50")
        self.assertEqual(chart.range_stats.cget("text"), stats)

        chart._zoom(True)
        self.wait_for(lambda: chart._plot and len(chart._plot[0]) < 91)
        chart._keyboard_point(-1)
        self.assertTrue(chart.canvas.find_withtag("hover"))
        chart.reset_view()
        self.wait_for(lambda: chart._plot and len(chart._plot[0]) == 91)

    def test_switching_assets_ignores_the_previous_in_flight_read(self):
        gate = Event()
        self.addCleanup(gate.set)

        def load(key, _period):
            if key == "first":
                gate.wait(2)
                return [(self.base, 1)], "FIRST", "live"
            return [(self.base, 20), (self.base + timedelta(days=1), 22)], "SI=F", "1d"

        chart = self.chart(load)
        chart.set_instrument({"key": "first", "label": "İlk", "currency": "₺"})
        chart.set_instrument({"key": "silver", "label": "Gümüş", "currency": "$", "source": "silver_spot"})
        gate.set()
        self.wait_for(lambda: chart._loading is None and chart._plot is not None)
        self.assertEqual(chart.heading.cget("text"), "Gümüş")
        self.assertEqual(chart.quote.cget("text"), "$22,00")
        self.assertIn("SI=F", chart.source.cget("text"))


if __name__ == "__main__":
    unittest.main()
