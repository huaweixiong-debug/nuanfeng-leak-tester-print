#!/usr/bin/env python3
"""Slice D: print lock/timeout/disabled states and saved-PASS reprint only."""

import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_print_")
os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")
os.environ["ATEQ_LINE_SETTINGS"] = str(Path(_TMP) / "line_settings.json")
os.environ["ATEQ_TEST_DB"] = str(Path(_TMP) / "test.db")
os.environ["ATEQ_SERIAL_DB"] = str(Path(_TMP) / "serial.db")
os.environ["ATEQ_RUNTIME_STATE"] = str(Path(_TMP) / "state.json")
os.environ["ATEQ_SCANNER_LOG"] = str(Path(_TMP) / "scanner.log")
os.environ["ATEQ_LABEL_DIR"] = str(Path(_TMP) / "data")
os.environ["ATEQ_LABEL_TEMPLATE_DIR"] = str(Path(_TMP) / "templates")
os.environ["ATEQ_DEFAULT_TEMPLATE"] = str(Path(_TMP) / "templates" / "label.btw")

from fastapi.testclient import TestClient  # noqa: E402

import bartender_print  # noqa: E402
import database  # noqa: E402
import line_runtime  # noqa: E402
import webui_server  # noqa: E402
from tests.fakes import FakePrintRunner  # noqa: E402


def prepare_template():
    template = Path(os.environ["ATEQ_DEFAULT_TEMPLATE"])
    template.parent.mkdir(parents=True, exist_ok=True)
    if not template.exists():
        template.write_text("fake btw", encoding="utf-8")


class PrintBackendTests(unittest.TestCase):
    def setUp(self):
        prepare_template()
        self.runner = FakePrintRunner()
        bartender_print.set_runner(self.runner)

    def tearDown(self):
        bartender_print.set_runner(None)

    def test_fixed_print_writes_files_and_submits(self):
        printer = bartender_print.BarTenderPrinter()
        result = printer.print_fixed_label_files_ex(
            product_model="M1", supplier_code="SUP", sequence_code="7")
        self.assertTrue(result["success"])
        self.assertTrue(result["printed"])
        self.assertEqual(result["state"], "submitted")
        data_dir = Path(os.environ["ATEQ_LABEL_DIR"])
        self.assertTrue((data_dir / "产品型号.txt").exists())
        self.assertEqual((data_dir / "序号.txt").read_text(encoding="gbk") if
                         os.environ.get("LABEL_DATA_ENCODING", "gbk") == "gbk" else
                         (data_dir / "序号.txt").read_text(encoding="utf-8"), "00007")
        self.assertEqual(len(self.runner.calls), 1)

    def test_timeout_is_reported_not_success(self):
        self.runner.timeout = True
        printer = bartender_print.BarTenderPrinter()
        result = printer.print_fixed_label_files_ex(
            product_model="M1", supplier_code="S", sequence_code="1")
        self.assertFalse(result["success"])
        self.assertFalse(result["printed"])
        self.assertEqual(result["state"], "timeout")

    def test_disabled_printer_is_never_attempted(self):
        line_runtime.update_line_settings(printer_enabled=False)
        result = line_runtime.print_label_if_enabled(
            template_name="", serial_number="SN-1", product_model="M1")
        self.assertEqual(result["state"], "disabled")
        self.assertFalse(result["printed"])
        self.assertTrue(result["disabled"])
        self.assertEqual(len(self.runner.calls), 0)

    def test_failed_runner_reports_failed(self):
        self.runner.returncode = 1
        line_runtime.update_line_settings(printer_enabled=True)
        result = line_runtime.print_label_if_enabled(
            template_name="", serial_number="SN-1", product_model="M1")
        self.assertFalse(result["success"])
        self.assertFalse(result["printed"])
        self.assertEqual(result["state"], "failed")

    def test_inflight_record_claim_is_exclusive_and_released(self):
        """The per-record claim is owned by the caller and covers save->submission."""
        line_runtime.update_line_settings(printer_enabled=True)
        self.assertTrue(line_runtime.claim_print(77))
        self.assertFalse(line_runtime.claim_print(77),
                         "second claim for the same record must be rejected")
        line_runtime.release_print(77)
        self.assertTrue(line_runtime.claim_print(77))
        line_runtime.release_print(77)
        result = line_runtime.print_label_if_enabled(
            template_name="", serial_number="SN-77", product_model="M1",
            record_id=77)
        self.assertEqual(result["state"], "submitted")

    def test_concurrent_prints_are_serialized(self):
        class TrackingRunner:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def run(self, cmd, **kwargs):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.15)
                with self.lock:
                    self.active -= 1
                return subprocess.CompletedProcess(cmd, 0, "", "")

        tracking = TrackingRunner()
        bartender_print.set_runner(tracking)
        printer = bartender_print.BarTenderPrinter()
        results = []
        threads = [threading.Thread(
            target=lambda: results.append(printer.print_fixed_label_files_ex(
                product_model="M", supplier_code="S", sequence_code=str(i))))
            for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(tracking.max_active, 1)
        self.assertTrue(all(r["success"] for r in results))


class ReprintApiTests(unittest.TestCase):
    def setUp(self):
        prepare_template()
        self.runner = FakePrintRunner()
        bartender_print.set_runner(self.runner)
        line_runtime.update_line_settings(printer_enabled=True)
        database._INITIALIZED = False
        database.init_database()
        self.record_id, _ = database.save_record_ex(
            qr_code="QR-X", pressure1=1.0, leak1=0.1, pressure2=None, leak2=None,
            test_result="PASS", product_model="M1", operator="op",
            serial_number="20260101-M1-00007", cycle_id="reprint-cycle")
        self.fail_id, _ = database.save_record_ex(
            qr_code="QR-Y", pressure1=1.0, leak1=0.1, pressure2=None, leak2=None,
            test_result="FAIL", product_model="M1", operator="op",
            serial_number="20260101-M1-00008", cycle_id="reprint-fail")
        self.client = TestClient(webui_server.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        bartender_print.set_runner(None)

    def test_reprint_requires_record_id(self):
        response = self.client.post("/api/print_label",
                                    json={"product_model": "M1"}).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "RECORD_REQUIRED")
        self.assertEqual(len(self.runner.calls), 0)

    def test_reprint_reuses_stored_serial(self):
        response = self.client.post("/api/print_label",
                                    json={"record_id": self.record_id}).json()
        self.assertTrue(response["success"])
        self.assertTrue(response["printed"])
        self.assertEqual(response["serial_number"], "20260101-M1-00007")
        record = database.get_record(self.record_id)
        self.assertEqual(record["print_state"], "submitted")

    def test_reprint_uses_stored_template_and_supplier(self):
        template_dir = Path(os.environ["ATEQ_LABEL_TEMPLATE_DIR"])
        template_dir.mkdir(parents=True, exist_ok=True)
        (template_dir / "orig.btw").write_text("orig", encoding="utf-8")
        record_id, _ = database.save_record_ex(
            qr_code="QR-IDENT", pressure1=1.0, leak1=0.1, pressure2=None, leak2=None,
            test_result="PASS", product_model="M1", operator="op",
            serial_number="20260101-M1-00009", cycle_id="reprint-identity",
            label_template="orig.btw", supplier_code="SUP-ORIG")
        response = self.client.post("/api/print_label", json={
            "record_id": record_id,
            "label_template": "other.btw",
            "supplier_code": "SUP-OTHER"}).json()
        self.assertTrue(response["success"], response)
        used_cmd = self.runner.calls[-1]
        self.assertTrue(any("orig.btw" in part for part in used_cmd), used_cmd)
        supplier_file = Path(os.environ["ATEQ_LABEL_DIR"]) / "供应商代码.txt"
        self.assertEqual(supplier_file.read_text(encoding="gbk"), "SUP-ORIG")

    def test_fail_record_cannot_be_reprinted(self):
        response = self.client.post("/api/print_label",
                                    json={"record_id": self.fail_id}).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "RECORD_NOT_PASS")
        self.assertEqual(len(self.runner.calls), 0)

    def test_disabled_printer_does_not_report_printed(self):
        line_runtime.update_line_settings(printer_enabled=False)
        response = self.client.post("/api/print_label",
                                    json={"record_id": self.record_id}).json()
        self.assertFalse(response["printed"])
        self.assertEqual(response["state"], "disabled")
        self.assertEqual(len(self.runner.calls), 0)


if __name__ == "__main__":
    unittest.main()
