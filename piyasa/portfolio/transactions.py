"""Transaction ledger and portfolio valuation."""

import json
import math
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from piyasa.market.instruments import portfolio_unit, price_to_tl, safe_float
from piyasa.paths import app_path
from piyasa.portfolio.ledger_storage import (
    decode_records,
    encode_records,
    ledger_write_lock,
    read_snapshot,
    replace_snapshot,
)


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
