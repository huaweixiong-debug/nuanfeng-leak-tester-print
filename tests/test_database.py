#!/usr/bin/env python3
"""Slice D: additive migration, idempotent records, atomic daily serial."""

import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_db_")
os.environ["ATEQ_TEST_DB"] = str(Path(_TMP) / "test.db")

import database  # noqa: E402


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        db_path = Path(os.environ["ATEQ_TEST_DB"])
        if db_path.exists():
            db_path.unlink()
        database._INITIALIZED = False
        database.init_database()

    def test_legacy_schema_migration_preserves_rows(self):
        db_path = Path(os.environ["ATEQ_TEST_DB"])
        db_path.unlink()
        conn = sqlite3.connect(db_path)
        conn.execute('''
            CREATE TABLE test_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                daily_serial INTEGER NOT NULL,
                qr_code TEXT,
                pressure1 REAL,
                leak1 REAL,
                pressure2 REAL,
                leak2 REAL,
                test_result TEXT NOT NULL,
                product_model TEXT,
                operator TEXT,
                serial_number TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        from datetime import datetime
        local_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO test_records (test_time, daily_serial, test_result, "
                     "product_model) VALUES (?, 1, 'PASS', 'LEGACY')", (local_now,))
        conn.execute("INSERT INTO test_records (test_time, daily_serial, test_result, "
                     "product_model) VALUES (?, 2, 'FAIL', 'LEGACY')", (local_now,))
        conn.commit()
        conn.close()

        database._INITIALIZED = False
        database.init_database()
        records = database.get_records_by_date(_today())
        self.assertEqual(len(records), 2)
        self.assertEqual({r["product_model"] for r in records}, {"LEGACY"})
        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(test_records)").fetchall()}
            indexes = {row[1] for row in conn.execute(
                "PRAGMA index_list(test_records)").fetchall()}
        finally:
            conn.close()
        for column in ("cycle_id", "program1_actual", "print_state",
                       "terminal_reason", "started_at"):
            self.assertIn(column, columns)
        self.assertIn("idx_test_records_cycle_id", indexes)

    def test_legacy_positional_signature_returns_int(self):
        record_id = database.save_test_record(
            "QR-1", 1.5, 0.2, None, None, "PASS", "M1", "op")
        self.assertIsInstance(record_id, int)
        record = database.get_record(record_id)
        self.assertEqual(record["test_result"], "PASS")
        self.assertIsNone(record["cycle_id"])

    def test_duplicate_cycle_is_idempotent(self):
        first_id, created = database.save_record_ex(
            qr_code="Q", pressure1=1.0, leak1=0.1, pressure2=None, leak2=None,
            test_result="PASS", product_model="M", operator="op",
            serial_number="SN-1", cycle_id="cycle-1")
        second_id, created_again = database.save_record_ex(
            qr_code="Q", pressure1=1.0, leak1=0.1, pressure2=None, leak2=None,
            test_result="PASS", product_model="M", operator="op",
            serial_number="SN-2", cycle_id="cycle-1")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first_id, second_id)
        rows = database.get_records_by_date(_today())
        self.assertEqual(len([r for r in rows if r["cycle_id"] == "cycle-1"]), 1)
        self.assertEqual(database.get_record(first_id)["serial_number"], "SN-1")

    def test_concurrent_daily_serial_allocation_is_unique(self):
        ids = []
        lock = threading.Lock()

        def worker(index):
            record_id, _ = database.save_record_ex(
                qr_code=None, pressure1=1.0, leak1=0.1, pressure2=None, leak2=None,
                test_result="PASS", product_model="M", operator=None,
                cycle_id=f"concurrent-{index}")
            with lock:
                ids.append(record_id)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = database.get_records_by_date(_today())
        serials = sorted(r["daily_serial"] for r in rows)
        self.assertEqual(serials, list(range(1, 21)))
        self.assertEqual(len(ids), 20)

    def test_update_print_status_persists(self):
        record_id, _ = database.save_record_ex(
            qr_code=None, pressure1=1.0, leak1=0.1, pressure2=None, leak2=None,
            test_result="PASS", product_model="M", operator=None,
            serial_number="SN-P", cycle_id="cycle-print")
        database.update_print_status(record_id, "submitted", "ok")
        record = database.get_record(record_id)
        self.assertEqual(record["print_state"], "submitted")
        self.assertEqual(record["print_message"], "ok")
        self.assertIsNotNone(record["print_attempted_at"])

    def test_unique_index_rejects_duplicate_cycle_directly(self):
        database.save_test_record(
            None, 1.0, 0.1, None, None, "PASS", "M", None,
            cycle_id="dup-cycle")
        conn = sqlite3.connect(os.environ["ATEQ_TEST_DB"])
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO test_records (daily_serial, test_result, cycle_id) "
                    "VALUES (999, 'PASS', 'dup-cycle')")
                conn.commit()
        finally:
            conn.close()


def _today():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


if __name__ == "__main__":
    unittest.main()
