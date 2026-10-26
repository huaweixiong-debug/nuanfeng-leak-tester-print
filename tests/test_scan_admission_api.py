#!/usr/bin/env python3
"""Slice C: scan switch matrix, idempotent events, API snapshots, latency."""

from collections import deque
import os
from pathlib import Path
import tempfile
import time
import unittest

# Environment must be set before importing webui_server/line_runtime/database.
_TMP = tempfile.mkdtemp(prefix="ateq_api_")
os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")
os.environ["ATEQ_LINE_SETTINGS"] = str(Path(_TMP) / "line_settings.json")
os.environ["ATEQ_TEST_DB"] = str(Path(_TMP) / "test.db")
os.environ["ATEQ_SERIAL_DB"] = str(Path(_TMP) / "serial.db")
os.environ["ATEQ_RUNTIME_STATE"] = str(Path(_TMP) / "state.json")
os.environ["ATEQ_SCANNER_LOG"] = str(Path(_TMP) / "scanner.log")
os.environ["ATEQ_PRODUCT_CONFIG"] = str(Path(_TMP) / "products.json")
os.environ["ATEQ_LEGACY_PRODUCT_MODELS"] = str(Path(_TMP) / "legacy_models.json")

from fastapi.testclient import TestClient  # noqa: E402

import line_runtime  # noqa: E402
import product_config  # noqa: E402
import program_selector  # noqa: E402
import serial_scanner  # noqa: E402
import webui_server  # noqa: E402
from cycle_state import CycleManager  # noqa: E402
from program_selector import TestExecutor  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeClock,
    FakePrinter,
    FakeRecorder,
    ScriptedAdapter,
    reset_scanner_state,
    sample,
)


def wait_idle(executor, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not executor._manager.is_busy():
            return True
        time.sleep(0.005)
    return False


class SlowAdapter(ScriptedAdapter):
    def read_step_code(self):
        time.sleep(3.0)
        return super().read_step_code()


class ScanAdmissionApiTests(unittest.TestCase):
    def setUp(self):
        reset_scanner_state(serial_scanner)
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.default_step = 65535
        adapter.default_sample = sample()
        self.adapter = adapter
        self.executor = TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=FakePrinter(),
            scanner_module=serial_scanner, persist_enabled=False,
        )
        program_selector.set_test_executor(self.executor)
        serial_scanner.set_event_callback(self.executor.handle_scan_event)
        store = Path(os.environ["ATEQ_PRODUCT_CONFIG"])
        if store.exists():
            store.unlink()
        product_config.replace_products(
            [{"id": 1, "model": "M1", "program1": 1, "program2": 0}], revision=0)
        self.executor.set_pending_test_context(product_model="M1", program1=1,
                                               program2=0)
        line_runtime.update_line_settings(scan_required=True, scan_start=True,
                                          printer_enabled=False)
        self.client = TestClient(webui_server.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        reset_scanner_state(serial_scanner)
        self.executor.stop_instrument_supervisor()

    def start(self, **overrides):
        params = {"program1": 1, "program2": 0, "product_model": "M1",
                  "operator": "tester", "qr_code": "", "label_template": "",
                  "supplier_code": ""}
        params.update(overrides)
        return self.client.post("/api/start_test_sequence", params=params)

    def test_scan_start_true_auto_starts_once_and_replay_is_duplicate(self):
        self.adapter.step_script = deque([4, 6, 65535])
        event = serial_scanner.publish_scan_for_test(b"PART-1\r\n")
        self.assertTrue(wait_idle(self.executor))
        self.assertEqual(self.adapter.start_calls, 1)
        first = self.client.post("/api/scan_qualified",
                                 json={"event_id": event.event_id,
                                       "qr_code": event.code}).json()
        second = self.client.post("/api/scan_qualified",
                                  json={"event_id": event.event_id,
                                        "qr_code": event.code}).json()
        self.assertTrue(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.adapter.start_calls, 1)

    def test_scan_start_false_binds_then_manual_start_consumes(self):
        line_runtime.update_line_settings(scan_required=True, scan_start=False)
        self.adapter.step_script = deque([4, 6, 65535])
        event = serial_scanner.publish_scan_for_test(b"PART-2\r\n")
        self.assertEqual(event.result, "BOUND")
        self.assertEqual(self.adapter.start_calls, 0)
        response = self.start(qr_code="PART-2").json()
        self.assertTrue(response["success"], response)
        self.assertIsNone(serial_scanner.get_binding())
        self.assertTrue(wait_idle(self.executor))
        replay = self.client.post("/api/scan_qualified",
                                  json={"event_id": event.event_id,
                                        "qr_code": event.code}).json()
        self.assertTrue(replay["duplicate"])
        self.assertFalse(replay["started"])

    def test_scan_required_blocks_manual_start_without_code(self):
        line_runtime.update_line_settings(scan_required=True, scan_start=False)
        response = self.start().json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "SCAN_REQUIRED")

    def test_scan_not_required_allows_blank_code_start(self):
        line_runtime.update_line_settings(scan_required=False, scan_start=False)
        self.adapter.step_script = deque([4, 6, 65535])
        response = self.start().json()
        self.assertTrue(response["success"], response)
        self.assertTrue(wait_idle(self.executor))

    def test_busy_device_scan_is_consumed_not_staged(self):
        line_runtime.update_line_settings(scan_required=False, scan_start=True)
        self.adapter.write_gate = __import__("threading").Event()
        self.adapter.step_script = deque([4, 6, 65535])
        response = self.start().json()
        self.assertTrue(response["success"], response)
        self.assertTrue(self.adapter.write_entered.wait(2.0))
        event = serial_scanner.publish_scan_for_test(b"BUSY-1\r\n")
        stored = serial_scanner.get_event(event.event_id)
        self.assertEqual(stored["result"], "BUSY")
        self.assertIsNone(serial_scanner.get_binding())
        replay = self.client.post("/api/scan_qualified",
                                  json={"event_id": event.event_id,
                                        "qr_code": event.code}).json()
        self.assertTrue(replay["duplicate"])
        self.assertFalse(replay["started"])
        self.adapter.write_gate.set()
        self.adapter.write_gate = None
        self.assertTrue(wait_idle(self.executor))
        self.assertEqual(self.adapter.start_calls, 1)

    def test_unknown_event_id_is_rejected(self):
        response = self.client.post("/api/scan_qualified",
                                    json={"event_id": "ghost:1",
                                          "qr_code": "X"}).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "UNKNOWN_EVENT")

    def test_legacy_manual_qr_binding_is_not_device_verified(self):
        line_runtime.update_line_settings(scan_required=True, scan_start=False)
        response = self.client.post("/api/scan_qualified",
                                    json={"qr_code": "TYPED-1"}).json()
        self.assertTrue(response["confirmed"])
        binding = serial_scanner.get_binding()
        self.assertEqual(binding["source"], "manual")
        self.assertFalse(binding["device_verified"])
        self.adapter.step_script = deque([4, 6, 65535])
        started = self.start(qr_code="TYPED-1").json()
        self.assertTrue(started["success"], started)
        self.assertTrue(wait_idle(self.executor))

    def test_qr_mismatch_is_rejected(self):
        line_runtime.update_line_settings(scan_required=True, scan_start=False)
        serial_scanner.publish_scan_for_test(b"RIGHT-1\r\n")
        response = self.start(qr_code="WRONG-1").json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "QR_MISMATCH")

    def test_old_completion_payload_not_leaked_to_new_cycle(self):
        line_runtime.update_line_settings(scan_required=False, scan_start=False)
        self.adapter.step_script = deque([4, 6, 65535])
        self.assertTrue(self.start().json()["success"])
        self.assertTrue(wait_idle(self.executor))
        status = self.client.get("/api/test_status").json()
        self.assertEqual(status["overall_result"], "PASS")
        self.assertEqual(status["record_id"], 101)
        self.assertIsNotNone(status["program1_pressure"])
        sequence_before = status["completion_sequence"]

        import threading
        self.adapter.write_gate = threading.Event()
        self.assertTrue(self.start().json()["success"])
        self.assertTrue(self.adapter.write_entered.wait(2.0))
        busy_status = self.client.get("/api/test_status").json()
        self.assertTrue(busy_status["busy"])
        self.assertIsNone(busy_status["overall_result"])
        self.assertIsNone(busy_status["program1_pressure"])
        self.assertIsNone(busy_status["program1_result"])
        self.assertEqual(busy_status["completion_sequence"], sequence_before)
        self.adapter.write_gate.set()
        self.adapter.write_gate = None
        self.assertTrue(wait_idle(self.executor))

    def test_slow_serial_does_not_delay_status_or_stop(self):
        line_runtime.update_line_settings(scan_required=False, scan_start=False)
        clock = FakeClock()
        slow = SlowAdapter(clock)
        slow.default_step = 65535
        slow.default_sample = sample(step_code=65535, status=0)
        self.executor._io = slow
        self.adapter = slow
        started = self.start().json()
        self.assertTrue(started["success"], started)
        time.sleep(0.05)

        t0 = time.time()
        status = self.client.get("/api/test_status")
        status_elapsed = time.time() - t0
        self.assertEqual(status.status_code, 200)

        t0 = time.time()
        scanner_status = self.client.get("/api/scanner/status")
        scanner_elapsed = time.time() - t0
        self.assertEqual(scanner_status.status_code, 200)

        t0 = time.time()
        stop = self.client.post("/api/stop_test").json()
        stop_elapsed = time.time() - t0

        self.assertLess(status_elapsed, 1.0)
        self.assertLess(scanner_elapsed, 1.0)
        self.assertLess(stop_elapsed, 1.0)
        self.assertTrue(stop["cancel_accepted"])
        self.assertTrue(stop["reset_pending"] or stop["reset_confirmed"])
        slow.default_step = 65535
        self.assertTrue(wait_idle(self.executor, timeout=8.0))


if __name__ == "__main__":
    unittest.main()
