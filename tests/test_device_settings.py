#!/usr/bin/env python3
"""Slice 2: persisted device settings (scanner enable + scanner/instrument ports).

Covers: strict validation with no partial writes, duplicate effective COM ports,
scanner-disable normalization, all four scanner/printer combinations, supervisor
gating from persisted scanner_enabled, disabled-scanner event rejection, live
communication-change rejection while a cycle is active, idle apply, restart_required
fallback, GET-only COM enumeration, and restart persistence.

Offline only: ATEQ_DISABLE_HARDWARE=1, temporary settings/DB paths, fake runners.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_device_")
os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")
os.environ["ATEQ_LINE_SETTINGS"] = str(Path(_TMP) / "line_settings.json")
os.environ["ATEQ_TEST_DB"] = str(Path(_TMP) / "test.db")
os.environ["ATEQ_SERIAL_DB"] = str(Path(_TMP) / "serial.db")
os.environ["ATEQ_RUNTIME_STATE"] = str(Path(_TMP) / "state.json")
os.environ["ATEQ_SCANNER_LOG"] = str(Path(_TMP) / "scanner.log")
os.environ["ATEQ_LABEL_DIR"] = str(Path(_TMP) / "data")
os.environ["ATEQ_LABEL_TEMPLATE_DIR"] = str(Path(_TMP) / "templates")
os.environ["ATEQ_DEFAULT_TEMPLATE"] = str(Path(_TMP) / "templates" / "label.btw")
os.environ["ATEQ_PRODUCT_CONFIG"] = str(Path(_TMP) / "products.json")

from fastapi.testclient import TestClient  # noqa: E402

import bartender_print  # noqa: E402
import line_runtime  # noqa: E402
import modbus_utils  # noqa: E402
import program_selector  # noqa: E402
import serial_scanner  # noqa: E402
import webui_server  # noqa: E402
from cycle_state import CycleManager  # noqa: E402
from program_selector import TestExecutor  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeClock,
    FakePrintRunner,
    FakeRecorder,
    FakeScanner,
    ScriptedAdapter,
    sample,
)


def settings_path() -> Path:
    return Path(os.environ["ATEQ_LINE_SETTINGS"])


def settings_bytes() -> bytes:
    return settings_path().read_bytes() if settings_path().exists() else b""


def prepare_template():
    template = Path(os.environ["ATEQ_DEFAULT_TEMPLATE"])
    template.parent.mkdir(parents=True, exist_ok=True)
    if not template.exists():
        template.write_text("fake btw", encoding="utf-8")


def reset_settings(**overrides):
    values = dict(scan_required=False, scan_start=False, printer_enabled=False,
                  scanner_enabled=True, scanner_port="", instrument_port="")
    values.update(overrides)
    return line_runtime.update_line_settings(**values)


class TrackingScanner(FakeScanner):
    """FakeScanner + the lifecycle hooks the supervisor actually calls."""

    def __init__(self):
        super().__init__()
        self.start_calls = 0
        self.stop_calls = 0
        self.ports = []
        self.callback = None
        self.cleared_events = []

    def set_event_callback(self, callback):
        self.callback = callback

    def start_serial_scanner(self):
        self.start_calls += 1
        return True

    def stop_serial_scanner(self, timeout=2.0):
        self.stop_calls += 1
        return True

    def clear_pending_events(self, reason=""):
        self.cleared_events.append(reason)
        self.binding = None

    def set_scan_port(self, port):
        self.ports.append(port)

    def get_scan_port(self):
        return self.ports[-1] or "COM7" if self.ports else "COM7"

    def get_scanner_state(self):
        return {"connected": False, "port": self.get_scan_port()}


class PortValidationTests(unittest.TestCase):
    def setUp(self):
        reset_settings()
        self.addCleanup(
            lambda: line_runtime.update_line_settings(
                scanner_enabled=True, scanner_port="", instrument_port=""))

    def test_malformed_ports_rejected_without_any_write(self):
        before = settings_bytes()
        for bad in ("COM0", "COM", "COM12x", "5", "COM-1", "usb001", 123):
            with self.subTest(port=bad):
                with self.assertRaises(line_runtime.SettingsError):
                    line_runtime.update_line_settings(instrument_port=bad)
                self.assertEqual(settings_bytes(), before)

    def test_ports_canonicalised_strip_uppercase(self):
        settings = line_runtime.update_line_settings(scanner_port=" com3 ")
        self.assertEqual(settings["scanner_port"], "COM3")

    def test_multi_digit_and_high_number_ports_allowed(self):
        settings = line_runtime.update_line_settings(scanner_port="COM10",
                                                     instrument_port="COM255")
        self.assertEqual(settings["scanner_port"], "COM10")
        self.assertEqual(settings["instrument_port"], "COM255")

    def test_empty_port_returns_to_default_fallback(self):
        line_runtime.update_line_settings(instrument_port="COM1")
        settings = line_runtime.update_line_settings(instrument_port="")
        self.assertEqual(settings["instrument_port"], "")

    def test_duplicate_effective_ports_rejected_no_partial_write(self):
        line_runtime.update_line_settings(instrument_port="COM5")
        before = settings_bytes()
        with self.assertRaises(line_runtime.SettingsError):
            line_runtime.update_line_settings(scanner_port="COM5")
        self.assertEqual(settings_bytes(), before)
        settings = line_runtime.get_line_settings()
        self.assertEqual(settings["instrument_port"], "COM5")
        self.assertEqual(settings["scanner_port"], "")

    def test_disabled_scanner_normalizes_scan_flags_off(self):
        reset_settings(scan_required=True, scan_start=True, scanner_enabled=True)
        settings = line_runtime.update_line_settings(scanner_enabled=False)
        self.assertFalse(settings["scan_required"])
        self.assertFalse(settings["scan_start"])
        self.assertFalse(settings["scanner_enabled"])

    def test_disabled_scanner_rejects_contradictory_true_flags(self):
        reset_settings(scanner_enabled=False)
        with self.assertRaises(line_runtime.SettingsError):
            line_runtime.update_line_settings(scan_required=True)
        with self.assertRaises(line_runtime.SettingsError):
            line_runtime.update_line_settings(scan_start=True)

    def test_legacy_file_without_new_keys_reads_defaults(self):
        settings_path().write_text(json.dumps({
            "scan_required": False, "printer_enabled": True, "scan_start": False
        }), encoding="utf-8")
        settings = line_runtime.get_line_settings()
        self.assertFalse(settings["scan_required"])
        self.assertTrue(settings["scanner_enabled"])
        self.assertEqual(settings["scanner_port"], "")
        self.assertEqual(settings["instrument_port"], "")

    def test_settings_survive_module_restart(self):
        reset_settings(scanner_enabled=True, scanner_port="COM3", instrument_port="COM1")
        project = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = str(project)
        env["ATEQ_DISABLE_HARDWARE"] = "1"
        env["ATEQ_LINE_SETTINGS"] = str(settings_path())
        code = ("import json, line_runtime; "
                "print(json.dumps(line_runtime.get_line_settings()))")
        result = subprocess.run([sys.executable, "-c", code], cwd=str(project),
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        persisted = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(persisted["scanner_enabled"])
        self.assertEqual(persisted["scanner_port"], "COM3")
        self.assertEqual(persisted["instrument_port"], "COM1")


class DeviceCombinationTests(unittest.TestCase):
    """All four scanner/printer combinations, offline."""

    def setUp(self):
        prepare_template()
        reset_settings()
        self.runner = FakePrintRunner()
        bartender_print.set_runner(self.runner)
        self.addCleanup(lambda: bartender_print.set_runner(None))
        self.addCleanup(lambda: serial_scanner.set_scan_port(None))
        self.addCleanup(
            lambda: line_runtime.update_line_settings(
                scanner_enabled=True, scanner_port="", instrument_port=""))
        self._hardware_disabled = program_selector.HARDWARE_DISABLED
        program_selector.HARDWARE_DISABLED = False
        self.addCleanup(
            lambda: setattr(program_selector, "HARDWARE_DISABLED",
                            self._hardware_disabled))

    def _make_executor(self, tracking):
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.default_step = 65535
        adapter.default_sample = sample()
        return TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=lambda cycle: None,
            scanner_module=tracking, persist_enabled=False,
        )

    def test_four_device_combinations(self):
        for scanner_enabled in (True, False):
            for printer_enabled in (True, False):
                with self.subTest(scanner=scanner_enabled, printer=printer_enabled):
                    reset_settings(scanner_enabled=scanner_enabled,
                                   printer_enabled=printer_enabled)
                    tracking = TrackingScanner()
                    executor = self._make_executor(tracking)
                    executor.start_instrument_supervisor(start_scanner=True)
                    try:
                        if scanner_enabled:
                            self.assertEqual(tracking.start_calls, 1)
                        else:
                            self.assertEqual(tracking.start_calls, 0,
                                             "disabled scanner must not start/open")
                    finally:
                        executor.stop_instrument_supervisor()

                    self.runner.calls.clear()
                    result = line_runtime.print_label_if_enabled(
                        template_name="", serial_number="SN-1", product_model="M1")
                    if printer_enabled:
                        self.assertNotEqual(result["state"], "disabled")
                        self.assertEqual(len(self.runner.calls), 1)
                    else:
                        self.assertEqual(result["state"], "disabled")
                        self.assertEqual(len(self.runner.calls), 0,
                                         "disabled printer must not submit")

    def test_persisted_scanner_port_is_applied_before_start(self):
        reset_settings(scanner_enabled=True, scanner_port="COM3")
        tracking = TrackingScanner()
        executor = self._make_executor(tracking)
        executor.start_instrument_supervisor(start_scanner=True)
        try:
            self.assertEqual(tracking.start_calls, 1)
            self.assertEqual(tracking.ports, ["COM3"])
        finally:
            executor.stop_instrument_supervisor()

    def test_disabled_scanner_rejects_device_events_without_binding(self):
        reset_settings(scanner_enabled=False)
        tracking = TrackingScanner()
        executor = self._make_executor(tracking)
        event = tracking.add_event("QR-1")
        executor.handle_scan_event(event)
        self.assertIsNone(tracking.binding, "disabled scanner must not bind events")

        reset_settings(scanner_enabled=True, scan_required=True, scan_start=False)
        event2 = tracking.add_event("QR-2")
        executor.handle_scan_event(event2)
        self.assertIsNotNone(tracking.binding, "enabled scanner still binds events")


class CommSettingsApiTests(unittest.TestCase):
    def setUp(self):
        prepare_template()
        reset_settings()
        self.client = TestClient(webui_server.app)
        self.client.__enter__()
        serial_scanner.set_scan_port(None)
        modbus_utils.set_instrument_port(None)
        self.addCleanup(lambda: serial_scanner.set_scan_port(None))
        self.addCleanup(lambda: modbus_utils.set_instrument_port(None))
        self.addCleanup(
            lambda: line_runtime.update_line_settings(
                scanner_enabled=True, scanner_port="", instrument_port=""))

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def _busy_executor(self):
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.write_gate = threading.Event()
        executor = TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=lambda cycle: None,
            scanner_module=FakeScanner(), persist_enabled=False,
        )
        program_selector.set_test_executor(executor)
        executor.set_pending_test_context(product_model="M1", program1=1)
        ok, error, _ = executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok, error)
        self.assertTrue(adapter.write_entered.wait(2.0))
        return executor, adapter

    def _release(self, executor, adapter):
        adapter.write_gate.set()
        adapter.write_gate = None
        deadline = time.time() + 5.0
        while time.time() < deadline and executor._manager.is_busy():
            time.sleep(0.005)

    def test_active_cycle_rejects_communication_change_without_persist(self):
        executor, adapter = self._busy_executor()
        try:
            before = settings_bytes()
            response = self.client.post("/api/line_settings",
                                        json={"scanner_port": "COM3"}).json()
            self.assertFalse(response["success"])
            self.assertEqual(response["error_code"], "TEST_ACTIVE")
            self.assertEqual(settings_bytes(), before)
            self.assertEqual(line_runtime.get_line_settings()["scanner_port"], "")
        finally:
            self._release(executor, adapter)

    def test_active_cycle_allows_switch_only_change(self):
        executor, adapter = self._busy_executor()
        try:
            response = self.client.post("/api/line_settings",
                                        json={"printer_enabled": False}).json()
            self.assertTrue(response["success"], response)
            self.assertFalse(response["settings"]["printer_enabled"])
        finally:
            self._release(executor, adapter)

    def test_idle_port_change_applies_and_persists(self):
        response = self.client.post("/api/line_settings",
                                    json={"scanner_port": "COM3",
                                          "instrument_port": "COM1"}).json()
        self.assertTrue(response["success"], response)
        self.assertEqual(response["settings"]["scanner_port"], "COM3")
        self.assertEqual(response["settings"]["instrument_port"], "COM1")
        self.assertNotIn("restart_required", response)
        self.assertEqual(serial_scanner.get_scan_port(), "COM3")
        self.assertEqual(modbus_utils.get_instrument_port(), "COM1")
        # persisted across a fresh read
        self.assertEqual(line_runtime.get_line_settings()["instrument_port"], "COM1")

    def test_malformed_port_api_rejected_without_persist(self):
        before = settings_bytes()
        response = self.client.post("/api/line_settings",
                                    json={"scanner_port": "COM0"}).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "SETTINGS_INVALID")
        self.assertEqual(settings_bytes(), before)

    def test_restart_required_when_scanner_module_unavailable(self):
        original = webui_server._scanner_module
        webui_server._scanner_module = lambda: None
        try:
            response = self.client.post("/api/line_settings",
                                        json={"scanner_port": "COM4"}).json()
        finally:
            webui_server._scanner_module = original
        self.assertTrue(response["success"], response)
        self.assertTrue(response.get("restart_required"))
        self.assertEqual(line_runtime.get_line_settings()["scanner_port"], "COM4")

    def test_disabled_scanner_clears_binding_via_api(self):
        reset_settings(scanner_enabled=True, scan_required=True)
        serial_scanner.set_event_callback(None)
        serial_scanner.create_manual_binding("QR-STALE")
        response = self.client.post("/api/line_settings",
                                    json={"scanner_enabled": False}).json()
        self.assertTrue(response["success"], response)
        self.assertFalse(response["settings"]["scanner_enabled"])
        self.assertFalse(response["settings"]["scan_required"])
        self.assertFalse(response["settings"]["scan_start"])
        self.assertIsNone(serial_scanner.get_binding(),
                          "disabling scanner must clear the stale binding")

    def test_scan_start_rejected_when_scanner_disabled(self):
        reset_settings(scanner_enabled=False)
        response = self.client.post("/api/line_settings",
                                    json={"scan_start": True}).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "SCANNER_DISABLED")
        self.assertFalse(line_runtime.get_line_settings()["scan_start"])

    def test_legacy_payload_shape_still_works(self):
        response = self.client.post("/api/line_settings", json={
            "scan_required": True, "scan_start": False, "printer_enabled": True,
        }).json()
        self.assertTrue(response["success"], response)
        settings = response["settings"]
        self.assertTrue(settings["scan_required"])
        self.assertTrue(settings["printer_enabled"])
        # additive keys present, legacy keys unchanged
        self.assertIn("scanner_enabled", settings)
        self.assertIn("scanner_port", settings)
        self.assertIn("instrument_port", settings)

    def test_com_ports_endpoint_is_get_only(self):
        response = self.client.get("/api/com_ports").json()
        self.assertTrue(response["success"])
        self.assertIsInstance(response["ports"], list)
        post = self.client.post("/api/com_ports")
        self.assertEqual(post.status_code, 405)


if __name__ == "__main__":
    unittest.main()
