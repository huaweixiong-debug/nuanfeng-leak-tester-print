#!/usr/bin/env python3
"""FIX-1 findings regression tests.

Covers:
- effective-port validation (env/default fallback collisions) + malformed booleans
- cycle-admission reservation around communication saves (deterministic race test)
- instrument/scanner startup persistence in a fresh interpreter
- scanner lifecycle truthfulness (slow worker cannot be overlapped or admit events)

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

_TMP = tempfile.mkdtemp(prefix="ateq_fix1_")
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

import line_runtime  # noqa: E402
import program_selector  # noqa: E402
import serial_scanner  # noqa: E402
import webui_server  # noqa: E402
from cycle_state import CycleManager  # noqa: E402
from program_selector import TestExecutor  # noqa: E402
from tests.fakes import FakeClock, FakeScanner, ScriptedAdapter, sample  # noqa: E402


def settings_path() -> Path:
    return Path(os.environ["ATEQ_LINE_SETTINGS"])


def settings_bytes() -> bytes:
    return settings_path().read_bytes() if settings_path().exists() else b""


def reset_settings(**overrides):
    values = dict(scan_required=False, scan_start=False, printer_enabled=False,
                  scanner_enabled=True, scanner_port="", instrument_port="")
    values.update(overrides)
    return line_runtime.update_line_settings(**values)


def make_idle_executor():
    clock = FakeClock()
    adapter = ScriptedAdapter(clock)
    adapter.default_step = 65535
    adapter.default_sample = sample()
    return TestExecutor(
        io=adapter, clock=clock.now, sleeper=clock.sleep,
        manager=CycleManager(persist_enabled=False),
        recorder=lambda cycle: None, printer=lambda cycle: None,
        scanner_module=FakeScanner(), persist_enabled=False,
    )


class EffectivePortValidationTests(unittest.TestCase):
    def setUp(self):
        reset_settings()
        self.client = TestClient(webui_server.app)
        self.client.__enter__()
        self.addCleanup(
            lambda: line_runtime.update_line_settings(
                scanner_enabled=True, scanner_port="", instrument_port=""))
        self.addCleanup(lambda: serial_scanner.set_scan_port(None))

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_scanner_port_colliding_with_instrument_default_rejected(self):
        # persisted instrument_port "" means COM8 (env default) -> COM8 collision
        before = settings_bytes()
        response = self.client.post("/api/line_settings",
                                    json={"scanner_port": "COM8"}).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "SETTINGS_INVALID")
        self.assertIn("COM8", response["message"])
        self.assertEqual(settings_bytes(), before)

    def test_instrument_port_colliding_with_scanner_default_rejected(self):
        # persisted scanner_port "" means COM7 (env default) -> COM7 collision
        before = settings_bytes()
        response = self.client.post("/api/line_settings",
                                    json={"instrument_port": "COM7"}).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "SETTINGS_INVALID")
        self.assertIn("COM7", response["message"])
        self.assertEqual(settings_bytes(), before)

    def test_disabled_scanner_does_not_block_instrument_same_port(self):
        # Disabled scanner: an equal (unused) scanner port must not reject.
        response = self.client.post("/api/line_settings", json={
            "scanner_enabled": False}).json()
        self.assertTrue(response["success"], response)
        settings = line_runtime.update_line_settings(scanner_port="COM8",
                                                     instrument_port="COM8")
        self.assertFalse(settings["scanner_enabled"])
        self.assertEqual(settings["scanner_port"], "COM8")

    def test_explicit_default_port_is_not_a_comm_change(self):
        # scanner_port "" (COM7 default) -> explicit COM7 must be persisted as a
        # non-communication edit (no reservation requirement, no restart flag).
        response = self.client.post("/api/line_settings",
                                    json={"scanner_port": "COM7"}).json()
        self.assertTrue(response["success"], response)
        self.assertEqual(response["settings"]["scanner_port"], "COM7")
        self.assertNotIn("restart_required", response)

    def test_malformed_booleans_rejected_without_write(self):
        before = settings_bytes()
        for payload in ({"scanner_enabled": {"bad": 1}},
                        {"scan_required": "maybe"},
                        {"printer_enabled": []},
                        {"scan_start": "2"}):
            with self.subTest(payload=payload):
                response = self.client.post("/api/line_settings",
                                            json=payload).json()
                self.assertFalse(response["success"], payload)
                self.assertEqual(response["error_code"], "SETTINGS_INVALID")
                self.assertEqual(settings_bytes(), before)

    def test_legacy_boolean_forms_still_accepted(self):
        response = self.client.post("/api/line_settings",
                                    json={"printer_enabled": "true"}).json()
        self.assertTrue(response["success"], response)
        self.assertTrue(response["settings"]["printer_enabled"])
        response = self.client.post("/api/line_settings",
                                    json={"printer_enabled": 0}).json()
        self.assertTrue(response["success"], response)
        self.assertFalse(response["settings"]["printer_enabled"])


class CommReservationRaceTests(unittest.TestCase):
    """Deterministic proof that a cycle cannot be admitted mid comm-save."""

    def setUp(self):
        reset_settings()
        self.executor = make_idle_executor()
        program_selector.set_test_executor(self.executor)
        self.client = TestClient(webui_server.app)
        self.client.__enter__()
        self.addCleanup(
            lambda: line_runtime.update_line_settings(
                scanner_enabled=True, scanner_port="", instrument_port=""))
        self.addCleanup(lambda: serial_scanner.set_scan_port(None))

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_admission_refused_while_comm_save_paused_then_allowed(self):
        entered = threading.Event()
        release = threading.Event()
        original_apply = webui_server._apply_communication_settings

        def paused_apply(previous, settings):
            entered.set()
            if not release.wait(10.0):
                raise AssertionError("test did not release the paused apply")
            return original_apply(previous, settings)

        webui_server._apply_communication_settings = paused_apply
        self.addCleanup(
            lambda: setattr(webui_server, "_apply_communication_settings",
                            original_apply))

        results = {}

        def save():
            results["response"] = self.client.post(
                "/api/line_settings", json={"scanner_port": "COM3"}).json()

        worker = threading.Thread(target=save, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(10.0), "save never reached the apply phase")
        try:
            # The reservation is held: real admission paths must refuse.
            self.executor.set_pending_test_context(product_model="M1", program1=1)
            ok, error, _ = self.executor.start_cycle(
                {"product_model": "M1", "program1": 1, "program2": 0}, "software")
            self.assertFalse(ok, "admission must be refused during comm save")
            self.assertEqual(error, "DEVICE_BUSY")
            cycle, err = self.executor._manager.admit(
                {"product_model": "M1", "program1": 1}, "software", [1])
            self.assertIsNone(cycle)
            self.assertEqual(err, "DEVICE_BUSY")
        finally:
            release.set()
            worker.join(10.0)
        response = results["response"]
        self.assertTrue(response["success"], response)
        self.assertEqual(response["settings"]["scanner_port"], "COM3")
        self.assertFalse(self.executor._manager.external_reserved(),
                         "reservation must be released after the save")

    def test_refused_reservation_leaves_file_byte_identical(self):
        self.assertTrue(self.executor._manager.reserve_external())
        try:
            before = settings_bytes()
            response = self.client.post("/api/line_settings",
                                        json={"scanner_port": "COM4"}).json()
            self.assertFalse(response["success"])
            self.assertEqual(response["error_code"], "TEST_ACTIVE")
            self.assertEqual(settings_bytes(), before)
        finally:
            self.executor._manager.release_external()

    def test_unavailable_executor_fails_closed_without_write(self):
        original = webui_server._executor

        def broken():
            raise RuntimeError("executor unavailable")

        webui_server._executor = broken
        self.addCleanup(lambda: setattr(webui_server, "_executor", original))
        before = settings_bytes()
        response = self.client.post("/api/line_settings",
                                    json={"instrument_port": "COM1"}).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "TEST_ACTIVE")
        self.assertEqual(settings_bytes(), before)

    def test_switch_only_change_needs_no_reservation(self):
        self.assertTrue(self.executor._manager.reserve_external())
        try:
            response = self.client.post("/api/line_settings",
                                        json={"printer_enabled": False}).json()
            self.assertTrue(response["success"], response)
            self.assertFalse(response["settings"]["printer_enabled"])
        finally:
            self.executor._manager.release_external()


class StartupPersistenceTests(unittest.TestCase):
    """Persisted ports must be effective in a fresh interpreter before any open."""

    SUBPROCESS_CODE = r'''
import json, os
os.environ["ATEQ_DISABLE_HARDWARE"] = "1"
os.environ["ATEQ_LINE_SETTINGS"] = r"{settings}"
import serial as real_serial
captured = {}
class FakeSerial:
    def __init__(self, *args, **kwargs):
        captured.update(kwargs)
        self.is_open = True
        self.timeout = kwargs.get("timeout", 1)
    def close(self):
        self.is_open = False
    def reset_input_buffer(self):
        pass
    def write(self, data):
        return len(data)
    def flush(self):
        pass
    def read(self, size):
        return b""
real_serial.Serial = FakeSerial
from fastapi.testclient import TestClient
import webui_server
client = TestClient(webui_server.app)
client.__enter__()
import modbus_utils
modbus_utils._ensure_serial_open(0.1)
import serial_scanner
result = {
    "captured_port": captured.get("port"),
    "effective_instrument": modbus_utils.get_instrument_port(),
    "effective_scanner": serial_scanner.get_scan_port(),
}
client.__exit__(None, None, None)
print("RESULT=" + json.dumps(result))
'''

    def _run(self, settings: dict) -> dict:
        tmp = tempfile.mkdtemp(prefix="ateq_fix1_sub_")
        path = Path(tmp) / "line_settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        code = self.SUBPROCESS_CODE.replace("{settings}", str(path))
        project = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = str(project)
        env["ATEQ_DISABLE_HARDWARE"] = "1"
        env["ATEQ_LINE_SETTINGS"] = str(path)
        result = subprocess.run([sys.executable, "-c", code], cwd=str(project),
                                env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        line = [l for l in result.stdout.splitlines() if l.startswith("RESULT=")][-1]
        return json.loads(line[len("RESULT="):])

    def test_saved_instrument_port_is_effective_after_restart(self):
        data = self._run({
            "scan_required": False, "printer_enabled": False, "scan_start": False,
            "scanner_enabled": True, "scanner_port": "COM3",
            "instrument_port": "COM5",
        })
        self.assertEqual(data["effective_instrument"], "COM5")
        self.assertEqual(data["captured_port"], "COM5",
                         "the first serial open must use the saved instrument port")
        self.assertEqual(data["effective_scanner"], "COM3")

    def test_cleared_instrument_port_falls_back_to_default(self):
        data = self._run({
            "scan_required": False, "printer_enabled": False, "scan_start": False,
            "scanner_enabled": True, "scanner_port": "",
            "instrument_port": "",
        })
        self.assertEqual(data["effective_instrument"], "COM8")
        self.assertEqual(data["captured_port"], "COM8")
        self.assertEqual(data["effective_scanner"], "COM7")


class ScannerLifecycleTests(unittest.TestCase):
    """A slow old worker must not be overlapped or admit stale events."""

    class _SlowFakeSerial:
        created = threading.Event()
        first_read_delay = 0.4

        def __init__(self, *args, **kwargs):
            type(self).created.set()
            self._first = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def reset_input_buffer(self):
            pass

        def read(self, size):
            if self._first:
                self._first = False
                time.sleep(type(self).first_read_delay)
            return b""

    class _FastFakeSerial(_SlowFakeSerial):
        first_read_delay = 0.0

    def setUp(self):
        serial_scanner.stop_serial_scanner(timeout=1.0)
        # fresh module state even if a prior test left the sticky flag
        serial_scanner._RECONFIG_PENDING = False
        serial_scanner._STARTED = False
        serial_scanner._THREAD = None
        serial_scanner._STOP_EVENT.clear()
        serial_scanner.clear_pending_events("test setup")
        self.original_serial = serial_scanner.serial.Serial
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        serial_scanner.serial.Serial = self.original_serial
        serial_scanner.stop_serial_scanner(timeout=1.0)
        serial_scanner._RECONFIG_PENDING = False
        serial_scanner._STARTED = False
        serial_scanner._THREAD = None
        serial_scanner._STOP_EVENT.clear()
        serial_scanner.clear_pending_events("test cleanup")

    def _wait_until_stopped(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            thread = serial_scanner._THREAD
            if thread is None or not thread.is_alive():
                return True
            time.sleep(0.01)
        return False

    def test_slow_worker_blocks_restart_and_stale_admission(self):
        self._SlowFakeSerial.created = threading.Event()
        serial_scanner.serial.Serial = self._SlowFakeSerial
        self.assertTrue(serial_scanner.start_serial_scanner())
        self.assertTrue(self._SlowFakeSerial.created.wait(5.0))
        stopped = serial_scanner.stop_serial_scanner(timeout=0.1)
        self.assertFalse(stopped, "slow worker cannot be confirmed stopped")
        self.assertTrue(serial_scanner.scanner_running(),
                        "thread state must stay truthful while alive")
        self.assertTrue(serial_scanner.reconfig_pending())
        # no second worker may start while the old one lives or after a failed stop
        self.assertFalse(serial_scanner.start_serial_scanner())
        # stale events (device or manual) must not become admission credentials
        self.assertIsNone(serial_scanner.create_manual_binding("QR-STALE"))
        self.assertIsNone(serial_scanner.publish_scan_for_test(b"QR-RAW"))
        self.assertTrue(self._wait_until_stopped())
        # sticky until process restart: still refused after the worker finally dies
        self.assertFalse(serial_scanner.start_serial_scanner())
        self.assertIsNone(serial_scanner.create_manual_binding("QR-AFTER"))

    def test_clean_stop_confirms_termination_before_restart(self):
        self._FastFakeSerial.created = threading.Event()
        serial_scanner.serial.Serial = self._FastFakeSerial
        self.assertTrue(serial_scanner.start_serial_scanner())
        self.assertTrue(self._FastFakeSerial.created.wait(5.0))
        self.assertTrue(serial_scanner.stop_serial_scanner(timeout=2.0))
        self.assertFalse(serial_scanner.scanner_running())
        self.assertFalse(serial_scanner.reconfig_pending())
        self.assertTrue(serial_scanner.start_serial_scanner())
        self.assertTrue(serial_scanner.stop_serial_scanner(timeout=2.0))
        self.assertFalse(serial_scanner.scanner_running())

    def test_clear_pending_events_drops_binding_and_queue(self):
        binding = serial_scanner.create_manual_binding("QR-KEEP")
        self.assertIsNotNone(binding)
        self.assertIsNotNone(serial_scanner.get_binding())
        serial_scanner.clear_pending_events("reconfigured")
        self.assertIsNone(serial_scanner.get_binding())
        event = serial_scanner.publish_scan_for_test(b"QR-NEW\r\n")
        self.assertIsNotNone(event)
        serial_scanner.clear_pending_events("again")
        self.assertIsNone(serial_scanner.get_event(event.event_id))


if __name__ == "__main__":
    unittest.main()
