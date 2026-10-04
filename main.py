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
import winreg
import ctypes
import requests
import webbrowser
import subprocess
from datetime import datetime, timedelta
from tkinter import messagebox, filedialog

from theme import SPACING, THEME, configure_ttk_styles, font
from ui_components import (
    HoverButton,
    IconButton,
    ModernEntry,
    SearchEntry,
    SegmentedControl,
    TransactionTable,
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
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                prices[str(key)] = value

    for legacy_key, new_key in LEGACY_MARKET_FIELDS.items():
        if new_key in prices:
            continue
        try:
            value = float(data.get(legacy_key, 0))
        except (TypeError, ValueError):
            value = 0
        if value > 0:
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
    }
    for legacy_key, new_key in LEGACY_MARKET_FIELDS.items():
        if new_key in prices:
            normalized[legacy_key] = prices[new_key]
    return normalized


def market_data_for_save(prices, timestamp=None, sources=None):
    data = {
        "prices": {str(key): float(value) for key, value in prices.items() if value and value > 0},
        "timestamp": timestamp or time.strftime("%d.%m %H:%M"),
    }
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
    return f"{prefix}{value:,.{decimals}f}"


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
    if price is None or price <= 0:
        return None
    currency = instrument.get("currency", "₺")
    if currency == "$":
        dolar = safe_float(prices.get("dolar"), 0)
        if dolar <= 0:
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
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump({"instruments": self.instruments}, f, indent=4, ensure_ascii=False)

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

        key = make_watchlist_key(label, symbol)
        existing_keys = {item["key"] for item in self.instruments}
        if key in existing_keys:
            base = key
            suffix = 2
            while f"{base}_{suffix}" in existing_keys:
                suffix += 1
            key = f"{base}_{suffix}"

        instrument = normalize_instrument({
            "key": key,
            "label": label,
            "symbol": symbol,
            "currency": currency or "",
            "decimals": 2,
            "color": "#2196f3",
            "source": "direct",
        })
        self.instruments.append(instrument)
        self.save_all()
        return instrument

    def remove(self, key):
        if len(self.instruments) <= 1:
            raise ValueError("En az bir varlık izleme listesinde kalmalı.")
        original_count = len(self.instruments)
        self.instruments = [item for item in self.instruments if item["key"] != key]
        if len(self.instruments) == original_count:
            raise ValueError("Silinecek sembol bulunamadı.")
        self.save_all()

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
        self.transactions = self.load()

    def load(self):
        if not os.path.exists(self.filename):
            return []
        try:
            with open(self.filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []

    def save(self, transaction):
        self.validate_transaction(transaction)
        self.transactions.append(transaction)
        self.save_all()

    def replace(self, index, transaction):
        if index < 0 or index >= len(self.transactions):
            raise ValueError("Düzenlenecek işlem bulunamadı.")
        self.validate_transaction(transaction, exclude_index=index)
        self.transactions[index] = transaction
        self.save_all()

    def validate_transaction(self, transaction, exclude_index=None):
        normalized = self.normalize_transaction(transaction)
        if normalized["quantity"] <= 0:
            raise ValueError("Miktar pozitif olmalı.")
        if normalized["total_tl"] < 0:
            raise ValueError("Toplam tutar sıfır veya pozitif olmalı.")
        if normalized["action"] == "sell":
            current_qty = self.get_position_quantity(normalized["instrument_key"], exclude_index=exclude_index)
            if normalized["quantity"] > current_qty + 1e-9:
                raise ValueError("Satış miktarı mevcut miktardan büyük olamaz.")
        
    def save_all(self):
        with open(self.filename, 'w', encoding='utf-8') as f:
            json.dump(self.transactions, f, indent=4, ensure_ascii=False)

    @staticmethod
    def normalize_transaction(transaction):
        t = dict(transaction or {})
        instrument_key = str(t.get("instrument_key") or "gumus_tl")
        action = str(t.get("action") or "buy").lower()
        if action not in ("buy", "sell"):
            action = "buy"

        quantity = safe_float(t.get("quantity", t.get("amount_g", 0)))
        total_tl = safe_float(t.get("total_tl", 0))
        total_usd = safe_float(t.get("total_usd", 0))
        fx_rate = safe_float(t.get("fx_rate", 0))
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
        }

    def get_position_quantity(self, instrument_key, exclude_index=None):
        positions = self.get_positions(exclude_index=exclude_index)
        return positions.get(instrument_key, {}).get("quantity", 0.0)

    def get_positions(self, exclude_index=None):
        positions = {}
        for idx, transaction in enumerate(self.transactions):
            if exclude_index is not None and idx == exclude_index:
                continue
            t = self.normalize_transaction(transaction)
            key = t["instrument_key"]
            qty = t["quantity"]
            total_tl = t["total_tl"]
            if qty <= 0:
                continue

            position = positions.setdefault(key, {
                "quantity": 0.0,
                "cost_basis_tl": 0.0,
                "realized_profit_tl": 0.0,
                "instrument_label": t.get("instrument_label", key),
            })

            if t["action"] == "buy":
                position["quantity"] += qty
                position["cost_basis_tl"] += total_tl
            else:
                if position["quantity"] <= 0:
                    continue
                sell_qty = min(qty, position["quantity"])
                avg_cost = position["cost_basis_tl"] / position["quantity"] if position["quantity"] else 0
                removed_cost = avg_cost * sell_qty
                sale_value = total_tl * (sell_qty / qty) if qty else 0
                position["quantity"] -= sell_qty
                position["cost_basis_tl"] -= removed_cost
                position["realized_profit_tl"] += sale_value - removed_cost
                if position["quantity"] <= 1e-9:
                    position["quantity"] = 0.0
                    position["cost_basis_tl"] = 0.0
        return positions

    def get_portfolio_summary(self, prices, instruments):
        prices = prices or {}
        instrument_map = {item["key"]: item for item in instruments}
        positions = self.get_positions()
        rows = []
        total_value = 0.0
        total_cost = 0.0

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
            rows.append({
                "key": key,
                "label": instrument.get("label", key),
                "quantity": quantity,
                "unit": unit,
                "cost_basis_tl": cost_basis,
                "current_price_tl": current_price_tl,
                "value_tl": value_tl,
                "profit_tl": profit_tl,
                "profit_pct": profit_pct,
                "has_price": current_price_tl is not None,
                "color": instrument.get("color", "#2196f3"),
            })

        unrealized_profit = total_value - total_cost if total_cost > 0 else 0.0
        unrealized_profit_pct = (unrealized_profit / total_cost) * 100 if total_cost > 0 else 0.0
        rows.sort(key=lambda item: item["value_tl"], reverse=True)
        return {
            "total_value_tl": total_value,
            "total_cost_tl": total_cost,
            "profit_tl": unrealized_profit,
            "profit_pct": unrealized_profit_pct,
            "rows": rows,
        }

    def get_summary(self):
        positions = self.get_positions()
        gumus = positions.get("gumus_tl", {})
        total_investment = gumus.get("cost_basis_tl", 0.0)
        total_gumus = gumus.get("quantity", 0.0)
        return total_investment, total_gumus


class MarketHistoryDB:
    def __init__(self, db_name="market_history.db"):
        self.db_path = db_name if os.path.isabs(db_name) else app_path(db_name)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._create_table()
        self.cleanup_bad_records()
        self._migrate_legacy_history()

    def _create_table(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS market_price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                instrument_key TEXT NOT NULL,
                label TEXT,
                source_symbol TEXT,
                price REAL NOT NULL
            )
        """)
        columns = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(market_price_history)").fetchall()
        }
        if "source_symbol" not in columns:
            self.conn.execute(
                "ALTER TABLE market_price_history ADD COLUMN source_symbol TEXT"
            )
        self.conn.execute(
            "UPDATE market_price_history SET source_symbol = ? "
            "WHERE source_symbol IS NULL AND instrument_key IN (?, ?)",
            (LEGACY_SILVER_FUTURES_SYMBOL, "gumus_ons", "gumus_tl")
        )
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_market_price_history_key_time
            ON market_price_history (instrument_key, timestamp)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_market_price_history_key_source_time
            ON market_price_history (instrument_key, source_symbol, timestamp)
        """)
        self.conn.commit()

    def _table_exists(self, table_name):
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
                            "INSERT INTO market_price_history "
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

    def insert_prices(self, prices, instruments=None, timestamp=None):
        if not prices:
            return
        timestamp = timestamp or datetime.now().isoformat()
        instrument_map = {item["key"]: item for item in (instruments or [])}
        inserted = 0
        for key, value in prices.items():
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            label = instrument_map.get(key, {}).get("label", key)
            source_symbol = instrument_map.get(key, {}).get("symbol")
            self.conn.execute(
                "INSERT INTO market_price_history "
                "(timestamp, instrument_key, label, source_symbol, price) "
                "VALUES (?, ?, ?, ?, ?)",
                (timestamp, key, label, source_symbol, value)
            )
            inserted += 1
        if inserted:
            self.conn.commit()

    def get_history(self, instrument_key="gumus_tl", days=7, source_symbol=None):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        query = (
            "SELECT timestamp, price FROM market_price_history "
            "WHERE instrument_key = ? AND timestamp >= ? AND price > 0"
        )
        params = [instrument_key, cutoff]
        if source_symbol:
            query += " AND source_symbol = ?"
            params.append(source_symbol)
        cursor = self.conn.execute(query + " ORDER BY timestamp", tuple(params))
        return cursor.fetchall()

    def get_all_history(self, instrument_key="gumus_tl", source_symbol=None):
        query = (
            "SELECT timestamp, price FROM market_price_history "
            "WHERE instrument_key = ? AND price > 0"
        )
        params = [instrument_key]
        if source_symbol:
            query += " AND source_symbol = ?"
            params.append(source_symbol)
        cursor = self.conn.execute(query + " ORDER BY timestamp", tuple(params))
        return cursor.fetchall()

    def get_stats(self, instrument_key="gumus_tl", days=None, source_symbol=None):
        clauses = ["instrument_key = ?", "price > 0"]
        params = [instrument_key]
        if days:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            clauses.append("timestamp >= ?")
            params.append(cutoff)
        if source_symbol:
            clauses.append("source_symbol = ?")
            params.append(source_symbol)
        cursor = self.conn.execute(
            "SELECT MIN(price), MAX(price), AVG(price), COUNT(*) "
            "FROM market_price_history WHERE " + " AND ".join(clauses),
            tuple(params)
        )
        return cursor.fetchone()

    def get_first_last(self, instrument_key="gumus_tl", days=None, source_symbol=None):
        clauses = ["instrument_key = ?", "price > 0"]
        params = [instrument_key]
        if days:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            clauses.append("timestamp >= ?")
            params.append(cutoff)
        if source_symbol:
            clauses.append("source_symbol = ?")
            params.append(source_symbol)
        base_query = (
            "SELECT price FROM market_price_history WHERE "
            + " AND ".join(clauses)
        )
        first = self.conn.execute(
            base_query + " ORDER BY timestamp ASC LIMIT 1", tuple(params)
        ).fetchone()
        last = self.conn.execute(
            base_query + " ORDER BY timestamp DESC LIMIT 1", tuple(params)
        ).fetchone()
        return first, last

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
            response = requests.get(self.api_url)
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
                
                if latest_tag > self.current_version and download_url:
                    return True, latest_tag, download_url
            return False, None, None
        except Exception as e:
            log_message(f"Update Check Error: {e}")
            return False, None, None

    def update_application(self, download_url):
        try:
            # İndirme işlemi
            temp_path = os.path.join(os.environ["TEMP"], "PiyasaWidget_Update.exe")
            response = requests.get(download_url, stream=True)
            with open(temp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # Installer'ı çalıştır ve uygulamayı kapat
            subprocess.Popen([temp_path, "/SILENT"]) # Silent kurulum deneyebiliriz veya normal
            return True
        except Exception as e:
            messagebox.showerror("Hata", f"Güncelleme hatası: {e}")
            return False

class PortfolioManagerDialog(tk.Toplevel):
    ALL_ASSETS_LABEL = "Tüm Varlıklar"

    def __init__(self, parent, manager, on_save_callback, current_dollar_rate, instruments):
        super().__init__(parent)
        self.manager = manager
        self.on_save_callback = on_save_callback
        self.current_dollar_rate = current_dollar_rate
        self.instruments = portfolio_instruments(instruments)
        self.instrument_by_key = {item["key"]: item for item in self.instruments}
        self.instrument_label_to_key = {item["label"]: item["key"] for item in self.instruments}
        self.edit_index = None
        self._form_scroll_after_id = None
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

        self.title("Portföy Yönetimi")
        self.geometry("1120x760")
        self.minsize(980, 700)
        self.configure(bg=self.bg_color)
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
        tk.Label(
            header,
            text="Portföy Yönetimi",
            bg=self.bg_color,
            fg=THEME["text_primary"],
            font=font(self, "title", "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Alım ve satış işlemlerinizi tek yerden görüntüleyin ve yönetin.",
            bg=self.bg_color,
            fg=THEME["text_secondary"],
            font=font(self, "body"),
            anchor="w",
        ).pack(anchor="w", pady=(SPACING["xxs"], 0))

        content = tk.Frame(self, bg=self.bg_color)
        content.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=SPACING["xl"],
            pady=(0, SPACING["xl"]),
        )
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1, minsize=590)
        content.grid_columnconfigure(1, weight=0, minsize=330)

        # --- Sol kart: İşlem geçmişi ---
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
        self.tree = TransactionTable(table_border, self.start_edit, self.delete_transaction)
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

        # --- Sağ kart: Yeni işlem / düzenleme formu ---
        right_frame = tk.Frame(
            content,
            bg=self.color_card,
            padx=SPACING["lg"],
            pady=SPACING["lg"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        right_frame.grid(row=0, column=1, sticky="nsew")
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_rowconfigure(2, weight=1)

        self.var_form_title = tk.StringVar(value="Yeni İşlem")
        tk.Label(
            right_frame,
            textvariable=self.var_form_title,
            bg=self.color_card,
            fg=self.color_text_main,
            font=font(self, "section", "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            right_frame,
            text="Portföyünüze yeni bir alım veya satış işlemi ekleyin.",
            bg=self.color_card,
            fg=self.color_text_dim,
            font=font(self, "small"),
            justify="left",
            wraplength=290,
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(SPACING["xxs"], SPACING["md"]))

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

        def field_frame(label_text):
            frame = tk.Frame(form_body, bg=self.color_card)
            tk.Label(frame, text=label_text, **self.style_label).pack(anchor="w", pady=(0, SPACING["xxs"]))
            return frame

        # 1. Tarih
        date_field = field_frame("Tarih")
        date_field.pack(fill="x", pady=(0, SPACING["sm"]))
        self.entry_date = ModernEntry(date_field)
        self.entry_date.pack(fill="x")
        self.entry_date.insert(0, datetime.now().strftime("%d-%m-%Y"))

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

        # 3. İşlem türü
        action_field = field_frame("İşlem")
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
        self.entry_amount = ModernEntry(amount_field)
        self.entry_amount.pack(fill="x")
        
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
        self.entry_rate = ModernEntry(self.frame_rate)
        self.entry_rate.pack(fill="x")
        self.entry_rate.insert(0, f"{self.current_dollar_rate:.4f}")
        
        # 7. Toplam Tutar
        self.total_field_frame = field_frame("Toplam Tutar")
        self.total_field_frame.pack(fill="x", pady=(0, SPACING["sm"]))
        self.entry_total = ModernEntry(self.total_field_frame)
        self.entry_total.pack(fill="x")
        
        # Ekle/Güncelle Butonları
        button_frame = tk.Frame(form_body, bg=self.color_card)
        button_frame.pack(fill="x", pady=(SPACING["xs"], 0))
        self.btn_save = HoverButton(
            button_frame,
            text="+  İşlemi Ekle",
            command=self.save,
            pady=10,
        )
        self.btn_save.pack(fill="x", pady=(0, SPACING["xs"]))

        self.btn_cancel_edit = HoverButton(
            button_frame,
            text="Düzenlemeyi İptal Et",
            command=self.cancel_edit,
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
        self._search_trace_id = self.var_search.trace_add("write", self._on_search_changed)
        self.load_list()

    def selected_instrument(self):
        key = self.instrument_label_to_key.get(self.var_instrument.get())
        return self.instrument_by_key.get(key)

    def update_amount_label(self):
        instrument = self.selected_instrument()
        unit = portfolio_unit(instrument or {})
        self.lbl_amount.config(text=f"Miktar ({unit})")

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
        if event.widget is not self or self._form_scroll_after_id is None:
            return
        try:
            self.after_cancel(self._form_scroll_after_id)
        except (tk.TclError, ValueError):
            pass
        self._form_scroll_after_id = None

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
        self._schedule_form_scroll_sync()

    def _on_search_changed(self, *_args):
        self.load_list()

    def _transaction_row(self, source_index, raw_transaction):
        transaction = self.manager.normalize_transaction(raw_transaction)
        instrument = self.instrument_by_key.get(transaction["instrument_key"])
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
            "asset": str(label),
            "amount": quantity_text,
            "total": total_text,
            "searchable": searchable,
        }

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
        self._refresh_asset_filter_values()
        rows = self._filtered_transaction_rows()
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
        instrument = self.instrument_by_key.get(transaction["instrument_key"])
        if instrument:
            self.var_instrument.set(instrument["label"])
            self.update_amount_label()

        self.entry_date.delete(0, 'end')
        self.entry_date.insert(0, transaction["date"])
        self.var_action.set(transaction["action"])
        self.entry_amount.delete(0, 'end')
        self.entry_amount.insert(0, f"{transaction['quantity']:.4f}".rstrip("0").rstrip("."))
        self.var_currency.set(transaction["currency"])
        self.toggle_rate_entry()
        self.entry_total.delete(0, 'end')
        if transaction["currency"] == "USD":
            self.entry_total.insert(0, f"{transaction['total_usd']:.2f}")
            self.entry_rate.delete(0, 'end')
            self.entry_rate.insert(0, f"{transaction['fx_rate'] or self.current_dollar_rate:.4f}")
        else:
            self.entry_total.insert(0, f"{transaction['total_tl']:.2f}")

        self.edit_index = idx
        self.var_form_title.set("İşlemi Düzenle")
        self.btn_save.config(text="Değişiklikleri Kaydet")
        if not self.btn_cancel_edit.winfo_ismapped():
            self.btn_cancel_edit.pack(fill="x")

    def cancel_edit(self):
        self.edit_index = None
        self.var_form_title.set("Yeni İşlem")
        self.btn_save.config(text="+  İşlemi Ekle")
        self.btn_cancel_edit.pack_forget()
        self.clear_form()

    def clear_form(self):
        self.entry_amount.delete(0, 'end')
        self.entry_total.delete(0, 'end')
        self.entry_date.delete(0, 'end')
        self.entry_date.insert(0, datetime.now().strftime("%d-%m-%Y"))
        self.var_action.set("buy")
        self.var_currency.set("TL")
        self.toggle_rate_entry()

    def delete_transaction(self, item_id):
        idx = int(item_id)
        transaction = self.manager.normalize_transaction(self.manager.transactions[idx])
        instrument = self.instrument_by_key.get(transaction["instrument_key"], {})
        label = instrument.get("label", transaction["instrument_key"])
        
        action_text = "alış" if transaction["action"] == "buy" else "satış"
        msg = f"{transaction['date']} tarihindeki {label} {action_text} işlemi silinecektir.\nOnaylıyor musunuz?"
        if tk.messagebox.askyesno("Onay", msg, parent=self):
            del self.manager.transactions[idx]
            self.manager.save_all()
            if self.edit_index == idx:
                self.cancel_edit()
            elif self.edit_index is not None and self.edit_index > idx:
                self.edit_index -= 1
            self.load_list()
            self.on_save_callback()

    def save(self):
        try:
            instrument = self.selected_instrument()
            if not instrument:
                tk.messagebox.showerror("Hata", "Lütfen bir varlık seçiniz.", parent=self)
                return

            amount = float(self.entry_amount.get().replace(',', '.'))
            total_entered = float(self.entry_total.get().replace(',', '.'))
            if amount <= 0 or total_entered < 0:
                raise ValueError("Miktar pozitif, toplam tutar sıfır veya pozitif olmalı.")
            currency = self.var_currency.get()
            
            data = {
                "date": self.entry_date.get(),
                "instrument_key": instrument["key"],
                "instrument_label": instrument["label"],
                "action": self.var_action.get(),
                "quantity": amount,
                "currency": currency
            }
            
            if currency == "USD":
                data["total_usd"] = total_entered
                
                # Kullanıcının girdiği kur
                try:
                    user_rate = float(self.entry_rate.get().replace(',', '.'))
                except:
                    user_rate = self.current_dollar_rate # Fallback
                
                data["fx_rate"] = user_rate
                data["total_tl"] = total_entered * user_rate
            else:
                data["total_tl"] = total_entered
                data["total_usd"] = 0
                data["fx_rate"] = 0
            
            if self.edit_index is None:
                self.manager.save(data)
            else:
                self.manager.replace(self.edit_index, data)
                self.edit_index = None
                self.var_form_title.set("Yeni İşlem")
                self.btn_save.config(text="+  İşlemi Ekle")
                self.btn_cancel_edit.pack_forget()
            self.load_list()
            self.on_save_callback()
            
            # Formu temizle
            self.clear_form()
            
        except ValueError as e:
            message = str(e) if str(e) else "Lütfen geçerli sayısal değerler giriniz."
            tk.messagebox.showerror("Hata", message, parent=self)


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
        for instrument in self.manager.instruments:
            self.tree.insert("", "end", iid=instrument["key"], values=(
                instrument["label"],
                instrument["symbol"],
                instrument.get("currency", ""),
                "Sil",
            ))

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
        instrument = next((item for item in self.manager.instruments if item["key"] == item_id), None)
        if not instrument:
            return
        msg = f"{instrument['label']} ({instrument['symbol']}) izleme listesinden kaldırılsın mı?"
        if messagebox.askyesno("Onay", msg, parent=self):
            try:
                self.manager.remove(item_id)
                self.load_list()
                self.on_change_callback()
            except ValueError as e:
                messagebox.showerror("Hata", str(e), parent=self)

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
    ROW_DRAG_HOLD_MS = 1000
    ROW_DRAG_MOVE_TOLERANCE_PX = 6

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Market Widget")
        
        # --- AYARLAR ---
        self.bg_color = "#1e1e1e"  # Koyu Gri Arka Plan
        self.text_color = "#00ff41" # Matrix Yeşili
        self.alpha = 1.0           # Saydamlık kapalı (tam opak)
        self.refresh_rate = 60     # Saniye cinsinden yenileme
        self.default_topmost = False
        
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
        try:
            self.root.iconbitmap("icon.ico")
        except:
             pass
        
        # Taskbar'dan gizleme (Windows Widget Modu)
        self.make_toolwindow()
        
        # Başlangıç Konumu (Sağ Üst)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        initial_height = min(520, max(420, screen_height - 100))
        self.root.geometry(f"320x{initial_height}+{screen_width-350}+50")
        
        # Managers
        self.tm = TransactionManager()
        self.asm = AutoStartManager()
        self.um = UpdateManager(VERSION, GITHUB_REPO)
        self.watchlist_manager = WatchlistManager()
        self.watchlist = self.watchlist_manager.instruments
        self.history_db = MarketHistoryDB()
        self.current_page = 0
        self._fetch_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._row_hold_after_id = None
        self._row_press_key = None
        self._row_press_origin = None
        self._row_press_last = None
        self._row_drag_key = None
        self._row_drag_order = None
        self._row_drag_original_order = None
        self.price_row_widgets = {}
        
        # UI Elemanları
        self.setup_ui()
        
        # Sürükleme Özelliği
        self._is_resizing = False
        self.root.bind("<Button-1>", self.start_move)
        self.root.bind("<B1-Motion>", self.do_move)
        self.root.bind("<ButtonRelease-1>", self._on_button_release)
        
        # Sağ Tık Menüsü
        menu_style = {
            "bg": self.color_card_alt,
            "fg": self.color_text_main,
            "activebackground": self.color_accent,
            "activeforeground": "#ffffff",
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
        self.settings_menu = tk.Menu(self.root, tearoff=0, **menu_style)
        self.settings_menu.add_checkbutton(
            label="Windows ile başlat",
            variable=self.var_autostart,
            command=self.toggle_autostart,
            selectcolor=self.color_card
        )
        self.settings_menu.add_checkbutton(
            label="Her zaman üstte",
            variable=self.var_topmost,
            command=self.toggle_topmost,
            selectcolor=self.color_card
        )
        self.settings_menu.add_separator()
        self.settings_menu.add_command(label="İzlenenleri düzenle", command=self.open_watchlist_settings)
        self.settings_menu.add_command(label="Güncellemeleri kontrol et", command=self.check_updates)
        self.settings_menu.add_separator()
        self.settings_menu.add_command(label="Kapat", command=self.kapat)
        
        # İlk Veri Çekme
        self.update_thread = threading.Thread(target=self.veri_dongusu, daemon=True)
        self.update_thread.start()
        
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
            "nav_arrow": 13,
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
        self.font_nav_arrow = tkfont.Font(family=font_family, size=13)
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
        
        status_frame = tk.Frame(header_frame, bg=self.bg_color)
        status_frame.pack(side="left", fill="x", expand=True)

        self.var_market_status = tk.StringVar(value="•")
        self.lbl_market_status = tk.Label(
            status_frame,
            textvariable=self.var_market_status,
            bg=self.bg_color,
            fg=self.color_text_dim,
            font=self.font_market_status,
            anchor="w",
        )
        self.lbl_market_status.pack(side="left", padx=(0, SPACING["xxs"]))

        self.var_market_label = tk.StringVar(value="Piyasa bekleniyor")
        self.lbl_market_label = tk.Label(
            status_frame,
            textvariable=self.var_market_label,
            bg=self.bg_color,
            fg=self.color_text_dim,
            font=self.font_header,
            anchor="w",
        )
        self.lbl_market_label.pack(side="left")
        
        # Navigasyon çerçevesi
        nav_frame = tk.Frame(header_frame, bg=self.bg_color)
        nav_frame.pack(side="right")
        
        self.btn_prev = tk.Label(nav_frame, text="‹", bg=self.bg_color, fg=self.color_text_muted, font=self.font_nav_arrow, cursor="hand2")
        self.btn_prev.pack(side="left", padx=(0, SPACING["xxs"]))
        self.btn_prev.bind("<Button-1>", lambda e: self.prev_page())
        self.btn_prev.bind("<Enter>", lambda e: self.btn_prev.config(fg=self.color_text_main))
        self.btn_prev.bind("<Leave>", lambda e: self._update_arrow_colors())
        
        self.var_time = tk.StringVar(value="--:--")
        self.lbl_time = tk.Label(nav_frame, textvariable=self.var_time, bg=self.bg_color, fg=self.color_text_dim, font=self.font_header, anchor="center")
        self.lbl_time.pack(side="left")
        
        self.btn_next = tk.Label(nav_frame, text="›", bg=self.bg_color, fg=self.color_text_muted, font=self.font_nav_arrow, cursor="hand2")
        self.btn_next.pack(side="left", padx=(SPACING["xxs"], 0))
        self.btn_next.bind("<Button-1>", lambda e: self.next_page())
        self.btn_next.bind("<Enter>", lambda e: self.btn_next.config(fg=self.color_text_main))
        self.btn_next.bind("<Leave>", lambda e: self._update_arrow_colors())

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

        self.footer_buttons = {}
        for key, icon_name, callback in (
            ("settings", "settings", self.open_settings),
            ("import", "import", self.import_transactions),
            ("add", "plus", self.open_add_transaction),
            ("refresh", "refresh", self.request_data_refresh),
        ):
            button = IconButton(
                footer_frame,
                icon_name,
                callback,
                size=28,
                bg=self.color_card,
                hover_bg=THEME["surface_hover"],
                fg=self.color_text_muted,
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
        
        portfolio_frame = tk.Frame(
            self.page_main,
            bg=self.color_card,
            padx=SPACING["sm"],
            pady=SPACING["sm"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        portfolio_frame.pack(fill="x", pady=(0, SPACING["sm"]))
        
        tk.Label(portfolio_frame, text="TOPLAM VARLIK", bg=self.color_card, fg=self.color_text_dim, font=self.font_header, anchor="w").pack(fill="x")
        
        self.var_portfolio = tk.StringVar(value="₺...")
        tk.Label(portfolio_frame, textvariable=self.var_portfolio, bg=self.color_card, fg=self.color_text_main, font=self.font_portfolio, anchor="w").pack(fill="x", pady=(SPACING["xxs"], SPACING["xs"]))
        
        self.var_profit = tk.StringVar(value="...")
        self.lbl_profit = tk.Label(portfolio_frame, textvariable=self.var_profit, bg=THEME["surface_hover"], fg=self.color_text_dim, font=self.font_profit, anchor="w", padx=SPACING["xs"], pady=SPACING["xxs"])
        self.lbl_profit.pack(anchor="w")

        self.portfolio_breakdown_frame = tk.Frame(portfolio_frame, bg=self.color_card)
        self.portfolio_breakdown_frame.pack(fill="x", pady=(SPACING["xs"], 0))

        self.price_rows_frame = tk.Frame(self.page_main, bg=self.bg_color)
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
        self.grip = tk.Label(self.root, text="◢", bg=self.bg_color, fg=self.color_border, font=(font_family, 8), cursor="sizing")
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.grip.bind("<ButtonPress-1>", self.start_resize)
        self.grip.bind("<B1-Motion>", self.do_resize)
        self.grip.bind("<ButtonRelease-1>", self._on_resize_end)
        self.grip.bind("<Enter>", lambda e: self.grip.config(fg=self.color_text_muted))
        self.grip.bind("<Leave>", lambda e: self.grip.config(fg=self.color_border))

    def _draw_asset_icon(self, canvas, instrument):
        """Draw a small dependency-free asset mark on a market row."""
        canvas.delete("all")
        color = instrument.get("color", self.color_accent)
        canvas.create_oval(2, 2, 22, 22, fill=THEME["surface_hover"], outline=color, width=1)
        label = str(instrument.get("label") or instrument.get("key") or "?")
        canvas.create_text(12, 12, text=label[:1].upper(), fill=color, font=self.font_small)

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
            pady=6,
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        row.pack(fill="x", pady=SPACING["xxs"] // 2)
        row.grid_columnconfigure(2, weight=1)

        icon = tk.Canvas(row, width=24, height=24, bg=self.color_card_alt, highlightthickness=0, bd=0)
        icon.grid(row=0, column=0, sticky="w", padx=(0, 6))
        self._draw_asset_icon(icon, instrument)

        name_label = tk.Label(row, text=instrument["label"], bg=self.color_card_alt, fg=self.color_text_main, font=self.font_label, anchor="w")
        name_label.grid(row=0, column=1, sticky="w", padx=(0, SPACING["xs"]))

        spark = tk.Canvas(row, width=62, height=22, bg=self.color_card_alt, highlightthickness=0, bd=0)
        spark.grid(row=0, column=2, sticky="ew", padx=(0, SPACING["xs"]))
        
        placeholder = f"{instrument.get('currency', '')}..."
        var = tk.StringVar(value=placeholder)
        self.price_vars[instrument["key"]] = var
        if instrument["key"] == "gumus_ons":
            self.var_gumus_ons = var
        elif instrument["key"] == "gumus_tl":
            self.var_gumus_tl = var
        elif instrument["key"] == "altin_tl":
            self.var_altin_tl = var
        price_label = tk.Label(row, textvariable=var, bg=self.color_card_alt, fg=instrument.get("color", self.color_text_main), font=self.font_value, anchor="e")
        price_label.grid(row=0, column=3, sticky="e", padx=(0, 6))

        change_var = tk.StringVar(value="--")
        self.price_change_vars[instrument["key"]] = change_var
        badge = tk.Label(row, textvariable=change_var, bg=THEME["surface_pressed"], fg=self.color_text_muted, font=self.font_badge, padx=6, pady=2)
        badge.grid(row=0, column=4, sticky="e")
        self.price_change_labels[instrument["key"]] = badge
        self.price_spark_canvases[instrument["key"]] = spark
        self.price_row_widgets[instrument["key"]] = row
        drag_widgets = (row, icon, name_label, spark, price_label, badge)
        self._bind_price_row_drag(drag_widgets, instrument["key"])

        surface_widgets = (icon, name_label, spark, price_label)
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
        for instrument in self.watchlist:
            self.create_price_row(instrument, self.price_rows_frame)

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
        # Keep the existing quick window-drag gesture available. Row motion
        # starts intercepting events only while this press remains a click or
        # after the one-second hold activates reordering.
        return None

    def _activate_price_row_drag(self, key):
        self._row_hold_after_id = None
        if self._row_press_key != key or not self._row_press_origin:
            return
        last = self._row_press_last or self._row_press_origin
        dx = last[0] - self._row_press_origin[0]
        dy = last[1] - self._row_press_origin[1]
        tolerance = self.ROW_DRAG_MOVE_TOLERANCE_PX
        if (dx * dx) + (dy * dy) > tolerance * tolerance:
            self._cancel_price_row_drag()
            return

        self._row_drag_key = key
        self._row_drag_original_order = [item["key"] for item in self.watchlist]
        self._row_drag_order = list(self._row_drag_original_order)
        row = self.price_row_widgets.get(key)
        if row:
            row.config(highlightbackground=self.color_accent)
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
                    self._cancel_price_row_drag()
                    return None
            return "break"

        self.root.update_idletasks()
        midpoints = {}
        for item_key in self._row_drag_order or []:
            if item_key == key:
                continue
            row = self.price_row_widgets.get(item_key)
            if row and row.winfo_exists():
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
            return None

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
        for row in rows:
            row.pack(fill="x", pady=SPACING["xxs"] // 2)

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
        return f"{sign}{change_pct:.1f}%"

    def _get_change_pct(self, key, current_value):
        try:
            first, last = self.history_db.get_first_last(
                key,
                days=7,
                source_symbol=self._history_source_symbol(key)
            )
        except Exception:
            return None
        first_value = None
        if first:
            try:
                first_value = float(first[0])
            except (TypeError, ValueError):
                first_value = None
        if not first_value:
            return None
        try:
            current_value = float(current_value)
        except (TypeError, ValueError):
            if last:
                try:
                    current_value = float(last[0])
                except (TypeError, ValueError):
                    return None
            else:
                return None
        if first_value == 0:
            return None
        return ((current_value - first_value) / first_value) * 100

    def _get_sparkline_values(self, key, current_value):
        values = []
        try:
            data = self.history_db.get_history(
                key,
                days=7,
                source_symbol=self._history_source_symbol(key)
            )
        except Exception:
            data = []
        if isinstance(data, (list, tuple)):
            for row in data[-28:]:
                try:
                    values.append(float(row[1]))
                except (TypeError, ValueError, IndexError):
                    pass
        try:
            current_value = float(current_value)
        except (TypeError, ValueError):
            current_value = None
        if current_value and (not values or values[-1] != current_value):
            values.append(current_value)
        return [v for v in values if v > 0]

    def _draw_sparkline(self, canvas, values, color):
        if not canvas:
            return
        try:
            canvas.delete("all")
            width = canvas.winfo_width() or 62
            height = canvas.winfo_height() or 22
            if len(values) < 2:
                y = height // 2
                canvas.create_line(0, y, width, y, fill=self.color_border, width=1)
                return
            min_v = min(values)
            max_v = max(values)
            value_range = max_v - min_v if max_v != min_v else 1
            points = []
            for i, value in enumerate(values):
                x = int((width - 2) * i / max(len(values) - 1, 1)) + 1
                y = int((height - 4) * (1 - (value - min_v) / value_range)) + 2
                points.append((x, y))
            flat = [coord for point in points for coord in point]
            canvas.create_line(flat, fill=color, width=1.5, smooth=True, splinesteps=12)
            last_x, last_y = points[-1]
            canvas.create_oval(last_x - 1.75, last_y - 1.75, last_x + 1.75, last_y + 1.75, fill=color, outline="")
        except Exception:
            pass

    def _update_price_row_visuals(self, prices):
        if not hasattr(self, "price_change_vars"):
            return
        for instrument in self.watchlist:
            key = instrument["key"]
            value = prices.get(key)
            change_pct = self._get_change_pct(key, value)
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
            values = self._get_sparkline_values(key, value)
            self._draw_sparkline(self.price_spark_canvases.get(key), values, instrument.get("color", self.color_accent))

    def _format_tl(self, value, decimals=0):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "₺0"
        return f"₺{value:,.{decimals}f}"

    def rebuild_portfolio_breakdown(self, summary):
        if not hasattr(self, "portfolio_breakdown_frame"):
            return
        for child in self.portfolio_breakdown_frame.winfo_children():
            child.destroy()

        rows = summary.get("rows", []) if summary else []
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
                pady=5,
                highlightthickness=1,
                highlightbackground=self.color_border,
            )
            row.pack(fill="x", pady=SPACING["xxs"] // 2)
            row.grid_columnconfigure(2, weight=1)

            marker = tk.Canvas(
                row,
                width=10,
                height=10,
                bg=self.color_card_alt,
                highlightthickness=0,
                bd=0,
            )
            marker.grid(row=0, column=0, sticky="w", padx=(0, 6))
            marker.create_oval(
                2,
                2,
                8,
                8,
                fill=row_data.get("color", self.color_accent),
                outline="",
            )

            qty = f"{row_data['quantity']:,.2f}".rstrip("0").rstrip(".")
            left = f"{row_data['label']}: {qty} {row_data['unit']}"
            tk.Label(row, text=left, bg=self.color_card_alt, fg=self.color_text_dim, font=self.font_small, anchor="w").grid(row=0, column=1, sticky="w")

            if row_data["has_price"]:
                value_text = self._format_tl(row_data["value_tl"], 0)
            else:
                value_text = "Fiyat yok"
            tk.Label(row, text=value_text, bg=self.color_card_alt, fg=self.color_text_main, font=self.font_small, anchor="e").grid(row=0, column=2, sticky="e", padx=(SPACING["xs"], SPACING["xs"]))

            profit = row_data.get("profit_tl")
            if profit is None:
                profit_text = "--"
                profit_color = self.color_text_muted
            else:
                sign = "+" if profit >= 0 else ""
                profit_text = f"{sign}{self._format_tl(abs(profit), 0)}" if profit >= 0 else f"-{self._format_tl(abs(profit), 0)}"
                profit_color = self.color_success if profit >= 0 else self.color_danger
            tk.Label(row, text=profit_text, bg=self.color_card_alt, fg=profit_color, font=self.font_badge, anchor="e").grid(row=0, column=3, sticky="e")

    # --- Sayfa Navigasyon Sistemi ---
    def show_page(self, index):
        for p in self.pages:
            p.pack_forget()
        self.pages[index].pack(fill="both", expand=True)
        self.current_page = index
        self._update_arrow_colors()
        # Sayfa değiştiğinde içeriği güncelle
        if index == 1:
            self._update_chart()
        elif index == 2:
            self._update_stats()
    
    def next_page(self):
        if self.current_page < len(self.pages) - 1:
            self.show_page(self.current_page + 1)
    
    def prev_page(self):
        if self.current_page > 0:
            self.show_page(self.current_page - 1)
    
    def _update_arrow_colors(self):
        self.btn_prev.config(fg=self.color_text_dim if self.current_page > 0 else self.color_border)
        self.btn_next.config(fg=self.color_text_dim if self.current_page < len(self.pages) - 1 else self.color_border)

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
        self.chart_period = tk.IntVar(value=7)

        selector_header = tk.Frame(selector_frame, bg=self.color_card)
        selector_header.pack(fill="x", pady=(0, SPACING["xs"]))
        tk.Label(
            selector_header,
            text="FİYAT GEÇMİŞİ",
            bg=self.color_card,
            fg=self.color_text_dim,
            font=self.font_header,
        ).pack(side="left")

        self.chart_period_control = SegmentedControl(
            selector_header,
            self.chart_period,
            (("7G", 7), ("30G", 30), ("Tümü", 0)),
            command=self._update_chart,
        )
        self.chart_period_control.pack(side="right")

        self.chart_symbol_frame = tk.Frame(selector_frame, bg=self.color_card)
        self.chart_symbol_frame.pack(fill="x")
        self._rebuild_chart_symbol_buttons()

        self.chart_canvas = tk.Canvas(
            self.page_chart,
            bg=self.color_card,
            highlightthickness=1,
            highlightbackground=self.color_border,
            bd=0,
        )
        self.chart_canvas.pack(fill="both", expand=True)

    def _instrument_by_key(self, key):
        for instrument in self.watchlist:
            if instrument["key"] == key:
                return instrument
        return self.watchlist[0] if self.watchlist else None

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
            command=self._update_chart,
            selection_colors=selection_colors,
        )
        self.chart_symbol_control.pack(fill="x")

    @staticmethod
    def _filter_outliers(values, timestamps):
        """IQR tabanlı outlier filtreleme. Bozuk veri noktalarını temizler."""
        if len(values) < 4:
            return values, timestamps
        
        # None ve sıfır değerleri filtrele
        clean_vals = []
        clean_ts = []
        for v, t in zip(values, timestamps):
            if v is not None and v > 0:
                clean_vals.append(v)
                clean_ts.append(t)
        
        if len(clean_vals) < 4:
            return clean_vals, clean_ts
        
        # IQR hesapla
        sorted_v = sorted(clean_vals)
        n = len(sorted_v)
        q1 = sorted_v[n // 4]
        q3 = sorted_v[(3 * n) // 4]
        iqr = q3 - q1
        
        # IQR 0 ise medyan etrafında %20 tolerans kullan
        if iqr == 0:
            median = sorted_v[n // 2]
            lower = median * 0.8
            upper = median * 1.2
        else:
            lower = q1 - 2.0 * iqr
            upper = q3 + 2.0 * iqr
        
        filtered_vals = []
        filtered_ts = []
        for v, t in zip(clean_vals, clean_ts):
            if lower <= v <= upper:
                filtered_vals.append(v)
                filtered_ts.append(t)
        
        # Filtreleme sonrası veri kalmadıysa orijinali döndür
        if not filtered_vals:
            return clean_vals, clean_ts
        
        return filtered_vals, filtered_ts

    def _update_chart(self):
        try:
            self.chart_canvas.delete("all")
            
            cw = self.chart_canvas.winfo_width()
            ch = self.chart_canvas.winfo_height()
            if cw < 10 or ch < 10:
                cw, ch = 230, 170
            
            # Dinamik font boyutu hesaplama
            scale = max(1.0, min(1.5, cw / 230.0))
            f_title = int(8 * scale)
            f_tick = int(6 * scale)
            f_val = int(7 * scale)
            f_no_data = int(10 * scale)
            
            days = self.chart_period.get()
            selected = self.chart_var.get()
            source_symbol = self._history_source_symbol(selected)
            if days == 0:
                data = self.history_db.get_all_history(
                    selected, source_symbol=source_symbol
                )
            else:
                data = self.history_db.get_history(
                    selected, days=days, source_symbol=source_symbol
                )
            
            if not data:
                self.chart_canvas.create_text(cw//2, ch//2, text="Henüz grafik verisi yok", fill=self.color_text_muted, font=(self.font_family, f_no_data))
                return
            
            instrument = self._instrument_by_key(selected)
            if not instrument:
                self.chart_canvas.create_text(cw//2, ch//2, text="Henüz grafik verisi yok", fill=self.color_text_muted, font=(self.font_family, f_no_data))
                return
            title = instrument["label"]
            color = instrument.get("color", self.color_accent)
            
            raw_values = [row[1] for row in data]
            raw_timestamps = [row[0] for row in data]
            
            # Outlier filtreleme (bozuk/saçma verileri temizle)
            values, timestamps = self._filter_outliers(raw_values, raw_timestamps)
            
            if not values:
                self.chart_canvas.create_text(cw//2, ch//2, text="Henüz grafik verisi yok", fill=self.color_text_muted, font=(self.font_family, f_no_data))
                return
            
            # Grafik alanı (padding) — sol taraf daha geniş, okunabilirlik için
            pad_l, pad_r, pad_t, pad_b = 48, 14, 34, 28
            gw = cw - pad_l - pad_r
            gh = ch - pad_t - pad_b
            
            min_v = min(values)
            max_v = max(values)
            
            # Y ekseninde %5 marj bırak (grafik taşmasın)
            margin = (max_v - min_v) * 0.05 if max_v != min_v else max_v * 0.02
            chart_min = min_v - margin
            chart_max = max_v + margin
            val_range = chart_max - chart_min if chart_max != chart_min else 1
            
            self.chart_canvas.create_text(
                pad_l,
                15,
                text=title,
                fill=self.color_text_main,
                font=(self.font_family, f_title, "bold"),
                anchor="w",
            )
            
            # Grid çizgileri ve Y ekseni etiketleri
            for i in range(5):
                y = pad_t + int(gh * i / 4)
                self.chart_canvas.create_line(pad_l, y, cw - pad_r, y, fill=self.color_border, dash=(2, 5))
                v = chart_max - (val_range * i / 4)
                if v >= 10000:
                    fmt = f"{v:,.0f}"
                elif v >= 100:
                    fmt = f"{v:.1f}"
                else:
                    fmt = f"{v:.2f}"
                self.chart_canvas.create_text(pad_l - 6, y, text=fmt, fill=self.color_text_muted, font=(self.font_family, f_tick), anchor="e")
            
            # Veri noktalarını canvas koordinatlarına çevir
            n = len(values)
            points = []
            for i, v in enumerate(values):
                x = pad_l + int(gw * i / max(n - 1, 1))
                y = pad_t + int(gh * (1 - (v - chart_min) / val_range))
                points.append((x, y))
            
            # A light stippled area preserves depth without adding animation.
            if len(points) >= 2:
                fill_points = list(points) + [(points[-1][0], pad_t + gh), (points[0][0], pad_t + gh)]
                flat = [coord for p in fill_points for coord in p]
                self.chart_canvas.create_polygon(flat, fill=color, stipple="gray12", outline="")
            
            # Çizgi
            if len(points) >= 2:
                flat_line = [coord for p in points for coord in p]
                self.chart_canvas.create_line(flat_line, fill=color, width=2, smooth=True, splinesteps=12)
            
            # X ekseni etiketleri
            label_count = min(4, n)
            for i in range(label_count):
                idx = int(i * (n - 1) / max(label_count - 1, 1))
                x = points[idx][0]
                ts = timestamps[idx]
                try:
                    dt = datetime.fromisoformat(ts)
                    if n > 1:
                        span = (datetime.fromisoformat(timestamps[-1]) - datetime.fromisoformat(timestamps[0])).days
                        label = dt.strftime("%H:%M") if span <= 2 else dt.strftime("%d/%m")
                    else:
                        label = dt.strftime("%d/%m")
                except:
                    label = str(ts)[:5]
                self.chart_canvas.create_text(x, ch - 10, text=label, fill=self.color_text_muted, font=(self.font_family, f_tick))
            
            # Son değer etiketi
            last_x, last_y = points[-1]
            last_v = values[-1]
            fmt_v = format_instrument_value(instrument, last_v)
            self.chart_canvas.create_oval(last_x-3, last_y-3, last_x+3, last_y+3, fill=color, outline=self.color_card, width=1)
            # Etiketi grafik sınırları içinde tut
            label_y = max(last_y - 12, pad_t + 5)
            self.chart_canvas.create_text(last_x, label_y, text=fmt_v, fill=color, font=(self.font_family, f_val, "bold"))
            
        except Exception as e:
            log_message(f"Chart error: {e}")

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
            days_param = days if days > 0 else None

            for instrument in self.watchlist:
                key = instrument["key"]
                labels = self.stats_labels.get(key)
                if not labels:
                    continue

                source_symbol = self._history_source_symbol(key)
                stats = self.history_db.get_stats(
                    key, days=days_param, source_symbol=source_symbol
                )
                first_last = self.history_db.get_first_last(
                    key, days=days_param, source_symbol=source_symbol
                )

                if not stats or stats[3] == 0:
                    labels["min"].config(text="Min: --")
                    labels["max"].config(text="Max: --")
                    labels["avg"].config(text="Ort: --")
                    labels["chg"].config(text="Değişim: --", fg=self.color_text_muted)
                    continue

                mn, mx, avg = stats[0], stats[1], stats[2]
                labels["min"].config(text=f"Min: {format_instrument_value(instrument, mn)}")
                labels["max"].config(text=f"Max: {format_instrument_value(instrument, mx)}")
                labels["avg"].config(text=f"Ort: {format_instrument_value(instrument, avg)}")

                # Değişim %
                first, last = first_last
                if first and last:
                    first_value = first[0]
                    last_value = last[0]
                    if first_value and first_value != 0:
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
        self.root.attributes("-topmost", self.var_topmost.get())
        
    def check_updates(self):
        has_update, new_version, url = self.um.check_for_updates()
        if has_update:
            if messagebox.askyesno("Güncelleme Mevcut", f"Yeni sürüm bulundu: v{new_version}\nŞimdi indirilip kurulsun mu?"):
                self.var_time.set("Güncelleme indiriliyor...")
                self.root.update()
                if self.um.update_application(url):
                    self.root.destroy()
                    sys.exit()
        else:
            messagebox.showinfo("Güncelleme", "Uygulama güncel!")

    def is_market_closed(self):
        """
        Piyasa Kapalı mı kontrolü (Türkiye saati varsayımıyla):
        Kapanış: Cumartesi 01:00
        Açılış: Pazartesi 02:00
        """
        now = datetime.now()
        weekday = now.weekday() # 0: Pzt, 6: Paz
        hour = now.hour
        
        # Cumartesi (5)
        if weekday == 5:
            return hour >= 1
        # Pazar (6)
        if weekday == 6:
            return True
        # Pazartesi (0)
        if weekday == 0:
            return hour < 2
            
        return False

    def save_last_data(self, data):
        try:
            normalized = normalize_market_data(data)
            if not normalized:
                return
            filepath = getattr(self, "market_data_path", app_path("market_data.json"))
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(
                    market_data_for_save(
                        normalized["prices"],
                        normalized.get("timestamp"),
                        normalized.get("sources")
                    ),
                    f,
                    indent=4,
                    ensure_ascii=False
                )
        except Exception as e:
            log_message(f"Veri kaydetme hatası: {e}")

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
                    "bid",
                ):
                    try:
                        val = fast_info.get(key) if hasattr(fast_info, "get") else getattr(fast_info, key, None)
                        if val and val > 0:
                            return float(val)
                    except:
                        pass
        except:
            pass

        try:
            info = ticker.info
            val = info.get("regularMarketPrice") or info.get("bid") or 0
            return float(val) if val and val > 0 else 0
        except:
            return 0

    def _required_yahoo_symbols(self):
        symbols = set()
        for instrument in self.watchlist:
            source = instrument.get("source")
            if source not in SILVER_SPOT_SOURCES:
                symbols.add(instrument["symbol"])
            if source in ("metal_try", "silver_spot_try") or instrument.get("currency") == "$":
                symbols.add("TRY=X")
        return sorted(symbols)

    @staticmethod
    def _fetch_spot_silver_price():
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
            return price if 1 < price < 1000 else 0
        except Exception as e:
            log_message(f"Spot gümüş verisi alınamadı: {e}")
            return 0

    def _watchlist_price_sources(self):
        return {
            instrument["key"]: instrument["symbol"]
            for instrument in self.watchlist
        }

    def _compatible_cached_data(self, data):
        data = normalize_market_data(data)
        if not data:
            return None
        prices = dict(data.get("prices", {}))
        sources = dict(data.get("sources", {}))
        for instrument in self.watchlist:
            key = instrument["key"]
            if instrument.get("source") not in SILVER_SPOT_SOURCES:
                continue
            if sources.get(key) != SILVER_SPOT_SYMBOL:
                prices.pop(key, None)
                sources.pop(key, None)
        return market_data_for_save(
            prices,
            timestamp=data.get("timestamp"),
            sources=sources
        )

    def _fetch_raw_prices(self, symbols):
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
            price = self._extract_price(ticker)
            if price and price > 0:
                prices[symbol] = price
        return prices

    def validate_yahoo_symbol(self, symbol):
        symbol = symbol.strip().upper()
        prices = self._fetch_raw_prices([symbol])
        return prices.get(symbol, 0)

    def _resolve_watchlist_prices(self, raw_prices, last_prices=None):
        last_prices = last_prices or {}
        prices = {}
        fresh_keys = set()
        dolar = raw_prices.get("TRY=X") or last_prices.get("dolar")

        for instrument in self.watchlist:
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
            # Piyasa kontrolü
            if self.is_market_closed():
                def set_closed_ui():
                    self.var_market_status.set("•")
                    self.lbl_market_status.config(fg=self.color_danger) # Kırmızı nokta
                    if hasattr(self, "var_market_label"):
                        self.var_market_label.set("Piyasa Kapalı")
                    
                    # Kayıtlı son veriyi yükle
                    last_data = self._compatible_cached_data(self.load_last_data())
                    if last_data:
                        self.guncelle_arayuz(last_data)
                        # Dolar kurunu da güncelle, portföy hesaplamaları için gerekebilir
                        self.last_dolar_rate = last_data.get("dolar", 36.0)
                        
                        last_time = last_data.get("timestamp", "")
                        self.var_time.set(f"Son: {last_time}" if last_time else "Kapalı")
                    else:
                        self.var_time.set(time.strftime("%H:%M"))
                
                self.root.after(0, set_closed_ui)
                return # API isteği atma

            def set_open_ui():
                self.var_market_status.set("•")
                self.lbl_market_status.config(fg=self.color_success) # Yeşil nokta
                if hasattr(self, "var_market_label"):
                    self.var_market_label.set("Piyasa Açık")
            
            self.root.after(0, set_open_ui)

            last_data = self._compatible_cached_data(self.load_last_data())
            last_prices = last_data.get("prices", {}) if last_data else {}

            raw_prices = {}
            if any(
                item.get("source") in SILVER_SPOT_SOURCES
                for item in self.watchlist
            ):
                spot_silver = self._fetch_spot_silver_price()
                if spot_silver:
                    raw_prices[SILVER_SPOT_SYMBOL] = spot_silver
            try:
                raw_prices.update(
                    self._fetch_raw_prices(self._required_yahoo_symbols())
                )
            except Exception as e:
                log_message(f"Yahoo Finance verisi alınamadı: {e}")
            if raw_prices.get("TRY=X"):
                self.last_dolar_rate = raw_prices["TRY=X"]
            prices, fresh_keys = self._resolve_watchlist_prices(raw_prices, last_prices)

            if fresh_keys:
                market_data = market_data_for_save(
                    prices,
                    sources=self._watchlist_price_sources()
                )
                self.save_last_data(market_data)
                fresh_prices = {key: prices[key] for key in fresh_keys}
                self.history_db.insert_prices(fresh_prices, self.watchlist)
                
                # UI Güncelleme (Main Thread'e güvenli geçiş için)
                self.root.after(0, lambda data=market_data: self.guncelle_arayuz(data))
            elif last_data:
                def set_cached_ui():
                    self.guncelle_arayuz(last_data)
                    last_time = last_data.get("timestamp", "")
                    if hasattr(self, "var_market_label"):
                        self.var_market_label.set("Bağlantı Hatası")
                    self.lbl_market_status.config(fg=self.color_danger)
                    self.var_time.set(f"Son: {last_time}" if last_time else "Hata")
                self.root.after(0, set_cached_ui)
            else:
                def set_error_ui():
                    if hasattr(self, "var_market_label"):
                        self.var_market_label.set("Bağlantı Hatası")
                    self.lbl_market_status.config(fg=self.color_danger)
                    self.var_time.set("Hata")
                self.root.after(0, set_error_ui)
            
        except Exception as e:
            last_data = self._compatible_cached_data(self.load_last_data())
            if last_data:
                def set_error_cached_ui():
                    self.guncelle_arayuz(last_data)
                    if hasattr(self, "var_market_label"):
                        self.var_market_label.set("Bağlantı Hatası")
                    self.lbl_market_status.config(fg=self.color_danger)
                    last_time = last_data.get("timestamp", "")
                    self.var_time.set(f"Son: {last_time}" if last_time else "Hata")
                self.root.after(0, set_error_cached_ui)
            else:
                def set_error_ui():
                    if hasattr(self, "var_market_label"):
                        self.var_market_label.set("Bağlantı Hatası")
                    self.lbl_market_status.config(fg=self.color_danger)
                    self.var_time.set("Hata")
                self.root.after(0, set_error_ui)
        finally:
            self._fetch_lock.release()

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

        prices = data.get("prices", {})
        for instrument in self.watchlist:
            var = getattr(self, "price_vars", {}).get(instrument["key"])
            if var:
                var.set(format_instrument_value(instrument, prices.get(instrument["key"])))

        self._update_price_row_visuals(prices)

        if prices.get("dolar"):
            self.last_dolar_rate = prices["dolar"]

        current_time = time.strftime("%H:%M")
        self.var_time.set(current_time)

        # Portföy hesapla
        summary = self.tm.get_portfolio_summary(prices, self.watchlist)
        self.rebuild_portfolio_breakdown(summary)
        current_val = summary.get("total_value_tl", 0)
        total_cost = summary.get("total_cost_tl", 0)
        profit_tl = summary.get("profit_tl", 0)
        profit_pct = summary.get("profit_pct", 0)
        if summary.get("rows"):
            
            self.var_portfolio.set(f"₺{current_val:,.0f}")
            
            sign = "+" if profit_tl >= 0 else ""
            self.var_profit.set(f"{sign}%{profit_pct:.1f} ({sign}₺{profit_tl:,.0f})")
            
            color = self.color_success if profit_tl >= 0 else self.color_danger
            bg = self.color_success_bg if profit_tl >= 0 else self.color_danger_bg
            self.lbl_profit.config(fg=color, bg=bg)
        else:
             self.var_portfolio.set("₺0")
             self.var_profit.set("%0.0 (₺0)")
             self.lbl_profit.config(fg=self.color_text_dim, bg=self.color_card_alt)

    def open_add_transaction(self):
        # Güncel dolar kurunu bul
        current_dollar = 0
        try:
             # var_gumus_tl'den veya hesaplamadan bulabilirdik ama temiz olsun diye yeniden çekebiliriz
             # veya veri_getir içindeki 'dolar' değişkenini class attribute yapalım.
             # Hızlı çözüm: self.last_dolar_rate ekleyelim.
             pass
        except:
             pass
             
        PortfolioManagerDialog(self.root, self.tm, self.request_data_refresh, getattr(self, 'last_dolar_rate', 36.0), self.watchlist)

    def import_transactions(self):
        filename = filedialog.askopenfilename(title="İçe Aktarılacak Dosyayı Seç", filetypes=[("JSON Files", "*.json")])
        if filename:
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    new_data = json.load(f)
                
                if isinstance(new_data, list):
                    # Basit doğrulama: İlk öğe beklenen anahtarlara sahip mi?
                    if new_data and ("amount_g" in new_data[0] or "quantity" in new_data[0] or "total_tl" in new_data[0]):
                        count = 0
                        for item in new_data:
                             self.tm.save(item) # Tek tek eklersek sürekli save çağırır, transaction manager'a bulk add eklemek daha iyi ama bu da çalışır.
                             # Daha iyisi: self.tm.transactions.extend(new_data); self.tm.save_all()
                             count += 1
                        
                        # Hepsini tek seferde kaydetmek daha performanslı olurdu ama tm.save tek tek ekleyip save ediyor.
                        # Şimdilik sorun değil.
                        
                        messagebox.showinfo("Başarılı", f"{len(new_data)} adet işlem başarıyla içeri aktarıldı.")
                    else:
                        messagebox.showwarning("Uyarı", "Dosya formatı uyumsuz görünüyor veya boş.")
                else:
                    messagebox.showerror("Hata", "JSON formatı geçersiz (Liste olmalı).")
            except Exception as e:
                messagebox.showerror("Hata", f"İçe aktarma hatası: {e}")
            finally:
                # Arayüzü güncelle
                self.request_data_refresh()

    def open_watchlist_settings(self):
        WatchlistDialog(self.root, self.watchlist_manager, self.on_watchlist_changed, self.validate_yahoo_symbol)

    def on_watchlist_changed(self):
        self.watchlist = self.watchlist_manager.instruments
        self.rebuild_price_rows()
        self._rebuild_chart_symbol_buttons()
        self._rebuild_stats_sections()

        last_data = self._compatible_cached_data(self.load_last_data())
        if last_data:
            self.guncelle_arayuz(last_data)
        self.request_data_refresh()

    def open_settings(self, event=None):
        # Menüyü fare konumunda aç
        x = self.root.winfo_pointerx()
        y = self.root.winfo_pointery()
        self.settings_menu.tk_popup(x, y)
        self.settings_menu.grab_release()
        
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
        while not self._stop_event.is_set():
            self.veri_getir()
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
        # Son boyutlara göre fontları ve grafiği güncelle
        self._apply_font_scale()
        if hasattr(self, 'current_page') and self.current_page == 1:
            self.root.after(50, self._update_chart)
    
    def _on_button_release(self, event):
        """Genel buton bırakma - resize flag'ini temizle."""
        self._is_resizing = False

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
        self.font_nav_arrow.config(size=int(self.base_fonts["nav_arrow"] * scale))
        self.font_icon.config(size=int(self.base_fonts["icon"] * scale))
        self.font_small.config(size=int(self.base_fonts["small"] * scale))
        self.font_badge.config(size=int(self.base_fonts["badge"] * scale))
        
        # Grafik sayfasındaysa güncel boyutlara göre yeniden çiz
        if hasattr(self, 'current_page') and self.current_page == 1:
            self.root.after(50, self._update_chart)

    # --- Sürükleme Mantığı ---
    def start_move(self, event):
        # Resize grip üzerindeyse sürüklemeyi başlatma
        if event.widget == self.grip:
            return
        self.x = event.x
        self.y = event.y

    def do_move(self, event):
        # Resize sırasında pencereyi sürükleme
        if self._is_resizing or self._row_drag_key is not None:
            return
        deltax = event.x - self.x
        deltay = event.y - self.y
        x = self.root.winfo_x() + deltax
        y = self.root.winfo_y() + deltay
        self.root.geometry(f"+{x}+{y}")

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
