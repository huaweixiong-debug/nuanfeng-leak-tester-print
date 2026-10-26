#!/usr/bin/env python3
"""Daily, per-product four-digit serial allocation regressions."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")

from serial_generator import SerialNumberGenerator


class SerialGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="ateq_serial_rule_")
        self.addCleanup(self.tempdir.cleanup)
        self.generator = SerialNumberGenerator(
            str(Path(self.tempdir.name) / "serial.db"))

    def at_date(self, date):
        return mock.patch.object(self.generator, "_get_current_date_str",
                                 return_value=date)

    def test_each_model_and_each_day_starts_at_0001(self):
        with self.at_date("20261026"):
            first = self.generator.get_next_serial_number("EV37")
            second = self.generator.get_next_serial_number("EV37")
            other = self.generator.get_next_serial_number("M2")
        self.assertEqual(first.serial_number, "20261026-EV37-0001")
        self.assertEqual(second.serial_number, "20261026-EV37-0002")
        self.assertEqual(other.serial_number, "20261026-M2-0001")

        with self.at_date("20261027"):
            next_day = self.generator.get_next_serial_number("EV37")
            other_next_day = self.generator.get_next_serial_number("M2")
        self.assertEqual(next_day.serial_number, "20261027-EV37-0001")
        self.assertEqual(other_next_day.serial_number, "20261027-M2-0001")

    def test_9999_is_last_valid_sequence_and_counter_rolls_back(self):
        with self.generator._get_connection() as conn:
            conn.execute(
                "INSERT INTO daily_counters (date_str, product_model, current_count) "
                "VALUES (?, ?, ?)", ("20261026", "EV37", 9998))
            conn.commit()
        with self.at_date("20261026"):
            last = self.generator.get_next_serial_number("EV37")
            self.assertEqual(last.serial_number, "20261026-EV37-9999")
            with self.assertRaisesRegex(RuntimeError, "9999"):
                self.generator.get_next_serial_number("EV37")
            # The failed UPDATE must roll back; repeated attempts fail cleanly,
            # rather than allocating 10000 or corrupting the durable counter.
            with self.assertRaisesRegex(RuntimeError, "9999"):
                self.generator.get_next_serial_number("EV37")
        with self.generator._get_connection() as conn:
            row = conn.execute(
                "SELECT current_count FROM daily_counters "
                "WHERE date_str = ? AND product_model = ?",
                ("20261026", "EV37")).fetchone()
        self.assertEqual(row["current_count"], 9999)


if __name__ == "__main__":
    unittest.main()
