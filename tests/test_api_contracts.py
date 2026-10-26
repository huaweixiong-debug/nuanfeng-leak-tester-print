#!/usr/bin/env python3
"""Slice C/D/E: API contracts for start/stop/status/data/params/records/export."""

from collections import deque
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_contracts_")
os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")
os.environ["ATEQ_PRODUCT_CONFIG"] = str(Path(_TMP) / "products.json")
os.environ["ATEQ_LEGACY_PRODUCT_MODELS"] = str(Path(_TMP) / "product_models.json")
os.environ["ATEQ_LINE_SETTINGS"] = str(Path(_TMP) / "line_settings.json")
os.environ["ATEQ_RUNTIME_STATE"] = str(Path(_TMP) / "state.json")
os.environ["ATEQ_SCANNER_LOG"] = str(Path(_TMP) / "scanner.log")
os.environ["ATEQ_TEST_DB"] = str(Path(_TMP) / "test.db")
os.environ["ATEQ_SERIAL_DB"] = str(Path(_TMP) / "serial.db")

from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
import line_runtime  # noqa: E402
import modbus_utils  # noqa: E402
import product_config  # noqa: E402
import program_selector  # noqa: E402
import webui_server  # noqa: E402
from cycle_state import CycleManager  # noqa: E402
from modbus_utils import STATION_ID  # noqa: E402
from program_selector import TestExecutor  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeClock,
    FakePrinter,
    FakeRecorder,
    FakeScanner,
    FakeRTUDevice,
    ScriptedAdapter,
    sample,
)


def wait_idle(executor, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not executor._manager.is_busy():
            return True
        time.sleep(0.005)
    return False


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        db_path = Path(os.environ["ATEQ_TEST_DB"])
        if db_path.exists():
            db_path.unlink()
        database._INITIALIZED = False
        database.init_database()
        product_store = Path(os.environ["ATEQ_PRODUCT_CONFIG"])
        if product_store.exists():
            product_store.unlink()
        product_config.replace_products(
            [{"id": 1, "model": "M1", "program1": 1, "program2": 0}], revision=0)
        line_runtime.update_line_settings(scan_required=False, scan_start=False,
                                          printer_enabled=False)
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.default_step = 65535
        adapter.default_sample = sample()
        self.adapter = adapter
        self.executor = TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=FakePrinter(),
            scanner_module=FakeScanner(), persist_enabled=False,
        )
        program_selector.set_test_executor(self.executor)
        self.client = TestClient(webui_server.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def start(self, **overrides):
        params = {"program1": 1, "program2": 0, "product_model": "M1"}
        params.update(overrides)
        return self.client.post("/api/start_test_sequence", params=params)

    # -------------------------------------------------------------- starts
    def test_start_validates_program_range(self):
        self.executor.set_pending_test_context(product_model="M1", program1=1)
        for program1 in (0, 256, -1):
            response = self.start(program1=program1).json()
            self.assertFalse(response["success"])
            self.assertEqual(response["error_code"], "PROGRAM1_INVALID")
        response = self.start(program2=300).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "PROGRAM2_INVALID")

    def test_start_without_context_or_config_is_model_unknown(self):
        response = self.start(product_model="NOPE").json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "MODEL_UNKNOWN")

    def test_legacy_start_is_explicit_compat_error(self):
        response = self.client.post("/api/start").json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "PROGRAM_REQUIRED")

    def test_stop_when_idle_reports_no_active_cycle(self):
        response = self.client.post("/api/stop_test").json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "NO_ACTIVE_CYCLE")
        alias = self.client.post("/api/stop").json()
        self.assertFalse(alias["success"])
        self.assertEqual(alias["error_code"], "NO_ACTIVE_CYCLE")

    def test_stop_while_busy_reports_cancel_and_reset_fields(self):
        self.executor.set_pending_test_context(product_model="M1", program1=1)
        self.adapter.write_gate = threading.Event()
        self.assertTrue(self.start().json()["success"])
        self.assertTrue(self.adapter.write_entered.wait(2.0))
        response = self.client.post("/api/stop_test").json()
        self.assertTrue(response["success"])
        self.assertTrue(response["cancel_accepted"])
        self.assertIn("reset_confirmed", response)
        self.assertIn("reset_pending", response)
        self.assertIn("reset_state", response)
        self.adapter.write_gate.set()
        self.adapter.write_gate = None
        self.assertTrue(wait_idle(self.executor))

    # -------------------------------------------------------------- status
    def test_status_contains_cycle_identity_and_cache_fields(self):
        response = self.client.get("/api/test_status").json()
        self.assertTrue(response["success"])
        for key in ("cycle_id", "busy", "running", "sample", "reset", "prepare",
                    "supervisor", "completion_sequence", "required_programs",
                    "terminal_reason", "actual_program1", "actual_program2"):
            self.assertIn(key, response)

    def test_data_serves_cache_and_reports_no_sample(self):
        self.executor._sample_cache = None
        empty = self.client.get("/api/data").json()
        self.assertFalse(empty["success"])
        self.assertEqual(empty["error_code"], "NO_SAMPLE")

        self.executor._update_sample_cache(sample(step_code=6, pressure=42.0,
                                                  leak=0.42))
        data = self.client.get("/api/data").json()
        self.assertTrue(data["success"])
        self.assertEqual(data["pressure"], 42.0)
        self.assertIn("sampled_at", data)
        self.assertIn("stale", data)

    # -------------------------------------------------------------- params
    def test_params_read_uses_station_id_and_returns_real_values(self):
        device = FakeRTUDevice(station=STATION_ID)
        modbus_utils.set_transport_override(device.handle)
        try:
            response = self.client.get("/api/params?program=2").json()
        finally:
            modbus_utils.clear_transport_override()
        self.assertTrue(response["success"], response)
        self.assertEqual(response["program"], 2)
        self.assertAlmostEqual(response["fill_time"], 3.0, places=2)
        self.assertAlmostEqual(response["stab_time"], 1.5, places=2)
        self.assertAlmostEqual(response["test_time"], 4.0, places=2)
        self.assertFalse(response["cached"])
        first_frame = device.commands[0][1]
        self.assertTrue(first_frame.lower().startswith(f"{STATION_ID:02x}10"))

    def test_params_failure_never_fabricates_defaults(self):
        modbus_utils.set_transport_override(lambda frame, timeout: None)
        try:
            response = self.client.get("/api/params?program=1").json()
        finally:
            modbus_utils.clear_transport_override()
        self.assertFalse(response["success"])
        self.assertNotIn("fill_time", response)
        self.assertIn("error_code", response)

    def test_params_busy_sends_no_frames(self):
        device = FakeRTUDevice(station=STATION_ID)
        modbus_utils.set_transport_override(device.handle)
        self.executor.set_pending_test_context(product_model="M1", program1=1)
        self.adapter.write_gate = threading.Event()
        try:
            self.assertTrue(self.start().json()["success"])
            self.assertTrue(self.adapter.write_entered.wait(2.0))
            before = len(device.commands)
            response = self.client.get("/api/params?program=1").json()
            self.assertFalse(response["success"])
            self.assertEqual(response["error_code"], "BUSY")
            self.assertEqual(len(device.commands), before)
        finally:
            self.adapter.write_gate.set()
            self.adapter.write_gate = None
            modbus_utils.clear_transport_override()
        self.assertTrue(wait_idle(self.executor))

    # -------------------------------------------------------------- records
    def test_record_routes_keep_legacy_shapes(self):
        created = self.client.post("/api/records", json={
            "qr_code": "QR-1", "pressure1": 1.0, "leak1": 0.1,
            "test_result": "PASS", "product_model": "M1",
            "serial_number": "SN-ROUTE-1"}).json()
        self.assertTrue(created["success"])
        self.assertIsInstance(created["id"], int)
        today = self.client.get("/api/records/today").json()
        self.assertTrue(today["success"])
        self.assertTrue(any(r["serial_number"] == "SN-ROUTE-1"
                            for r in today["records"]))
        latest = self.client.get("/api/records").json()
        self.assertTrue(latest["success"])
        query = self.client.get("/api/query/records?serial=SN-ROUTE-1").json()
        self.assertTrue(query["success"])
        self.assertEqual(query["total"], 1)

    def test_export_does_not_perform_serial_io(self):
        self.client.post("/api/records", json={
            "qr_code": "QR-2", "pressure1": 1.0, "leak1": 0.1,
            "test_result": "PASS", "product_model": "M1"})
        device = FakeRTUDevice(station=STATION_ID)
        modbus_utils.set_transport_override(device.handle)
        try:
            response = self.client.get("/api/query/export")
        finally:
            modbus_utils.clear_transport_override()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(device.commands, [])

    def test_cycle_end_to_end_via_api_uses_frozen_snapshot(self):
        self.adapter.step_script = deque([4, 6, 65535])
        response = self.start(program1=1).json()
        self.assertTrue(response["success"], response)
        self.assertTrue(wait_idle(self.executor))
        status = self.client.get("/api/test_status").json()
        self.assertEqual(status["overall_result"], "PASS")
        self.assertEqual(status["actual_program1"], 1)
        self.assertEqual(status["program1_result"], "PASS")
        self.assertIsNotNone(status["program1_pressure"])
        self.assertEqual(status["pressure1_unit"], "kPa")


if __name__ == "__main__":
    unittest.main()
