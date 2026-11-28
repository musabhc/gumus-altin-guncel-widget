"""SQLite storage for observations and provider history."""

import os
import sqlite3
import threading
from datetime import datetime, timedelta

from piyasa.market.instruments import LEGACY_SILVER_FUTURES_SYMBOL, default_watchlist
from piyasa.market.quotes import positive_price
from piyasa.market.series import normalize_history_timestamp, parse_history_timestamp
from piyasa.paths import app_path, log_message


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

    def insert_prices(self, prices, instruments=None, timestamp=None, interval="live", *, changes_only=False):
        if not prices:
            return
        timestamp = normalize_history_timestamp(timestamp or datetime.now())
        instrument_map = {item["key"]: item for item in (instruments or [])}
        query = (
            "INSERT OR IGNORE INTO market_price_history "
            "(timestamp, instrument_key, label, source_symbol, price, interval) "
            "SELECT ?, ?, ?, ?, ?, ?"
        )
        if changes_only:
            query += (
                " WHERE ? != COALESCE(("
                "SELECT price FROM market_price_history WHERE instrument_key = ? "
                "AND source_symbol IS ? AND interval = ? AND timestamp <= ? "
                "ORDER BY timestamp DESC LIMIT 1), 0)"
            )
        inserted = 0
        with self._lock, self.conn:
            for key, value in prices.items():
                value = positive_price(value)
                if not value:
                    continue
                label = instrument_map.get(key, {}).get("label", key)
                source_symbol = instrument_map.get(key, {}).get("symbol")
                params = (timestamp, key, label, source_symbol, value, interval)
                if changes_only:
                    params += (value, key, source_symbol, interval, timestamp)
                cursor = self.conn.execute(query, params)
                inserted += max(cursor.rowcount, 0)
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
        anchor_to_latest_change=False,
    ):
        delta = timedelta(hours=hours) if hours is not None else timedelta(days=days)
        clauses = ["instrument_key = ?", "price > 0"]
        params = [instrument_key]
        self._append_source_filter(clauses, params, source_symbol)
        self._append_interval_filter(clauses, params, intervals)
        end_at = None
        query = "SELECT timestamp, price FROM market_price_history WHERE " + " AND ".join(clauses)
        with self._lock:
            if anchor_to_latest or anchor_to_latest_change:
                latest = self.conn.execute(query + " ORDER BY timestamp DESC LIMIT 1", params).fetchone()
                if latest:
                    end_stamp = latest[0]
                    if anchor_to_latest_change:
                        end_stamp = self._last_price_change(query, params, latest)
                    end_at = parse_history_timestamp(end_stamp)
            end_at = end_at or datetime.now()
            cutoff = normalize_history_timestamp(end_at - delta)
            return self.conn.execute(
                query + " AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                (*params, cutoff, normalize_history_timestamp(end_at)),
            ).fetchall()

    def _last_price_change(self, query, params, latest):
        # Anchor old poll data at the start of its final run of identical prices.
        stamp, price = latest
        previous = self.conn.execute(
            query + " AND timestamp < ? AND price <> ? ORDER BY timestamp DESC LIMIT 1",
            (*params, stamp, price),
        ).fetchone()
        if previous:
            query += " AND timestamp > ?"
            params = (*params, previous[0])
        first = self.conn.execute(query + " ORDER BY timestamp LIMIT 1", params).fetchone()
        return first[0]

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
