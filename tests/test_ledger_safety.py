"""Regression tests for trade chronology and transaction file protection."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from piyasa.portfolio.transactions import TransactionManager


class TestLedgerSafety(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "transactions.json"

    @staticmethod
    def trade(date="01-01-2025", *, action="buy", amount=2, gross=200, fee=0):
        return {"date": date, "action": action, "quantity": amount, "total_tl": gross,
                "fee_tl": fee, "instrument_key": "thyao"}

    def test_invalid_file_is_protected_and_recoverable(self):
        broken = b'{"damaged":true}'
        self.path.write_bytes(broken)
        ledger = TransactionManager(self.path)
        self.assertTrue(ledger.load_error)
        with self.assertRaises(OSError):
            ledger.save(self.trade())
        self.assertEqual(self.path.read_bytes(), broken)

        good = TransactionManager(self.path.with_name("good.json"))
        good.save(self.trade())
        recovery = ledger.restore_backup(good.filename)
        self.assertEqual(recovery.read_bytes(), broken)
        self.assertIsNone(ledger.load_error)
        self.assertEqual(TransactionManager(self.path).get_average_cost("thyao"), 100)

    def test_stale_application_snapshot_cannot_overwrite_newer_transactions(self):
        first = TransactionManager(self.path)
        stale = TransactionManager(self.path)
        first.save(self.trade())
        before = self.path.read_bytes()
        with self.assertRaisesRegex(OSError, "başka"):
            stale.save(self.trade("02-01-2025"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_import_compares_exact_duplicates_including_multiplicity(self):
        ledger = TransactionManager(self.path)
        record = self.trade()
        ledger.save(record)
        rows, duplicates = ledger.preview_import([record, record])
        self.assertEqual(rows, [record])
        self.assertEqual(duplicates, 1)
        ledger.save_many(rows)
        self.assertEqual(ledger.preview_import([record, record]), ([], 2))

    def test_backdated_trade_previews_and_commission_reaches_closed_profit(self):
        ledger = TransactionManager(self.path)
        ledger.save(self.trade("03-01-2025", fee=10))
        earlier = self.trade("02-01-2025", gross=100)
        preview = ledger.preview_trade(earlier)
        self.assertTrue(preview["historical"])
        self.assertEqual(preview["average_cost_tl"], 77.5)
        self.assertEqual(ledger.get_average_cost("thyao"), 105)
        ledger.save(earlier)
        self.assertEqual([row["date"] for row in ledger.transactions], ["02-01-2025", "03-01-2025"])
        self.assertEqual(ledger.available_to_sell("thyao", "02-01-2025"), 2)
        with self.assertRaises(ValueError):
            ledger.save(self.trade("01-01-2025", action="sell", amount=1))
        ledger.save(self.trade("04-01-2025", action="sell", amount=4, gross=400, fee=5))
        result = ledger.get_portfolio_summary({}, [])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["realized_profit_tl"], 85)


if __name__ == "__main__":
    unittest.main()
