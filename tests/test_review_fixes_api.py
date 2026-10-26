#!/usr/bin/env python3
"""API-level failing-first regressions for the repair round (R7..R12)."""

from collections import deque
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_review_api_")
os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")
os.environ["ATEQ_TEST_DB"] = str(Path(_TMP) / "test.db")
os.environ["ATEQ_SERIAL_DB"] = str(Path(_TMP) / "serial.db")
os.environ["ATEQ_RUNTIME_STATE"] = str(Path(_TMP) / "state.json")
os.environ["ATEQ_SCANNER_LOG"] = str(Path(_TMP) / "scanner.log")
os.environ["ATEQ_LINE_SETTINGS"] = str(Path(_TMP) / "line_settings.json")
os.environ["ATEQ_PRODUCT_CONFIG"] = str(Path(_TMP) / "products.json")
os.environ["ATEQ_LEGACY_PRODUCT_MODELS"] = str(Path(_TMP) / "legacy_models.json")
os.environ["ATEQ_LABEL_DIR"] = str(Path(_TMP) / "data")
os.environ["ATEQ_LABEL_TEMPLATE_DIR"] = str(Path(_TMP) / "templates")
os.environ["ATEQ_DEFAULT_TEMPLATE"] = str(Path(_TMP) / "templates" / "label.btw")

from fastapi.testclient import TestClient  # noqa: E402

import bartender_print  # noqa: E402
import database  # noqa: E402
import line_runtime  # noqa: E402
import modbus_utils  # noqa: E402
import product_config  # noqa: E402
import program_selector  # noqa: E402
import serial_scanner  # noqa: E402
import webui_server  # noqa: E402
from cycle_state import CycleManager  # noqa: E402
from modbus_utils import STATION_ID  # noqa: E402
from program_selector import TestExecutor  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeClock,
    FakePrintRunner,
    FakePrinter,
    FakeRecorder,
    FakeRTUDevice,
    ScriptedAdapter,
    reset_scanner_state,
    sample,
)


def wait_idle(executor, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not executor._manager.is_busy():
            return True
        time.sleep(0.005)
    return not executor._manager.is_busy()


def prepare_template():
    template = Path(os.environ["ATEQ_DEFAULT_TEMPLATE"])
    template.parent.mkdir(parents=True, exist_ok=True)
    if not template.exists():
        template.write_text("fake btw", encoding="utf-8")


class ReviewApiBase(unittest.TestCase):
    def setUp(self):
        reset_scanner_state(serial_scanner)
        for path in (Path(os.environ["ATEQ_PRODUCT_CONFIG"]),
                     Path(os.environ["ATEQ_PRODUCT_CONFIG"]).with_name("active_product.json"),
                     Path(os.environ["ATEQ_LEGACY_PRODUCT_MODELS"])):
            if path.exists():
                path.unlink()
        database._INITIALIZED = False
        database.init_database()
        line_runtime.update_line_settings(scan_required=False, scan_start=False,
                                          printer_enabled=False)
        prepare_template()
        self.runner = FakePrintRunner()
        bartender_print.set_runner(self.runner)
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.default_step = 65535
        adapter.default_sample = sample()
        self.adapter = adapter
        self.clock = clock
        self.executor = TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=FakePrinter(),
            scanner_module=serial_scanner, persist_enabled=False)
        program_selector.set_test_executor(self.executor)
        self.client = TestClient(webui_server.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        bartender_print.set_runner(None)
        reset_scanner_state(serial_scanner)

    def put_products(self, products, revision=0):
        return self.client.put("/api/product_config",
                               json={"revision": revision,
                                     "products": products})

    def start(self, **overrides):
        params = {"program1": 1, "program2": 0, "product_model": "M1"}
        params.update(overrides)
        return self.client.post("/api/start_test_sequence", params=params)


class R9ParamsSelectorAddressTests(ReviewApiBase):
    def test_selector_frame_uses_original_address_0x3004(self):
        device = FakeRTUDevice(station=STATION_ID)
        modbus_utils.set_transport_override(device.handle)
        try:
            response = self.client.get("/api/params?program=2").json()
        finally:
            modbus_utils.clear_transport_override()
        self.assertTrue(response["success"], response)
        first = device.commands[0][1]
        expected_prefix = f"{STATION_ID:02X}103004000102"
        self.assertTrue(first.startswith(expected_prefix),
                        f"selector frame must target 0x3004, got {first}")
        self.assertAlmostEqual(response["fill_time"], 3.0, places=2)


class R8ConfigAuthorityTests(ReviewApiBase):
    def test_unknown_or_deleted_model_rejected_without_stale_fallback(self):
        self.put_products([{"id": 1, "model": "M2", "program1": 1}], revision=0)
        self.executor.set_pending_test_context(product_model="M1", program1=1)
        response = self.start(product_model="M1").json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "MODEL_UNKNOWN")
        self.assertEqual(self.adapter.write_program_calls, [])

    def test_tampered_template_and_supplier_ignored(self):
        self.put_products([{"id": 1, "model": "M1", "program1": 1, "program2": 0,
                            "labelTemplate": "orig.btw", "supplierCode": "SUP-1"}],
                          revision=0)
        import threading as _threading
        self.adapter.write_gate = _threading.Event()
        response = self.start(label_template="evil.btw", supplier_code="EVIL")
        self.assertTrue(response.json()["success"], response.text)
        self.assertTrue(self.adapter.write_entered.wait(2.0))
        cycle = self.executor._manager.get_active()
        self.assertEqual(cycle.context["label_template"], "orig.btw")
        self.assertEqual(cycle.context["supplier_code"], "SUP-1")
        self.adapter.write_gate.set()
        self.adapter.write_gate = None
        self.assertTrue(wait_idle(self.executor))

    def test_sync_unknown_model_rejected_and_keeps_active_product(self):
        self.put_products([{"id": 1, "model": "M1", "program1": 1}], revision=0)
        self.client.post("/api/sync_test_context",
                         json={"product_model": "M1"})
        response = self.client.post("/api/sync_test_context",
                                    json={"product_model": "NOPE"})
        self.assertFalse(response.json()["success"])
        self.assertEqual(response.json()["error_code"], "MODEL_UNKNOWN")
        pending = self.executor.get_pending_context()
        self.assertEqual(pending.get("product_model"), "M1")
        self.assertEqual(pending.get("program1"), 1)
        self.assertEqual(product_config.get_active_model(), "M1")


class ActiveProductPersistenceTests(ReviewApiBase):
    def test_empty_sync_from_another_browser_keeps_product(self):
        self.put_products([{"id": 1, "model": "M1", "program1": 1}], revision=0)
        self.client.post("/api/sync_test_context",
                         json={"product_model": "M1", "operator": "op1"})
        response = self.client.post("/api/sync_test_context",
                                    json={"product_model": "", "operator": ""})
        self.assertTrue(response.json()["success"], response.text)
        pending = self.executor.get_pending_context()
        self.assertEqual(pending.get("product_model"), "M1")
        self.assertEqual(pending.get("program1"), 1)
        self.assertEqual(pending.get("operator"), "op1")
        self.assertEqual(product_config.get_active_model(), "M1")

    def test_active_product_restored_after_restart(self):
        self.put_products([{"id": 1, "model": "M1", "program1": 3, "program2": 4}],
                          revision=0)
        self.client.post("/api/sync_test_context", json={"product_model": "M1"})
        # Simulate a service restart: fresh executor with empty memory, lifespan runs.
        self.client.__exit__(None, None, None)
        fresh = TestExecutor(
            io=self.adapter, clock=self.clock.now, sleeper=self.clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=FakePrinter(),
            scanner_module=serial_scanner, persist_enabled=False)
        program_selector.set_test_executor(fresh)
        self.executor = fresh
        self.client = TestClient(webui_server.app)
        self.client.__enter__()
        pending = fresh.get_pending_context()
        self.assertEqual(pending.get("product_model"), "M1")
        self.assertEqual(pending.get("program1"), 3)
        self.assertEqual(pending.get("program2"), 4)
        context = self.client.get("/api/test_context").json()
        self.assertEqual(context["product_model"], "M1")
        status = self.client.get("/api/test_status").json()
        self.assertEqual(status["active_product_model"], "M1")

    def test_config_edit_rearms_active_product_programs(self):
        self.put_products([{"id": 1, "model": "M1", "program1": 1}], revision=0)
        self.client.post("/api/sync_test_context", json={"product_model": "M1"})
        revision = product_config.get_revision()
        self.put_products([{"id": 1, "model": "M1", "program1": 5, "program2": 6}],
                          revision=revision)
        pending = self.executor.get_pending_context()
        self.assertEqual(pending.get("program1"), 5)
        self.assertEqual(pending.get("program2"), 6)

    def test_never_selected_stays_empty(self):
        self.put_products([{"id": 1, "model": "M1", "program1": 1}], revision=0)
        response = self.client.post("/api/sync_test_context", json={"product_model": ""})
        self.assertTrue(response.json()["success"])
        self.assertIsNone(self.executor.get_pending_context().get("product_model"))
        self.assertIsNone(self.client.get("/api/test_context").json()["product_model"])

    def test_strict_put_rejects_invalid_program2_preserving_store(self):
        self.put_products([{"id": 1, "model": "M1", "program1": 1, "program2": 2}],
                          revision=0)
        revision_before = product_config.get_revision()
        bad = self.put_products([{"id": 1, "model": "M1", "program1": 1,
                                  "program2": 256}], revision=revision_before)
        self.assertFalse(bad.json()["success"])
        self.assertEqual(product_config.get_revision(), revision_before)
        self.assertEqual(product_config.find_product("M1")["program2"], 2)

    def test_corrupt_store_reports_config_unavailable(self):
        Path(os.environ["ATEQ_PRODUCT_CONFIG"]).parent.mkdir(parents=True, exist_ok=True)
        Path(os.environ["ATEQ_PRODUCT_CONFIG"]).write_text("{not json", encoding="utf-8")
        response = self.client.get("/api/product_config").json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "CONFIG_UNAVAILABLE")
        start = self.start().json()
        self.assertFalse(start["success"])
        self.assertEqual(start["error_code"], "CONFIG_UNAVAILABLE")

    def test_migration_does_not_silently_zero_explicit_invalid_p2(self):
        Path(os.environ["ATEQ_LEGACY_PRODUCT_MODELS"]).write_text(json.dumps([
            {"id": 1, "model": "BADP2", "program": 1, "program2": 256},
            {"id": 2, "model": "GOOD", "program": 2},
        ]), encoding="utf-8")
        products = product_config.list_products()
        bad = [p for p in products if p["model"] == "BADP2"]
        if bad:
            self.assertNotEqual(bad[0]["program2"], 0,
                                "explicit invalid P2 must not become single-stage")
        good = product_config.find_product("GOOD")
        self.assertIsNotNone(good)
        self.assertEqual(good["program1"], 2)
        self.assertEqual(good["program2"], 0)


class R7OptionalScanBindingTests(ReviewApiBase):
    def test_optional_scan_binding_identifies_manual_start(self):
        self.put_products([{"id": 1, "model": "M1", "program1": 1, "program2": 0}],
                          revision=0)
        line_runtime.update_line_settings(scan_required=False, scan_start=False)
        self.adapter.step_script = deque([4, 6, 65535])
        event = serial_scanner.publish_scan_for_test(b"SCAN-OPT-1\r\n")
        serial_scanner.set_binding(event.event_id)  # backend consumer binding
        response = self.start().json()
        self.assertTrue(response["success"], response)
        self.assertTrue(wait_idle(self.executor))
        payload = self.executor.runtime_snapshot()["completion_payload"]
        self.assertEqual(payload["qr_code"], event.code)
        self.assertEqual(payload["scan_source"], "device")
        self.assertIsNone(serial_scanner.get_binding())


class R10DataTruthfulnessTests(ReviewApiBase):
    def test_data_reports_failed_current_sample_not_success(self):
        self.executor._update_sample_cache(sample(pressure=10.0, leak=1.0),
                                           error_code=None)
        good = self.client.get("/api/data").json()
        self.assertTrue(good["success"])
        self.executor._update_sample_cache(None, error_code="READ_TIMEOUT",
                                           message="no response")
        failed = self.client.get("/api/data").json()
        self.assertFalse(failed["success"],
                         "failed current sample must not be reported as success")
        self.assertEqual(failed["error_code"], "READ_TIMEOUT")
        self.assertTrue(failed["stale"])


class R11LifecycleFailClosedTests(ReviewApiBase):
    def test_database_failure_sets_startup_error_and_rejects_start(self):
        from unittest import mock
        webui_server.STARTUP_ERROR = None
        old_init = os.environ.get("ATEQ_INIT_DB")
        os.environ["ATEQ_INIT_DB"] = "1"  # force DB init even with hardware disabled
        client = TestClient(webui_server.app)
        try:
            with mock.patch.object(webui_server, "init_database",
                                   side_effect=RuntimeError("migration failed")):
                with client:
                    self.assertTrue(webui_server.STARTUP_ERROR)
                    self.assertFalse(self.executor.supervisor_running())
                    response = self.start().json()
        finally:
            if old_init is None:
                os.environ.pop("ATEQ_INIT_DB", None)
            else:
                os.environ["ATEQ_INIT_DB"] = old_init
        self.assertFalse(response["success"])
        self.assertIn(response["error_code"],
                      ("STARTUP_UNAVAILABLE", "DATABASE_UNAVAILABLE"))


class R12AutoPrintClaimTests(ReviewApiBase):
    def test_manual_reprint_busy_while_auto_print_pending(self):
        line_runtime.update_line_settings(printer_enabled=True)
        self.put_products([{"id": 1, "model": "M1", "program1": 1,
                            "labelTemplate": "", "supplierCode": ""}], revision=0)
        database._INITIALIZED = False
        database.init_database()
        self.executor._recorder = program_selector._default_record_saver
        self.executor._printer = self.executor._default_print
        self.adapter.step_script = deque([4, 6, 65535])
        # Hold the shared print lock so the automatic submission waits.
        bartender_print._PRINT_LOCK.acquire()
        try:
            ok, error, cycle_id = self.executor.start_cycle(
                {"product_model": "M1", "program1": 1, "program2": 0}, "software")
            self.assertTrue(ok, error)
            deadline = time.time() + 4.0
            record = None
            while time.time() < deadline:
                record = database.find_record_by_cycle(cycle_id)
                if record:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(record, "auto path must save before printing")
            manual = self.client.post("/api/print_label",
                                      json={"record_id": record["id"]}).json()
            self.assertEqual(manual.get("state"), "busy",
                             "manual reprint must not race the pending auto job")
        finally:
            bartender_print._PRINT_LOCK.release()
        self.assertTrue(wait_idle(self.executor))
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(database.get_record(record["id"])["print_state"], "submitted")

    def test_stop_before_submission_cancels_waiting_auto_print(self):
        line_runtime.update_line_settings(printer_enabled=True)
        database._INITIALIZED = False
        database.init_database()
        self.executor._recorder = program_selector._default_record_saver
        self.executor._printer = self.executor._default_print
        self.executor._manager  # ensure manager exists
        cycle, _ = self.executor._manager.admit(
            {"product_model": "M1", "program1": 1, "program2": 0},
            "software", [1])
        bartender_print._PRINT_LOCK.acquire()
        thread = threading.Thread(target=self.executor._finalize_and_finish,
                                  args=(cycle, "PASS", "OK"))
        thread.start()
        time.sleep(0.2)
        cycle.cancel_event.set()
        bartender_print._PRINT_LOCK.release()
        thread.join(5.0)
        self.assertEqual(self.runner.calls, [],
                         "cancelled job must not reach the printer after stop")
        payload = self.executor.runtime_snapshot()["completion_payload"]
        self.assertIn(payload["print_state"], ("skipped", "cancelled"))


if __name__ == "__main__":
    unittest.main()
