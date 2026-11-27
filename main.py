import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
import yfinance as yf
import threading
import time
import sys
import json
import sqlite3
import os
import tempfile
import math
import winreg
import ctypes
import requests
import webbrowser
import subprocess
import re
from collections import Counter
from pathlib import Path
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from tkinter import messagebox, filedialog
from queue import Empty, SimpleQueue
from PIL import Image, ImageDraw, ImageTk

from desktop_workspace import DesktopWorkspace, quantity_text
from ledger_storage import decode_records, encode_records, ledger_write_lock, read_snapshot, replace_snapshot
from trade_calendar import TradeCalendar
from market_quotes import (
    merge_observations, normalize_quotes, observation, positive_price,
    quote_status, session_from_metadata,
)
from theme import SPACING, THEME, configure_ttk_styles, enable_high_dpi, font, turkish_number
from rendering import render_asset_ring, render_chart_line
from ui_components import (
    HoverButton,
    IconButton,
    ModernEntry,
    SearchEntry,
    SegmentedControl,
    ThemedPopupMenu,
    TransactionTable,
    monitor_work_area,
)

# Configuration
GITHUB_REPO = "musabhc/gumus-altin-guncel-widget"

try:
    import _version
    VERSION = _version.__version__
except ImportError:
    VERSION = "0.0.0-dev"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TROY_OUNCE_GRAMS = 31.1035
SILVER_SPOT_SYMBOL = "XAG"
SILVER_SPOT_API_URL = "https://api.gold-api.com/price/XAG"
LEGACY_SILVER_FUTURES_SYMBOL = "SI=F"
SILVER_HISTORY_PROXY_SYMBOL = LEGACY_SILVER_FUTURES_SYMBOL
SILVER_SPOT_KEYS = {"gumus_ons", "gumus_tl"}
SILVER_SPOT_SOURCES = {"silver_spot", "silver_spot_try"}

DEFAULT_WATCHLIST = [
    {
        "key": "gumus_ons",
        "label": "Gümüş ONS",
        "symbol": SILVER_SPOT_SYMBOL,
        "currency": "$",
        "decimals": 2,
        "color": "#ffffff",
        "source": "silver_spot",
    },
    {
        "key": "gumus_tl",
        "label": "Gümüş TL",
        "symbol": SILVER_SPOT_SYMBOL,
        "currency": "₺",
        "decimals": 2,
        "color": "#ffffff",
        "source": "silver_spot_try",
    },
    {
        "key": "altin_tl",
        "label": "Altın TL",
        "symbol": "GC=F",
        "currency": "₺",
        "decimals": 0,
        "color": "#d4af37",
        "source": "metal_try",
    },
    {
        "key": "dolar",
        "label": "Dolar",
        "symbol": "TRY=X",
        "currency": "₺",
        "decimals": 2,
        "color": "#2ecc71",
        "source": "direct",
    },
    {
        "key": "thyao",
        "label": "THYAO",
        "symbol": "THYAO.IS",
        "currency": "₺",
        "decimals": 2,
        "color": "#2196f3",
        "source": "direct",
    },
]

LEGACY_MARKET_FIELDS = {
    "ons_gumus": "gumus_ons",
    "gram_gumus_tl": "gumus_tl",
    "gram_altin_tl": "altin_tl",
    "dolar": "dolar",
}


def app_path(filename):
    return os.path.join(APP_DIR, filename)


def default_watchlist():
    return [dict(item) for item in DEFAULT_WATCHLIST]


def reorder_instruments(instruments, ordered_keys):
    """Return instruments in an exact, validated key order."""
    current_keys = [item["key"] for item in instruments]
    ordered_keys = list(ordered_keys)
    if (
        len(ordered_keys) != len(current_keys)
        or len(set(ordered_keys)) != len(ordered_keys)
        or set(ordered_keys) != set(current_keys)
    ):
        raise ValueError("Yeni sıra mevcut izleme listesinin tam bir eşleşmesi olmalı.")
    by_key = {item["key"]: item for item in instruments}
    return [by_key[key] for key in ordered_keys]


def calculate_reordered_keys(ordered_keys, dragged_key, pointer_y, midpoints_by_key):
    """Calculate a drag order without mutating the persisted watchlist."""
    ordered_keys = list(ordered_keys)
    if dragged_key not in ordered_keys:
        return ordered_keys
    remaining = [key for key in ordered_keys if key != dragged_key]
    target_index = sum(
        1
        for key in remaining
        if key in midpoints_by_key and pointer_y > midpoints_by_key[key]
    )
    remaining.insert(target_index, dragged_key)
    return remaining


def parse_history_timestamp(value):
    """Return a local, timezone-naive datetime for stored/provider timestamps."""
    try:
        if hasattr(value, "to_pydatetime"):
            parsed = value.to_pydatetime()
        elif isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_history_timestamp(value):
    """Normalize provider timestamps so SQLite text ordering stays reliable."""
    parsed = parse_history_timestamp(value)
    if parsed is None:
        return str(value)
    return parsed.replace(microsecond=0).isoformat()


def downsample_history_rows(rows, max_points):
    """Reduce dense history while retaining each bucket's local high and low."""
    rows = list(rows or [])
    try:
        max_points = int(max_points)
    except (TypeError, ValueError):
        max_points = 0
    if max_points < 3 or len(rows) <= max_points:
        return rows

    interior = rows[1:-1]
    bucket_count = max(1, (max_points - 2) // 2)
    bucket_size = max(1, (len(interior) + bucket_count - 1) // bucket_count)
    sampled = [rows[0]]
    for offset in range(0, len(interior), bucket_size):
        bucket = interior[offset:offset + bucket_size]
        if not bucket:
            continue
        indexed = list(enumerate(bucket))
        low = min(indexed, key=lambda item: float(item[1][1]))
        high = max(indexed, key=lambda item: float(item[1][1]))
        for _index, row in sorted({low[0]: low, high[0]: high}.values()):
            sampled.append(row)
    sampled.append(rows[-1])
    return sampled[:max_points - 1] + [rows[-1]] if len(sampled) > max_points else sampled


def history_rows_in_window(rows, seconds):
    """Return the requested time span, anchored to the newest available bar."""
    prepared = []
    for timestamp, value in rows or []:
        parsed = parse_history_timestamp(timestamp)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if parsed is None or not math.isfinite(numeric) or numeric <= 0:
            continue
        prepared.append((parsed, normalize_history_timestamp(parsed), numeric))
    prepared.sort(key=lambda row: row[0])
    if not prepared or not seconds:
        return [(stored, value) for _parsed, stored, value in prepared]
    cutoff = prepared[-1][0] - timedelta(seconds=float(seconds))
    return [
        (stored, value)
        for parsed, stored, value in prepared
        if parsed >= cutoff
    ]


def calculate_zoomed_range(start, end, zoom_in, anchor=0.5, min_span=0.02):
    """Calculate a clamped normalized chart viewport around an anchor point."""
    start = max(0.0, min(float(start), 1.0))
    end = max(start, min(float(end), 1.0))
    anchor = max(0.0, min(float(anchor), 1.0))
    min_span = max(0.001, min(float(min_span), 1.0))
    span = max(min_span, end - start)
    new_span = span * (0.65 if zoom_in else (1 / 0.65))
    new_span = max(min_span, min(new_span, 1.0))
    anchor_value = start + (span * anchor)
    new_start = anchor_value - (new_span * anchor)
    new_end = new_start + new_span
    if new_start < 0:
        new_end -= new_start
        new_start = 0.0
    if new_end > 1:
        new_start -= new_end - 1.0
        new_end = 1.0
    return max(0.0, new_start), min(1.0, new_end)


def calculate_panned_range(start, end, shift):
    """Shift a normalized chart viewport without changing its zoom level."""
    start = float(start)
    end = float(end)
    span = round(max(0.0, min(end - start, 1.0)), 12)
    new_start = start + float(shift)
    new_end = new_start + span
    if new_start < 0:
        return 0.0, span
    if new_end > 1:
        return 1.0 - span, 1.0
    return new_start, new_end


def make_watchlist_key(label, symbol):
    raw = (symbol or label or "asset").lower()
    chars = []
    for ch in raw:
        if ch.isalnum():
            chars.append(ch)
        elif ch in (".", "-", "=", "_", " "):
            chars.append("_")
    key = "".join(chars).strip("_")
    return key or "asset"


def normalize_instrument(item, fallback_index=0):
    item = dict(item or {})
    label = str(item.get("label") or item.get("name") or item.get("symbol") or f"Varlık {fallback_index + 1}").strip()
    symbol = str(item.get("symbol") or "").strip().upper()
    key = str(item.get("key") or make_watchlist_key(label, symbol)).strip()
    if not symbol:
        symbol = key.upper()
    try:
        decimals = int(item.get("decimals", 2))
    except (TypeError, ValueError):
        decimals = 2
    decimals = max(0, min(decimals, 6))
    return {
        "key": key,
        "label": label,
        "symbol": symbol,
        "currency": str(item.get("currency", "₺")),
        "decimals": decimals,
        "color": str(item.get("color") or "#2196f3"),
        "source": str(item.get("source") or "direct"),
    }


def migrate_legacy_silver_instrument(instrument):
    """Move only the two built-in silver rows from COMEX futures to spot XAG."""
    instrument = dict(instrument)
    key = instrument.get("key")
    symbol = str(instrument.get("symbol") or "").strip().upper()
    if key not in SILVER_SPOT_KEYS or symbol != LEGACY_SILVER_FUTURES_SYMBOL:
        return instrument, False
    instrument["symbol"] = SILVER_SPOT_SYMBOL
    instrument["source"] = "silver_spot" if key == "gumus_ons" else "silver_spot_try"
    return instrument, True


def normalize_market_data(data):
    if not isinstance(data, dict):
        return None

    prices = {}
    raw_prices = data.get("prices")
    if isinstance(raw_prices, dict):
        for key, value in raw_prices.items():
            price = positive_price(value)
            if price:
                prices[str(key)] = price

    for legacy_key, new_key in LEGACY_MARKET_FIELDS.items():
        if new_key in prices:
            continue
        value = positive_price(data.get(legacy_key))
        if value:
            prices[new_key] = value

    sources = {}
    raw_sources = data.get("sources")
    if isinstance(raw_sources, dict):
        for key, symbol in raw_sources.items():
            key = str(key)
            symbol = str(symbol or "").strip().upper()
            if key in prices and symbol:
                sources[key] = symbol

    normalized = {
        "prices": prices,
        "sources": sources,
        "timestamp": data.get("timestamp", ""),
        "quotes": normalize_quotes(data.get("quotes"), prices),
    }
    for legacy_key, new_key in LEGACY_MARKET_FIELDS.items():
        if new_key in prices:
            normalized[legacy_key] = prices[new_key]
    return normalized


def market_data_for_save(prices, timestamp=None, sources=None, *, quotes=None):
    data = {
        "prices": {str(key): price for key, value in prices.items() if (price := positive_price(value))},
        "timestamp": timestamp or time.strftime("%d.%m %H:%M"),
    }
    if quotes is not None:
        data["quotes"] = normalize_quotes(quotes, data["prices"])
    sources = sources or {}
    data["sources"] = {
        str(key): str(symbol).strip().upper()
        for key, symbol in sources.items()
        if str(key) in data["prices"] and str(symbol or "").strip()
    }
    for legacy_key, new_key in LEGACY_MARKET_FIELDS.items():
        if new_key in data["prices"]:
            data[legacy_key] = data["prices"][new_key]
    return data


def format_instrument_value(instrument, value):
    if value is None:
        return "..."
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "..."
    decimals = int(instrument.get("decimals", 2))
    prefix = instrument.get("currency", "")
    return f"{prefix}{turkish_number(value, decimals)}"


def safe_float(value, default=0.0):
    try:
        return float(str(value).replace(',', '.'))
    except (TypeError, ValueError):
        return default


def portfolio_unit(instrument):
    key = instrument.get("key", "")
    source = instrument.get("source", "")
    if key in ("gumus_tl", "altin_tl") or source == "metal_try":
        return "g"
    if key == "dolar" or instrument.get("symbol") == "TRY=X":
        return "USD"
    return "adet"


def portfolio_instruments(watchlist):
    # Gümüş ONS referans fiyat; portföyde gram gümüş için Gümüş TL kullanılır.
    return [item for item in watchlist if item.get("key") != "gumus_ons"]


def price_to_tl(instrument, prices):
    price = prices.get(instrument.get("key"))
    if price is None:
        return None
    price = safe_float(price, None)
    if price is None or not math.isfinite(price) or price <= 0:
        return None
    currency = instrument.get("currency", "₺")
    if currency == "$":
        dolar = safe_float(prices.get("dolar"), 0)
        if not math.isfinite(dolar) or dolar <= 0:
            return None
        return price * dolar
    return price


def log_message(message):
    try:
        print(message)
    except UnicodeEncodeError:
        print(str(message).encode("ascii", "replace").decode("ascii"))


class WatchlistManager:
    def __init__(self, filename="watchlist.json"):
        self.filename = filename
        self.filepath = filename if os.path.isabs(filename) else app_path(filename)
        self._loaded_with_migrations = False
        self.archived = {}
        self.instruments = self.load()
        if self._loaded_with_migrations:
            try:
                self.save_all()
            except Exception as e:
                log_message(f"İzleme listesi kaynak geçişi kaydedilemedi: {e}")

    def load(self):
        if not os.path.exists(self.filepath):
            return default_watchlist()
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                archived = data.get("archived", {})
                if isinstance(archived, dict):
                    self.archived = {
                        key: migrate_legacy_silver_instrument(normalize_instrument({**item, "key": key}))[0]
                        for key, item in archived.items() if isinstance(item, dict)
                    }
                data = data.get("instruments", [])
            if not isinstance(data, list):
                return default_watchlist()

            instruments = []
            seen_keys = set()
            for idx, item in enumerate(data):
                instrument = normalize_instrument(item, idx)
                instrument, migrated = migrate_legacy_silver_instrument(instrument)
                self._loaded_with_migrations = self._loaded_with_migrations or migrated
                key = instrument["key"]
                if key in seen_keys:
                    base = key
                    suffix = 2
                    while f"{base}_{suffix}" in seen_keys:
                        suffix += 1
                    instrument["key"] = f"{base}_{suffix}"
                seen_keys.add(instrument["key"])
                instruments.append(instrument)
            return instruments or default_watchlist()
        except Exception as e:
            log_message(f"İzleme listesi okuma hatası: {e}")
            return default_watchlist()

    def save_all(self):
        directory = os.path.dirname(os.path.abspath(self.filepath))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".watchlist-", delete=False) as target:
                temporary = target.name
                json.dump({"instruments": self.instruments, "archived": self.archived}, target,
                          indent=4, ensure_ascii=False)
            os.replace(temporary, self.filepath)
        finally:
            if temporary and os.path.exists(temporary):
                os.remove(temporary)

    def has_symbol(self, symbol):
        symbol = symbol.strip().upper()
        return any(item["symbol"].upper() == symbol for item in self.instruments)

    def add(self, label, symbol, currency="₺"):
        label = label.strip()
        symbol = symbol.strip().upper()
        if not label or not symbol:
            raise ValueError("Görünen ad ve sembol zorunlu.")
        if self.has_symbol(symbol):
            raise ValueError("Bu sembol zaten izleme listesinde.")

        previous = next((item for item in self.archived.values()
                         if item["symbol"] == symbol and item["currency"] == currency), None)
        key = previous["key"] if previous else make_watchlist_key(label, symbol)
        existing_keys = {item["key"] for item in self.instruments} | (set(self.archived) if previous is None else set())
        if key in existing_keys:
            base = key
            suffix = 2
            while f"{base}_{suffix}" in existing_keys:
                suffix += 1
            key = f"{base}_{suffix}"

        instrument = normalize_instrument({**(previous or {}),
            "key": key,
            "label": label,
            "symbol": symbol,
            "currency": currency or "",
        })
        self.instruments.append(instrument)
        archived_item = self.archived.pop(key, None)
        try:
            self.save_all()
        except Exception:
            self.instruments.pop()
            if archived_item is not None:
                self.archived[key] = archived_item
            raise
        return instrument

    def remove(self, key):
        if len(self.instruments) <= 1:
            raise ValueError("En az bir varlık izleme listesinde kalmalı.")
        removed = next((item for item in self.instruments if item["key"] == key), None)
        if removed is None:
            raise ValueError("Silinecek sembol bulunamadı.")
        previous = self.instruments
        self.instruments = [item for item in previous if item["key"] != key]
        self.archived[key] = removed
        try:
            self.save_all()
        except Exception:
            self.instruments = previous
            self.archived.pop(key)
            raise

    def restore(self, key):
        if any(item["key"] == key for item in self.instruments) or key not in self.archived:
            raise ValueError("Gösterilecek varlık bulunamadı.")
        instrument = self.archived.pop(key)
        self.instruments.append(instrument)
        try:
            self.save_all()
        except Exception:
            self.instruments.pop()
            self.archived[key] = instrument
            raise

    def reorder(self, ordered_keys):
        previous = list(self.instruments)
        reordered = reorder_instruments(previous, ordered_keys)
        if [item["key"] for item in reordered] == [item["key"] for item in previous]:
            return False
        self.instruments = reordered
        try:
            self.save_all()
        except Exception:
            self.instruments = previous
            raise
        return True


class TransactionManager:
    def __init__(self, filename="transactions.json"):
        self.filename = filename if os.path.isabs(filename) else app_path(filename)
        self.load_error = None
        self._disk_snapshot = None
        self.transactions = self.load()
        self._positions = None

    def load(self):
        try:
            self._disk_snapshot = read_snapshot(self.filename)
            records = decode_records(self._disk_snapshot) if self._disk_snapshot is not None else []
        except (OSError, ValueError) as error:
            self.load_error = f"İşlem kayıtları açılamadı. Dosya korunuyor. {error}"
            return []
        self.load_error = None
        return records

    @staticmethod
    def read_import(path):
        return decode_records(read_snapshot(path))

    def _require_writable(self):
        if self.load_error:
            raise OSError(self.load_error)

    def _read_current_snapshot(self):
        current = read_snapshot(self.filename)
        if current != self._disk_snapshot:
            raise OSError("İşlem dosyası başka bir uygulamada değişti. Devam etmeden önce Piyasa'yı yeniden açın.")
        return current

    def export_backup(self, path):
        self._require_writable()
        if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(self.filename)):
            raise ValueError("Yedek için işlem dosyasından farklı bir konum seçin.")
        with ledger_write_lock(self.filename):
            self._read_current_snapshot()
            replace_snapshot(path, encode_records(self.transactions))

    def restore_backup(self, path):
        records = self.read_import(path)
        for record in records:
            normalized = self.normalize_transaction(record)
            self._validate_amounts(normalized, normalized["quantity"])
        restored = encode_records(records)
        recovery = None
        with ledger_write_lock(self.filename):
            current = self._read_current_snapshot()
            if current is not None:
                directory = Path(self.filename).absolute().parent / "ledger-backups"
                directory.mkdir(exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=directory, prefix="transactions-before-restore-", suffix=".json", delete=False) as backup:
                    backup.write(current)
                    backup.flush()
                    os.fsync(backup.fileno())
                    recovery = Path(backup.name)
            replace_snapshot(self.filename, restored)
        self._disk_snapshot = restored
        self.transactions[:] = records
        self._positions = None
        self.load_error = None
        return recovery

    @staticmethod
    def _fingerprint(record):
        return json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)

    def preview_import(self, transactions):
        self._require_writable()
        existing = Counter(map(self._fingerprint, self.transactions))
        accepted = []
        duplicates = 0
        for record in transactions:
            fingerprint = self._fingerprint(record)
            if existing[fingerprint]:
                existing[fingerprint] -= 1
                duplicates += 1
            else:
                accepted.append(record)
        self._evaluate_changes(self._plan_additions(accepted))
        return accepted, duplicates

    def save(self, transaction):
        self.save_many([transaction])

    def save_many(self, transactions):
        self._require_writable()
        new_transactions = [dict(transaction) for transaction in transactions]
        if new_transactions:
            self._commit_entries(self._plan_additions(new_transactions))

    def replace(self, index, transaction):
        self._require_writable()
        self._commit_entries(self._plan_replacement(index, dict(transaction)))

    def delete(self, index):
        self._require_writable()
        if index < 0 or index >= len(self.transactions):
            raise ValueError("Silinecek işlem bulunamadı.")
        entries = list(enumerate(self.transactions))
        del entries[index]
        self._commit_entries(entries)

    def validate_transaction(self, transaction, exclude_index=None):
        self.preview_trade(transaction, exclude_index)

    def preview_average_cost(self, transaction, edit_index=None):
        return self.preview_trade(transaction, edit_index)["average_cost_tl"]

    def preview_trade(self, transaction, edit_index=None):
        self._require_writable()
        entries = self._draft_entries(transaction, edit_index)
        effective_index = next(index for index, (original, _) in enumerate(entries) if original is None)
        key = self._record_key(transaction)
        before = self._position_snapshot().get(key, {})
        proposed = self._evaluate_changes(entries).get(key, {})
        return {
            "average_cost_tl": proposed.get("average_cost_tl"),
            "previous_average_cost_tl": before.get("average_cost_tl"),
            "realized_change_tl": proposed.get("realized_profit_tl", 0.0) - before.get("realized_profit_tl", 0.0),
            "historical": effective_index < len(entries) - 1 or (edit_index is not None and edit_index != effective_index),
        }

    def available_to_sell(self, instrument_key, date, edit_index=None):
        entries = self._draft_entries({"instrument_key": instrument_key, "date": date}, edit_index)
        if edit_index is None and entries[-1][0] is None:
            return self.get_position_quantity(instrument_key)
        positions = {}
        for index, record in entries:
            if index is None:
                break
            if self._record_key(record) == instrument_key:
                self._apply_transaction(positions, self.normalize_transaction(record))
        return positions.get(instrument_key, {}).get("quantity", 0.0)

    @staticmethod
    def parse_record_date(value):
        for template in ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(str(value).strip(), template)
            except ValueError:
                continue
        return None

    @staticmethod
    def _record_key(record):
        return str(record.get("instrument_key") or "gumus_tl")

    def _insert_entry(self, entries, record, *, old_index=None):
        effective_date = self.parse_record_date(record.get("date"))
        key = self._record_key(record)
        if old_index is not None:
            old_date = self.parse_record_date(self.transactions[old_index].get("date"))
            if effective_date is None or effective_date == old_date:
                entries.insert(old_index, (None, record))
                return
        position = len(entries)
        if effective_date is not None:
            for index, (_, existing) in enumerate(entries):
                if self._record_key(existing) == key:
                    saved_date = self.parse_record_date(existing.get("date"))
                    if saved_date is not None and saved_date > effective_date:
                        position = index
                        break
        entries.insert(position, (None, record))

    def _draft_entries(self, transaction, edit_index):
        return self._plan_additions([transaction]) if edit_index is None else self._plan_replacement(edit_index, transaction)

    def _plan_additions(self, transactions):
        entries = list(enumerate(self.transactions))
        for record in transactions:
            self._insert_entry(entries, record)
        return entries

    def _plan_replacement(self, index, transaction):
        if not 0 <= index < len(self.transactions):
            raise ValueError("Düzenlenecek işlem bulunamadı.")
        entries = list(enumerate(self.transactions))
        del entries[index]
        self._insert_entry(entries, transaction, old_index=index)
        return entries

    def _commit_entries(self, entries):
        self._commit_transactions([record for _, record in entries], self._evaluate_changes(entries))

    def _evaluate_changes(self, entries):
        old_count = len(self.transactions)
        unchanged = len(entries) >= old_count and all(
            index == original for index, (original, _) in enumerate(entries[:old_count])
        )
        previous_sales = {}
        if unchanged:
            positions = self.get_positions()
            entries = entries[old_count:]
        else:
            positions = {}
            for index, record in enumerate(self.transactions):
                previous_sales[index] = self._apply_transaction(positions, self.normalize_transaction(record))
            positions = {}
        # Retained legacy sales may already be undersupplied. An edit can fix
        # that shortfall, but may not cut the quantity they previously fulfilled.
        for index, record in entries:
            normalized = self.normalize_transaction(record)
            if index is None:
                available = positions.get(normalized["instrument_key"], {}).get("quantity", 0.0)
                self._validate_amounts(normalized, available)
            sold = self._apply_transaction(positions, normalized)
            if index is not None and sold + 1e-9 < previous_sales[index]:
                raise ValueError("Bu değişiklik sonraki bir satışta mevcut miktarın aşılmasına neden olur.")
        return positions

    @staticmethod
    def _validate_amounts(transaction, available_quantity):
        quantity = transaction["quantity"]
        total_tl = transaction["total_tl"]
        fee_tl = transaction["fee_tl"]
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("Miktar pozitif olmalı.")
        if not math.isfinite(total_tl) or total_tl < 0:
            raise ValueError("Toplam tutar sıfır veya pozitif olmalı.")
        if not math.isfinite(fee_tl) or fee_tl < 0:
            raise ValueError("Komisyon sıfır veya pozitif olmalı.")
        if transaction["action"] == "sell" and fee_tl > total_tl:
            raise ValueError("Komisyon brüt satış tutarını aşamaz.")
        if not math.isfinite(total_tl + fee_tl):
            raise ValueError("Komisyon dâhil işlem tutarı çok büyük.")
        if transaction["action"] == "sell" and quantity > available_quantity + 1e-9:
            raise ValueError("Satış miktarı mevcut miktardan büyük olamaz.")

    def save_all(self):
        self._commit_transactions(self.transactions)

    def _commit_transactions(self, transactions, positions=None):
        self._require_writable()
        raw = encode_records(transactions)
        with ledger_write_lock(self.filename):
            self._read_current_snapshot()
            replace_snapshot(self.filename, raw)
        self._disk_snapshot = raw
        self.transactions[:] = transactions
        self._positions = positions

    @staticmethod
    def normalize_transaction(transaction):
        t = transaction if isinstance(transaction, dict) else {}
        instrument_key = str(t.get("instrument_key") or "gumus_tl")
        action = str(t.get("action") or "buy").lower()
        if action not in ("buy", "sell"):
            action = "buy"

        quantity = safe_float(t.get("quantity", t.get("amount_g", 0)), math.nan)
        total_tl = safe_float(t.get("total_tl", 0), math.nan)
        total_usd = safe_float(t.get("total_usd", 0))
        fx_rate = safe_float(t.get("fx_rate", 0))
        if "total_tl" not in t and t.get("currency") == "USD" and fx_rate > 0:
            total_tl = total_usd * fx_rate
        if not fx_rate and total_usd > 0 and total_tl > 0:
            fx_rate = total_tl / total_usd

        return {
            "date": t.get("date", ""),
            "instrument_key": instrument_key,
            "instrument_label": t.get("instrument_label", instrument_key),
            "action": action,
            "quantity": quantity,
            "currency": t.get("currency", "TL"),
            "total_tl": total_tl,
            "total_usd": total_usd,
            "fx_rate": fx_rate,
            "fee_tl": safe_float(t.get("fee_tl", 0), math.nan),
        }

    def get_position_quantity(self, instrument_key, exclude_index=None):
        positions = self._position_snapshot() if exclude_index is None else self.get_positions(exclude_index)
        return positions.get(instrument_key, {}).get("quantity", 0.0)

    def get_average_cost(self, instrument_key):
        return self._position_snapshot().get(instrument_key, {}).get("average_cost_tl")

    def _position_snapshot(self):
        if self._positions is None:
            self._positions = self._calculate_positions()
        return self._positions

    def get_positions(self, exclude_index=None):
        if exclude_index is not None:
            return self._calculate_positions(exclude_index)
        return {key: position.copy() for key, position in self._position_snapshot().items()}

    def _calculate_positions(self, exclude_index=None, *, end_index=None):
        positions = {}
        transactions = self.transactions if end_index is None else self.transactions[:end_index]
        for idx, transaction in enumerate(transactions):
            if idx != exclude_index:
                self._apply_transaction(positions, self.normalize_transaction(transaction))
        return positions

    @staticmethod
    def _apply_transaction(positions, transaction):
        key = transaction["instrument_key"]
        quantity = transaction["quantity"]
        total_tl = transaction["total_tl"]
        if not math.isfinite(quantity) or quantity <= 0 or not math.isfinite(total_tl) or total_tl < 0:
            return 0.0
        fee_tl = transaction["fee_tl"]
        if not math.isfinite(fee_tl) or fee_tl < 0:
            fee_tl = 0.0

        position = positions.setdefault(key, {
            "quantity": 0.0,
            "cost_basis_tl": 0.0,
            "average_cost_tl": None,
            "realized_profit_tl": 0.0,
            "instrument_label": transaction["instrument_label"],
        })
        if transaction["action"] == "buy":
            position["quantity"] += quantity
            position["cost_basis_tl"] += total_tl + fee_tl
            position["average_cost_tl"] = position["cost_basis_tl"] / position["quantity"]
        elif position["quantity"] > 0:
            sold_quantity = min(quantity, position["quantity"])
            removed_cost = position["average_cost_tl"] * sold_quantity
            sale_value = (total_tl - fee_tl) * (sold_quantity / quantity)
            position["quantity"] -= sold_quantity
            position["realized_profit_tl"] += sale_value - removed_cost
            if position["quantity"] <= 1e-9:
                position["quantity"] = 0.0
                position["cost_basis_tl"] = 0.0
                position["average_cost_tl"] = None
            else:
                position["cost_basis_tl"] = position["average_cost_tl"] * position["quantity"]
            return sold_quantity
        return 0.0

    def get_portfolio_summary(self, prices, instruments):
        prices = prices or {}
        instrument_map = {item["key"]: item for item in instruments}
        positions = self._position_snapshot()
        rows = []
        total_value = 0.0
        total_cost = 0.0
        priced_cost = 0.0

        for key, position in positions.items():
            quantity = position["quantity"]
            if quantity <= 1e-9:
                continue
            instrument = instrument_map.get(key, {
                "key": key,
                "label": position.get("instrument_label", key),
                "currency": "₺",
                "decimals": 2,
                "color": "#2196f3",
            })
            unit = portfolio_unit(instrument)
            current_price_tl = price_to_tl(instrument, prices)
            cost_basis = position["cost_basis_tl"]
            value_tl = quantity * current_price_tl if current_price_tl is not None else 0.0
            profit_tl = value_tl - cost_basis if current_price_tl is not None else None
            profit_pct = (profit_tl / cost_basis) * 100 if profit_tl is not None and cost_basis > 0 else None

            total_value += value_tl
            total_cost += cost_basis
            if current_price_tl is not None:
                priced_cost += cost_basis
            rows.append({
                "key": key,
                "label": instrument.get("label", key),
                "quantity": quantity,
                "unit": unit,
                "cost_basis_tl": cost_basis,
                "average_cost_tl": position["average_cost_tl"],
                "current_price_tl": current_price_tl,
                "value_tl": value_tl,
                "profit_tl": profit_tl,
                "profit_pct": profit_pct,
                "realized_profit_tl": position["realized_profit_tl"],
                "has_price": current_price_tl is not None,
                "color": instrument.get("color", "#2196f3"),
            })

        unrealized_profit = total_value - priced_cost
        unrealized_profit_pct = (unrealized_profit / priced_cost) * 100 if priced_cost > 0 else 0.0
        rows.sort(key=lambda item: item["value_tl"], reverse=True)
        for row in rows:
            row["weight_pct"] = row["value_tl"] / total_value * 100 if row["has_price"] and total_value > 0 else None
        return {
            "total_value_tl": total_value,
            "total_cost_tl": total_cost,
            "profit_tl": unrealized_profit,
            "profit_pct": unrealized_profit_pct,
            "realized_profit_tl": sum(position["realized_profit_tl"] for position in positions.values()),
            "rows": rows,
        }

    def get_summary(self):
        positions = self._position_snapshot()
        gumus = positions.get("gumus_tl", {})
        total_investment = gumus.get("cost_basis_tl", 0.0)
        total_gumus = gumus.get("quantity", 0.0)
        return total_investment, total_gumus


class MarketHistoryDB:
    def __init__(self, db_name="market_history.db"):
        self.db_path = db_name if os.path.isabs(db_name) else app_path(db_name)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.RLock()
        self._create_table()
        self.cleanup_bad_records()
        self._migrate_legacy_history()

    def _create_table(self):
        with self._lock:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    label TEXT,
                    source_symbol TEXT,
                    price REAL NOT NULL,
                    interval TEXT NOT NULL DEFAULT 'live'
                )
                """
            )
            columns = {
                row[1]
                for row in self.conn.execute(
                    "PRAGMA table_info(market_price_history)"
                ).fetchall()
            }
            if "source_symbol" not in columns:
                self.conn.execute(
                    "ALTER TABLE market_price_history ADD COLUMN source_symbol TEXT"
                )
            if "interval" not in columns:
                self.conn.execute(
                    "ALTER TABLE market_price_history "
                    "ADD COLUMN interval TEXT NOT NULL DEFAULT 'live'"
                )
            self.conn.execute(
                "UPDATE market_price_history SET source_symbol = ? "
                "WHERE source_symbol IS NULL AND instrument_key IN (?, ?)",
                (LEGACY_SILVER_FUTURES_SYMBOL, "gumus_ons", "gumus_tl")
            )
            self.conn.execute(
                "UPDATE market_price_history SET interval = 'live' "
                "WHERE interval IS NULL OR interval = ''"
            )
            unique_index_exists = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' "
                "AND name = 'idx_market_history_unique_bar'"
            ).fetchone()
            # This one-time cleanup lets older databases adopt immutable bars.
            # Avoid rescanning a potentially large history on every startup.
            if not unique_index_exists:
                self.conn.execute(
                    "DELETE FROM market_price_history WHERE id NOT IN ("
                    "SELECT MAX(id) FROM market_price_history GROUP BY "
                    "timestamp, instrument_key, COALESCE(source_symbol, ''), interval)"
                )
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_market_price_history_key_time
                ON market_price_history (instrument_key, timestamp)
            """)
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_market_price_history_key_source_time
                ON market_price_history (instrument_key, source_symbol, timestamp)
            """)
            self.conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_market_history_unique_bar
                ON market_price_history (
                    instrument_key,
                    COALESCE(source_symbol, ''),
                    interval,
                    timestamp
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS market_history_sync (
                    instrument_key TEXT NOT NULL,
                    source_symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (instrument_key, source_symbol, interval)
                )
            """)
            self.conn.commit()

    def _table_exists(self, table_name):
        with self._lock:
            cursor = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            return cursor.fetchone() is not None

    def cleanup_bad_records(self):
        """Veritabanındaki sıfır veya None değerli bozuk kayıtları temizle."""
        try:
            self.conn.execute(
                "DELETE FROM market_price_history WHERE price IS NULL OR price <= 0"
            )
            self.conn.commit()
        except Exception as e:
            log_message(f"Dinamik geçmiş temizlik hatası: {e}")

        if not self._table_exists("market_history"):
            return
        try:
            deleted = self.conn.execute(
                "DELETE FROM market_history WHERE ons_gumus IS NULL OR ons_gumus <= 0 "
                "OR gram_gumus_tl IS NULL OR gram_gumus_tl <= 0 "
                "OR gram_altin_tl IS NULL OR gram_altin_tl <= 0 "
                "OR dolar IS NULL OR dolar <= 0"
            ).rowcount
            if deleted > 0:
                self.conn.commit()
                log_message(f"Veritabanından {deleted} bozuk kayıt temizlendi.")
        except Exception as e:
            log_message(f"Temizlik hatası: {e}")

    def _migrate_legacy_history(self):
        if not self._table_exists("market_history"):
            return

        try:
            existing = self.conn.execute(
                "SELECT COUNT(*) FROM market_price_history"
            ).fetchone()[0]
            if existing:
                return

            rows = self.conn.execute(
                "SELECT timestamp, ons_gumus, gram_gumus_tl, gram_altin_tl, dolar "
                "FROM market_history ORDER BY timestamp"
            ).fetchall()
            migrated = 0
            legacy_labels = {
                "gumus_ons": "Gümüş ONS",
                "gumus_tl": "Gümüş TL",
                "altin_tl": "Altın TL",
                "dolar": "Dolar",
            }
            legacy_sources = {
                "gumus_ons": LEGACY_SILVER_FUTURES_SYMBOL,
                "gumus_tl": LEGACY_SILVER_FUTURES_SYMBOL,
                "altin_tl": "GC=F",
                "dolar": "TRY=X",
            }
            for timestamp, ons_gumus, gram_gumus_tl, gram_altin_tl, dolar in rows:
                values = {
                    "gumus_ons": ons_gumus,
                    "gumus_tl": gram_gumus_tl,
                    "altin_tl": gram_altin_tl,
                    "dolar": dolar,
                }
                for key, value in values.items():
                    if value and value > 0:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO market_price_history "
                            "(timestamp, instrument_key, label, source_symbol, price) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                timestamp,
                                key,
                                legacy_labels[key],
                                legacy_sources[key],
                                float(value),
                            )
                        )
                        migrated += 1
            if migrated:
                self.conn.commit()
                log_message(f"{migrated} legacy fiyat kaydi dinamik gecmise tasindi.")
        except Exception as e:
            log_message(f"Legacy gecmis tasima hatasi: {e}")

    def insert(self, ons_gumus, gram_gumus_tl, gram_altin_tl, dolar):
        prices = {
            "gumus_ons": ons_gumus,
            "gumus_tl": gram_gumus_tl,
            "altin_tl": gram_altin_tl,
            "dolar": dolar,
        }
        self.insert_prices(prices, default_watchlist())

    def insert_prices(self, prices, instruments=None, timestamp=None, interval="live"):
        if not prices:
            return
        timestamp = normalize_history_timestamp(timestamp or datetime.now())
        instrument_map = {item["key"]: item for item in (instruments or [])}
        inserted = 0
        with self._lock:
            for key, value in prices.items():
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                if value <= 0:
                    continue
                label = instrument_map.get(key, {}).get("label", key)
                source_symbol = instrument_map.get(key, {}).get("symbol")
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO market_price_history "
                    "(timestamp, instrument_key, label, source_symbol, price, interval) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (timestamp, key, label, source_symbol, value, interval)
                )
                inserted += max(cursor.rowcount, 0)
            if inserted:
                self.conn.commit()
        return inserted

    def insert_history_rows(
        self,
        instrument,
        rows,
        interval,
        *,
        source_symbol=None,
    ):
        """Persist immutable provider bars and return the number newly cached."""
        if not instrument or not rows:
            return 0
        key = instrument["key"]
        label = instrument.get("label", key)
        source_symbol = source_symbol or instrument.get("symbol") or ""
        prepared = []
        for timestamp, value in rows:
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            prepared.append(
                (
                    normalize_history_timestamp(timestamp),
                    key,
                    label,
                    source_symbol,
                    value,
                    interval,
                )
            )
        if not prepared:
            return 0
        with self._lock:
            before = self.conn.total_changes
            self.conn.executemany(
                "INSERT OR IGNORE INTO market_price_history "
                "(timestamp, instrument_key, label, source_symbol, price, interval) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                prepared,
            )
            inserted = self.conn.total_changes - before
            if inserted:
                self.conn.commit()
        return inserted

    @staticmethod
    def _append_interval_filter(clauses, params, intervals):
        intervals = tuple(intervals or ())
        if not intervals:
            return
        placeholders = ",".join("?" for _item in intervals)
        clauses.append(f"interval IN ({placeholders})")
        params.extend(intervals)

    @staticmethod
    def _append_source_filter(clauses, params, source_symbol):
        if not source_symbol:
            return
        if isinstance(source_symbol, (list, tuple, set)):
            sources = tuple(str(item) for item in source_symbol if item)
            if not sources:
                return
            placeholders = ",".join("?" for _item in sources)
            clauses.append(f"source_symbol IN ({placeholders})")
            params.extend(sources)
        else:
            clauses.append("source_symbol = ?")
            params.append(source_symbol)

    def get_history(
        self,
        instrument_key="gumus_tl",
        days=7,
        source_symbol=None,
        *,
        hours=None,
        intervals=None,
        anchor_to_latest=False,
    ):
        delta = timedelta(hours=hours) if hours is not None else timedelta(days=days)
        clauses = ["instrument_key = ?", "price > 0"]
        params = [instrument_key]
        self._append_source_filter(clauses, params, source_symbol)
        self._append_interval_filter(clauses, params, intervals)
        end_at = None
        if anchor_to_latest:
            with self._lock:
                latest = self.conn.execute(
                    "SELECT MAX(timestamp) FROM market_price_history WHERE "
                    + " AND ".join(clauses),
                    tuple(params),
                ).fetchone()
            end_at = parse_history_timestamp(latest[0]) if latest and latest[0] else None
        end_at = end_at or datetime.now()
        cutoff = normalize_history_timestamp(end_at - delta)
        clauses.extend(("timestamp >= ?", "timestamp <= ?"))
        params.extend((cutoff, normalize_history_timestamp(end_at)))
        query = (
            "SELECT timestamp, price FROM market_price_history WHERE "
            + " AND ".join(clauses)
            + " ORDER BY timestamp"
        )
        with self._lock:
            return self.conn.execute(query, tuple(params)).fetchall()

    def get_all_history(
        self,
        instrument_key="gumus_tl",
        source_symbol=None,
        *,
        intervals=None,
    ):
        clauses = ["instrument_key = ?", "price > 0"]
        params = [instrument_key]
        self._append_source_filter(clauses, params, source_symbol)
        self._append_interval_filter(clauses, params, intervals)
        query = (
            "SELECT timestamp, price FROM market_price_history WHERE "
            + " AND ".join(clauses)
            + " ORDER BY timestamp"
        )
        with self._lock:
            return self.conn.execute(query, tuple(params)).fetchall()

    def get_stats(
        self,
        instrument_key="gumus_tl",
        days=None,
        source_symbol=None,
        *,
        hours=None,
        intervals=None,
    ):
        clauses = ["instrument_key = ?", "price > 0"]
        params = [instrument_key]
        if hours is not None or days:
            delta = timedelta(hours=hours) if hours is not None else timedelta(days=days)
            cutoff = normalize_history_timestamp(datetime.now() - delta)
            clauses.append("timestamp >= ?")
            params.append(cutoff)
        self._append_source_filter(clauses, params, source_symbol)
        self._append_interval_filter(clauses, params, intervals)
        with self._lock:
            cursor = self.conn.execute(
                "SELECT MIN(price), MAX(price), AVG(price), COUNT(*) "
                "FROM market_price_history WHERE " + " AND ".join(clauses),
                tuple(params)
            )
            return cursor.fetchone()

    def get_first_last(
        self,
        instrument_key="gumus_tl",
        days=None,
        source_symbol=None,
        *,
        hours=None,
        intervals=None,
    ):
        clauses = ["instrument_key = ?", "price > 0"]
        params = [instrument_key]
        if hours is not None or days:
            delta = timedelta(hours=hours) if hours is not None else timedelta(days=days)
            cutoff = normalize_history_timestamp(datetime.now() - delta)
            clauses.append("timestamp >= ?")
            params.append(cutoff)
        self._append_source_filter(clauses, params, source_symbol)
        self._append_interval_filter(clauses, params, intervals)
        base_query = (
            "SELECT price FROM market_price_history WHERE "
            + " AND ".join(clauses)
        )
        with self._lock:
            first = self.conn.execute(
                base_query + " ORDER BY timestamp ASC LIMIT 1", tuple(params)
            ).fetchone()
            last = self.conn.execute(
                base_query + " ORDER BY timestamp DESC LIMIT 1", tuple(params)
            ).fetchone()
        return first, last

    def history_sync_due(
        self,
        instrument_key,
        source_symbol,
        interval,
        max_age,
        now=None,
    ):
        with self._lock:
            row = self.conn.execute(
                "SELECT synced_at FROM market_history_sync "
                "WHERE instrument_key = ? AND source_symbol = ? AND interval = ?",
                (instrument_key, source_symbol or "", interval),
            ).fetchone()
        if not row:
            return True
        synced_at = parse_history_timestamp(row[0])
        now = now or datetime.now()
        return synced_at is None or now - synced_at >= max_age

    def mark_history_synced(
        self,
        instrument_key,
        source_symbol,
        interval,
        timestamp=None,
    ):
        synced_at = normalize_history_timestamp(timestamp or datetime.now())
        with self._lock:
            self.conn.execute(
                "INSERT INTO market_history_sync "
                "(instrument_key, source_symbol, interval, synced_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(instrument_key, source_symbol, interval) "
                "DO UPDATE SET synced_at = excluded.synced_at",
                (instrument_key, source_symbol or "", interval, synced_at),
            )
            self.conn.commit()

class AutoStartManager:
    def __init__(self, app_name="PiyasaWidget"):
        self.app_name = app_name
        self.key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        
    def is_enabled(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, self.app_name)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            return False
            
    def set_autostart(self, enable=True):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_ALL_ACCESS)
            if enable:
                # Script path logic
                exe_path = sys.executable
                script_path = os.path.abspath(__file__)
                # If running as .py, use pythonw.exe to run it without console
                if exe_path.endswith("python.exe") or exe_path.endswith("pythonw.exe"):
                     cmd = f'"{exe_path.replace("python.exe", "pythonw.exe")}" "{script_path}"'
                else:
                     # Frozen exe (pyinstaller)
                     cmd = f'"{sys.executable}"'
                
                winreg.SetValueEx(key, self.app_name, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, self.app_name)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            messagebox.showerror("Hata", f"Kayıt defteri hatası: {e}")

class UpdateManager:
    def __init__(self, current_version, repo_name):
        self.current_version = current_version
        self.repo_name = repo_name
        self.api_url = f"https://api.github.com/repos/{repo_name}/releases/latest"
        
    def check_for_updates(self):
        try:
            response = requests.get(self.api_url, timeout=(5, 15))
            response.raise_for_status()
            if response.status_code == 200:
                data = response.json()
                latest_tag = data.get("tag_name", "").replace("v", "")
                download_url = ""
                
                # Varlıklar içinde .exe ara (Setup öncelikli)
                for asset in data.get("assets", []):
                    if asset["name"].endswith("Setup.exe"):
                        download_url = asset["browser_download_url"]
                        break
                    elif asset["name"].endswith(".exe"):
                        download_url = asset["browser_download_url"]
                
                if self._version_parts(latest_tag) > self._version_parts(self.current_version) and download_url:
                    return True, latest_tag, download_url
            return False, None, None
        except (requests.RequestException, ValueError, KeyError) as error:
            raise RuntimeError("Güncelleme bilgisi alınamadı.") from error

    @staticmethod
    def _version_parts(version):
        return tuple(map(int, version.lstrip("v").split("-", 1)[0].split(".")))

    def update_application(self, download_url):
        prefix = f"https://github.com/{self.repo_name}/releases/download/"
        if not download_url.startswith(prefix) or not download_url.endswith("/PiyasaWidget_Setup.exe"):
            raise ValueError("Kurulum paketi beklenen GitHub sürümünde bulunamadı.")
        directory = tempfile.mkdtemp(prefix="piyasa-update-")
        target = os.path.join(directory, "PiyasaWidget_Setup.exe")
        try:
            with requests.get(download_url, stream=True, timeout=(5, 30)) as response:
                response.raise_for_status()
                with open(target, "wb") as file:
                    for chunk in response.iter_content(chunk_size=65536):
                        file.write(chunk)
            with open(target, "rb") as file:
                if file.read(2) != b"MZ":
                    raise ValueError("İndirilen dosya geçerli bir Windows kurulum paketi değil.")
        except (OSError, ValueError):
            if os.path.exists(target):
                os.remove(target)
            os.rmdir(directory)
            raise
        return target

class PortfolioManagerDialog(tk.Toplevel):
    ALL_ASSETS_LABEL = "Tüm Varlıklar"

    def __init__(self, parent, manager, on_save_callback, current_dollar_rate, instruments, *, quick=False):
        super().__init__(parent)
        self.quick = quick
        self.manager = manager
        self.on_save_callback = on_save_callback
        self.current_dollar_rate = current_dollar_rate
        self.instruments = portfolio_instruments(instruments)
        self.instrument_by_key = {item["key"]: item for item in self.instruments}
        self.instrument_label_to_key = {item["label"]: item["key"] for item in self.instruments}
        self.edit_index = None
        self.sort_column = None
        self.sort_direction = 0
        self._form_scroll_after_id = None
        self._preview_after_id = None
        self._present_after_id = None
        self._quick_focused = False
        self._calendar_window = None
        self._price_mode = "Birim fiyat" if quick else "Toplam tutar"
        self.var_price_mode = tk.StringVar(self, value=self._price_mode)
        self.var_form_title = tk.StringVar(self, value="İşlem ekle" if quick else "Yeni İşlem")
        self.bg_color = THEME["background"]
        self.color_card = THEME["surface"]
        self.color_card_alt = THEME["surface_alt"]
        self.color_border = THEME["border"]
        self.color_text_main = THEME["text_primary"]
        self.color_text_dim = THEME["text_secondary"]
        self.color_text_muted = THEME["text_muted"]
        self.color_accent = THEME["primary"]
        self.color_danger = THEME["negative"]
        self.style_entry = {
            "bg": THEME["input"],
            "fg": THEME["text_primary"],
            "insertbackground": THEME["text_primary"],
            "relief": "flat",
            "font": font(self, "body"),
        }
        self.style_label = {
            "bg": THEME["surface"],
            "fg": THEME["text_secondary"],
            "font": font(self, "small"),
        }

        self.title("İşlem ekle" if quick else "Portföy Yönetimi")
        self.geometry("456x624" if quick else "1120x760")
        self.minsize(*((360, 440) if quick else (980, 700)))
        self.configure(bg=self.bg_color)
        self.overrideredirect(True)
        self.transient(parent)
        self.bind("<Escape>", lambda _event: self._close_requested())
        self.protocol("WM_DELETE_WINDOW", self._close_requested)
        configure_ttk_styles(self)

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self, bg=self.bg_color)
        header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=SPACING["xl"],
            pady=(SPACING["lg"], SPACING["md"]),
        )
        self.close_button = IconButton(header, "close", self._close_requested, size=30,
                                       bg=self.bg_color, danger=True, show_border=False)
        self.close_button.pack(side="right", anchor="n")
        title_label = tk.Label(
            header,
            text="Portföy Yönetimi",
            bg=self.bg_color,
            fg=THEME["text_primary"],
            font=font(self, "title", "bold"),
            anchor="w",
        )
        title_label.pack(anchor="w")
        if quick:
            title_label.configure(textvariable=self.var_form_title)
        for handle in (header, title_label):
            handle.bind("<ButtonPress-1>", self._start_window_move)
            handle.bind("<B1-Motion>", self._move_window)
        self.resize_grip = IconButton(self, "grip", None, size=14, bg=self.bg_color,
                                      fg=self.color_text_muted, show_border=False)
        self.resize_grip.configure(cursor="sizing")
        self.resize_grip.place(relx=1, rely=1, anchor="se")
        self.resize_grip.bind("<ButtonPress-1>", self._start_window_resize)
        self.resize_grip.bind("<B1-Motion>", self._resize_window)

        content = tk.Frame(self, bg=self.bg_color)
        content.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=SPACING["xl"],
            pady=(0, SPACING["xl"]),
        )
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1, minsize=0 if quick else 590)
        if not quick:
            content.grid_columnconfigure(1, minsize=330)

        if not self.quick:
            self._build_history(content)

        # --- Sağ kart: Yeni işlem / düzenleme formu ---
        right_frame = tk.Frame(
            content,
            bg=self.color_card,
            padx=SPACING["lg"],
            pady=SPACING["lg"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        right_frame.grid(row=0, column=0 if quick else 1, sticky="nsew")
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_rowconfigure(2, weight=1)

        if not quick:
            tk.Label(
                right_frame, textvariable=self.var_form_title, bg=self.color_card,
                fg=self.color_text_main, font=font(self, "section", "bold"), anchor="w",
            ).grid(row=0, column=0, sticky="ew", pady=(0, SPACING["md"]))

        form_host = tk.Frame(right_frame, bg=self.color_card)
        form_host.grid(row=2, column=0, sticky="nsew")
        form_host.grid_rowconfigure(0, weight=1)
        form_host.grid_columnconfigure(0, weight=1)
        self.form_canvas = tk.Canvas(
            form_host,
            width=290,
            height=1,
            bg=self.color_card,
            highlightthickness=0,
            bd=0,
        )
        self.form_scrollbar = ttk.Scrollbar(
            form_host,
            orient="vertical",
            command=self.form_canvas.yview,
            style="Modern.Vertical.TScrollbar",
        )
        self.form_canvas.configure(yscrollcommand=self.form_scrollbar.set)
        self.form_canvas.grid(row=0, column=0, sticky="nsew")
        self.form_scrollbar.grid(row=0, column=1, sticky="ns")
        self.form_body = tk.Frame(self.form_canvas, bg=self.color_card)
        form_body = self.form_body
        self._form_window_id = self.form_canvas.create_window(
            (0, 0), window=form_body, anchor="nw"
        )
        form_body.bind("<Configure>", self._sync_form_scrollregion)
        self.form_canvas.bind("<Configure>", self._resize_form_canvas)
        self.form_canvas.bind("<MouseWheel>", self._on_form_mousewheel)

        def field_frame(label_text, parent=form_body):
            frame = tk.Frame(parent, bg=self.color_card)
            tk.Label(frame, text=label_text, **self.style_label).pack(anchor="w", pady=(0, SPACING["xxs"]))
            return frame

        trade_details = tk.Frame(form_body, bg=self.color_card) if quick else form_body
        date_field = field_frame("Tarih", trade_details)
        if quick:
            date_field.pack(side="right", fill="both", expand=True, padx=(SPACING["xs"], 0))
        else:
            date_field.pack(fill="x", pady=(0, SPACING["sm"]))
        date_input = tk.Frame(date_field, bg=self.color_card)
        date_input.pack(fill="x")
        self.var_date = tk.StringVar(self, value=datetime.now().strftime("%d-%m-%Y"))
        self.entry_date = ModernEntry(date_input, textvariable=self.var_date)
        self.entry_date.pack(side="left", fill="x", expand=True)
        HoverButton(date_input, text="Takvim", command=self._open_calendar,
                    bg=THEME["surface_alt"], fg=THEME["text_primary"], padx=4).pack(side="right", padx=(4, 0), fill="y")

        # 2. Varlık
        instrument_field = field_frame("Varlık")
        instrument_field.pack(fill="x", pady=(0, SPACING["sm"]))
        default_label = self.instruments[0]["label"] if self.instruments else ""
        self.var_instrument = tk.StringVar(value=default_label)
        self.combo_instrument = ttk.Combobox(
            instrument_field,
            textvariable=self.var_instrument,
            values=[item["label"] for item in self.instruments],
            state="readonly",
            style="Modern.TCombobox",
        )
        self.combo_instrument.pack(fill="x")
        self.combo_instrument.bind("<<ComboboxSelected>>", lambda e: self.update_amount_label())
        self.lbl_average_cost = tk.Label(
            instrument_field, anchor="w", **self.style_label
        )
        self.lbl_average_cost.pack(fill="x", pady=(SPACING["xxs"], 0))

        # 3. İşlem türü
        action_field = field_frame("İşlem", trade_details)
        if quick:
            action_field.pack(side="left", fill="both", expand=True)
            trade_details.pack(fill="x", pady=(0, SPACING["sm"]))
        else:
            action_field.pack(fill="x", pady=(0, SPACING["sm"]))
        self.var_action = tk.StringVar(value="buy")
        self.segment_action = SegmentedControl(
            action_field,
            self.var_action,
            (("↑  Alış", "buy"), ("↓  Satış", "sell")),
            selection_colors={
                "buy": (THEME["positive_soft"], THEME["positive"]),
                "sell": (THEME["negative_soft"], THEME["negative"]),
            },
        )
        self.segment_action.pack(fill="x")
        
        # 4. Miktar
        amount_field = tk.Frame(form_body, bg=self.color_card)
        amount_field.pack(fill="x", pady=(0, SPACING["sm"]))
        self.lbl_amount = tk.Label(amount_field, text="Miktar", **self.style_label)
        self.lbl_amount.pack(anchor="w", pady=(0, SPACING["xxs"]))
        self.var_amount = tk.StringVar(self)
        self.entry_amount = ModernEntry(amount_field, textvariable=self.var_amount)
        self.entry_amount.pack(fill="x")
        self.sell_available = tk.StringVar(self)
        self.sell_available_label = tk.Label(amount_field, textvariable=self.sell_available,
                                             anchor="w", **self.style_label)
        
        # 5. Para Birimi
        currency_field = field_frame("Para Birimi")
        currency_field.pack(fill="x", pady=(0, SPACING["sm"]))
        self.var_currency = tk.StringVar(value="TL")
        self.segment_currency = SegmentedControl(
            currency_field,
            self.var_currency,
            (("₺  TL", "TL"), ("$  USD", "USD")),
            command=self.toggle_rate_entry,
        )
        self.segment_currency.pack(fill="x")
        
        # 6. Kur (Sadece USD seçiliyse görünür)
        self.frame_rate = field_frame("İşlem Kuru (USD/TL)")
        self.frame_rate.pack(fill="x", pady=(0, SPACING["sm"]))
        self.var_rate = tk.StringVar(self)
        self.entry_rate = ModernEntry(self.frame_rate, textvariable=self.var_rate)
        self.entry_rate.pack(fill="x")
        self._set_rate_input(self.current_dollar_rate)
        
        # 7. Toplam Tutar
        self.total_field_frame = field_frame("Fiyat" if quick else "Toplam Tutar")
        self.total_field_frame.pack(fill="x", pady=(0, SPACING["sm"]))
        self.var_entered_total = tk.StringVar(self)
        if quick:
            price_inputs = tk.Frame(self.total_field_frame, bg=self.color_card)
            price_inputs.pack(fill="x")
            mode = ttk.Combobox(
                price_inputs, textvariable=self.var_price_mode, values=("Birim fiyat", "Toplam tutar"),
                state="readonly", style="Modern.TCombobox", width=12,
            )
            mode.pack(side="right", padx=(SPACING["xs"], 0))
            mode.bind("<<ComboboxSelected>>", self._on_price_mode)
        else:
            price_inputs = self.total_field_frame
        self.entry_total = ModernEntry(price_inputs, textvariable=self.var_entered_total)
        self.entry_total.pack(fill="x")
        commission = field_frame("Komisyon (₺, isteğe bağlı)")
        commission.pack(fill="x", pady=(0, SPACING["sm"]))
        self.var_fee = tk.StringVar(self)
        self.entry_fee = ModernEntry(commission, textvariable=self.var_fee)
        self.entry_fee.pack(fill="x")
        if quick:
            self._build_trade_preview(form_body)
        
        # Ekle/Güncelle Butonları
        button_frame = tk.Frame(right_frame, bg=self.color_card)
        button_frame.grid(row=3, column=0, sticky="ew", pady=(SPACING["xs"], 0))
        self.btn_save = HoverButton(
            button_frame,
            text="İşlemi kaydet" if quick else "+  İşlemi Ekle",
            command=self.save,
            pady=10,
        )
        if quick:
            self.btn_save.pack(side="right", padx=(SPACING["xs"], 0))
            HoverButton(
                button_frame, text="Vazgeç", command=self._close_requested,
                bg=THEME["surface_alt"], hover_bg=THEME["surface_hover"],
                pressed_bg=THEME["surface_pressed"], fg=THEME["text_primary"], pady=10,
            ).pack(side="right")
        else:
            self.btn_save.pack(fill="x", pady=(0, SPACING["xs"]))

        self.btn_cancel_edit = HoverButton(
            button_frame,
            text="Düzenlemeyi İptal Et",
            command=self._cancel_edit_requested,
            bg=THEME["surface_alt"],
            hover_bg=THEME["surface_hover"],
            pressed_bg=THEME["surface_pressed"],
            fg=THEME["text_secondary"],
            pady=8,
        )

        self.update_amount_label()
        self.toggle_rate_entry()
        self._bind_form_mousewheel(self.form_body)
        self._schedule_form_scroll_sync()
        self.bind("<Destroy>", self._on_dialog_destroy, add="+")
        for variable in self._draft_variables():
            variable.trace_add("write", self._queue_trade_preview)
        if not quick:
            self._search_trace_id = self.var_search.trace_add("write", self._on_search_changed)
            self.load_list()
        self.mark_current_draft()
        self._present_after_id = self.after_idle(self._present_on_idle)

    def _draft_variables(self):
        return (self.var_date, self.var_amount, self.var_entered_total, self.var_rate, self.var_fee,
                self.var_instrument, self.var_action, self.var_currency, self.var_price_mode)

    def mark_current_draft(self):
        self._original_draft = tuple(variable.get() for variable in self._draft_variables())

    def confirm_discard_draft(self):
        current = tuple(variable.get() for variable in self._draft_variables())
        return current == self._original_draft or messagebox.askyesno(
            "Kaydedilmemiş işlem", "Formdaki değişikliklerden vazgeçmek istiyor musunuz?", parent=self,
        )

    def _close_requested(self):
        if self.confirm_discard_draft():
            self.destroy()

    def _cancel_edit_requested(self):
        if self.confirm_discard_draft():
            self.cancel_edit()

    def _open_calendar(self):
        if self._calendar_window is not None and self._calendar_window.winfo_exists():
            self._calendar_window.lift()
            return
        selected = self.manager.parse_record_date(self.var_date.get()) or datetime.now()
        self._calendar_window = TradeCalendar(
            self, min(selected.date(), datetime.now().date()),
            lambda day: self.var_date.set(day.strftime("%d-%m-%Y")),
        )

    def _build_history(self, content):
        left_frame = tk.Frame(
            content,
            bg=self.color_card,
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, SPACING["sm"]))
        left_frame.grid_rowconfigure(3, weight=1)
        left_frame.grid_columnconfigure(0, weight=1)

        history_header = tk.Frame(left_frame, bg=self.color_card)
        history_header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=SPACING["lg"],
            pady=(SPACING["lg"], SPACING["md"]),
        )
        tk.Label(
            history_header,
            text="İşlem Geçmişi",
            bg=self.color_card,
            fg=self.color_text_main,
            font=font(self, "section", "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            history_header,
            text="Tüm alım-satım kayıtlarınızı arayın, düzenleyin veya silin.",
            bg=self.color_card,
            fg=self.color_text_dim,
            font=font(self, "small"),
            anchor="w",
        ).pack(anchor="w", pady=(SPACING["xxs"], 0))

        toolbar = tk.Frame(left_frame, bg=self.color_card)
        toolbar.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=SPACING["lg"],
            pady=(0, SPACING["md"]),
        )
        toolbar.grid_columnconfigure(0, weight=1)
        toolbar.grid_columnconfigure(1, minsize=180)

        self.var_search = tk.StringVar(value="")
        self.search_entry = SearchEntry(toolbar, self.var_search, placeholder="İşlem ara...")
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, SPACING["sm"]))

        self.var_asset_filter = tk.StringVar(value=self.ALL_ASSETS_LABEL)
        self.combo_asset_filter = ttk.Combobox(
            toolbar,
            textvariable=self.var_asset_filter,
            values=(self.ALL_ASSETS_LABEL,),
            state="readonly",
            style="Modern.TCombobox",
            width=20,
        )
        self.combo_asset_filter.grid(row=0, column=1, sticky="ew")
        self.combo_asset_filter.bind("<<ComboboxSelected>>", lambda _event: self.load_list())

        table_border = tk.Frame(
            left_frame,
            bg=self.color_border,
            padx=1,
            pady=1,
        )
        table_border.grid(
            row=3,
            column=0,
            sticky="nsew",
            padx=SPACING["lg"],
        )
        table_border.grid_rowconfigure(0, weight=1)
        table_border.grid_columnconfigure(0, weight=1)
        self.tree = TransactionTable(table_border, self.start_edit, self.delete_transaction,
                                     on_sort=self.cycle_sort)
        self.tree.grid(row=0, column=0, sticky="nsew")

        history_footer = tk.Frame(left_frame, bg=self.color_card)
        history_footer.grid(
            row=4,
            column=0,
            sticky="ew",
            padx=SPACING["lg"],
            pady=(SPACING["sm"], SPACING["md"]),
        )
        self.var_result_count = tk.StringVar(value="Toplam 0 işlem")
        tk.Label(
            history_footer,
            textvariable=self.var_result_count,
            bg=self.color_card,
            fg=self.color_text_muted,
            font=font(self, "small"),
            anchor="w",
        ).pack(side="left")

    def _present_on_idle(self):
        self._present_after_id = None
        self.present()

    def present(self):
        """Map above the owner, including when the widget is always on top."""
        if not self.winfo_exists():
            return
        if self._present_after_id is not None:
            self.after_cancel(self._present_after_id)
            self._present_after_id = None
        parent = self.master.winfo_toplevel()
        self.attributes("-topmost", bool(parent.attributes("-topmost")))
        self.deiconify()
        self.lift(parent)
        self.focus_force()
        if self.quick and not self._quick_focused:
            self.entry_amount.focus_set()
            self._quick_focused = True

    def _start_window_move(self, event):
        self._window_move_origin = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _move_window(self, event):
        dx, dy = self._window_move_origin
        self.geometry(f"{event.x_root - dx:+d}{event.y_root - dy:+d}")

    def _start_window_resize(self, event):
        self._window_resize_origin = (event.x_root, event.y_root, self.winfo_width(), self.winfo_height())

    def _resize_window(self, event):
        x, y, width, height = self._window_resize_origin
        min_width, min_height = self.minsize()
        self.geometry(f"{max(min_width, width + event.x_root - x)}x{max(min_height, height + event.y_root - y)}")

    def selected_instrument(self):
        key = self.instrument_label_to_key.get(self.var_instrument.get())
        return self.instrument_by_key.get(key)

    def update_amount_label(self):
        instrument = self.selected_instrument()
        unit = portfolio_unit(instrument or {})
        self.lbl_amount.config(text=f"Miktar ({unit})")
        average = self.manager.get_average_cost(instrument["key"]) if instrument else None
        if average is None:
            cost_text = "Ortalama maliyet —"
        else:
            cost_text = f"Ortalama maliyet ₺{average:,.2f} / {unit}"
        self.lbl_average_cost.config(text=cost_text)

    def _sync_form_scrollregion(self, _event=None):
        try:
            if not self.form_canvas.winfo_exists():
                return
        except tk.TclError:
            return
        self.form_canvas.configure(scrollregion=self.form_canvas.bbox("all"))
        needs_scroll = self.form_body.winfo_reqheight() > self.form_canvas.winfo_height()
        if needs_scroll:
            self.form_scrollbar.grid()
        else:
            self.form_scrollbar.grid_remove()
            self.form_canvas.yview_moveto(0)

    def _run_form_scroll_sync(self):
        self._form_scroll_after_id = None
        self._sync_form_scrollregion()

    def _schedule_form_scroll_sync(self):
        if not self.winfo_exists():
            return
        if self._form_scroll_after_id is not None:
            try:
                self.after_cancel(self._form_scroll_after_id)
            except (tk.TclError, ValueError):
                pass
        self._form_scroll_after_id = self.after_idle(self._run_form_scroll_sync)

    def _on_dialog_destroy(self, event):
        if event.widget is not self:
            return
        for name in ("_form_scroll_after_id", "_preview_after_id", "_present_after_id"):
            callback = getattr(self, name)
            if callback is not None:
                self.after_cancel(callback)
                setattr(self, name, None)

    def _resize_form_canvas(self, event):
        self.form_canvas.itemconfigure(self._form_window_id, width=event.width)
        self._schedule_form_scroll_sync()

    def _on_form_mousewheel(self, event):
        if self.form_scrollbar.winfo_ismapped():
            self.form_canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
            return "break"
        return None

    def _bind_form_mousewheel(self, widget):
        widget.bind("<MouseWheel>", self._on_form_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_form_mousewheel(child)

    def toggle_rate_entry(self):
        if self.var_currency.get() == "USD":
            self.frame_rate.pack(
                fill="x",
                before=self.total_field_frame,
                pady=(0, SPACING["sm"]),
            )
        else:
            self.frame_rate.pack_forget()

    def _set_rate_input(self, rate):
        self.entry_rate.delete(0, "end")
        rate = safe_float(rate)
        if math.isfinite(rate) and rate > 0:
            self.entry_rate.insert(0, self._trade_input_number(rate))
        self._schedule_form_scroll_sync()

    @staticmethod
    def _parse_trade_number(value):
        value = value.strip()
        if "," in value:
            if re.fullmatch(r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d+", value) is None:
                raise ValueError("Geçerli bir sayı girin (ör. 1.000,25).")
            value = value.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"[+-]?[1-9]\d{0,2}(?:\.\d{3})+", value):
            value = value.replace(".", "")
        try:
            number = float(value)
        except ValueError:
            raise ValueError("Geçerli bir sayı girin.") from None
        if not math.isfinite(number):
            raise ValueError("Geçerli bir sayı girin.")
        return number

    @staticmethod
    def _trade_input_number(value):
        text = str(value)
        if re.fullmatch(r"[+-]?[1-9]\d{0,2}\.\d{3}", text):
            return text.replace(".", ",")
        return text

    @staticmethod
    def _format_trade_money(value, currency="₺"):
        return f"{currency}{value:,.2f}".translate(str.maketrans(",.", ".,"))

    def _on_price_mode(self, _event=None):
        mode = self.var_price_mode.get()
        if mode == self._price_mode:
            return
        self._price_mode = mode
        try:
            quantity = self._parse_trade_number(self.var_amount.get())
            value = self._parse_trade_number(self.var_entered_total.get())
        except ValueError:
            return
        if quantity > 0:
            converted = value / quantity if mode == "Birim fiyat" else value * quantity
            if math.isfinite(converted):
                self.var_entered_total.set(self._trade_input_number(converted))

    def _build_trade_preview(self, parent):
        frame = tk.Frame(parent, bg=self.color_card_alt, padx=SPACING["sm"], pady=SPACING["xs"])
        frame.pack(fill="x", pady=(0, SPACING["xs"]))
        frame.grid_columnconfigure(1, weight=1)
        self.preview_total = tk.StringVar(self, value="—")
        self.preview_net = tk.StringVar(self, value="—")
        self.preview_cost = tk.StringVar(self, value="—")
        self.preview_error = tk.StringVar(self)
        for index, (label, variable) in enumerate((
            ("Brüt işlem tutarı", self.preview_total),
            ("Komisyon dâhil net tutar", self.preview_net),
            ("İşlem sonrası ort. maliyet", self.preview_cost),
        )):
            tk.Label(frame, text=label, bg=self.color_card_alt, fg=self.color_text_dim,
                     font=font(self, "small"), anchor="w").grid(row=index, column=0, sticky="w")
            tk.Label(frame, textvariable=variable, bg=self.color_card_alt, fg=self.color_text_main,
                     font=font(self, "small", "bold"), anchor="e").grid(row=index, column=1, sticky="e")
        tk.Label(parent, textvariable=self.preview_error, bg=self.color_card, fg=self.color_danger,
                 font=font(self, "small"), justify="left", anchor="w", wraplength=255).pack(fill="x")
        self.preview_history = tk.StringVar(self)
        tk.Label(parent, textvariable=self.preview_history, bg=self.color_card, fg=self.color_text_dim,
                 font=font(self, "small"), justify="left", anchor="w", wraplength=320).pack(fill="x")

    def _queue_trade_preview(self, *_args):
        if self._preview_after_id is not None:
            self.after_cancel(self._preview_after_id)
        self._preview_after_id = self.after(85, self._update_trade_preview)

    def _update_trade_preview(self):
        self._preview_after_id = None
        self._update_sell_available()
        if not self.quick:
            return
        self.preview_total.set("—")
        self.preview_net.set("—")
        self.preview_cost.set("—")
        self.preview_error.set("")
        self.preview_history.set("")
        if not self.var_amount.get().strip() or not self.var_entered_total.get().strip():
            return
        try:
            data = self._read_trade_form()
            if data["currency"] == "USD":
                total = self._format_trade_money(data["total_usd"], "$")
            else:
                total = self._format_trade_money(data["total_tl"])
            self.preview_total.set(total)
            net = data["total_tl"] + (data["fee_tl"] if data["action"] == "buy" else -data["fee_tl"])
            self.preview_net.set(self._format_trade_money(net))
            impact = self.manager.preview_trade(data, self.edit_index)
            average = impact["average_cost_tl"]
        except (OSError, ValueError) as error:
            self.preview_error.set(str(error))
            return
        if average is not None:
            unit = portfolio_unit(self.selected_instrument())
            self.preview_cost.set(f"{self._format_trade_money(average)} / {unit}")
        else:
            self.preview_cost.set("Pozisyon kapanır")
        if impact["historical"]:
            previous_cost = impact["previous_average_cost_tl"]
            previous = "—" if previous_cost is None else self._format_trade_money(previous_cost)
            realized = self._format_trade_money(impact["realized_change_tl"])
            self.preview_history.set(f"Geçmişe etkisi · Önceki ort. maliyet {previous} · Gerçekleşen K/Z değişimi {realized}")

    def _update_sell_available(self):
        instrument = self.selected_instrument()
        if self.var_action.get() != "sell" or instrument is None:
            self.sell_available_label.pack_forget()
            return
        try:
            quantity = self.manager.available_to_sell(instrument["key"], self.var_date.get(), self.edit_index)
        except ValueError:
            return
        amount = turkish_number(quantity, 8).rstrip("0").rstrip(",")
        self.sell_available.set(f"Satılabilir · {amount} {portfolio_unit(instrument)}")
        self.sell_available_label.pack(fill="x", pady=(SPACING["xxs"], 0))

    def _on_search_changed(self, *_args):
        self.load_list()

    def _transaction_row(self, source_index, raw_transaction):
        return self.transaction_row(self.manager, self.instrument_by_key, source_index, raw_transaction)

    @staticmethod
    def transaction_row(manager, instrument_by_key, source_index, raw_transaction):
        transaction = manager.normalize_transaction(raw_transaction)
        instrument = instrument_by_key.get(transaction["instrument_key"])
        label = (
            instrument["label"]
            if instrument
            else transaction.get("instrument_label", transaction["instrument_key"])
        )
        unit = portfolio_unit(instrument or {})
        action_text = "Alış" if transaction["action"] == "buy" else "Satış"
        quantity_text = f"{transaction['quantity']:.2f} {unit}"
        if transaction["currency"] == "USD":
            total_text = f"${transaction['total_usd']:.2f}"
            raw_total = transaction["total_usd"]
        else:
            total_text = f"₺{transaction['total_tl']:.2f}"
            raw_total = transaction["total_tl"]

        raw_quantity_text = f"{transaction['quantity']:.8f}".rstrip("0").rstrip(".")
        raw_total_text = f"{raw_total:.8f}".rstrip("0").rstrip(".")
        searchable = " ".join((
            str(transaction["date"]),
            action_text,
            transaction["action"],
            str(label),
            quantity_text,
            raw_quantity_text,
            total_text,
            raw_total_text,
        )).casefold()

        return {
            "source_index": source_index,
            "date": str(transaction["date"]),
            "action": action_text,
            "action_key": transaction["action"],
            "instrument_key": transaction["instrument_key"],
            "asset": str(label),
            "amount": quantity_text,
            "total": total_text,
            "searchable": searchable,
            "sort_values": {
                "date": PortfolioManagerDialog._sortable_date(transaction["date"]),
                "action": action_text.casefold(),
                "asset": str(label).casefold(),
                "amount": transaction["quantity"],
                "total": raw_total,
            },
        }

    @staticmethod
    def _sortable_date(value):
        return TransactionManager.parse_record_date(value) or datetime.min

    def cycle_sort(self, column):
        if column not in ("date", "action", "asset", "amount", "total"):
            return
        self.sort_direction = (self.sort_direction + 1) % 3 if self.sort_column == column else 1
        self.sort_column = column if self.sort_direction else None
        self.load_list()

    def _refresh_asset_filter_values(self):
        labels = []
        seen = set()

        for instrument in self.instruments:
            label = str(instrument.get("label", instrument.get("key", ""))).strip()
            if label and label not in seen:
                seen.add(label)
                labels.append(label)

        for index, raw_transaction in enumerate(self.manager.transactions):
            label = self._transaction_row(index, raw_transaction)["asset"]
            if label and label not in seen:
                seen.add(label)
                labels.append(label)

        values = (self.ALL_ASSETS_LABEL, *labels)
        self.combo_asset_filter.configure(values=values)
        if self.var_asset_filter.get() not in values:
            self.var_asset_filter.set(self.ALL_ASSETS_LABEL)

    def _filtered_transaction_rows(self):
        query = self.var_search.get().strip().casefold()
        normalized_query = query.replace(",", ".")
        selected_asset = self.var_asset_filter.get()
        rows = []

        for source_index, raw_transaction in enumerate(self.manager.transactions):
            row = self._transaction_row(source_index, raw_transaction)
            if selected_asset != self.ALL_ASSETS_LABEL and row["asset"] != selected_asset:
                continue
            if query:
                searchable = row["searchable"]
                normalized_searchable = searchable.replace(",", ".")
                if query not in searchable and normalized_query not in normalized_searchable:
                    continue
            rows.append(row)
        return rows

    def load_list(self):
        if self.quick:
            return
        self._refresh_asset_filter_values()
        rows = self._filtered_transaction_rows()
        if self.sort_column and self.sort_direction:
            rows.sort(key=lambda row: row["sort_values"][self.sort_column],
                      reverse=self.sort_direction == 2)
        self.tree.set_sort_indicator(self.sort_column, self.sort_direction)
        self.tree.set_rows(rows)

        total = len(self.manager.transactions)
        filters_active = bool(self.var_search.get().strip()) or (
            self.var_asset_filter.get() != self.ALL_ASSETS_LABEL
        )
        if filters_active:
            self.var_result_count.set(f"{len(rows)} / {total} işlem")
        else:
            self.var_result_count.set(f"Toplam {total} işlem")

    def on_click(self, event):
        """Compatibility dispatcher for callers that provide row metadata.

        ``TransactionTable`` binds its edit/delete buttons directly, but this
        method remains available for the dialog's historical callback API.
        """
        item_id = getattr(event, "source_index", None)
        action = getattr(event, "action", None)
        if item_id is None:
            return
        if action == "edit":
            self.start_edit(str(item_id))
        elif action == "delete":
            self.delete_transaction(str(item_id))

    def start_edit(self, item_id):
        idx = int(item_id)
        if idx < 0 or idx >= len(self.manager.transactions):
            return
        transaction = self.manager.normalize_transaction(self.manager.transactions[idx])
        key = transaction["instrument_key"]
        instrument = self.instrument_by_key.get(key)
        display = next((label for label, asset_key in self.instrument_label_to_key.items() if asset_key == key), None)
        if display is None:
            label = transaction["instrument_label"]
            instrument = instrument or {"key": key, "label": label}
            display = f"{label} · {key}" if label in self.instrument_label_to_key else label
            self.instrument_by_key[key] = instrument
            self.instrument_label_to_key[display] = key
            self.combo_instrument.configure(values=tuple(self.instrument_label_to_key))
        self.var_instrument.set(display)
        self.update_amount_label()

        self.entry_date.delete(0, 'end')
        self.entry_date.insert(0, transaction["date"])
        self.var_action.set(transaction["action"])
        self.entry_amount.delete(0, 'end')
        self.entry_amount.insert(0, self._trade_input_number(transaction["quantity"]))
        self.var_currency.set(transaction["currency"])
        self.toggle_rate_entry()
        self._price_mode = "Toplam tutar"
        self.var_price_mode.set(self._price_mode)
        self.entry_total.delete(0, 'end')
        if transaction["currency"] == "USD":
            self.entry_total.insert(0, self._trade_input_number(transaction["total_usd"]))
            self._set_rate_input(transaction["fx_rate"])
        else:
            self.entry_total.insert(0, self._trade_input_number(transaction["total_tl"]))
        fee = transaction["fee_tl"]
        self.var_fee.set(self._trade_input_number(fee) if math.isfinite(fee) and fee else "")

        self.edit_index = idx
        self.var_form_title.set("İşlemi düzenle" if self.quick else "İşlemi Düzenle")
        self.btn_save.config(text="Değişiklikleri kaydet" if self.quick else "Değişiklikleri Kaydet")
        self.mark_current_draft()
        self._queue_trade_preview()
        if not self.quick and not self.btn_cancel_edit.winfo_ismapped():
            self.btn_cancel_edit.pack(fill="x")

    def cancel_edit(self):
        self.edit_index = None
        self.var_form_title.set("İşlem ekle" if self.quick else "Yeni İşlem")
        self.btn_save.config(text="İşlemi kaydet" if self.quick else "+  İşlemi Ekle")
        self.btn_cancel_edit.pack_forget()
        self.clear_form()

    def clear_form(self):
        self.entry_amount.delete(0, 'end')
        self.entry_total.delete(0, 'end')
        self.var_fee.set("")
        self.entry_date.delete(0, 'end')
        self.entry_date.insert(0, datetime.now().strftime("%d-%m-%Y"))
        self.var_action.set("buy")
        self.var_currency.set("TL")
        self._price_mode = "Birim fiyat" if self.quick else "Toplam tutar"
        self.var_price_mode.set(self._price_mode)
        self._set_rate_input(self.current_dollar_rate)
        self.toggle_rate_entry()
        self.mark_current_draft()

    def delete_transaction(self, item_id):
        idx = int(item_id)
        if self.confirm_transaction_deletion(self, self.manager, self.instrument_by_key, idx):
            self.refresh_after_deletion(idx)
            self.on_save_callback()

    @staticmethod
    def confirm_transaction_deletion(parent, manager, instruments, index):
        if not 0 <= index < len(manager.transactions):
            return False
        transaction = manager.normalize_transaction(manager.transactions[index])
        instrument = instruments.get(transaction["instrument_key"], {})
        label = instrument.get("label", transaction["instrument_label"])
        action = "alış" if transaction["action"] == "buy" else "satış"
        prompt = f"{transaction['date']} tarihindeki {label} {action} kaydı silinecek. Onaylıyor musunuz?"
        if not messagebox.askyesno("İşlemi sil", prompt, parent=parent):
            return False
        try:
            manager.delete(index)
        except (OSError, ValueError) as error:
            messagebox.showerror("İşlem silinemedi", str(error), parent=parent)
            return False
        return True

    def refresh_after_deletion(self, index):
        if self.edit_index == index:
            self.cancel_edit()
        elif self.edit_index is not None and self.edit_index > index:
            self.edit_index -= 1
        self.load_list()
        self.update_amount_label()
        self._queue_trade_preview()

    def _read_trade_form(self):
        instrument = self.selected_instrument()
        if not instrument:
            raise ValueError("Lütfen bir varlık seçin.")
        date = self.manager.parse_record_date(self.var_date.get())
        if date is None:
            raise ValueError("İşlem tarihini gün-ay-yıl biçiminde girin veya takvimden seçin.")
        if date.date() > datetime.now().date():
            raise ValueError("İşlem tarihi gelecekte olamaz.")
        amount = self._parse_trade_number(self.var_amount.get())
        entered_price = self._parse_trade_number(self.var_entered_total.get())
        if amount <= 0 or entered_price < 0:
            raise ValueError("Miktar pozitif, tutar sıfır veya pozitif olmalı.")
        total = entered_price * amount if self.var_price_mode.get() == "Birim fiyat" else entered_price
        currency = self.var_currency.get()
        original = self.manager.transactions[self.edit_index] if self.edit_index is not None else {}
        original = original if isinstance(original, dict) else {}
        data = {
            **original,
            "date": date.strftime("%d-%m-%Y"),
            "instrument_key": instrument["key"],
            "instrument_label": instrument["label"],
            "action": self.var_action.get(),
            "quantity": amount,
            "currency": currency,
            "fee_tl": self._parse_trade_number(self.var_fee.get()) if self.var_fee.get().strip() else 0.0,
        }
        for field in ("symbol", "currency", "source", "decimals"):
            if instrument.get(field) is not None:
                data[f"instrument_{field}"] = instrument[field]
        if currency == "USD":
            try:
                rate = self._parse_trade_number(self.var_rate.get())
            except ValueError:
                rate = 0
            if rate <= 0:
                raise ValueError("USD işlemi için pozitif bir işlem kuru girin.")
            original_values = self.manager.normalize_transaction(original)
            if original_values["currency"] == "USD" and (original_values["total_usd"], original_values["fx_rate"]) == (total, rate):
                total_tl = original_values["total_tl"]
            else:
                total_tl = total * rate
            data.update(total_usd=total, fx_rate=rate, total_tl=total_tl)
        else:
            data.update(total_tl=total, total_usd=0, fx_rate=0)
        if not math.isfinite(data["total_tl"]):
            raise ValueError("İşlem tutarı çok büyük.")
        return data

    def save(self):
        try:
            data = self._read_trade_form()
            if self.edit_index is None:
                self.manager.save(data)
            else:
                self.manager.replace(self.edit_index, data)
        except ValueError as error:
            messagebox.showerror("İşlem kaydedilemedi", str(error), parent=self)
            return
        except OSError as error:
            messagebox.showerror("Kayıt hatası", str(error), parent=self)
            return

        if self.quick:
            self.destroy()
        else:
            self.cancel_edit()
            self.load_list()
            self.update_amount_label()
        self.on_save_callback()


class WatchlistDialog(tk.Toplevel):
    def __init__(self, parent, manager, on_change_callback, price_validator):
        super().__init__(parent)
        self.manager = manager
        self.on_change_callback = on_change_callback
        self.price_validator = price_validator
        self.title("İzlenenler")
        self.geometry("780x560")
        self.minsize(700, 520)
        self.configure(bg=THEME["background"])
        configure_ttk_styles(self)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self, bg=THEME["background"])
        header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=SPACING["xl"],
            pady=(SPACING["lg"], SPACING["md"]),
        )
        tk.Label(
            header,
            text="İzlenen Varlıklar",
            bg=THEME["background"],
            fg=THEME["text_primary"],
            font=font(self, "title", "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Widget'ta gösterilecek piyasa sembollerini yönetin.",
            bg=THEME["background"],
            fg=THEME["text_secondary"],
            font=font(self, "body"),
            anchor="w",
        ).pack(anchor="w", pady=(SPACING["xxs"], 0))

        content = tk.Frame(self, bg=THEME["background"])
        content.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=SPACING["xl"],
            pady=(0, SPACING["xl"]),
        )
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1, minsize=410)
        content.grid_columnconfigure(1, weight=0, minsize=260)

        left_frame = tk.Frame(
            content,
            bg=THEME["surface"],
            padx=SPACING["md"],
            pady=SPACING["md"],
            highlightthickness=1,
            highlightbackground=THEME["border"],
        )
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, SPACING["sm"]))
        left_frame.grid_rowconfigure(1, weight=1)
        left_frame.grid_columnconfigure(0, weight=1)

        tk.Label(
            left_frame,
            text="İzleme Listesi",
            bg=THEME["surface"],
            fg=THEME["text_primary"],
            font=font(self, "section", "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, SPACING["sm"]))

        table_frame = tk.Frame(left_frame, bg=THEME["border"], padx=1, pady=1)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        columns = ("label", "symbol", "currency", "delete")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=13,
            style="Modern.Treeview",
        )
        self.tree.heading("label", text="Ad")
        self.tree.heading("symbol", text="Yahoo Sembolü")
        self.tree.heading("currency", text="Birim")
        self.tree.heading("delete", text="")
        self.tree.column("label", width=150, minwidth=100, anchor="w")
        self.tree.column("symbol", width=120, minwidth=90, anchor="center")
        self.tree.column("currency", width=60, minwidth=50, anchor="center", stretch=False)
        self.tree.column("delete", width=52, minwidth=48, anchor="center", stretch=False)
        self.tree.tag_configure("hidden", foreground=THEME["text_muted"])
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<ButtonRelease-1>", self.on_click)

        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview,
            style="Modern.Vertical.TScrollbar",
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        right_frame = tk.Frame(
            content,
            bg=THEME["surface"],
            padx=SPACING["lg"],
            pady=SPACING["lg"],
            highlightthickness=1,
            highlightbackground=THEME["border"],
        )
        right_frame.grid(row=0, column=1, sticky="nsew")

        tk.Label(
            right_frame,
            text="Yeni Varlık",
            bg=THEME["surface"],
            fg=THEME["text_primary"],
            font=font(self, "section", "bold"),
        ).pack(anchor="w")
        tk.Label(
            right_frame,
            text="Geçerli bir Yahoo Finance sembolü ekleyin.",
            bg=THEME["surface"],
            fg=THEME["text_secondary"],
            font=font(self, "small"),
            wraplength=220,
            justify="left",
        ).pack(anchor="w", pady=(SPACING["xxs"], SPACING["md"]))

        field_label = {
            "bg": THEME["surface"],
            "fg": THEME["text_secondary"],
            "font": font(self, "small"),
        }

        tk.Label(right_frame, text="Görünen Ad", **field_label).pack(anchor="w")
        self.entry_label = ModernEntry(right_frame)
        self.entry_label.pack(fill="x", pady=(SPACING["xxs"], SPACING["sm"]))

        tk.Label(right_frame, text="Yahoo Sembolü", **field_label).pack(anchor="w")
        self.entry_symbol = ModernEntry(right_frame)
        self.entry_symbol.pack(fill="x", pady=(SPACING["xxs"], SPACING["xxs"]))
        tk.Label(
            right_frame,
            text="Örnek: THYAO.IS, AAPL, BTC-USD",
            bg=THEME["surface"],
            fg=THEME["text_muted"],
            font=font(self, "caption"),
        ).pack(anchor="w", pady=(0, SPACING["sm"]))

        tk.Label(right_frame, text="Para Birimi", **field_label).pack(anchor="w")
        self.var_currency = tk.StringVar(value="₺")
        self.combo_currency = ttk.Combobox(
            right_frame,
            textvariable=self.var_currency,
            values=["₺", "$", "€", ""],
            state="readonly",
            style="Modern.TCombobox",
        )
        self.combo_currency.pack(fill="x", pady=(SPACING["xxs"], SPACING["sm"]))

        self.var_status = tk.StringVar(value="")
        tk.Label(
            right_frame,
            textvariable=self.var_status,
            bg=THEME["surface"],
            fg=THEME["text_secondary"],
            font=font(self, "small"),
            wraplength=220,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(0, SPACING["xs"]))

        self.btn_add = HoverButton(
            right_frame,
            text="+  Varlık Ekle",
            command=self.add_symbol,
            pady=10,
        )
        self.btn_add.pack(fill="x")

        self.load_list()

    def load_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        visible = {item["key"] for item in self.manager.instruments}
        for instrument in (*self.manager.instruments, *self.manager.archived.values()):
            active = instrument["key"] in visible
            if self.tree.exists(instrument["key"]):
                continue
            self.tree.insert("", "end", iid=instrument["key"], values=(
                instrument["label"],
                instrument["symbol"],
                instrument.get("currency", ""),
                "Gizle" if active else "Göster",
            ), tags=() if active else ("hidden",))

    def on_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        column = self.tree.identify_column(event.x)
        if column != "#4":
            return
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        try:
            if item_id in self.manager.archived:
                self.manager.restore(item_id)
            else:
                self.manager.remove(item_id)
            self.load_list()
            self.on_change_callback()
        except (OSError, ValueError) as error:
            messagebox.showerror("İzleme listesi", str(error), parent=self)

    def add_symbol(self):
        label = self.entry_label.get().strip()
        symbol = self.entry_symbol.get().strip().upper()
        currency = self.var_currency.get()

        if not label or not symbol:
            messagebox.showwarning("Eksik Bilgi", "Görünen ad ve Yahoo sembolü zorunlu.", parent=self)
            return
        if self.manager.has_symbol(symbol):
            messagebox.showwarning("Tekrar", "Bu sembol zaten izleme listesinde.", parent=self)
            return

        self.btn_add.config(state="disabled")
        self.var_status.set("Sembol kontrol ediliyor...")
        threading.Thread(target=self._validate_and_add, args=(label, symbol, currency), daemon=True).start()

    def _validate_and_add(self, label, symbol, currency):
        error = None
        try:
            price = self.price_validator(symbol)
            if not price or price <= 0:
                error = "Bu sembol için geçerli fiyat bulunamadı."
        except Exception as e:
            error = f"Sembol doğrulanamadı: {e}"
        self.after(0, lambda: self._finish_add(label, symbol, currency, error))

    def _finish_add(self, label, symbol, currency, error):
        self.btn_add.config(state="normal")
        if error:
            self.var_status.set("")
            messagebox.showerror("Sembol Eklenemedi", error, parent=self)
            return
        try:
            self.manager.add(label, symbol, currency)
            self.load_list()
            self.on_change_callback()
            self.entry_label.delete(0, 'end')
            self.entry_symbol.delete(0, 'end')
            self.var_status.set("Eklendi.")
        except ValueError as e:
            self.var_status.set("")
            messagebox.showerror("Hata", str(e), parent=self)


class PiyasaWidget:
    ROW_DRAG_HOLD_MS = 350
    ROW_DRAG_MOVE_TOLERANCE_PX = 6
    SUMMARY_WATCH_COUNT = 5
    DEFAULT_WIDGET_WIDTH = 368
    DEFAULT_WIDGET_HEIGHT = 536
    DEFAULT_RIGHT_MARGIN = 30
    DEFAULT_TOP_MARGIN = 50
    CHART_PERIOD_OPTIONS = (
        ("30 dakika", 30 * 60),
        ("1 saat", 60 * 60),
        ("6 saat", 6 * 60 * 60),
        ("12 saat", 12 * 60 * 60),
        ("24 saat", 24 * 60 * 60),
        ("7 gün", 7 * 24 * 60 * 60),
        ("30 gün", 30 * 24 * 60 * 60),
        ("Tümü", 0),
    )

    @classmethod
    def calculate_default_geometry(cls, screen_width, screen_height):
        width = min(cls.DEFAULT_WIDGET_WIDTH, max(300, screen_width - 20))
        available_height = max(360, screen_height - 100)
        height = min(cls.DEFAULT_WIDGET_HEIGHT, available_height)
        x = max(0, screen_width - width - cls.DEFAULT_RIGHT_MARGIN)
        y = max(0, min(cls.DEFAULT_TOP_MARGIN, screen_height - height))
        return width, height, x, y

    def _initial_widget_geometry(self, screen_width, screen_height):
        default = self.calculate_default_geometry(screen_width, screen_height)
        saved = self._preferences.get("widget_geometry")
        if not isinstance(saved, dict):
            return default
        width, height, x, y = (saved.get(key) for key in ("width", "height", "x", "y"))
        if (
            not all(type(value) is int for value in (width, height, x, y))
            or width <= 0 or height <= 0
            or not all(-2**30 <= value <= 2**30 for value in (x, y))
        ):
            return default
        # Use the visible header when checking whether the previous display still exists.
        area = monitor_work_area(x, y, min(128, width), 28)
        if area is None:
            return default
        left, top, right, bottom = area
        width = min(max(300, width), right - left)
        height = min(max(360, height), bottom - top)
        return width, height, min(max(left, x), right - width), min(max(top, y), bottom - height)

    def __init__(self):
        enable_high_dpi()
        self.root = tk.Tk()
        self.root.title("Market Widget")
        
        # --- AYARLAR ---
        self.bg_color = "#1e1e1e"  # Koyu Gri Arka Plan
        self.text_color = "#00ff41" # Matrix Yeşili
        self.alpha = 1.0           # Saydamlık kapalı (tam opak)
        self.refresh_rate = 60     # Saniye cinsinden yenileme
        self.preferences_path = os.path.join(APP_DIR, "preferences.json")
        self._preferences = self._load_preferences()
        self.default_topmost = self._preferences.get("topmost") is True
        
        # Pencere Ayarları
        self.root.overrideredirect(True) # Çerçevesiz
        if self.alpha < 1.0:
            self.root.attributes("-alpha", self.alpha)
        # self.root.attributes("-topmost", True) # Her zaman üstte - Widget modunda her zaman üstte olması istenmeyebilir, ama widget mantığı genelde masaüstünde durur. Kullanıcı "arkaplanda" dedi.
        # Kullanıcı "üstte" demedi, "arkaplanda" dedi. Genellikle widgetlar masaüstünde durur (altta).
        # Ancak "Topmost" açık olursa diğer pencerelerin üstünde durur. Kullanıcı bunu istemiyor olabilir.
        # "Programın altta uygulama olarak gözükerek değil" -> Taskbar'da görünmesin.
        
        if self.default_topmost:
            self.root.attributes("-topmost", True)
        
        self.root.configure(bg=self.bg_color)
        icon_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else APP_DIR
        self.brand_logo_path = os.path.join(icon_dir, "icon.ico")
        self._brand_logo = None
        try:
            with Image.open(self.brand_logo_path) as logo:
                self._brand_logo = ImageTk.PhotoImage(logo.ico.getimage((24, 24)), master=self.root)
            self.root.iconbitmap(default=self.brand_logo_path)
        except (OSError, tk.TclError):
            pass
        
        # Taskbar'dan gizleme (Windows Widget Modu)
        self.make_toolwindow()
        
        # Başlangıç Konumu (Sağ Üst)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width, height, x, y = self._initial_widget_geometry(
            screen_width, screen_height
        )
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        
        # Managers
        self.tm = TransactionManager()
        self.asm = AutoStartManager()
        self.um = UpdateManager(VERSION, GITHUB_REPO)
        self.watchlist_manager = WatchlistManager()
        self.watchlist = self.watchlist_manager.instruments
        self.history_db = MarketHistoryDB()
        self.current_page = 0
        self._fetch_lock = threading.Lock()
        self._history_fetch_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._history_thread = None
        self._history_resync_requested = False
        self._last_prices = {}
        self.workspace = None
        self._row_hold_after_id = None
        self._row_press_key = None
        self._row_press_origin = None
        self._row_press_last = None
        self._row_drag_key = None
        self._row_drag_order = None
        self._row_drag_original_order = None
        self.price_row_widgets = {}
        self.sparkline_period = tk.IntVar(master=self.root, value=self._preferences["sparkline_period"])
        
        # UI Elemanları
        self.setup_ui()
        self._update_portfolio_view()
        
        # Sürükleme Özelliği
        self._is_resizing = False
        self._window_move_origin = None
        self.root.bind("<Button-1>", self.start_move)
        self.root.bind("<B1-Motion>", self.do_move)
        self.root.bind("<ButtonRelease-1>", self._on_button_release)
        
        # Sağ Tık Menüsü
        menu_style = {
            "bg": self.color_card_alt,
            "fg": self.color_text_main,
            "activebackground": self.color_accent,
            "activeforeground": THEME["primary_ink"],
            "disabledforeground": self.color_text_muted,
            "bd": 0,
            "relief": "flat",
        }
        self.menu = tk.Menu(self.root, tearoff=0, **menu_style)
        self.menu.add_command(label="Kapat", command=self.kapat)
        self.root.bind("<Button-3>", self.show_menu)

        # Ayarlar Menüsü (çark simgesi)
        self.var_autostart = tk.BooleanVar(value=self.asm.is_enabled())
        self.var_topmost = tk.BooleanVar(value=self.default_topmost)
        self.settings_menu = ThemedPopupMenu(self.root, width=340)
        self.settings_menu.add_command(label="Portföyü yönet", command=self.open_add_transaction)
        self.settings_menu.add_command(label="İşlemleri içe aktar", command=self.import_transactions)
        self.settings_menu.add_command(label="Veri ve yedek", command=lambda: self.open_workspace("settings"))
        self.settings_menu.add_separator()
        self.settings_menu.add_checkbutton(
            label="Windows ile başlat",
            variable=self.var_autostart,
            command=self.toggle_autostart,
        )
        self.settings_menu.add_checkbutton(
            label="Her zaman üstte",
            variable=self.var_topmost,
            command=self.toggle_topmost,
        )
        self.settings_menu.add_separator()
        self.sparkline_period_control = self.settings_menu.add_choice(
            label="Mini grafik zaman aralığı",
            variable=self.sparkline_period,
            options=(("30 dk", 1800), ("1 sa", 3600), ("6 sa", 21600), ("12 sa", 43200),
                     ("24 sa", 86400), ("7 gün", 604800), ("30 gün", 2592000), ("Tümü", 0)),
            command=self._on_sparkline_period_changed,
        )
        self.settings_menu.add_separator()
        self.settings_menu.add_command(label="Detayları aç", command=self.open_workspace)
        self.settings_menu.add_command(label="İstatistikler", command=lambda: self.show_page(2))
        self.settings_menu.add_command(
            label="İzlenenleri düzenle",
            command=self.open_watchlist_settings,
        )
        self.settings_menu.add_command(
            label="Güncellemeleri kontrol et",
            command=self.check_updates,
        )
        self.settings_menu.add_separator()
        self.settings_menu.add_command(
            label="Kapat",
            command=self.kapat,
            danger=True,
        )
        
        cached = self._compatible_cached_data(self.load_last_data())
        if cached:
            self.guncelle_arayuz(cached)
        self.update_thread = threading.Thread(target=self.veri_dongusu, daemon=True)
        self.update_thread.start()
        
    def _load_preferences(self):
        try:
            with open(self.preferences_path, "r", encoding="utf-8") as source:
                preferences = json.load(source)
            if not isinstance(preferences, dict):
                preferences = {}
        except (OSError, ValueError):
            preferences = {}
        allowed = {value for _label, value in self.CHART_PERIOD_OPTIONS}
        period = preferences.get("sparkline_period")
        if type(period) is not int or period not in allowed:
            preferences["sparkline_period"] = 86400
        return preferences

    def _save_preferences(self):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(self.preferences_path),
                prefix=".preferences-", suffix=".json", delete=False,
            ) as target:
                temporary = target.name
                json.dump(self._preferences, target, ensure_ascii=False, indent=2)
            os.replace(temporary, self.preferences_path)
        except OSError as exc:
            log_message(f"Pencere veya grafik ayarı kaydedilemedi: {exc}")
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError as exc:
                    log_message(f"Geçici ayar dosyası silinemedi: {exc}")

    def _on_sparkline_period_changed(self):
        self._preferences["sparkline_period"] = self.sparkline_period.get()
        self._save_preferences()
        if hasattr(self, "var_watch_period"):
            self.var_watch_period.set(self._widget_watch_period())
        self._refresh_history_views()

    def _save_widget_geometry(self, previous_position=None):
        if not self.root.winfo_ismapped():
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if (x, y) == previous_position:
            return
        geometry = {
            "width": self.root.winfo_width(), "height": self.root.winfo_height(),
            "x": x, "y": y,
        }
        if self._preferences.get("widget_geometry") != geometry:
            self._preferences["widget_geometry"] = geometry
            self._save_preferences()

    def setup_ui(self):
        """Build the compact desktop widget without changing its state contract."""
        configure_ttk_styles(self.root)

        # Shared theme tokens are mirrored onto the legacy attributes used by
        # the existing render/update callbacks.
        self.bg_color = THEME["background"]
        self.color_card = THEME["surface"]
        self.color_card_alt = THEME["surface_alt"]
        self.color_border = THEME["border"]
        self.color_text_main = THEME["text_primary"]
        self.color_text_dim = THEME["text_secondary"]
        self.color_text_muted = THEME["text_muted"]
        self.color_accent = THEME["primary"]
        self.color_success = THEME["positive"]
        self.color_danger = THEME["negative"]
        self.color_gold = THEME["gold"]
        self.color_success_bg = THEME["positive_soft"]
        self.color_danger_bg = THEME["negative_soft"]
        self.root.configure(bg=self.bg_color)

        # Dynamic fonts are retained because resize handling updates these
        # exact tkfont.Font instances in-place.
        self.base_fonts = {
            "header": 9,
            "label": 9,
            "value": 11,
            "portfolio": 25,
            "profit": 9,
            "market_status": 14,
            "icon": 10,
            "small": 8,
            "badge": 8,
            "chart_text": 10,
            "chart_title": 8,
            "chart_tick": 6,
            "chart_val": 7,
            "stats_title": 8,
            "stats_val": 7
        }

        font_family = font(self.root, "body")[0]
        self.font_family = font_family
        self.font_header = tkfont.Font(family=font_family, size=self.base_fonts["header"])
        self.font_label = tkfont.Font(family=font_family, size=self.base_fonts["label"], weight="bold")
        self.font_value = tkfont.Font(family=font_family, size=self.base_fonts["value"], weight="bold")
        self.font_portfolio = tkfont.Font(family=font_family, size=self.base_fonts["portfolio"], weight="bold")
        self.font_profit = tkfont.Font(family=font_family, size=self.base_fonts["profit"], weight="bold")
        self.font_market_status = tkfont.Font(family=font_family, size=self.base_fonts["market_status"], weight="bold")
        self.font_icon = tkfont.Font(family=font_family, size=10)
        self.font_small = tkfont.Font(family=font_family, size=8)
        self.font_badge = tkfont.Font(family=font_family, size=8, weight="bold")
        
        # Responsive tasarım için takip
        self.last_width = 320
        self._resize_after_id = None  # Debounce timer
        self.root.bind("<Configure>", self.on_resize)
        
        # Ana Konteyner
        self.frame = tk.Frame(
            self.root,
            bg=self.bg_color,
            padx=SPACING["sm"],
            pady=SPACING["sm"],
        )
        self.frame.pack(fill="both", expand=True)
        
        # 1. ÜST HEADER (durum + saat + sayfa navigasyonu)
        header_frame = tk.Frame(self.frame, bg=self.bg_color)
        header_frame.pack(fill="x", pady=(0, SPACING["xs"]))

        brand_frame = tk.Frame(header_frame, bg=self.bg_color)
        brand_frame.pack(fill="x", pady=(0, SPACING["xxs"]))
        if self._brand_logo is not None:
            tk.Label(brand_frame, image=self._brand_logo, bg=self.bg_color, bd=0).pack(side="left", padx=(0, 4))
        tk.Label(
            brand_frame, text="Piyasa", bg=self.bg_color, fg=self.color_text_main,
            font=font(self.root, "section", "bold"), anchor="w",
        ).pack(side="left")
        self.btn_open_workspace = HoverButton(
            brand_frame, text="Detaylar", command=self._open_workspace_from_widget,
            bg=self.color_card_alt, fg=self.color_accent,
            hover_bg=THEME["surface_hover"], pressed_bg=THEME["surface_pressed"],
            padx=SPACING["sm"], pady=3,
        )
        self.btn_open_workspace.pack(side="right")

        status_bar = tk.Frame(header_frame, bg=self.bg_color)
        status_bar.pack(fill="x")
        status_frame = tk.Frame(status_bar, bg=self.bg_color)
        status_frame.pack(side="left", fill="x", expand=True)

        self.var_market_status = tk.StringVar(value="●")
        self.lbl_market_status = tk.Label(
            status_frame,
            textvariable=self.var_market_status,
            bg=self.bg_color,
            fg=self.color_text_dim,
            font=self.font_header,
            anchor="center",
            pady=0,
        )
        self.lbl_market_status.pack(side="left", fill="y", padx=(0, SPACING["xxs"]))

        self.var_market_label = tk.StringVar(value="Piyasa bekleniyor")
        self.lbl_market_label = tk.Label(
            status_frame,
            textvariable=self.var_market_label,
            bg=self.bg_color,
            fg=self.color_text_dim,
            font=self.font_header,
            anchor="w",
        )
        self.lbl_market_label.pack(side="left", fill="y")
        
        # Navigasyon çerçevesi
        nav_frame = tk.Frame(status_bar, bg=self.bg_color)
        nav_frame.pack(side="right")
        
        self.var_time = tk.StringVar(value="--:--")
        self.lbl_time = tk.Label(nav_frame, textvariable=self.var_time, bg=self.bg_color, fg=self.color_text_dim, font=self.font_header, anchor="center")
        self.lbl_time.pack(side="left", fill="y", padx=4)
        status_frame.pack_configure(fill="both")

        navigation = tk.Frame(header_frame, bg=self.bg_color)
        navigation.pack(fill="x", pady=(SPACING["xxs"], 0))
        self.widget_nav = {}
        for mode, label in (("summary", "Özet"), ("watch", "İzleme"), ("portfolio", "Portföy")):
            button = HoverButton(
                navigation, text=label, command=lambda target=mode: self._show_widget_overview(target),
                bg=self.color_card_alt, fg=self.color_text_main, hover_bg=THEME["surface_hover"],
                pressed_bg=THEME["surface_pressed"], padx=7, pady=2,
            )
            button.pack(side="left", padx=(0, 4))
            self.widget_nav[mode] = button
        self._overview_mode = "summary"
        self._summary_breakdown_open = False

        # 4. FOOTER (kompakt araç çubuğu) - footer önce pack edilir (side=bottom)
        footer_frame = tk.Frame(
            self.frame,
            bg=self.color_card,
            padx=SPACING["xs"],
            pady=SPACING["xxs"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        footer_frame.pack(side="bottom", fill="x", pady=(SPACING["xs"], 0))

        self.btn_quick_trade = HoverButton(
            footer_frame, text="+  İşlem ekle", command=lambda: self.open_add_transaction(),
            padx=SPACING["sm"], pady=4,
        )
        self.btn_quick_trade.pack(side="left")
        self.var_trade_feedback = tk.StringVar(master=self.root)
        tk.Label(footer_frame, textvariable=self.var_trade_feedback, bg=self.color_card,
                 fg=self.color_accent, font=self.font_small).pack(side="left", padx=4)
        self.root.bind("<Destroy>", self._cancel_trade_feedback, add="+")
        self.footer_buttons = {}
        for key, icon_name, callback in (
            ("settings", "settings", self.open_settings),
            ("refresh", "refresh", self.request_data_refresh),
        ):
            button = IconButton(
                footer_frame,
                icon_name,
                callback,
                size=32,
                bg=self.color_card,
                hover_bg=THEME["surface_hover"],
                fg=self.color_text_dim,
                hover_fg=self.color_text_main,
                show_border=False,
            )
            button.pack(side="right", padx=(SPACING["xs"], 0))
            self.footer_buttons[key] = button

        # 2. İÇERİK KONTEYNERİ (Sayfa bazlı geçiş)
        self.content_container = tk.Frame(self.frame, bg=self.bg_color)
        self.content_container.pack(fill="both", expand=True)
        
        # --- Sayfa 0: Ana Sayfa ---
        self.page_main = tk.Frame(self.content_container, bg=self.bg_color)
        self.page_main.grid_rowconfigure(0, weight=1)
        self.page_main.grid_columnconfigure(0, weight=1)
        self.overview_canvas = tk.Canvas(
            self.page_main, bg=self.bg_color, highlightthickness=0, bd=0,
            yscrollincrement=28,
        )
        self.overview_canvas.grid(row=0, column=0, sticky="nsew")
        self.overview_scrollbar = ttk.Scrollbar(
            self.page_main, command=self.overview_canvas.yview, orient="vertical",
            style="Modern.Vertical.TScrollbar",
        )
        self.overview_canvas.configure(yscrollcommand=self.overview_scrollbar.set)
        self.overview_body = tk.Frame(self.overview_canvas, bg=self.bg_color)
        self._overview_window = self.overview_canvas.create_window(
            (0, 0), window=self.overview_body, anchor="nw",
        )
        self.overview_canvas.bind("<Configure>", self._resize_overview)
        self.overview_body.bind("<Configure>", self._sync_overview_scroll)
        self.root.bind("<MouseWheel>", self._on_overview_mousewheel, add="+")

        self.portfolio_frame = portfolio_frame = tk.Frame(
            self.overview_body,
            bg=self.color_card,
            padx=SPACING["sm"],
            pady=SPACING["xs"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        portfolio_frame.pack(fill="x", pady=(0, SPACING["xs"]))
        
        portfolio_header = tk.Frame(portfolio_frame, bg=self.color_card)
        portfolio_header.pack(fill="x")
        tk.Label(portfolio_header, text="Portföy değeri", bg=self.color_card, fg=self.color_text_dim, font=self.font_header).pack(side="left")
        
        self.var_portfolio = tk.StringVar(value="₺...")
        tk.Label(portfolio_frame, textvariable=self.var_portfolio, bg=self.color_card, fg=self.color_text_main, font=self.font_portfolio, anchor="w").pack(fill="x", pady=(0, SPACING["xxs"]))
        
        self.var_profit = tk.StringVar(value="...")
        portfolio_footer = tk.Frame(portfolio_frame, bg=self.color_card)
        portfolio_footer.pack(fill="x")
        self.lbl_profit = tk.Label(portfolio_footer, textvariable=self.var_profit, bg=THEME["surface_hover"], fg=self.color_text_dim, font=self.font_profit, anchor="w", padx=SPACING["xs"], pady=2)
        self.lbl_profit.pack(side="left")
        self.var_portfolio_coverage = tk.StringVar(master=self.root)
        tk.Label(portfolio_footer, textvariable=self.var_portfolio_coverage, bg=self.color_card,
                 fg=self.color_text_dim, font=self.font_small).pack(side="right")

        self.portfolio_breakdown_frame = tk.Frame(portfolio_frame, bg=self.color_card)
        self.btn_portfolio_breakdown = HoverButton(
            portfolio_header, text="Pozisyonlar", command=self._toggle_widget_breakdown,
            bg=self.color_card_alt, fg=self.color_text_main, hover_bg=THEME["surface_hover"],
            pressed_bg=THEME["surface_pressed"], padx=8, pady=2,
        )
        self.btn_portfolio_breakdown.pack(side="right")

        watch_header = self.watch_header = tk.Frame(self.overview_body, bg=self.bg_color)
        watch_header.pack(fill="x", pady=(0, SPACING["xxs"]))
        self.var_watch_period = tk.StringVar(master=self.root, value=self._widget_watch_period())
        tk.Label(
            watch_header, text="İzleme listesi", bg=self.bg_color,
            fg=self.color_text_main, font=self.font_label, anchor="w",
        ).pack(side="left")
        tk.Label(watch_header, textvariable=self.var_watch_period, bg=self.bg_color,
                 fg=self.color_text_dim, font=self.font_small).pack(side="right")
        self.price_rows_frame = tk.Frame(self.overview_body, bg=self.bg_color)
        self.price_rows_frame.pack(fill="x")
        self.price_vars = {}
        self.price_change_vars = {}
        self.price_change_labels = {}
        self.price_spark_canvases = {}
        self.price_row_widgets = {}
        self.rebuild_price_rows()

        # --- Sayfa 1: Grafik Sayfası ---
        self.page_chart = tk.Frame(self.content_container, bg=self.bg_color)
        self._build_chart_page()
        
        # --- Sayfa 2: İstatistik Sayfası ---
        self.page_stats = tk.Frame(self.content_container, bg=self.bg_color)
        self._build_stats_page()
        
        # Sayfa listesi ve ilk sayfa
        self.pages = [self.page_main, self.page_chart, self.page_stats]
        self.show_page(0)

        # --- Yeniden Boyutlandırma (Resize Grip) ---
        self.grip = IconButton(self.root, "grip", None, size=14,
                               bg=self.bg_color, fg=self.color_border, show_border=False)
        self.grip.configure(cursor="sizing")
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.grip.bind("<ButtonPress-1>", self.start_resize)
        self.grip.bind("<B1-Motion>", self.do_resize)
        self.grip.bind("<ButtonRelease-1>", self._on_resize_end)

    def _resize_overview(self, event):
        self.overview_canvas.itemconfigure(self._overview_window, width=event.width)
        self._sync_overview_scroll()

    def _sync_overview_scroll(self, _event=None):
        self.overview_canvas.configure(scrollregion=self.overview_canvas.bbox("all"))
        needs_scrollbar = self.overview_body.winfo_reqheight() > self.overview_canvas.winfo_height()
        if needs_scrollbar and not self.overview_scrollbar.winfo_manager():
            self.overview_scrollbar.grid(row=0, column=1, sticky="ns", padx=(SPACING["xxs"], 0))
        elif not needs_scrollbar and self.overview_scrollbar.winfo_manager():
            self.overview_scrollbar.grid_remove()
            self.overview_canvas.yview_moveto(0)

    def _on_overview_mousewheel(self, event):
        if self.current_page == 0 and self.overview_scrollbar.winfo_ismapped() and event.delta:
            self.overview_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

    def _draw_asset_icon(self, canvas, instrument):
        """Combine an antialiased ring with native, DPI-aware font rendering."""
        canvas.delete("all")
        color = instrument.get("color", self.color_accent)
        canvas._asset_image = ImageTk.PhotoImage(render_asset_ring(24, color, None), master=canvas)
        canvas.create_image(0, 0, anchor="nw", image=canvas._asset_image)
        label = str(instrument.get("label") or instrument.get("key") or "?")
        mark = {"gumus_ons": "Ag", "gumus_tl": "Ag", "altin_tl": "Au", "dolar": "$", "thyao": "TH"}
        text = mark.get(instrument.get("key"), label[:2].upper())
        canvas._asset_font = tkfont.Font(root=canvas, family=self.font_small.actual("family"), size=-10, weight="bold")
        while canvas._asset_font.measure(text) > 15 and canvas._asset_font.cget("size") < -6:
            canvas._asset_font.configure(size=canvas._asset_font.cget("size") + 1)
        canvas.create_text(12, 12, text=text, fill=color, font=canvas._asset_font)

    def _set_price_row_hover(self, row, surface_widgets, key, entered):
        """Apply an event-only row hover without masking drag feedback."""
        if not row.winfo_exists():
            return
        bg = THEME["surface_hover"] if entered else self.color_card_alt
        row.configure(bg=bg)
        for widget in surface_widgets:
            if widget.winfo_exists():
                widget.configure(bg=bg)
        if self._row_drag_key != key:
            row.configure(
                highlightbackground=THEME["border_strong"] if entered else self.color_border
            )

    def create_price_row(self, instrument, parent=None):
        if parent is None:
            parent = self.frame
        row = tk.Frame(
            parent,
            bg=self.color_card_alt,
            padx=SPACING["xs"],
            pady=2,
            takefocus=True,
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        row.pack(fill="x", pady=SPACING["xxs"] // 2)
        row.grid_columnconfigure(1, weight=1)

        icon = tk.Canvas(row, width=24, height=24, bg=self.color_card_alt, highlightthickness=0, bd=0)
        icon.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 6))
        self._draw_asset_icon(icon, instrument)

        name_label = tk.Label(
            row, text=instrument["label"], width=1, bg=self.color_card_alt,
            fg=self.color_text_main, font=self.font_label, anchor="w",
        )
        name_label.grid(row=0, column=1, sticky="sew", padx=(0, SPACING["xs"]))
        quote_label = tk.Label(row, text="—", width=1, bg=self.color_card_alt,
                               fg=self.color_text_dim, font=self.font_small, anchor="w", pady=0)
        quote_label.grid(row=1, column=1, sticky="new", padx=(0, 4))
        self.widget_quote_labels[instrument["key"]] = quote_label

        spark = tk.Canvas(row, width=62, height=22, bg=self.color_card_alt, highlightthickness=0, bd=0)
        spark.grid(row=0, column=2, rowspan=2, sticky="e", padx=(0, SPACING["xs"]))
        
        placeholder = f"{instrument.get('currency', '')}..."
        var = tk.StringVar(value=placeholder)
        self.price_vars[instrument["key"]] = var
        if instrument["key"] == "gumus_ons":
            self.var_gumus_ons = var
        elif instrument["key"] == "gumus_tl":
            self.var_gumus_tl = var
        elif instrument["key"] == "altin_tl":
            self.var_altin_tl = var
        price_label = tk.Label(row, textvariable=var, bg=self.color_card_alt, fg=self.color_text_main, font=self.font_value, anchor="e")
        price_label.grid(row=0, column=3, rowspan=2, sticky="e", padx=(0, 6))

        change_var = tk.StringVar(value="--")
        self.price_change_vars[instrument["key"]] = change_var
        badge = tk.Label(row, textvariable=change_var, bg=THEME["surface_pressed"], fg=self.color_text_muted, font=self.font_badge, padx=6, pady=2)
        badge.grid(row=0, column=4, rowspan=2, sticky="e")
        self.price_change_labels[instrument["key"]] = badge
        self.price_spark_canvases[instrument["key"]] = spark
        self.price_row_widgets[instrument["key"]] = row
        drag_widgets = (row, icon, name_label, quote_label, spark, price_label, badge)
        self._bind_price_row_drag(drag_widgets, instrument["key"])
        row.bind("<Return>", lambda _event: self._open_price_row_chart(instrument["key"]))

        surface_widgets = (icon, name_label, quote_label, spark, price_label)
        for widget in drag_widgets:
            widget.bind(
                "<Enter>",
                lambda event, r=row, ws=surface_widgets, key=instrument["key"]: self._set_price_row_hover(r, ws, key, True),
                add="+",
            )
            widget.bind(
                "<Leave>",
                lambda event, r=row, ws=surface_widgets, key=instrument["key"]: self._set_price_row_hover(r, ws, key, False),
                add="+",
            )

    def rebuild_price_rows(self):
        if not hasattr(self, "price_rows_frame"):
            return
        self._cancel_price_row_drag(restore=True)
        for child in self.price_rows_frame.winfo_children():
            child.destroy()
        self.price_vars = {}
        self.price_change_vars = {}
        self.price_change_labels = {}
        self.price_spark_canvases = {}
        self.price_row_widgets = {}
        self.widget_quote_labels = {}
        for instrument in self.watchlist:
            self.create_price_row(instrument, self.price_rows_frame)
        self._layout_price_rows([instrument["key"] for instrument in self.watchlist])
        self._update_widget_quote_labels()

    def _bind_price_row_drag(self, widgets, key):
        for widget in widgets:
            widget.config(cursor="hand2")
            widget.bind(
                "<ButtonPress-1>",
                lambda event, item_key=key: self._on_price_row_press(event, item_key),
                add="+"
            )
            widget.bind(
                "<B1-Motion>",
                lambda event, item_key=key: self._on_price_row_motion(event, item_key),
                add="+"
            )
            widget.bind(
                "<ButtonRelease-1>",
                lambda event, item_key=key: self._on_price_row_release(event, item_key),
                add="+"
            )

    def _on_price_row_press(self, event, key):
        self._cancel_price_row_drag(restore=True)
        self._row_press_key = key
        self._row_press_origin = (event.x_root, event.y_root)
        self._row_press_last = self._row_press_origin
        self._row_hold_after_id = self.root.after(
            self.ROW_DRAG_HOLD_MS,
            lambda item_key=key: self._activate_price_row_drag(item_key),
        )
        # Every part of a quote row owns this gesture, including its padding.
        return "break"

    def _activate_price_row_drag(self, key):
        self._row_hold_after_id = None
        if self._row_press_key != key or not self._row_press_origin:
            return
        self._row_drag_key = key
        self._row_drag_original_order = [item["key"] for item in self.watchlist]
        self._row_drag_order = list(self._row_drag_original_order)
        row = self.price_row_widgets.get(key)
        if row:
            row.config(highlightbackground=self.color_accent)
            for widget in (row, *row.winfo_children()):
                widget.configure(cursor="fleur")
        self.root.config(cursor="fleur")

    def _on_price_row_motion(self, event, key):
        if self._row_press_key != key:
            return None
        self._row_press_last = (event.x_root, event.y_root)

        if self._row_drag_key != key:
            origin = self._row_press_origin
            if origin:
                dx = event.x_root - origin[0]
                dy = event.y_root - origin[1]
                tolerance = self.ROW_DRAG_MOVE_TOLERANCE_PX
                if (dx * dx) + (dy * dy) > tolerance * tolerance:
                    if self._row_hold_after_id is not None:
                        self.root.after_cancel(self._row_hold_after_id)
                    self._activate_price_row_drag(key)
            if self._row_drag_key != key:
                return "break"

        self.root.update_idletasks()
        midpoints = {}
        for item_key in self._row_drag_order or []:
            if item_key == key:
                continue
            row = self.price_row_widgets.get(item_key)
            if row and row.winfo_ismapped():
                midpoints[item_key] = row.winfo_rooty() + (row.winfo_height() / 2)
        new_order = calculate_reordered_keys(
            self._row_drag_order,
            key,
            event.y_root,
            midpoints
        )
        if new_order != self._row_drag_order:
            self._row_drag_order = new_order
            self._layout_price_rows(new_order)
        return "break"

    def _on_price_row_release(self, event, key):
        if self._row_drag_key != key:
            origin = self._row_press_origin
            is_click = self._row_press_key == key and origin is not None
            if is_click:
                dx = event.x_root - origin[0]
                dy = event.y_root - origin[1]
                tolerance = self.ROW_DRAG_MOVE_TOLERANCE_PX
                is_click = (dx * dx) + (dy * dy) <= tolerance * tolerance
            self._cancel_price_row_drag()
            if is_click:
                self._open_price_row_chart(key)
                return "break"
            return "break"

        original_order = list(self._row_drag_original_order or [])
        final_order = list(self._row_drag_order or original_order)
        try:
            self.watchlist_manager.reorder(final_order)
        except Exception as e:
            log_message(f"İzleme listesi sırası kaydedilemedi: {e}")
            self._layout_price_rows(original_order)
        else:
            self.watchlist = self.watchlist_manager.instruments
            self._layout_price_rows([item["key"] for item in self.watchlist])
            try:
                if hasattr(self, "chart_symbol_frame"):
                    self._rebuild_chart_symbol_buttons()
                if hasattr(self, "stats_frame"):
                    self._rebuild_stats_sections()
            except Exception as e:
                log_message(f"Bağlı görünüm sırası yenilenemedi: {e}")
        finally:
            self._cancel_price_row_drag()
        return "break"

    def _open_price_row_chart(self, key):
        """Open the chart page with the clicked watchlist instrument selected."""
        valid_keys = {item["key"] for item in self.watchlist}
        if key not in valid_keys or not hasattr(self, "chart_var"):
            return
        self.chart_var.set(key)
        self.show_page(1)

    def _layout_price_rows(self, ordered_keys):
        rows = []
        for key in ordered_keys:
            row = self.price_row_widgets.get(key)
            if row and row.winfo_exists():
                rows.append(row)
        for row in rows:
            row.pack_forget()
        visible = rows[:self.SUMMARY_WATCH_COUNT] if self._overview_mode == "summary" else rows
        for row in visible:
            row.pack(fill="x", pady=SPACING["xxs"] // 2)

    def _widget_watch_period(self):
        return next((label for label, seconds in self.CHART_PERIOD_OPTIONS if seconds == self.sparkline_period.get()), "")

    def _update_widget_quote_labels(self):
        for key, label in getattr(self, "widget_quote_labels", {}).items():
            status = getattr(self, "workspace_quote_status", None)
            if status is not None:
                label.configure(text=status(key, compact=True))

    def _toggle_widget_breakdown(self):
        self._summary_breakdown_open = not self._summary_breakdown_open
        self._sync_widget_breakdown()

    def _sync_widget_breakdown(self):
        if self._overview_mode == "portfolio":
            self.btn_portfolio_breakdown.pack_forget()
        else:
            self.btn_portfolio_breakdown.pack(side="right")
        if self._overview_mode == "portfolio" or self._summary_breakdown_open:
            self.portfolio_breakdown_frame.pack(fill="x", pady=(SPACING["xs"], 0))
            self.btn_portfolio_breakdown.config(text="Pozisyonları gizle")
        else:
            self.portfolio_breakdown_frame.pack_forget()
            self.btn_portfolio_breakdown.config(text="Pozisyonlar")

    def _show_widget_overview(self, mode):
        self._cancel_price_row_drag(restore=True)
        self._overview_mode = mode
        self.portfolio_frame.pack_forget()
        self.watch_header.pack_forget()
        self.price_rows_frame.pack_forget()
        if mode != "watch":
            self.portfolio_frame.pack(fill="x", pady=(0, SPACING["xs"]))
        if mode != "portfolio":
            self.watch_header.pack(fill="x", pady=(0, SPACING["xxs"]))
            self.price_rows_frame.pack(fill="x")
        self._sync_widget_breakdown()
        self._layout_price_rows([instrument["key"] for instrument in self.watchlist])
        self.show_page(0)
        self.overview_canvas.yview_moveto(0)

    def _cancel_price_row_drag(self, restore=False):
        after_id = getattr(self, "_row_hold_after_id", None)
        if after_id is not None and hasattr(self, "root"):
            try:
                self.root.after_cancel(after_id)
            except (tk.TclError, ValueError):
                pass
        if restore and getattr(self, "_row_drag_original_order", None):
            self._layout_price_rows(self._row_drag_original_order)
        drag_key = getattr(self, "_row_drag_key", None)
        row = getattr(self, "price_row_widgets", {}).get(drag_key)
        if row and row.winfo_exists():
            row.config(highlightbackground=self.color_border)
            for widget in (row, *row.winfo_children()):
                widget.configure(cursor="hand2")
        if hasattr(self, "root"):
            try:
                self.root.config(cursor="")
            except tk.TclError:
                pass
        self._row_hold_after_id = None
        self._row_press_key = None
        self._row_press_origin = None
        self._row_press_last = None
        self._row_drag_key = None
        self._row_drag_order = None
        self._row_drag_original_order = None

    def _format_change(self, change_pct):
        if change_pct is None:
            return "--"
        sign = "+" if change_pct >= 0 else ""
        return f"{sign}{turkish_number(change_pct, 1)}%"

    def _sparkline_data(self, key, current_value):
        """Use one history series for the line and its change badge."""
        try:
            rows, _source, _interval = self._cached_history_series(
                key, self._sparkline_period_seconds()
            )
        except Exception:
            rows = []
        if len(rows) < 2:
            rows = rows or [(None, safe_float(current_value))]
            return rows, None
        first_value = float(rows[0][1])
        last_value = float(rows[-1][1])
        change_pct = ((last_value - first_value) / first_value) * 100 if first_value > 0 else None
        return downsample_history_rows(rows, 40), change_pct

    def _sparkline_period_seconds(self):
        variable = getattr(self, "sparkline_period", None)
        return variable.get() if variable is not None else 86400

    @staticmethod
    def _history_x_ratios(times):
        if len(times) <= 1:
            return [0.5] * len(times)
        if all(time is not None for time in times) and times[-1] > times[0]:
            span = (times[-1] - times[0]).total_seconds()
            return [(time - times[0]).total_seconds() / span for time in times]
        return [index / (len(times) - 1) for index in range(len(times))]

    def _draw_sparkline(self, canvas, rows, color):
        if not canvas:
            return
        try:
            canvas._sparkline_payload = (tuple(rows), color)
            if not getattr(canvas, "_sparkline_resize_bound", False):
                canvas.bind(
                    "<Configure>",
                    lambda _event, target=canvas: self._draw_sparkline(
                        target, *target._sparkline_payload
                    ),
                    add="+",
                )
                canvas._sparkline_resize_bound = True
            width = canvas.winfo_width()
            height = canvas.winfo_height()
            if width <= 1:
                width = max(canvas.winfo_reqwidth(), 62)
            if height <= 1:
                height = max(canvas.winfo_reqheight(), 22)
            render_key = (width, height, canvas._sparkline_payload)
            if render_key == getattr(canvas, "_sparkline_render_key", None):
                return
            if len(rows) < 2:
                y = height / 2
                image = render_chart_line(
                    width, height, [(0, y), (width, y)], self.color_border, stroke=1
                )
            else:
                times = [parse_history_timestamp(row[0]) for row in rows]
                values = [float(row[1]) for row in rows]
                min_v, max_v = min(values), max(values)
                value_range = max_v - min_v
                points = []
                for ratio, value in zip(self._history_x_ratios(times), values):
                    x = (width - 4) * ratio + 2
                    y = (height - 6) * (max_v - value) / value_range + 3 if value_range else height / 2
                    points.append((x, y))
                image = render_chart_line(width, height, points, color, endpoint=1.75)
            photo = ImageTk.PhotoImage(image, master=canvas)
            if hasattr(canvas, "_line_item"):
                canvas.itemconfigure(canvas._line_item, image=photo)
            else:
                canvas._line_item = canvas.create_image(0, 0, anchor="nw", image=photo)
            canvas._line_image = photo
            canvas._sparkline_render_key = render_key
        except Exception:
            pass

    def _update_price_row_visuals(self, prices):
        if not hasattr(self, "price_change_vars"):
            return
        for instrument in self.watchlist:
            key = instrument["key"]
            rows, change_pct = self._sparkline_data(key, prices.get(key))
            change_var = self.price_change_vars.get(key)
            badge = self.price_change_labels.get(key)
            if change_var:
                change_var.set(self._format_change(change_pct))
            if badge:
                if change_pct is None:
                    badge.config(bg=THEME["surface_pressed"], fg=self.color_text_muted)
                elif change_pct >= 0:
                    badge.config(bg=self.color_success_bg, fg=self.color_success)
                else:
                    badge.config(bg=self.color_danger_bg, fg=self.color_danger)
            color = self.color_danger if change_pct is not None and change_pct < 0 else self.color_success
            self._draw_sparkline(self.price_spark_canvases.get(key), rows, color)

    def _format_tl(self, value, decimals=0):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "₺0"
        return f"₺{turkish_number(value, decimals)}"

    def rebuild_portfolio_breakdown(self, summary):
        if not hasattr(self, "portfolio_breakdown_frame"):
            return
        rows = tuple(summary.get("rows", ())) if summary else ()
        if rows == getattr(self, "_portfolio_rows_rendered", None):
            return
        self._portfolio_rows_rendered = rows
        for child in self.portfolio_breakdown_frame.winfo_children():
            child.destroy()

        if not rows:
            tk.Label(
                self.portfolio_breakdown_frame,
                text="Portföy boş",
                bg=self.color_card,
                fg=self.color_text_muted,
                font=self.font_small,
                anchor="w",
            ).pack(fill="x")
            return

        for row_data in rows:
            row = tk.Frame(
                self.portfolio_breakdown_frame,
                bg=self.color_card_alt,
                padx=SPACING["xs"],
                pady=4,
            )
            row.pack(fill="x", pady=SPACING["xxs"] // 2)

            marker = tk.Canvas(
                row,
                width=10,
                height=10,
                bg=self.color_card_alt,
                highlightthickness=0,
                bd=0,
            )
            marker.grid(row=0, column=0, sticky="w", padx=(0, 6))
            marker._marker_image = ImageTk.PhotoImage(render_chart_line(
                10, 10, [(5, 5)], row_data.get("color", self.color_accent), endpoint=3
            ), master=marker)
            marker.create_image(0, 0, anchor="nw", image=marker._marker_image)

            row.grid_columnconfigure(1, weight=1)
            tk.Label(row, text=row_data["label"], width=1, bg=self.color_card_alt, fg=self.color_text_main,
                     font=self.font_label, anchor="w").grid(row=0, column=1, sticky="ew")
            quantity = f"{quantity_text(row_data['quantity'])} {row_data['unit']}"
            average = self._format_tl(row_data["average_cost_tl"], 2)
            tk.Label(row, text=quantity, bg=self.color_card_alt,
                     fg=self.color_text_dim, font=self.font_small, anchor="w").grid(
                         row=1, column=1, sticky="ew")
            tk.Label(row, text=f"Ort. {average}", bg=self.color_card_alt,
                     fg=self.color_text_dim, font=self.font_small, anchor="w").grid(
                         row=2, column=1, columnspan=2, sticky="ew")

            if row_data["has_price"]:
                value_text = self._format_tl(row_data["value_tl"], 2)
            else:
                value_text = "Fiyat yok"
            tk.Label(row, text=value_text, bg=self.color_card_alt, fg=self.color_text_main, font=self.font_small, anchor="e").grid(row=0, column=2, sticky="e", padx=(SPACING["xs"], SPACING["xs"]))

            profit = row_data.get("profit_tl")
            if profit is None:
                profit_text = "--"
                profit_color = self.color_text_muted
            else:
                sign = "+" if profit > 0 else "−" if profit < 0 else ""
                profit_text = f"{sign}{self._format_tl(abs(profit), 2)}"
                profit_color = self.color_success if profit >= 0 else self.color_danger
            tk.Label(row, text=profit_text, bg=self.color_card_alt, fg=profit_color, font=self.font_badge, anchor="e").grid(row=1, column=2, sticky="e", padx=SPACING["xs"])

    # --- Sayfa Navigasyon Sistemi ---
    def show_page(self, index):
        if index != 1:
            self._cancel_chart_redraw()
        for p in self.pages:
            p.pack_forget()
        self.pages[index].pack(fill="both", expand=True)
        self.current_page = index
        for mode, button in self.widget_nav.items():
            selected = index == 0 and mode == self._overview_mode
            button.configure(bg=THEME["primary_soft"] if selected else self.color_card_alt,
                             fg=self.color_accent if selected else self.color_text_main)
        # Sayfa değiştiğinde içeriği güncelle
        if index == 1:
            self._update_chart()
        elif index == 2:
            self._update_stats()
    
    # --- Grafik Sayfası ---
    def _build_chart_page(self):
        selector_frame = tk.Frame(
            self.page_chart,
            bg=self.color_card,
            padx=SPACING["xs"],
            pady=SPACING["xs"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        selector_frame.pack(fill="x", pady=(0, SPACING["xs"]))

        default_key = "gumus_tl" if any(item["key"] == "gumus_tl" for item in self.watchlist) else self.watchlist[0]["key"]
        self.chart_var = tk.StringVar(value=default_key)
        self.chart_period = tk.IntVar(value=24 * 60 * 60)
        self._chart_view_start = 0.0
        self._chart_view_end = 1.0
        self._chart_pan_origin = None
        self._chart_rows = []
        self._chart_time_ordered = False
        self._chart_frame_job = None
        self._chart_settle_job = None
        self._chart_rendered_frame = None
        self._chart_instrument = None
        self._chart_history_source = None
        self._chart_history_interval = None
        self._chart_plot_bounds = (48, 14, 230, 170)

        selector_header = tk.Frame(selector_frame, bg=self.color_card)
        selector_header.pack(fill="x", pady=(0, SPACING["xs"]))
        tk.Label(
            selector_header,
            text="FİYAT GEÇMİŞİ",
            bg=self.color_card,
            fg=self.color_text_dim,
            font=self.font_header,
        ).pack(side="left")

        zoom_toolbar = tk.Frame(selector_header, bg=self.color_card)
        zoom_toolbar.pack(side="right")
        self.chart_zoom_reset_button = IconButton(
            zoom_toolbar,
            "reset",
            self.reset_chart_view,
            size=24,
            bg=self.color_card,
            hover_bg=THEME["surface_hover"],
            fg=self.color_text_muted,
            hover_fg=self.color_text_main,
            show_border=False,
        )
        self.chart_zoom_reset_button.pack(side="right", padx=(SPACING["xxs"], 0))
        self.chart_zoom_out_button = IconButton(
            zoom_toolbar,
            "minus",
            lambda: self._zoom_chart(False, 0.5),
            size=24,
            bg=self.color_card,
            hover_bg=THEME["surface_hover"],
            fg=self.color_text_muted,
            hover_fg=self.color_text_main,
            show_border=False,
        )
        self.chart_zoom_out_button.pack(side="right", padx=(SPACING["xxs"], 0))
        self.chart_zoom_in_button = IconButton(
            zoom_toolbar,
            "plus",
            lambda: self._zoom_chart(True, 0.5),
            size=24,
            bg=self.color_card,
            hover_bg=THEME["surface_hover"],
            fg=self.color_text_muted,
            hover_fg=self.color_text_main,
            show_border=False,
        )
        self.chart_zoom_in_button.pack(side="right")

        self.chart_period_control = SegmentedControl(
            selector_frame,
            self.chart_period,
            self.CHART_PERIOD_OPTIONS,
            command=self._on_chart_filter_changed,
            columns=4,
        )
        self.chart_period_control.pack(fill="x", pady=(0, SPACING["xs"]))

        self.chart_symbol_frame = tk.Frame(selector_frame, bg=self.color_card)
        self.chart_symbol_frame.pack(fill="x")
        self._rebuild_chart_symbol_buttons()

        tk.Label(
            selector_frame,
            text="Tekerlek: yakınlaştır  •  Sürükle: kaydır  •  Çift tık: sıfırla",
            bg=self.color_card,
            fg=self.color_text_muted,
            font=self.font_small,
            anchor="w",
        ).pack(fill="x", pady=(SPACING["xs"], 0))

        self.chart_canvas = tk.Canvas(
            self.page_chart,
            bg=self.color_card,
            highlightthickness=1,
            highlightbackground=self.color_border,
            bd=0,
        )
        self.chart_canvas.pack(fill="both", expand=True)
        self.chart_canvas.configure(cursor="hand2")
        self.chart_canvas.bind("<Configure>", self._on_chart_canvas_configure, add="+")
        self.chart_canvas.bind("<MouseWheel>", self._on_chart_mousewheel, add="+")
        self.chart_canvas.bind("<Button-4>", lambda event: self._on_chart_mousewheel(event, 120), add="+")
        self.chart_canvas.bind("<Button-5>", lambda event: self._on_chart_mousewheel(event, -120), add="+")
        self.chart_canvas.bind("<ButtonPress-1>", self._on_chart_pan_start, add="+")
        self.chart_canvas.bind("<B1-Motion>", self._on_chart_pan_motion, add="+")
        self.chart_canvas.bind("<ButtonRelease-1>", self._on_chart_pan_end, add="+")
        self.chart_canvas.bind("<Double-Button-1>", self.reset_chart_view, add="+")

    def _instrument_by_key(self, key):
        watched = next((item for item in self.watchlist if item["key"] == key), None)
        if watched is not None:
            return watched
        return next((item for item in self.workspace_instruments() if item["key"] == key), None)

    @staticmethod
    def _chart_selector_label(instrument):
        """Keep dynamic symbol selectors readable at the compact widget width."""
        key = instrument.get("key")
        aliases = {
            "gumus_ons": "G. ONS",
            "gumus_tl": "G. TL",
            "altin_tl": "Altın",
            "dolar": "USD",
        }
        if key in aliases:
            return aliases[key]
        label = str(instrument.get("label") or key or "Varlık")
        return label if len(label) <= 8 else f"{label[:7]}…"

    def _history_source_symbol(self, key):
        instrument = self._instrument_by_key(key)
        if (
            instrument
            and key in SILVER_SPOT_KEYS
            and instrument.get("symbol") == SILVER_SPOT_SYMBOL
        ):
            return SILVER_SPOT_SYMBOL
        return None

    def _history_source_symbols(self, key):
        """Include the live spot source and its explicit historical proxy."""
        source = self._history_source_symbol(key)
        if source == SILVER_SPOT_SYMBOL:
            return (SILVER_SPOT_SYMBOL, SILVER_HISTORY_PROXY_SYMBOL)
        return source

    def _cached_history_series(self, key, period_seconds=0):
        """Select one source and one resolution so a line cannot zig-zag.

        Provider 5-minute bars are authoritative for finite periods. Runtime
        samples are only a fallback. For the all-time view, daily bars are
        authoritative. Silver spot uses the explicitly recorded SI=F proxy
        until a genuine spot-history source is available.
        """
        instrument = self._instrument_by_key(key)
        if not instrument:
            return [], None, None
        provider_source = self._history_cache_source(instrument)
        live_source = instrument.get("symbol") or provider_source

        if not period_seconds:
            rows = self.history_db.get_all_history(
                key,
                source_symbol=provider_source,
                intervals=("1d",),
            )
            if rows:
                return self._merge_history_rows(rows), provider_source, "1d"
            for source in dict.fromkeys((live_source, provider_source)):
                rows = self.history_db.get_all_history(
                    key,
                    source_symbol=source,
                    intervals=("live",),
                )
                if rows:
                    return self._merge_history_rows(rows), source, "live"
            return [], provider_source, "1d"

        hours = float(period_seconds) / 3600
        rows = self.history_db.get_history(
            key,
            source_symbol=provider_source,
            hours=hours,
            intervals=("5m",),
            anchor_to_latest=True,
        )
        if rows:
            return (
                history_rows_in_window(rows, period_seconds),
                provider_source,
                "5m",
            )

        for source in dict.fromkeys((live_source, provider_source)):
            rows = self.history_db.get_history(
                key,
                source_symbol=source,
                hours=hours,
                intervals=("live",),
                anchor_to_latest=True,
            )
            if rows:
                return (
                    history_rows_in_window(rows, period_seconds),
                    source,
                    "live",
                )
        return [], provider_source, "5m"

    def _rebuild_chart_symbol_buttons(self):
        if not hasattr(self, "chart_symbol_frame"):
            return
        previous_control = getattr(self, "chart_symbol_control", None)
        if previous_control is not None:
            try:
                self.chart_var.trace_remove("write", previous_control._trace_id)
            except (AttributeError, tk.TclError):
                pass
        for child in self.chart_symbol_frame.winfo_children():
            child.destroy()
        valid_keys = {item["key"] for item in self.watchlist}
        if self.chart_var.get() not in valid_keys and self.watchlist:
            self.chart_var.set(self.watchlist[0]["key"])
        if not self.watchlist:
            return
        selection_colors = {
            instrument["key"]: (
                THEME["primary_soft"],
                instrument.get("color", self.color_accent),
            )
            for instrument in self.watchlist
        }
        self.chart_symbol_control = SegmentedControl(
            self.chart_symbol_frame,
            self.chart_var,
            tuple(
                (self._chart_selector_label(instrument), instrument["key"])
                for instrument in self.watchlist
            ),
            command=self._on_chart_filter_changed,
            selection_colors=selection_colors,
        )
        self.chart_symbol_control.pack(fill="x")

    @staticmethod
    def _filter_outliers(values, timestamps):
        """Discard malformed values without deleting valid market moves."""
        clean_vals = []
        clean_ts = []
        for v, t in zip(values, timestamps):
            try:
                numeric = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric) and numeric > 0:
                clean_vals.append(numeric)
                clean_ts.append(t)
        return clean_vals, clean_ts

    def _on_chart_filter_changed(self):
        self._chart_view_start = 0.0
        self._chart_view_end = 1.0
        self._chart_pan_origin = None
        self._update_chart()

    def _chart_period_label(self):
        selected = self.chart_period.get()
        return next(
            (label for label, value in self.CHART_PERIOD_OPTIONS if value == selected),
            "Geçmiş",
        )

    @staticmethod
    def _merge_history_rows(*collections):
        by_timestamp = {}
        for rows in collections:
            for timestamp, value in rows or []:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    by_timestamp[normalize_history_timestamp(timestamp)] = value
        return sorted(by_timestamp.items(), key=lambda row: row[0])

    def _load_selected_chart_history(self):
        selected = self.chart_var.get()
        period_seconds = self.chart_period.get()
        data, source, interval = self._cached_history_series(
            selected, period_seconds
        )
        self._chart_history_source = source
        self._chart_history_interval = interval
        return data

    def _draw_chart_message(self, text):
        self.chart_canvas.delete("all")
        self.chart_canvas._chart_line_image = None
        self._chart_rendered_frame = None
        cw = max(self.chart_canvas.winfo_width(), 230)
        ch = max(self.chart_canvas.winfo_height(), 170)
        scale = max(1.0, min(1.5, cw / 230.0))
        self.chart_canvas.create_text(
            cw // 2,
            ch // 2,
            text=text,
            fill=self.color_text_muted,
            font=(self.font_family, int(10 * scale)),
            justify="center",
        )

    def _update_chart(self):
        self._cancel_chart_redraw()
        self._chart_rows = []
        self._chart_time_ordered = False
        self._chart_rendered_frame = None
        try:
            data = self._load_selected_chart_history()
            instrument = self._instrument_by_key(self.chart_var.get())
            self._chart_instrument = instrument
            if not data or not instrument:
                self._draw_chart_message(
                    "Bu aralık için henüz veri yok\nGeçmiş arka planda tamamlanıyor"
                )
                return

            raw_values = [row[1] for row in data]
            raw_timestamps = [row[0] for row in data]
            values, timestamps = self._filter_outliers(
                raw_values, raw_timestamps
            )
            times = [parse_history_timestamp(timestamp) for timestamp in timestamps]
            self._chart_rows = list(zip(times, values))
            self._chart_time_ordered = (
                len(times) > 1
                and all(time is not None for time in times)
                and times[-1] > times[0]
                and all(left <= right for left, right in zip(times, times[1:]))
            )
            self._draw_chart()
        except Exception as exc:
            log_message(f"Chart error: {exc}")
            self._draw_chart_message("Grafik verisi okunamadı")

    def _visible_chart_rows(self):
        rows = self._chart_rows
        if len(rows) <= 2:
            return rows
        if self._chart_time_ordered:
            first = rows[0][0]
            span = rows[-1][0] - first
            start_time = first + span * self._chart_view_start
            end_time = first + span * self._chart_view_end
            start_index = bisect_left(rows, start_time, key=lambda row: row[0])
            end_index = bisect_right(rows, end_time, key=lambda row: row[0])
            if end_index - start_index < 2:
                # A viewport inside a market closure should stay near that time.
                start_index = max(0, min(start_index - 1, len(rows) - 2))
                end_index = start_index + 2
            return rows[start_index:end_index]
        last_index = len(rows) - 1
        start_index = int(self._chart_view_start * last_index)
        end_index = int(self._chart_view_end * last_index) + 1
        end_index = max(start_index + 2, min(end_index, len(rows)))
        return rows[start_index:end_index]

    def _draw_chart(self, *, preview=False):
        try:
            cw = self.chart_canvas.winfo_width()
            ch = self.chart_canvas.winfo_height()
            if cw < 10 or ch < 10:
                cw, ch = 230, 170
            frame = (cw, ch, self._chart_view_start, self._chart_view_end, preview)
            previous = self._chart_rendered_frame
            if previous and previous[:4] == frame[:4] and (preview or not previous[4]):
                return
            if not self._chart_rows or not self._chart_instrument:
                self._draw_chart_message("Bu aralık için henüz veri yok")
                return

            rows = downsample_history_rows(
                self._visible_chart_rows(),
                max(120, cw if preview else cw * 2),
            )
            if not rows:
                self._draw_chart_message("Bu aralık için henüz veri yok")
                return
            times = [row[0] for row in rows]
            values = [float(row[1]) for row in rows]
            instrument = self._chart_instrument
            color = self.color_danger if values[-1] < values[0] else self.color_success

            scale = max(1.0, min(1.5, cw / 230.0))
            f_title = int(9 * scale)
            f_tick = int(8 * scale)
            f_val = int(8 * scale)
            pad_l, pad_r, pad_t, pad_b = max(52, int(43 * scale)), 18, 46, 28
            gw = max(1, cw - pad_l - pad_r)
            gh = max(1, ch - pad_t - pad_b)
            self._chart_plot_bounds = (pad_l, pad_t, cw - pad_r, pad_t + gh)

            min_v = min(values)
            max_v = max(values)
            margin = (
                (max_v - min_v) * 0.05
                if max_v != min_v
                else max(abs(max_v) * 0.02, 0.01)
            )
            chart_min = min_v - margin
            chart_max = max_v + margin
            val_range = max(chart_max - chart_min, 0.000001)

            points = []
            for ratio, value in zip(self._history_x_ratios(times), values):
                x = pad_l + gw * ratio
                y = pad_t + gh * (1 - (value - chart_min) / val_range)
                points.append((x, y))

            # Compositing before uploading avoids Tk's costly transparent region updates.
            plot = Image.new("RGB", (cw, ch), self.color_card)
            grid = ImageDraw.Draw(plot)
            grid_rows = [pad_t + int(gh * index / 4) for index in range(5)]
            for y in grid_rows:
                grid.line((pad_l, y, cw - pad_r, y), fill=self.color_border)
            curve = render_chart_line(
                cw, ch, points, color, stroke=2, endpoint=3, fill_to=pad_t + gh, preview=preview
            )
            plot.paste(curve, (0, 0), curve.getchannel("A"))
            photo = ImageTk.PhotoImage(plot, master=self.chart_canvas)

            self._chart_rendered_frame = None
            self.chart_canvas.delete("all")
            self.chart_canvas._chart_line_image = photo
            self.chart_canvas.create_image(0, 0, anchor="nw", image=photo)

            zoom_pct = int(round(100 / max(
                self._chart_view_end - self._chart_view_start, 0.001
            )))
            title = f"{instrument['label']} · {self._chart_period_label()}"
            if zoom_pct > 100:
                title += f" · %{zoom_pct}"
            self.chart_canvas.create_text(
                pad_l,
                15,
                text=title,
                fill=self.color_text_main,
                font=(self.font_family, f_title, "bold"),
                anchor="w",
            )
            latest = times[-1]
            source_note = ""
            if (
                self._chart_history_source == SILVER_HISTORY_PROXY_SYMBOL
                and instrument.get("source") in SILVER_SPOT_SOURCES
            ):
                source_note = "SI=F · "
            if latest is not None:
                self.chart_canvas.create_text(
                    cw - pad_r,
                    30,
                    text=f"{source_note}{latest.strftime('%d/%m %H:%M')}",
                    fill=self.color_text_muted,
                    font=(self.font_family, f_tick),
                    anchor="e",
                )

            for index, y in enumerate(grid_rows):
                value = chart_max - (val_range * index / 4)
                if value >= 10000:
                    formatted = f"{value:,.0f}"
                elif value >= 100:
                    formatted = f"{value:.1f}"
                else:
                    formatted = f"{value:.2f}"
                self.chart_canvas.create_text(
                    pad_l - 6,
                    y,
                    text=formatted,
                    fill=self.color_text_muted,
                    font=(self.font_family, f_tick),
                    anchor="e",
                )

            if self._chart_time_ordered:
                visible_span = times[-1] - times[0]
                # Label evenly spaced time positions so gaps do not bunch up labels.
                label_count = min(4, len(times), max(2, gw // 70))
                if visible_span <= timedelta(days=2):
                    time_format = "%H:%M"
                elif visible_span <= timedelta(days=45):
                    time_format = "%d/%m"
                else:
                    time_format = "%m/%Y"
                for index in range(label_count):
                    ratio = index / (label_count - 1)
                    time = times[0] + visible_span * ratio
                    self.chart_canvas.create_text(
                        pad_l + gw * ratio,
                        ch - 10,
                        text=time.strftime(time_format),
                        fill=self.color_text_muted,
                        font=(self.font_family, f_tick),
                    )

            last_x, last_y = points[-1]
            last_value = values[-1]
            self.chart_canvas.create_text(
                min(last_x - 5, cw - pad_r - 2),
                max(last_y - 12, pad_t + 5),
                text=format_instrument_value(instrument, last_value),
                fill=color,
                font=(self.font_family, f_val, "bold"),
                anchor="e",
            )
            self._chart_rendered_frame = frame
        except Exception as exc:
            log_message(f"Chart draw error: {exc}")

    def reset_chart_view(self, event=None):
        self._chart_view_start = 0.0
        self._chart_view_end = 1.0
        self._chart_pan_origin = None
        self._cancel_chart_redraw()
        self._draw_chart()
        return "break" if event is not None else None

    def _cancel_chart_redraw(self):
        if self._chart_frame_job is not None:
            self.root.after_cancel(self._chart_frame_job)
            self._chart_frame_job = None
        if self._chart_settle_job is not None:
            self.root.after_cancel(self._chart_settle_job)
            self._chart_settle_job = None

    def _queue_chart_redraw(self):
        if self._chart_frame_job is None:
            self._chart_frame_job = self.root.after(16, self._draw_chart_preview)
        if self._chart_settle_job is not None:
            self.root.after_cancel(self._chart_settle_job)
        self._chart_settle_job = self.root.after(120, self._finish_chart_redraw)

    def _draw_chart_preview(self):
        self._chart_frame_job = None
        self._draw_chart(preview=True)

    def _finish_chart_redraw(self):
        self._chart_settle_job = None
        self._cancel_chart_redraw()
        self._draw_chart()

    def _zoom_chart(self, zoom_in, anchor=0.5):
        point_count = len(self._chart_rows)
        if point_count < 3:
            return
        min_span = max(2 / max(point_count - 1, 1), 0.01)
        view = calculate_zoomed_range(
            self._chart_view_start,
            self._chart_view_end,
            zoom_in,
            anchor,
            min_span,
        )
        if view != (self._chart_view_start, self._chart_view_end):
            self._chart_view_start, self._chart_view_end = view
            self._queue_chart_redraw()

    def _on_chart_mousewheel(self, event, delta=None):
        delta = delta if delta is not None else getattr(event, "delta", 0)
        left, _top, right, _bottom = self._chart_plot_bounds
        width = max(right - left, 1)
        anchor = (getattr(event, "x", left + width / 2) - left) / width
        if delta:
            self._zoom_chart(delta > 0, anchor)
        return "break"

    def _on_chart_pan_start(self, event):
        self._chart_pan_origin = (
            event.x,
            self._chart_view_start,
            self._chart_view_end,
        )
        self.chart_canvas.configure(cursor="fleur")
        return "break"

    def _on_chart_pan_motion(self, event):
        if self._chart_pan_origin is None:
            return "break"
        origin_x, start, end = self._chart_pan_origin
        left, _top, right, _bottom = self._chart_plot_bounds
        width = max(right - left, 1)
        span = end - start
        shift = -((event.x - origin_x) / width) * span
        view = calculate_panned_range(start, end, shift)
        if view != (self._chart_view_start, self._chart_view_end):
            self._chart_view_start, self._chart_view_end = view
            self._queue_chart_redraw()
        return "break"

    def _on_chart_pan_end(self, _event=None):
        self._chart_pan_origin = None
        self.chart_canvas.configure(cursor="hand2")
        return "break"

    def _on_chart_canvas_configure(self, _event=None):
        if self._chart_rows:
            self._queue_chart_redraw()

    # --- İstatistik Sayfası ---
    def _build_stats_page(self):
        period_frame = tk.Frame(
            self.page_stats,
            bg=self.color_card,
            padx=SPACING["xs"],
            pady=SPACING["xs"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        period_frame.pack(fill="x", pady=(0, SPACING["xs"]))

        self.stats_period = tk.IntVar(value=7)

        tk.Label(
            period_frame,
            text="PİYASA İSTATİSTİKLERİ",
            bg=self.color_card,
            fg=self.color_text_dim,
            font=self.font_header,
        ).pack(side="left")
        self.stats_period_control = SegmentedControl(
            period_frame,
            self.stats_period,
            (("7G", 7), ("30G", 30), ("Tümü", 0)),
            command=self._update_stats,
        )
        self.stats_period_control.pack(side="right")

        stats_container = tk.Frame(self.page_stats, bg=self.bg_color)
        stats_container.pack(fill="both", expand=True)
        self.stats_canvas = tk.Canvas(
            stats_container,
            bg=self.bg_color,
            highlightthickness=0,
            bd=0,
        )
        self.stats_scrollbar = ttk.Scrollbar(
            stats_container,
            orient="vertical",
            command=self.stats_canvas.yview,
            style="Modern.Vertical.TScrollbar",
        )
        self.stats_canvas.configure(yscrollcommand=self.stats_scrollbar.set)
        self.stats_scrollbar.pack(side="right", fill="y")
        self.stats_canvas.pack(side="left", fill="both", expand=True)
        self.stats_frame = tk.Frame(self.stats_canvas, bg=self.bg_color)
        self._stats_window_id = self.stats_canvas.create_window(
            (0, 0), window=self.stats_frame, anchor="nw"
        )
        self.stats_frame.bind(
            "<Configure>",
            lambda _event: self.stats_canvas.configure(
                scrollregion=self.stats_canvas.bbox("all")
            ),
        )
        self.stats_canvas.bind(
            "<Configure>",
            lambda event: self.stats_canvas.itemconfigure(
                self._stats_window_id, width=event.width
            ),
        )
        self.stats_canvas.bind("<MouseWheel>", self._on_stats_mousewheel)
        
        self.stats_labels = {}
        self._rebuild_stats_sections()

    def _on_stats_mousewheel(self, event):
        self.stats_canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

    def _bind_stats_mousewheel(self, widget):
        widget.bind("<MouseWheel>", self._on_stats_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_stats_mousewheel(child)

    def _rebuild_stats_sections(self):
        if not hasattr(self, "stats_frame"):
            return
        for child in self.stats_frame.winfo_children():
            child.destroy()

        self.stats_labels = {}
        for instrument in self.watchlist:
            key = instrument["key"]
            section = tk.Frame(
                self.stats_frame,
                bg=self.color_card_alt,
                padx=SPACING["xs"],
                pady=6,
                highlightthickness=1,
                highlightbackground=self.color_border,
            )
            section.pack(fill="x", pady=SPACING["xxs"] // 2)

            tk.Label(
                section,
                text=instrument["label"],
                bg=self.color_card_alt,
                fg=instrument.get("color", self.color_accent),
                font=self.font_label,
            ).pack(anchor="w", pady=(0, SPACING["xxs"]))

            metrics = tk.Frame(section, bg=self.color_card_alt)
            metrics.pack(fill="x")
            for column in (0, 1):
                metrics.grid_columnconfigure(column, weight=1, uniform="stats")

            self.stats_labels[key] = {}
            for metric, text, grid_row, grid_column in (
                ("min", "Min: --", 0, 0),
                ("max", "Max: --", 0, 1),
                ("avg", "Ort: --", 1, 0),
                ("chg", "Değişim: --", 1, 1),
            ):
                label = tk.Label(
                    metrics,
                    text=text,
                    bg=self.color_card_alt,
                    fg=self.color_text_dim,
                    font=self.font_small,
                    anchor="w",
                )
                label.grid(row=grid_row, column=grid_column, sticky="ew", pady=1)
                self.stats_labels[key][metric] = label
            self._bind_stats_mousewheel(section)

        self.stats_canvas.yview_moveto(0)

    def _update_stats(self):
        try:
            days = self.stats_period.get()

            for instrument in self.watchlist:
                key = instrument["key"]
                labels = self.stats_labels.get(key)
                if not labels:
                    continue

                rows, _source, _interval = self._cached_history_series(
                    key,
                    days * 24 * 60 * 60 if days > 0 else 0,
                )
                values = [float(row[1]) for row in rows]
                if not values:
                    labels["min"].config(text="Min: --")
                    labels["max"].config(text="Max: --")
                    labels["avg"].config(text="Ort: --")
                    labels["chg"].config(text="Değişim: --", fg=self.color_text_muted)
                    continue

                mn, mx = min(values), max(values)
                avg = sum(values) / len(values)
                labels["min"].config(text=f"Min: {format_instrument_value(instrument, mn)}")
                labels["max"].config(text=f"Max: {format_instrument_value(instrument, mx)}")
                labels["avg"].config(text=f"Ort: {format_instrument_value(instrument, avg)}")

                first_value = values[0]
                last_value = values[-1]
                if first_value > 0:
                    chg = ((last_value - first_value) / first_value) * 100
                    sign = "+" if chg >= 0 else ""
                    color = self.color_success if chg >= 0 else self.color_danger
                    labels["chg"].config(text=f"Değişim: {sign}{chg:.1f}%", fg=color)
                else:
                    labels["chg"].config(text="Değişim: --", fg=self.color_text_muted)
        except Exception as e:
            log_message(f"Stats error: {e}")

    def toggle_autostart(self):
        self.asm.set_autostart(self.var_autostart.get())

    def toggle_topmost(self):
        enabled = self.var_topmost.get()
        self.root.attributes("-topmost", enabled)
        self._preferences["topmost"] = enabled
        self._save_preferences()
        
    def check_updates(self, *, parent=None):
        parent = parent or self.root
        if VERSION.endswith("-dev"):
            messagebox.showinfo("Güncelleme", "Bu yerel yapı kurulum paketiyle güncellenir.", parent=parent)
            return

        def show_result(result):
            available, version, url = result
            if not available:
                messagebox.showinfo("Güncelleme", "Uygulama güncel.", parent=parent)
            elif messagebox.askyesno("Güncelleme mevcut", f"v{version} indirilsin ve kurulum açılsın mı?", parent=parent):
                self._run_update_task(lambda: self.um.update_application(url), launch, parent)

        def launch(path):
            try:
                subprocess.Popen([path])
            except OSError as error:
                messagebox.showerror("Kurulum başlatılamadı", str(error), parent=parent)
                return
            self.kapat()

        self._run_update_task(self.um.check_for_updates, show_result, parent)

    def _run_update_task(self, task, on_success, parent):
        if getattr(self, "_update_task_running", False):
            return
        self._update_task_running = True
        result = SimpleQueue()

        def worker():
            try:
                result.put((task(), None))
            except Exception as error:
                result.put((None, error))

        def poll():
            try:
                value, error = result.get_nowait()
            except Empty:
                self.root.after(100, poll)
                return
            self._update_task_running = False
            if not parent.winfo_exists():
                return
            if error is not None:
                messagebox.showerror("Güncelleme tamamlanamadı", str(error), parent=parent)
            else:
                on_success(value)

        threading.Thread(target=worker, daemon=True, name="piyasa-update").start()
        self.root.after(100, poll)

    def save_last_data(self, data):
        temporary = None
        try:
            normalized = normalize_market_data(data)
            if not normalized:
                return
            filepath = getattr(self, "market_data_path", app_path("market_data.json"))
            directory = os.path.dirname(os.path.abspath(filepath))
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".market-data-", delete=False) as target:
                temporary = target.name
                json.dump(normalized, target, indent=4, ensure_ascii=False)
            os.replace(temporary, filepath)
        except Exception as e:
            log_message(f"Veri kaydetme hatası: {e}")
        finally:
            if temporary and os.path.exists(temporary):
                os.remove(temporary)

    def load_last_data(self):
        try:
            filepath = getattr(self, "market_data_path", app_path("market_data.json"))
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return normalize_market_data(json.load(f))
        except Exception as e:
            log_message(f"Veri okuma hatası: {e}")
        return None

    @staticmethod
    def _extract_price(ticker):
        try:
            fast_info = getattr(ticker, "fast_info", None)
            if fast_info:
                for key in (
                    "last_price", "lastPrice",
                    "regular_market_price", "regularMarketPrice",
                ):
                    try:
                        val = fast_info.get(key) if hasattr(fast_info, "get") else getattr(fast_info, key, None)
                        price = positive_price(val)
                        if price:
                            return price
                    except:
                        pass
        except:
            pass

        try:
            info = ticker.info
            return positive_price(info.get("regularMarketPrice"))
        except:
            return 0

    def workspace_instruments(self):
        """Keep valuing positions independently of the visible watchlist."""
        instruments = {item["key"]: item for item in self.watchlist}
        manager = getattr(self, "tm", None)
        if manager is None:
            return list(instruments.values())
        positions = manager.get_positions()
        hidden = {key for key, position in positions.items()
                  if key not in instruments and position.get("quantity", 0) > 1e-9}
        if not hidden:
            return list(instruments.values())
        known = {item["key"]: item for item in default_watchlist()}
        known.update(getattr(getattr(self, "watchlist_manager", None), "archived", {}))
        unresolved = hidden - known.keys()
        if unresolved:
            for record in reversed(manager.transactions):
                if not isinstance(record, dict):
                    continue
                key = record.get("instrument_key")
                if key in unresolved and record.get("instrument_symbol"):
                    known[key] = normalize_instrument({
                        "key": key, "symbol": record["instrument_symbol"],
                        "label": record.get("instrument_label"), "currency": record.get("instrument_currency", "₺"),
                        "source": record.get("instrument_source"), "decimals": record.get("instrument_decimals", 2),
                    })
                    unresolved.remove(key)
                    if not unresolved:
                        break
        for key in positions:
            if key in hidden and key in known:
                instruments[key] = known[key]
        return list(instruments.values())

    def _quote_instruments(self):
        instruments = self.workspace_instruments()
        if (any(item.get("currency") == "$" or item.get("source") in ("metal_try", "silver_spot_try") for item in instruments)
                and not any(item["key"] == "dolar" for item in instruments)):
            instruments.append(next(item for item in default_watchlist() if item["key"] == "dolar"))
        return instruments

    def _required_yahoo_symbols(self, instruments=None):
        symbols = set()
        for instrument in self._quote_instruments() if instruments is None else instruments:
            source = instrument.get("source")
            if source not in SILVER_SPOT_SOURCES:
                symbols.add(instrument["symbol"])
            if source in ("metal_try", "silver_spot_try") or instrument.get("currency") == "$":
                symbols.add("TRY=X")
        return sorted(symbols)

    @staticmethod
    def _fetch_spot_silver_price(*, metadata=None):
        try:
            response = requests.get(
                SILVER_SPOT_API_URL,
                headers={"User-Agent": "Disa-Finans-Widget/1.0"},
                timeout=10
            )
            response.raise_for_status()
            payload = response.json()
            if (
                str(payload.get("symbol", "")).upper() != SILVER_SPOT_SYMBOL
                or str(payload.get("currency", "")).upper() != "USD"
            ):
                return 0
            price = safe_float(payload.get("price"), 0)
            if metadata is not None and 1 < price < 1000:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                metadata[SILVER_SPOT_SYMBOL] = observation("Gold API", now, price_at=payload.get("updatedAt"))
            return price if 1 < price < 1000 else 0
        except Exception as e:
            log_message(f"Spot gümüş verisi alınamadı: {e}")
            return 0

    def _watchlist_price_sources(self, instruments=None):
        return {
            instrument["key"]: instrument["symbol"]
            for instrument in (self._quote_instruments() if instruments is None else instruments)
        }

    @staticmethod
    def _history_provider_symbol(instrument):
        if instrument.get("source") in SILVER_SPOT_SOURCES:
            return SILVER_HISTORY_PROXY_SYMBOL
        return instrument.get("symbol")

    @staticmethod
    def _combine_history_with_fx(base_rows, fx_rows, interval="1d"):
        """Convert ounce/USD rows to gram/TRY using the latest known FX row."""
        parsed_fx = []
        for timestamp, value in fx_rows or []:
            parsed = parse_history_timestamp(timestamp)
            if parsed is not None:
                parsed_fx.append((parsed, float(value)))
        parsed_fx.sort(key=lambda item: item[0])
        if not parsed_fx:
            return []

        max_fx_age = (
            timedelta(minutes=15)
            if interval == "5m"
            else timedelta(days=4)
        )
        combined = []
        fx_index = 0
        latest_fx = None
        for timestamp, value in base_rows or []:
            parsed = parse_history_timestamp(timestamp)
            if parsed is None:
                continue
            while (
                fx_index < len(parsed_fx)
                and parsed_fx[fx_index][0] <= parsed
            ):
                latest_fx = parsed_fx[fx_index]
                fx_index += 1
            if latest_fx is None:
                continue
            if parsed - latest_fx[0] > max_fx_age:
                continue
            converted = (float(value) * latest_fx[1]) / TROY_OUNCE_GRAMS
            if converted > 0:
                combined.append((normalize_history_timestamp(parsed), converted))
        return combined

    @staticmethod
    def _history_rows_for_instrument(instrument, raw_series, interval="1d"):
        provider_symbol = PiyasaWidget._history_provider_symbol(instrument)
        base_rows = raw_series.get(provider_symbol, [])
        source = instrument.get("source")
        if source in ("metal_try", "silver_spot_try"):
            return PiyasaWidget._combine_history_with_fx(
                base_rows,
                raw_series.get("TRY=X", []),
                interval,
            )
        return list(base_rows)

    @staticmethod
    def _history_cache_source(instrument):
        if instrument.get("source") in SILVER_SPOT_SOURCES:
            return SILVER_HISTORY_PROXY_SYMBOL
        return instrument.get("symbol") or ""

    @staticmethod
    def _extract_history_rows(frame):
        if frame is None or getattr(frame, "empty", True):
            return []
        try:
            close = frame["Close"].dropna()
        except Exception:
            return []
        rows = []
        for timestamp, value in close.items():
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value > 0 and value == value:
                rows.append((normalize_history_timestamp(timestamp), value))
        return rows

    def _download_yahoo_history(self, symbol, interval):
        kwargs = {
            "interval": interval,
            "auto_adjust": True,
            "actions": False,
            "repair": True,
            "timeout": 15,
        }
        if interval == "5m":
            end = datetime.now().astimezone()
            kwargs.update(start=end - timedelta(days=31), end=end)
        else:
            kwargs["period"] = "max"
        frame = yf.Ticker(symbol).history(**kwargs)
        return self._extract_history_rows(frame)

    def _sync_history_profile(self, interval, max_age):
        due_instruments = []
        for instrument in self.workspace_instruments():
            source_symbol = self._history_cache_source(instrument)
            if self.history_db.history_sync_due(
                instrument["key"],
                source_symbol,
                interval,
                max_age,
            ):
                due_instruments.append(instrument)
        if not due_instruments:
            return 0

        required_symbols = {
            self._history_provider_symbol(instrument)
            for instrument in due_instruments
            if self._history_provider_symbol(instrument)
        }
        if any(
            instrument.get("source") in ("metal_try", "silver_spot_try")
            for instrument in due_instruments
        ):
            required_symbols.add("TRY=X")

        raw_series = {}
        for symbol in sorted(required_symbols):
            if self._stop_event.is_set():
                return 0
            try:
                raw_series[symbol] = self._download_yahoo_history(
                    symbol, interval
                )
            except Exception as exc:
                log_message(
                    f"{symbol} {interval} geçmiş verisi alınamadı: {exc}"
                )
                raw_series[symbol] = []

        inserted = 0
        for instrument in due_instruments:
            rows = self._history_rows_for_instrument(
                instrument, raw_series, interval
            )
            if not rows:
                continue
            source_symbol = self._history_cache_source(instrument)
            inserted += self.history_db.insert_history_rows(
                instrument,
                rows,
                interval,
                source_symbol=source_symbol,
            )
            self.history_db.mark_history_synced(
                instrument["key"],
                source_symbol,
                interval,
            )
        return inserted

    def sync_historical_data(self):
        """Fill missing intraday/daily history without blocking the Tk thread."""
        if not self._history_fetch_lock.acquire(blocking=False):
            return 0
        inserted = 0
        try:
            inserted = self._sync_history_profile("5m", timedelta(minutes=30))
            if not self._stop_event.is_set():
                inserted += self._sync_history_profile("1d", timedelta(hours=24))
            if inserted and hasattr(self, "root"):
                try:
                    self.root.after(0, self._refresh_history_views)
                except tk.TclError:
                    pass
        except Exception as exc:
            log_message(f"Geçmiş veri eşitleme hatası: {exc}")
        finally:
            self._history_fetch_lock.release()
        return inserted

    def _history_backfill_worker(self):
        while not self._stop_event.is_set():
            self._history_resync_requested = False
            self.sync_historical_data()
            if not self._history_resync_requested:
                break

    def request_history_backfill(self):
        active = getattr(self, "_history_thread", None)
        if active is not None and active.is_alive():
            self._history_resync_requested = True
            return active
        self._history_resync_requested = False
        self._history_thread = threading.Thread(
            target=self._history_backfill_worker,
            name="market-history-backfill",
            daemon=True,
        )
        self._history_thread.start()
        return self._history_thread

    def _refresh_history_views(self):
        if self._last_prices:
            self._update_price_row_visuals(self._last_prices)
        if getattr(self, "current_page", 0) == 1:
            self._update_chart()
        self._refresh_workspace(history_changed=True)

    def _compatible_cached_data(self, data, instruments=None):
        data = normalize_market_data(data)
        if not data:
            return None
        prices = dict(data.get("prices", {}))
        sources = dict(data.get("sources", {}))
        available = self._quote_instruments() if instruments is None else instruments
        for instrument in available:
            key = instrument["key"]
            source = sources.get(key)
            if ((source and source != instrument["symbol"])
                    or (instrument.get("source") in SILVER_SPOT_SOURCES and source != SILVER_SPOT_SYMBOL)):
                prices.pop(key, None)
                sources.pop(key, None)
        return market_data_for_save(
            prices,
            timestamp=data.get("timestamp"),
            sources=sources,
            quotes={key: {**meta, "state": "cached", "market_state": ""}
                    for key, meta in data["quotes"].items()},
        )

    def _fetch_raw_prices(self, symbols, *, metadata=None):
        if not symbols:
            return {}
        tickers = yf.Tickers(" ".join(symbols))
        ticker_map = getattr(tickers, "tickers", {})
        prices = {}
        for symbol in symbols:
            ticker = None
            try:
                ticker = ticker_map.get(symbol) if hasattr(ticker_map, "get") else ticker_map[symbol]
            except:
                ticker = None
            if ticker is None:
                continue
            provider_data = {}
            try:
                provider_data = ticker.get_history_metadata() if metadata is not None else {}
            except Exception:
                # Metadata availability must not suppress the price fallback.
                provider_data = {}
            if not isinstance(provider_data, dict):
                provider_data = {}
            provider_price = positive_price(provider_data.get("regularMarketPrice"))
            price = provider_price or self._extract_price(ticker)
            if price:
                prices[symbol] = price
                if metadata is not None:
                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    metadata[symbol] = observation(
                        "Yahoo Finance", now,
                        price_at=provider_data.get("regularMarketTime") if provider_price else None,
                        market_state=session_from_metadata(provider_data, now),
                    )
        return prices

    def validate_yahoo_symbol(self, symbol):
        symbol = symbol.strip().upper()
        prices = self._fetch_raw_prices([symbol])
        return prices.get(symbol, 0)

    def _resolve_watchlist_prices(self, raw_prices, last_prices=None, *, instruments=None):
        last_prices = last_prices or {}
        prices = {}
        fresh_keys = set()
        dolar = raw_prices.get("TRY=X") or last_prices.get("dolar")

        for instrument in self._quote_instruments() if instruments is None else instruments:
            key = instrument["key"]
            symbol = instrument["symbol"]
            value = 0

            if instrument.get("source") in ("metal_try", "silver_spot_try"):
                base_price = raw_prices.get(symbol)
                if base_price and dolar:
                    value = (base_price * dolar) / TROY_OUNCE_GRAMS
            else:
                value = raw_prices.get(symbol, 0)

            if value and value > 0:
                prices[key] = float(value)
                fresh_keys.add(key)
            elif key in last_prices:
                prices[key] = last_prices[key]

        return prices, fresh_keys

    def request_data_refresh(self):
        threading.Thread(target=self.veri_getir, daemon=True).start()

    def veri_getir(self):
        if not self._fetch_lock.acquire(blocking=False):
            return
        try:
            instruments = self._quote_instruments()
            last_data = self._compatible_cached_data(self.load_last_data(), instruments) or {}
            last_prices = last_data.get("prices", {})
            last_quotes = last_data.get("quotes", {})
            raw_quotes = {}
            raw_prices = {}
            if any(item.get("source") in SILVER_SPOT_SOURCES for item in instruments):
                spot_silver = self._fetch_spot_silver_price(metadata=raw_quotes)
                if spot_silver:
                    raw_prices[SILVER_SPOT_SYMBOL] = spot_silver
                    raw_quotes.setdefault(SILVER_SPOT_SYMBOL, observation("Gold API", datetime.now(timezone.utc).isoformat(timespec="seconds")))
            try:
                raw_prices.update(self._fetch_raw_prices(self._required_yahoo_symbols(instruments), metadata=raw_quotes))
            except Exception as error:
                log_message(f"Yahoo Finance verisi alınamadı: {error}")
            prices, computed = self._resolve_watchlist_prices(raw_prices, last_prices, instruments=instruments)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            by_key = {item["key"]: item for item in instruments}
            quotes = merge_observations(prices, computed, by_key, raw_quotes, last_quotes, now)
            for key in computed:
                old_time = last_quotes.get(key, {}).get("price_at")
                price_time = quotes[key].get("price_at")
                if old_time and price_time and price_time < old_time:
                    prices[key] = last_prices[key]
                    quotes[key] = last_quotes[key]

            received = {key for key in computed if quotes[key]["state"] == "received"}
            market_data = market_data_for_save(
                prices, timestamp=None if computed else last_data.get("timestamp"),
                sources=self._watchlist_price_sources(instruments), quotes=quotes,
            )
            if not computed:
                market_data["timestamp"] = last_data.get("timestamp", "")
            if computed:
                self.save_last_data(market_data)
            samples = {}
            for key in received:
                stamp = quotes[key]["price_at"]
                # A provider can return the same closing print all night.
                if stamp and stamp == last_quotes.get(key, {}).get("price_at"):
                    continue
                samples.setdefault(stamp or now, {})[key] = prices[key]
            for stamp, batch in samples.items():
                self.history_db.insert_prices(batch, instruments, timestamp=stamp)
            self.root.after(0, lambda: self._publish_quotes(market_data, received))
        except Exception as error:
            log_message(f"Fiyat verisi yenilenemedi: {error}")
            cached = self._compatible_cached_data(self.load_last_data())
            self.root.after(0, lambda: self._publish_quotes(cached or {}, set()))
        finally:
            self._fetch_lock.release()

    def _publish_quotes(self, data, received):
        visible = {item["key"] for item in self.watchlist}
        fresh_count = len(visible & received)
        if fresh_count and fresh_count == len(visible):
            status, color = "Veri alındı", self.color_success
        elif fresh_count:
            status, color = f"{fresh_count}/{len(visible)} fiyat alındı", self.color_text_dim
        elif data.get("prices"):
            status, color = "Son kayıtlar", self.color_text_dim
        else:
            status, color = "Fiyat alınamadı", self.color_danger
        self.var_market_status.set("●")
        self.lbl_market_status.config(fg=color)
        self.var_market_label.set(status)
        self.guncelle_arayuz(data)

    def guncelle_arayuz(self, data_or_ons=None, gram_g=None, gram_a=None):
        if isinstance(data_or_ons, dict):
            data = normalize_market_data(data_or_ons)
        else:
            prices = {}
            if data_or_ons is not None:
                prices["gumus_ons"] = data_or_ons
            if gram_g is not None:
                prices["gumus_tl"] = gram_g
            if gram_a is not None:
                prices["altin_tl"] = gram_a
            data = market_data_for_save(prices)

        if not data:
            return

        available_keys = {item["key"] for item in self._quote_instruments()}
        prices = {key: price for key, price in data["prices"].items() if key in available_keys}
        self._last_prices = dict(prices)
        self._last_quotes = data["quotes"]
        for instrument in self.watchlist:
            var = getattr(self, "price_vars", {}).get(instrument["key"])
            if var:
                var.set(format_instrument_value(instrument, prices.get(instrument["key"])))

        self._update_price_row_visuals(prices)
        if hasattr(self, "_update_widget_quote_labels"):
            self._update_widget_quote_labels()

        if prices.get("dolar"):
            self.last_dolar_rate = prices["dolar"]

        self.var_time.set(data.get("timestamp") or "—")

        self._update_portfolio_view()

    def _update_portfolio_view(self, *, ledger_changed=False):
        summary = self.tm.get_portfolio_summary(self._last_prices, self.workspace_instruments())
        self.rebuild_portfolio_breakdown(summary)
        self._refresh_workspace(summary=summary, ledger_changed=ledger_changed)
        rows = summary["rows"]
        priced_positions = sum(row["has_price"] for row in rows)
        coverage = getattr(self, "var_portfolio_coverage", None)
        load_error = getattr(self.tm, "load_error", None)
        if coverage is not None:
            coverage.set(f"{priced_positions}/{len(rows)} fiyatlandı" if priced_positions < len(rows) else "")
        if isinstance(load_error, str) and load_error:
            self.var_portfolio.set("Kayıt okunamadı")
            self.var_profit.set("Ayarlar → Yedeği geri yükle")
            self.lbl_profit.config(fg=self.color_danger, bg=self.color_card_alt)
            return
        if not rows or not priced_positions:
            self.var_portfolio.set("Fiyat yok" if rows else "₺0")
            self.var_profit.set("Fiyat bekleniyor" if rows else "%0.0 (₺0)")
            self.lbl_profit.config(fg=self.color_text_dim, bg=self.color_card_alt)
            return

        self.var_portfolio.set(self._format_tl(summary["total_value_tl"]))
        profit_tl = summary["profit_tl"]
        profit_pct = summary["profit_pct"]
        sign = "+" if profit_tl >= 0 else "−"
        change = f"{sign}%{abs(profit_pct):.1f} ({sign}{self._format_tl(abs(profit_tl))})"
        self.var_profit.set(change)
        color = self.color_success if profit_tl >= 0 else self.color_danger
        bg = self.color_success_bg if profit_tl >= 0 else self.color_danger_bg
        self.lbl_profit.config(fg=color, bg=bg)

    def _open_workspace_from_widget(self):
        if self.current_page == 1:
            return self.open_workspace("markets", self.chart_var.get(), period=self.chart_period.get())
        return self.open_workspace()

    def open_workspace(self, view="overview", asset_key=None, *, period=None):
        if self.workspace is None or not self.workspace.winfo_exists():
            self.workspace = DesktopWorkspace(self)
        self.workspace.present(view, asset_key, period=period)
        return self.workspace

    def _refresh_workspace(self, **changes):
        workspace = getattr(self, "workspace", None)
        if workspace is not None and workspace.winfo_exists():
            workspace.refresh(**changes)

    @staticmethod
    def workspace_chart_helpers():
        return {"parse_timestamp": parse_history_timestamp, "downsample": downsample_history_rows}

    def workspace_watch_row(self, key):
        if key not in self.price_vars:
            return None
        return (
            self.price_vars[key].get(), self.price_change_vars[key].get(),
            self.price_change_labels[key].cget("fg"),
            getattr(self.price_spark_canvases[key], "_sparkline_payload", None),
        )

    def workspace_quote_status(self, key, compact=False):
        return quote_status(
            getattr(self, "_last_prices", {}).get(key),
            getattr(self, "_last_quotes", {}).get(key),
            compact=compact,
        )

    def workspace_transaction_rows(self):
        instruments = {item["key"]: item for item in portfolio_instruments(self.workspace_instruments())}
        return [
            PortfolioManagerDialog.transaction_row(self.tm, instruments, index, raw)
            for index, raw in enumerate(self.tm.transactions)
        ]

    def _on_transactions_changed(self):
        self._update_portfolio_view(ledger_changed=True)

    def _on_trade_saved(self):
        self._on_transactions_changed()
        workspace = getattr(self, "workspace", None)
        if workspace is not None and workspace.winfo_exists():
            workspace.show_notice("İşlem kaydedildi")
        self.var_trade_feedback.set("Kaydedildi")
        previous = getattr(self, "_trade_feedback_job", None)
        if previous is not None:
            self.root.after_cancel(previous)
        self._trade_feedback_job = self.root.after(3000, self._clear_trade_feedback)

    def _clear_trade_feedback(self):
        self._trade_feedback_job = None
        self.var_trade_feedback.set("")

    def _cancel_trade_feedback(self, event):
        job = getattr(self, "_trade_feedback_job", None)
        if event.widget is self.root and job is not None:
            self.root.after_cancel(job)
            self._trade_feedback_job = None

    def open_add_transaction(self, *, parent=None, instrument_key=None, edit_index=None):
        parent = parent or self.root
        if self.tm.load_error:
            if messagebox.askyesno("İşlemler salt okunur", f"{self.tm.load_error}\nYedekten geri yüklemek ister misiniz?", parent=parent):
                self.restore_transactions(parent=parent)
            return None
        dialog = getattr(self, "portfolio_dialog", None)
        new_dialog = dialog is None or not dialog.winfo_exists()
        if new_dialog:
            dialog = PortfolioManagerDialog(
                parent, self.tm, self._on_trade_saved,
                getattr(self, "last_dolar_rate", None), self.workspace_instruments(), quick=True,
            )
            self.portfolio_dialog = dialog
        selected = dialog.selected_instrument() or {}
        changing_asset = instrument_key in dialog.instrument_by_key and selected.get("key") != instrument_key
        destination = int(edit_index) if edit_index is not None else None
        changing_mode = dialog.edit_index != destination
        if not new_dialog and (changing_mode or changing_asset):
            if not dialog.confirm_discard_draft():
                dialog.present()
                return dialog
            if destination is None:
                dialog.cancel_edit()
        if destination is not None and (new_dialog or changing_mode):
            dialog.start_edit(edit_index)
        elif destination is None and changing_asset:
            dialog.var_instrument.set(dialog.instrument_by_key[instrument_key]["label"])
            dialog.update_amount_label()
            dialog.mark_current_draft()
        dialog.present()
        return dialog

    def delete_saved_transaction(self, source_index, *, parent):
        index = int(source_index)
        dialog = getattr(self, "portfolio_dialog", None)
        dialog_open = dialog is not None and dialog.winfo_exists()
        if dialog_open and dialog.edit_index == index and not dialog.confirm_discard_draft():
            return
        instruments = {item["key"]: item for item in self.workspace_instruments()}
        if PortfolioManagerDialog.confirm_transaction_deletion(parent, self.tm, instruments, index):
            if dialog_open:
                dialog.refresh_after_deletion(index)
            self._on_transactions_changed()

    def import_transactions(self, *, parent=None):
        parent = parent or self.root
        filename = filedialog.askopenfilename(parent=parent, title="İçe Aktarılacak Dosyayı Seç", filetypes=[("JSON Files", "*.json")])
        if not filename:
            return
        try:
            transactions, duplicates = self.tm.preview_import(self.tm.read_import(filename))
            if not transactions:
                messagebox.showinfo("İçe aktar", f"Yeni kayıt yok. Atlanan tekrar: {duplicates}.", parent=parent)
                return
            preview = f"Eklenecek: {len(transactions)}\nAtlanacak olası tekrar: {duplicates}"
            if not messagebox.askyesno("İçe aktarma önizlemesi", preview, parent=parent):
                return
            self.tm.save_many(transactions)
        except (ValueError, OSError) as error:
            messagebox.showerror("İçe aktarma hatası", str(error), parent=parent)
            return
        dialog = getattr(self, "portfolio_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.load_list()
            dialog.update_amount_label()
        self._on_transactions_changed()
        messagebox.showinfo("Başarılı", f"{len(transactions)} işlem içe aktarıldı.", parent=parent)

    def export_transactions(self, *, parent=None):
        parent = parent or self.root
        target = filedialog.asksaveasfilename(parent=parent, title="İşlem yedeğini dışa aktar",
                                            initialfile=f"Piyasa-islemler-{datetime.now():%Y%m%d}.json",
                                            defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not target:
            return
        try:
            self.tm.export_backup(target)
        except (ValueError, OSError) as error:
            messagebox.showerror("Yedek kaydedilemedi", str(error), parent=parent)
            return
        messagebox.showinfo("Yedek kaydedildi", os.path.basename(target), parent=parent)

    def restore_transactions(self, *, parent=None):
        parent = parent or self.root
        source = filedialog.askopenfilename(parent=parent, title="İşlem yedeğini seç",
                                           filetypes=[("JSON", "*.json")])
        if not source:
            return
        dialog = getattr(self, "portfolio_dialog", None)
        dialog_open = dialog is not None and dialog.winfo_exists()
        if dialog_open and not dialog.confirm_discard_draft():
            return
        try:
            records = self.tm.read_import(source)
            if not messagebox.askyesno("Yedeği geri yükle", f"Seçilen yedek {len(records)} işlem içeriyor.\n"
                                       "Mevcut dosya ayrı bir kurtarma kopyasına alınacak.", parent=parent):
                return
            recovery = self.tm.restore_backup(source)
        except (ValueError, OSError) as error:
            messagebox.showerror("Yedek yüklenemedi", str(error), parent=parent)
            return
        if dialog_open:
            dialog.destroy()
        self._on_transactions_changed()
        text = f"{len(records)} işlem geri yüklendi."
        if recovery:
            text += f"\nÖnceki dosya: {os.path.basename(recovery)}"
        messagebox.showinfo("Yedek geri yüklendi", text, parent=parent)

    def open_watchlist_settings(self, *, parent=None):
        return WatchlistDialog(parent or self.root, self.watchlist_manager, self.on_watchlist_changed, self.validate_yahoo_symbol)

    def on_watchlist_changed(self):
        self.watchlist = self.watchlist_manager.instruments
        self.rebuild_price_rows()
        self._rebuild_chart_symbol_buttons()
        self._rebuild_stats_sections()

        last_data = self._compatible_cached_data(self.load_last_data())
        if last_data:
            self.guncelle_arayuz(last_data)
        self._update_portfolio_view(ledger_changed=True)
        self.request_data_refresh()
        self.request_history_backfill()

    def open_settings(self, event=None):
        anchor = getattr(self, "footer_buttons", {}).get("settings")
        if anchor is not None:
            self.settings_menu.toggle_for_widget(anchor)
        
    def make_toolwindow(self):
        # Windows API kullanarak pencereyi Taskbar'dan ve Alt-Tab'dan gizleme
        # GWL_EXSTYLE = -20
        # WS_EX_TOOLWINDOW = 0x00000080
        # WS_EX_APPWINDOW = 0x00040000
        
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        style = style | 0x00000080 # WS_EX_TOOLWINDOW ekle
        style = style & ~0x00040000 # WS_EX_APPWINDOW çıkar (bazı durumlarda default olabilir)
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)
        
        # Değişikliğin hemen uygulanması için
        self.root.withdraw()
        self.root.after(10, self.root.deiconify)

    def veri_dongusu(self):
        next_history_sync = 0.0
        while not self._stop_event.is_set():
            self.veri_getir()
            now = time.monotonic()
            if now >= next_history_sync and not self._stop_event.is_set():
                self.request_history_backfill()
                next_history_sync = now + (30 * 60)
            self._stop_event.wait(self.refresh_rate)


    # --- Yeniden Boyutlandırma Mantığı ---
    def start_resize(self, event):
        self._is_resizing = True
        self.resize_start_x = event.x_root
        self.resize_start_y = event.y_root
        self.start_width = self.root.winfo_width()
        self.start_height = self.root.winfo_height()

    def do_resize(self, event):
        deltax = event.x_root - self.resize_start_x
        deltay = event.y_root - self.resize_start_y
        
        new_width = max(300, self.start_width + deltax)
        new_height = max(360, self.start_height + deltay)
        
        self.root.geometry(f"{new_width}x{new_height}")
    
    def _on_resize_end(self, event):
        """Resize grip bırakıldığında çağrılır."""
        self._is_resizing = False
        if self._resize_after_id is not None:
            self.root.after_cancel(self._resize_after_id)
        self._apply_font_scale()
        self.root.after_idle(self._save_widget_geometry)
    
    def _on_button_release(self, event):
        """Genel buton bırakma - resize flag'ini temizle."""
        self._is_resizing = False
        if self._window_move_origin is not None:
            self.root.after_idle(self._save_widget_geometry, self._window_move_origin[2:])
        self._window_move_origin = None

    def on_resize(self, event):
        if event.widget == self.root:
            w = event.width
            if abs(w - self.last_width) > 20:
                self.last_width = w
                # Debounce: Sürekli sürükleme sırasında font güncelleme yapma,
                # sadece kullanıcı durunca (150ms sonra) güncelle
                if self._resize_after_id is not None:
                    self.root.after_cancel(self._resize_after_id)
                self._resize_after_id = self.root.after(150, self._apply_font_scale)
    
    def _apply_font_scale(self):
        """Fontları mevcut pencere genişliğine göre ölçekle (debounced)."""
        self._resize_after_id = None
        w = self.root.winfo_width()
        scale = max(1.0, w / 320.0)
        scale = min(1.5, scale)  # Çok büyümesin
        
        self.font_header.config(size=int(self.base_fonts["header"] * scale))
        self.font_label.config(size=int(self.base_fonts["label"] * scale))
        self.font_value.config(size=int(self.base_fonts["value"] * scale))
        self.font_portfolio.config(size=int(self.base_fonts["portfolio"] * scale))
        self.font_profit.config(size=int(self.base_fonts["profit"] * scale))
        self.font_market_status.config(size=int(self.base_fonts["market_status"] * scale))
        self.font_icon.config(size=int(self.base_fonts["icon"] * scale))
        self.font_small.config(size=int(self.base_fonts["small"] * scale))
        self.font_badge.config(size=int(self.base_fonts["badge"] * scale))

    # --- Sürükleme Mantığı ---
    def start_move(self, event):
        self._window_move_origin = None
        widget = event.widget
        while widget is not None and widget != self.root:
            if widget == self.grip or isinstance(widget, (
                tk.Canvas, tk.Button, tk.Entry, tk.Radiobutton, tk.Checkbutton,
                ttk.Combobox, ttk.Scrollbar, ttk.Treeview,
            )):
                return
            widget = widget.master
        self._window_move_origin = (event.x_root, event.y_root,
                                    self.root.winfo_x(), self.root.winfo_y())

    def do_move(self, event):
        origin = self._window_move_origin
        if origin is None or self._is_resizing or self._row_press_key is not None:
            return
        pointer_x, pointer_y, window_x, window_y = origin
        dx, dy = event.x_root - pointer_x, event.y_root - pointer_y
        if abs(dx) <= 4 and abs(dy) <= 4:
            return
        # The leading plus selects literal offsets even on left/upper monitors.
        self.root.geometry(f"+{window_x + dx}+{window_y + dy}")

    def show_menu(self, event):
        self.menu.post(event.x_root, event.y_root)

    def kapat(self):
        self._stop_event.set()
        self.root.destroy()
        sys.exit()

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = PiyasaWidget()
    app.run()
