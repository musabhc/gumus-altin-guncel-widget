"""Interactive Tk price chart backed by the application's local history."""

from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
import math
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from rendering import render_chart_line
from theme import THEME, font
from ui_components import IconButton, SegmentedControl


DAY = 24 * 60 * 60
PERIODS = (("30 dk", 1800), ("1 sa", 3600), ("6 sa", 21600), ("12 sa", 43200),
           ("24 sa", DAY), ("7 gün", 7 * DAY), ("30 gün", 30 * DAY),
           ("90 gün", 90 * DAY), ("1 yıl", 365 * DAY), ("Tümü", 0))
PERIOD_LABELS = {value: label for label, value in PERIODS}
INTERVAL_LABELS = {"5m": "5 dk", "1d": "Günlük", "live": "Kaydedilmiş fiyat"}
TR_DIGITS = str.maketrans(",.", ".,")


def _parse_time(value):
    try:
        timestamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if timestamp.tzinfo is not None:
        return timestamp.astimezone().replace(tzinfo=None)
    return timestamp


def _positive_price(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _format_price(instrument, value):
    decimals = int(instrument.get("decimals", 2))
    amount = f"{value:,.{decimals}f}".translate(TR_DIGITS)
    return f"{instrument.get('currency', '')}{amount}"


def _keep_plot_extremes(rows, max_points):
    if len(rows) <= max_points:
        return rows
    interior = rows[1:-1]
    buckets = max(1, (max_points - 2) // 2)
    stride = (len(interior) + buckets - 1) // buckets
    sampled = [rows[0]]
    for offset in range(0, len(interior), stride):
        bucket = interior[offset:offset + stride]
        low = min(bucket, key=lambda row: row[1])
        high = max(bucket, key=lambda row: row[1])
        sampled.extend(sorted({low, high}))
    sampled.append(rows[-1])
    return sampled


class WorkspaceChart(tk.Frame):
    """Price chart whose history provider returns rows, source and interval."""

    def __init__(self, parent, load_history, *, parse_timestamp=_parse_time,
                 downsample=_keep_plot_extremes, format_price=_format_price,
                 compact=False, initial_period=30 * DAY, on_period_changed=None):
        super().__init__(parent, bg=THEME["surface"], highlightthickness=1,
                         highlightbackground=THEME["border"])
        self._load_history = load_history
        self._parse_timestamp = parse_timestamp
        self._downsample = downsample
        self._format_price = format_price
        self._compact = compact
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workspace-history")
        self._loading = None
        self._load_job = None
        self._frame_job = None
        self._settle_job = None
        self._destroyed = False
        self.instrument = None
        self._quote = None
        self._quote_status = ""
        self._on_period_changed = on_period_changed
        self._series = ()
        self._times = ()
        self._view = (0.0, 1.0)
        self._drag_origin = None
        self._frame = None
        self._image = None
        self._plot = None
        self._hover_index = None
        self._message = "Bir varlık seç"
        self._history_meta = ""
        self.period = tk.IntVar(master=self, value=initial_period)

        top = tk.Frame(self, bg=THEME["surface"])
        self.heading = self._label(top, "Fiyat grafiği", "section", weight="bold")
        if not compact:
            top.pack(fill="x", padx=18, pady=(14, 6))
            self.heading.pack(fill="x")
        value_row = tk.Frame(self, bg=THEME["surface"])
        value_row.pack(fill="x", padx=12 if compact else 18, pady=(4, 0) if compact else 0)
        self.quote = self._label(value_row, "—", "section" if compact else "value", weight="bold")
        self.quote.pack(side="left")
        self.change = self._label(value_row, "", "body", "text_secondary")
        self.change.pack(side="right")
        self.quote_meta = self._label(self, "", "small", "text_secondary")
        self.quote_meta.pack(fill="x", padx=12 if compact else 18)

        self.canvas = tk.Canvas(self, height=108 if compact else 200, bd=0, highlightthickness=0,
                                bg=THEME["surface"], takefocus=True)
        self.canvas.bind("<Configure>", self._queue_draw)
        self.canvas.bind("<Map>", self._queue_draw)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", self._hide_hover)
        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._pan)
        self.canvas.bind("<ButtonRelease-1>", self._end_drag)
        self.canvas.bind("<Double-Button-1>", self.reset_view)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Left>", lambda event: self._keyboard_point(-1))
        self.canvas.bind("<Right>", lambda event: self._keyboard_point(1))
        self.canvas.bind("<plus>", lambda event: self._zoom(True))
        self.canvas.bind("<minus>", lambda event: self._zoom(False))

        controls = tk.Frame(self, bg=THEME["surface"])
        controls.pack(side="bottom", fill="x", padx=12 if compact else 14, pady=(0, 3) if compact else (2, 6))
        self.source = self._label(self, "", "small", "text_secondary")
        self.source.pack(side="bottom", fill="x", padx=12 if compact else 18)
        self.period_control = None
        self.period_choice = None
        if compact:
            self.period_choice = ttk.Combobox(
                controls, state="readonly", style="Modern.TCombobox",
                width=9, values=tuple(label for label, _ in PERIODS),
            )
            self.period_choice.set(PERIOD_LABELS[initial_period])
            self.period_choice.bind("<<ComboboxSelected>>", self._select_compact_period)
            self.period_choice.pack(side="left")
        else:
            self.period_control = SegmentedControl(
                controls, self.period, PERIODS, command=self._select_period, columns=5,
            )
            self.period_control.pack(side="left")
        self.zoom_buttons = {}
        if not compact:
            for name, callback in (("reset", self.reset_view), ("plus", lambda: self._zoom(True)),
                                   ("minus", lambda: self._zoom(False))):
                button = IconButton(controls, name, callback, size=30, bg=THEME["surface"])
                button.pack(side="right", padx=(4, 0))
                self.zoom_buttons[name] = button

        self.range_stats = self._label(self, "", "small", "text_secondary")
        if not compact:
            self.range_stats.pack(side="bottom", fill="x", padx=18, pady=(0, 7))
        self.canvas.pack(fill="both", expand=True, padx=12, pady=(3, 0))
        self._update_labels()

    def _label(self, parent, text, role, color="text_primary", weight="normal", anchor="w"):
        return tk.Label(parent, text=text, bg=THEME["surface"], fg=THEME[color],
                        font=font(self, role, weight), anchor=anchor)

    def set_instrument(self, instrument, *, price=None, quote_status=""):
        old = self.instrument
        self.instrument = dict(instrument) if instrument else None
        self.set_quote(price, quote_status)
        self.heading.configure(text=(self.instrument or {}).get("label", "Fiyat grafiği"))
        if self.instrument != old:
            self._reset_series("Grafik yükleniyor…" if self.instrument else "Bir varlık seç")
            self.refresh()

    def set_quote(self, price, status=""):
        self._quote = _positive_price(price)
        self._quote_status = status
        self._update_labels()

    def set_period(self, period):
        if period not in PERIOD_LABELS or self.period.get() == period:
            return
        self.period.set(period)
        if self.period_choice is not None:
            self.period_choice.set(PERIOD_LABELS[period])
        self._period_changed()

    def _select_period(self):
        self._period_changed()
        if self._on_period_changed is not None:
            self._on_period_changed(self.period.get())

    def _select_compact_period(self, _event):
        period = dict(PERIODS)[self.period_choice.get()]
        self.set_period(period)
        if self._on_period_changed is not None:
            self._on_period_changed(period)

    def _period_changed(self):
        self._reset_series("Grafik yükleniyor…" if self.instrument else "Bir varlık seç")
        self.refresh()

    def _reset_series(self, message):
        self._series = ()
        self._times = ()
        self._view = (0.0, 1.0)
        self._drag_origin = None
        self._message = message
        self._history_meta = ""
        self._frame = None
        self._update_labels(())
        self._queue_draw()

    def _selection(self):
        return (self.instrument["key"], self.period.get()) if self.instrument else None

    def refresh(self):
        """Refresh the selected chart with one local read at a time."""
        selection = self._selection()
        if selection is None or self._loading is not None or self._destroyed:
            return
        future = self._worker.submit(self._read_series, *selection)
        self._loading = selection, future
        self._load_job = self.after(25, self._accept_series)

    def _read_series(self, key, period):
        # Longer ranges use the existing daily source. A 5-minute profile may
        # cover only recent history and would mislabel a 1-year graph.
        rows, source, interval = self._load_history(key, period if period <= 30 * DAY else 0)
        by_time = {}
        for value_time, raw_value in rows or ():
            timestamp = self._parse_timestamp(value_time)
            value = _positive_price(raw_value)
            if timestamp is not None and value is not None:
                by_time[timestamp] = value
        series = sorted(by_time.items())
        if series and period > 30 * DAY:
            cutoff = series[-1][0] - timedelta(seconds=period)
            series = series[bisect_left(series, cutoff, key=lambda row: row[0]):]
        return tuple(series), source, interval

    def _accept_series(self):
        self._load_job = None
        selection, future = self._loading
        if not future.done():
            self._load_job = self.after(25, self._accept_series)
            return
        self._loading = None
        if selection != self._selection():
            self.refresh()
            return
        try:
            series, source, interval = future.result()
        except Exception:
            logging.getLogger(__name__).exception("Could not read the local chart history")
            if not self._series:
                self._message = "Grafik verisi okunamadı"
                self._frame = None
                self._draw()
            return

        self._set_history_meta(series, source, interval)
        if series == self._series and series:
            return
        self._series = series
        self._times = tuple(row[0] for row in series)
        self._message = "Bu aralık için henüz veri yok"
        self._frame = None
        if len(series) < 3:
            self._view = (0.0, 1.0)
        self._update_labels()
        self._draw()

    def _set_history_meta(self, series, source, interval):
        parts = []
        if series:
            if source == "SI=F" and self.instrument.get("source") in {"silver_spot", "silver_spot_try"}:
                parts.append("Vadeli gümüş (SI=F)")
            elif source:
                parts.append(source)
            if interval in INTERVAL_LABELS:
                parts.append(INTERVAL_LABELS[interval])
            parts.append(series[-1][0].strftime("%d/%m %H:%M"))
        self._history_meta = "Grafik · " + " · ".join(parts) if parts else ""
        self.source.configure(text=self._history_meta)

    def _visible_series(self):
        if len(self._series) <= 2 or self._view == (0.0, 1.0):
            return self._series
        first = self._times[0]
        span = self._times[-1] - first
        start = bisect_left(self._times, first + span * self._view[0])
        end = bisect_right(self._times, first + span * self._view[1])
        if end - start < 2:
            start = max(0, min(start - 1, len(self._series) - 2))
            end = start + 2
        return self._series[start:end]

    def _update_labels(self, rows=None):
        price = self._quote
        if price is None and self._series:
            price = self._series[-1][1]
        self.quote.configure(text=self._format_price(self.instrument, price) if price is not None and self.instrument else "—")
        if self._quote is not None:
            quote_meta = f"Son fiyat · {self._quote_status}" if self._quote_status else "Son fiyat"
        elif self._series:
            quote_meta = f"Grafikten · {self._series[-1][0]:%d/%m %H:%M}"
        else:
            quote_meta = "Fiyat yok" if self.instrument else ""
        self.quote_meta.configure(text=quote_meta)
        self.source.configure(text=self._history_meta)
        if rows is None:
            return
        if not rows:
            self.change.configure(text="")
            self.range_stats.configure(text="")
            return
        delta = (rows[-1][1] / rows[0][1] - 1) * 100
        color = THEME["positive"] if delta >= 0 else THEME["negative"]
        self.change.configure(text=f"Dönem {delta:+.2f}%".replace(".", ","), fg=color)
        minimum = self._format_price(self.instrument, min(row[1] for row in rows))
        maximum = self._format_price(self.instrument, max(row[1] for row in rows))
        self.range_stats.configure(text=f"En düşük  {minimum}    ·    En yüksek  {maximum}")

    def _queue_draw(self, _event=None):
        if self._frame_job is None:
            self._frame_job = self.after(16, self._preview)
        if self._settle_job is not None:
            self.after_cancel(self._settle_job)
        self._settle_job = self.after(105, self._settle)

    def _preview(self):
        self._frame_job = None
        self._draw(preview=True)

    def _settle(self):
        self._settle_job = None
        self._draw()

    def _draw(self, *, preview=False):
        canvas = self.canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if not canvas.winfo_ismapped() or width < 100 or height < (28 if self._compact else 55):
            return
        frame = width, height, self._view, preview
        if self._frame and self._frame[:3] == frame[:3] and (preview or not self._frame[3]):
            return
        self._frame = frame
        self._plot = None
        self._hover_index = None
        canvas.delete("all")
        rows = self._visible_series()
        self._update_labels(rows)
        if not rows:
            self._image = None
            canvas.create_text(width / 2, height / 2, text=self._message,
                               font=font(self, "small" if self._compact else "body"), width=width - 12,
                               fill=THEME["text_secondary"])
            return

        # Compose an opaque image before uploading to Tk. Canvas keeps all text
        # native, preserving clear labels under Windows DPI scaling.
        low = min(row[1] for row in rows)
        high = max(row[1] for row in rows)
        decimals = int(self.instrument.get("decimals", 2))
        margin = max((high - low) * .12, high * .0005, 2 * 10 ** -decimals)
        low, high = low - margin, high + margin
        brief = self._compact and height < 82
        top, bottom = (5, height - 6) if brief else (12, height - 25)
        left, right = 10, width - (10 if brief else min(86, width // 3))
        first, last = rows[0][0], rows[-1][0]
        span = last - first
        color = THEME["positive"] if rows[-1][1] >= rows[0][1] else THEME["negative"]

        def coordinates(row):
            time, value = row
            x = left + (right - left) * ((time - first) / span if span else 0.5)
            y = bottom - (bottom - top) * (value - low) / (high - low)
            return x, y

        sampled = self._downsample(rows, max(100, width if preview else width * 2))
        points = [coordinates(row) for row in sampled]
        background = Image.new("RGB", (width, height), THEME["surface"])
        draw = ImageDraw.Draw(background)
        tick_rows = [top + (bottom - top) * tick / 4 for tick in range(5)]
        for y in tick_rows:
            for x in range(left, right, 8):
                draw.line((x, round(y), min(x + 3, right), round(y)), fill=THEME["border"])
        line = render_chart_line(
            width, height, points, color, stroke=2, endpoint=3, fill_to=bottom, preview=preview,
        )
        background.paste(line, (0, 0), line.getchannel("A"))
        self._image = ImageTk.PhotoImage(background, master=canvas)
        canvas.create_image(0, 0, image=self._image, anchor="nw")
        if not brief:
            tick_font = font(self, "small")
            for index, y in enumerate(tick_rows):
                tick_value = high - (high - low) * index / 4
                label = f"{tick_value:,.{decimals}f}".translate(TR_DIGITS)
                canvas.create_text(right + 8, y, anchor="w", text=label,
                                   fill=THEME["text_muted"], font=tick_font)
            time_format = "%H:%M" if span <= timedelta(days=2) else "%d/%m" if span <= timedelta(days=90) else "%m/%Y"
            num_labels = min(5, len(rows), max(2, (right - left) // 75))
            for index in range(num_labels):
                fraction = index / (num_labels - 1) if num_labels > 1 else 0.5
                anchor = "w" if index == 0 else "e" if index == num_labels - 1 else "center"
                canvas.create_text(left + (right - left) * fraction, height - 10,
                                   text=(first + span * fraction).strftime(time_format),
                                   anchor=anchor, fill=THEME["text_muted"], font=tick_font)
        self._plot = (rows, tuple(row[0] for row in rows), coordinates, left, top, right, bottom, color)
        canvas.configure(cursor="hand2" if self._view != (0.0, 1.0) else "crosshair")

    def _motion(self, event):
        if not self._plot or self._drag_origin is not None:
            return
        rows, times, _coordinates, left, top, right, bottom, _color = self._plot
        if not (left <= event.x <= right and top <= event.y <= bottom):
            self._hide_hover()
            return
        when = times[0] + (times[-1] - times[0]) * ((event.x - left) / (right - left))
        index = min(len(rows) - 1, bisect_left(times, when))
        if index and when - times[index - 1] < times[index] - when:
            index -= 1
        self._show_point(index)

    def _keyboard_point(self, direction):
        if self._plot:
            index = self._hover_index if self._hover_index is not None else len(self._plot[0]) - 1
            self._show_point(max(0, min(index + direction, len(self._plot[0]) - 1)))
        return "break"

    def _hide_hover(self, _event=None):
        self._hover_index = None
        self.canvas.delete("hover")

    def _show_point(self, index):
        if self._hover_index == index:
            return
        rows, _times, coordinates, left, top, right, bottom, color = self._plot
        self._hide_hover()
        self._hover_index = index
        time, value = rows[index]
        x, y = coordinates(rows[index])
        canvas = self.canvas
        canvas.create_line(x, top, x, bottom, dash=(3, 4), fill=THEME["border_strong"], tags="hover")
        canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline=THEME["surface"], tags="hover")
        label = f"{time:%d/%m %H:%M}  ·  {self._format_price(self.instrument, value)}"
        xpos = min(max(left + 8, x + 8), max(left + 8, right - 122))
        text = canvas.create_text(xpos, top + 11, text=label, anchor="w", tags="hover",
                                  fill=THEME["text_primary"], font=font(self, "small", "bold"))
        x1, y1, x2, y2 = canvas.bbox(text)
        box = canvas.create_rectangle(x1 - 5, y1 - 3, x2 + 5, y2 + 3, tags="hover",
                                      fill=THEME["surface_alt"], outline=THEME["border_strong"])
        canvas.tag_lower(box, text)

    def _change_view(self, start, span):
        start = max(0.0, min(1.0 - span, start))
        view = (start, min(1.0, start + span))
        if view != self._view:
            self._view = view
            self._hide_hover()
            self._queue_draw()

    def reset_view(self, _event=None):
        self._drag_origin = None
        self._change_view(0.0, 1.0)
        return "break"

    def _zoom(self, inward, anchor=0.5):
        if len(self._series) >= 3:
            start, end = self._view
            span = end - start
            min_span = max(.01, 2 / max(2, len(self._series) - 1))
            new_span = max(min_span, min(1.0, span * (0.65 if inward else 1 / .65)))
            self._change_view(start + (span - new_span) * anchor, new_span)
        return "break"

    def _wheel(self, event):
        if self._plot and event.delta:
            _rows, _times, _coordinates, left, _top, right, _bottom, _color = self._plot
            return self._zoom(event.delta > 0, max(0.0, min(1.0, (event.x - left) / (right - left))))
        return "break"

    def _start_drag(self, event):
        self.canvas.focus_set()
        if self._plot and self._view != (0.0, 1.0):
            self._drag_origin = (event.x, *self._view)
        return "break"

    def _pan(self, event):
        if self._drag_origin and self._plot:
            origin, start, end = self._drag_origin
            _rows, _times, _coordinates, left, _top, right, _bottom, _color = self._plot
            span = end - start
            self._change_view(start - (event.x - origin) * span / (right - left), span)
        return "break"

    def _end_drag(self, _event):
        self._drag_origin = None
        return "break"

    def destroy(self):
        self._destroyed = True
        for job in (self._load_job, self._frame_job, self._settle_job):
            if job is not None:
                self.after_cancel(job)
        self._worker.shutdown(wait=False, cancel_futures=True)
        super().destroy()
