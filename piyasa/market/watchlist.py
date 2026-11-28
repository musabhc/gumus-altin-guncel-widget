"""Watchlist validation and persistence."""

import json
import os
import tempfile

from piyasa.market.instruments import (
    default_watchlist,
    make_watchlist_key,
    migrate_legacy_silver_instrument,
    normalize_instrument,
    reorder_instruments,
)
from piyasa.paths import app_path, log_message


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
