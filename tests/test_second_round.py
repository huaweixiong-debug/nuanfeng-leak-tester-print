#!/usr/bin/env python3
"""Failing-first regressions for the second repair round (B1..B4 + COM8).

All tests are offline: fake transport / temporary SQLite / injected print runner.
"""

from collections import deque
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_round2_")
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
import serial_generator  # noqa: E402
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


class B1StopSendCommitTests(unittest.TestCase):
    """B1: no FC06/ON frame may commit once stop has returned."""

    def setUp(self):
        self.device = FakeRTUDevice(station=STATION_ID)
        modbus_utils.set_transport_override(self.device.handle)
        self.gates = set()

    def tearDown(self):
        modbus_utils.clear_transport_override()

    def _frames(self, station_func_prefix, address=None):
        found = []
        for _seq, frame in self.device.commands:
            upper = frame.upper()
            if not upper.startswith(station_func_prefix):
                continue
            if address is not None and upper[4:8] != address:
                continue
            found.append(frame)
        return found

    def test_stop_before_fc06_commit_blocks_program_write(self):
        entered = threading.Event()
        gate = threading.Event()

        def on_frame(frame_hex):
            upper = frame_hex.upper()
            if upper.startswith(f"{STATION_ID:02X}03") and not entered.is_set():
                entered.set()
                gate.wait(8.0)

        self.device.on_frame = on_frame
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        executor = TestExecutor(io=program_selector.DeviceAdapter(),
                                clock=clock.now, sleeper=clock.sleep,
                                manager=CycleManager(persist_enabled=False),
                                recorder=FakeRecorder(), printer=FakePrinter(),
                                scanner_module=serial_scanner, persist_enabled=False)
        ok, error, _ = executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok, error)
        self.assertTrue(entered.wait(2.0), "worker must reach the pre-write read")
        response = executor.request_stop()
        self.assertTrue(response["cancel_accepted"])
        entered_before = len(self.device.commands)
        self.device.step_code = 65535
        gate.set()
        self.assertTrue(wait_idle(executor))
        writes = self._frames(f"{STATION_ID:02X}06", address="0200")
        self.assertEqual(writes, [], "stop returned, so FC06 must not commit")
        starts = self._frames(f"{STATION_ID:02X}05", address="0001")
        self.assertEqual(starts, [])

    def test_stop_while_waiting_bus_blocks_fc06_and_on(self):
        clock = FakeClock()
        executor = TestExecutor(io=program_selector.DeviceAdapter(),
                                clock=clock.now, sleeper=clock.sleep,
                                manager=CycleManager(persist_enabled=False),
                                recorder=FakeRecorder(), printer=FakePrinter(),
                                scanner_module=serial_scanner, persist_enabled=False)
        executor._command_lock.acquire()
        try:
            ok, _, _ = executor.start_cycle(
                {"product_model": "M1", "program1": 1, "program2": 0}, "software")
            self.assertTrue(ok)
            response = executor.request_stop()
            self.assertTrue(response["cancel_accepted"])
        finally:
            executor._command_lock.release()
        self.device.step_code = 65535
        self.assertTrue(wait_idle(executor))
        self.assertEqual(self._frames(f"{STATION_ID:02X}06", "0200"), [])
        self.assertEqual(self._frames(f"{STATION_ID:02X}05", "0001"), [])

    def test_stop_during_p2_readback_blocks_on_pulse(self):
        p2_written = threading.Event()
        readback_entered = threading.Event()
        gate = threading.Event()

        def on_frame(frame_hex):
            upper = frame_hex.upper()
            if upper.startswith(f"{STATION_ID:02X}06") and upper[4:8] == "0200":
                p2_written.set()
                return
            if p2_written.is_set() and upper.startswith(f"{STATION_ID:02X}03"):
                readback_entered.set()
                gate.wait(8.0)

        self.device.on_frame = on_frame
        clock = FakeClock()
        executor = TestExecutor(io=program_selector.DeviceAdapter(),
                                clock=clock.now, sleeper=clock.sleep,
                                manager=CycleManager(persist_enabled=False),
                                recorder=FakeRecorder(), printer=FakePrinter(),
                                scanner_module=serial_scanner, persist_enabled=False)
        manager = executor._manager
        cycle, _ = manager.admit({"program1": 1, "program2": 2}, "software", [1, 2])
        chain = threading.Thread(target=executor._chain_program2, args=(cycle,))
        chain.start()
        self.assertTrue(readback_entered.wait(3.0), "must block on P2 readback")
        response = executor.request_stop()
        self.assertTrue(response["cancel_accepted"])
        self.device.step_code = 65535
        gate.set()
        chain.join(6.0)
        self.assertTrue(wait_idle(executor))
        self.assertEqual(self._frames(f"{STATION_ID:02X}05", "0001"), [],
                         "no start pulse after stop while P2 readback in flight")

    def test_start_test_respects_cancel_predicate_without_frames(self):
        wants_cancel = {"value": True}
        result = program_selector.start_test(
            cancel_check=lambda: wants_cancel["value"])
        self.assertFalse(result)
        self.assertEqual(self._frames(f"{STATION_ID:02X}05", "0001"), [])

    def test_write_program_respects_cancel_predicate_without_fc06(self):
        ok, message = program_selector.write_program(
            3, cancel_check=lambda: True)
        self.assertFalse(ok)
        self.assertEqual(self._frames(f"{STATION_ID:02X}06", "0200"), [])


class B2FairArbitrationTests(unittest.TestCase):
    """B2: reset waiters must deterministically outrank normal waiters."""

    def _executor(self):
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.default_step = 65535
        adapter.default_sample = sample()
        return TestExecutor(io=adapter, clock=clock.now, sleeper=clock.sleep,
                            manager=CycleManager(persist_enabled=False),
                            recorder=FakeRecorder(), printer=FakePrinter(),
                            scanner_module=serial_scanner, persist_enabled=False)

    def test_reset_arriving_after_normal_still_goes_first(self):
        executor = self._executor()
        gate = executor._bus_gate
        holder_ready = threading.Event()
        order = []
        holder_release = threading.Event()

        def external_holder():
            gate.mutex.acquire()
            holder_ready.set()
            holder_release.wait(8.0)
            gate.mutex.release()

        threading.Thread(target=external_holder, daemon=True).start()
        self.assertTrue(holder_ready.wait(2.0))

        normal_started = threading.Event()

        def normal():
            with gate.hold(priority=False, timeout=6.0) as acquired:
                if acquired:
                    order.append("normal")

        def reset():
            with gate.hold(priority=True, timeout=6.0) as acquired:
                if acquired:
                    order.append("reset")

        normal_thread = threading.Thread(target=normal)
        normal_thread.start()
        self.assertTrue(gate.wait_for_queue(1, timeout=2.0),
                        "normal waiter must register")
        reset_thread = threading.Thread(target=reset)
        reset_thread.start()
        self.assertTrue(gate.wait_for_queue(2, timeout=2.0),
                        "reset waiter must register while mutex is held")
        holder_release.set()
        normal_thread.join(6.0)
        reset_thread.join(6.0)
        self.assertEqual(order, ["reset", "normal"],
                         "reset waiter must be granted before the earlier normal")

    def test_high_concurrency_normals_do_not_starve_reset(self):
        executor = self._executor()
        gate = executor._bus_gate
        holder_ready = threading.Event()
        holder_release = threading.Event()
        order = []
        lock = threading.Lock()

        def external_holder():
            gate.mutex.acquire()
            holder_ready.set()
            holder_release.wait(10.0)
            gate.mutex.release()

        threading.Thread(target=external_holder, daemon=True).start()
        self.assertTrue(holder_ready.wait(2.0))

        def normal():
            with gate.hold(priority=False, timeout=8.0) as acquired:
                if acquired:
                    with lock:
                        order.append("normal")

        def reset():
            with gate.hold(priority=True, timeout=8.0) as acquired:
                if acquired:
                    with lock:
                        order.append("reset")

        normals = [threading.Thread(target=normal) for _ in range(8)]
        for t in normals:
            t.start()
        self.assertTrue(gate.wait_for_queue(8, timeout=3.0))
        reset_thread = threading.Thread(target=reset)
        reset_thread.start()
        self.assertTrue(gate.wait_for_queue(9, timeout=3.0))
        holder_release.set()
        reset_thread.join(8.0)
        for t in normals:
            t.join(8.0)
        self.assertEqual(len(order), 9)
        self.assertEqual(order[0], "reset", "reset must be first after release")

    def test_timed_out_waiter_does_not_block_bus(self):
        executor = self._executor()
        gate = executor._bus_gate
        gate.mutex.acquire()
        timed_out = []

        def waiter():
            with gate.hold(priority=False, timeout=0.15) as acquired:
                timed_out.append(acquired)

        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertTrue(gate.wait_for_queue(1, timeout=2.0))
        thread.join(3.0)
        gate.mutex.release()
        self.assertEqual(timed_out, [False])
        self.assertEqual(gate.queue_length(), 0,
                         "timed-out waiter token must be removed")
        with gate.hold(priority=False, timeout=1.0) as acquired:
            self.assertTrue(acquired, "bus must be usable after a timed-out waiter")


class B3OptionalScanContextTests(unittest.TestCase):
    """B3: optional scan without a current binding must freeze a blank QR."""

    def setUp(self):
        reset_scanner_state(serial_scanner)

    def tearDown(self):
        reset_scanner_state(serial_scanner)

    def _executor(self, settings=None):
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.default_step = 65535
        adapter.default_sample = sample()
        adapter.default_readback = 1
        return TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=FakePrinter(),
            settings_provider=lambda: (settings or {"scan_required": False,
                                                    "scan_start": False}),
            scanner_module=serial_scanner, persist_enabled=False), adapter

    def test_optional_scan_no_binding_ignores_stale_pending_qr(self):
        executor, adapter = self._executor()
        executor.set_pending_test_context(product_model="M", program1=1,
                                          qr_code="OLD-PART")
        executor._handle_hardware_sample({"step_code": 65535, "status": 0})
        executor._handle_hardware_sample(
            {"step_code": 6, "actual_program": 1, "status": 1,
             "pressure": 2, "leak": 3})
        cycle = executor._manager.get_active()
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle.context["qr_code"], "",
                         "no binding means no QR; stale pending must not be reused")

    def test_scan_required_true_no_binding_rejects_and_resets(self):
        executor, adapter = self._executor(
            settings={"scan_required": True, "scan_start": False})
        executor.set_pending_test_context(product_model="M", program1=1,
                                          qr_code="OLD-PART")
        executor._handle_hardware_sample({"step_code": 65535, "status": 0})
        executor._handle_hardware_sample(
            {"step_code": 6, "actual_program": 1, "status": 1,
             "pressure": 2, "leak": 3})
        # 该扫码没扫码而硬件启动: 拒绝周期并通过 Modbus 复位仪器
        self.assertIsNone(executor._manager.get_active())
        deadline = time.time() + 3
        state = None
        while time.time() < deadline:
            state = executor._manager.get_reset_state()['state']
            if state == program_selector.RESET_CONFIRMED:
                break
            time.sleep(0.02)
        self.assertEqual(state, program_selector.RESET_CONFIRMED)
        self.assertGreaterEqual(adapter.reset_calls, 1)
        self.assertIsNone(executor._manager.get_active())

    def test_new_binding_wins_over_stale_pending(self):
        executor, adapter = self._executor()
        serial_scanner.create_manual_binding("NEW-PART")
        executor.set_pending_test_context(product_model="M", program1=1,
                                          qr_code="OLD-PART")
        executor._handle_hardware_sample({"step_code": 65535, "status": 0})
        executor._handle_hardware_sample(
            {"step_code": 6, "actual_program": 1, "status": 1,
             "pressure": 2, "leak": 3})
        cycle = executor._manager.get_active()
        self.assertEqual(cycle.context["qr_code"], "NEW-PART")

    def test_completed_and_stopped_cycles_clear_pending_qr(self):
        executor, adapter = self._executor()
        adapter.step_script = deque([4, 6, 65535])
        ok, _, _ = executor.start_cycle(
            {"product_model": "M", "program1": 1, "program2": 0,
             "qr_code": "MANUAL-1", "scan_source": "manual"}, "software")
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        pending = executor.get_pending_context()
        self.assertIn(pending.get("qr_code"), (None, ""))

        executor.set_pending_test_context(product_model="M", program1=1,
                                          qr_code="STALE-2")
        ok, _, _ = executor.start_cycle(
            {"product_model": "M", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok)
        executor.request_stop()
        self.assertIn(executor.get_pending_context().get("qr_code"), (None, ""))

    def test_explicit_software_qr_is_kept_on_the_cycle(self):
        executor, adapter = self._executor()
        adapter.write_gate = threading.Event()
        ok, _, _ = executor.start_cycle(
            {"product_model": "M", "program1": 1, "program2": 0,
             "qr_code": "EXPLICIT-1", "scan_source": "manual"}, "software")
        self.assertTrue(ok)
        self.assertTrue(adapter.write_entered.wait(2.0))
        cycle = executor._manager.get_active()
        self.assertEqual(cycle.context["qr_code"], "EXPLICIT-1")
        adapter.write_gate.set()
        adapter.write_gate = None
        self.assertTrue(wait_idle(executor))


class B4PersistentPrintAdmissionTests(unittest.TestCase):
    """B4: record-level print admission must be durable and race-free."""

    def setUp(self):
        prepare_template()
        self.runner = FakePrintRunner()
        bartender_print.set_runner(self.runner)
        line_runtime.update_line_settings(scan_required=False, scan_start=False,
                                          printer_enabled=True)
        db_path = Path(os.environ["ATEQ_TEST_DB"])
        if db_path.exists():
            db_path.unlink()
        database._INITIALIZED = False
        database.init_database()
        serial_generator.reset_generator()
        store = Path(os.environ["ATEQ_PRODUCT_CONFIG"])
        if store.exists():
            store.unlink()
        product_config.replace_products(
            [{"id": 1, "model": "M1", "program1": 1, "program2": 0,
              "labelTemplate": "", "supplierCode": ""}], revision=0)
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.default_step = 65535
        adapter.default_sample = sample()
        self.adapter = adapter
        self.executor = TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=program_selector._default_record_saver,
            printer=FakePrinter(),
            scanner_module=serial_scanner, persist_enabled=False)
        self.executor._printer = self.executor._default_print
        program_selector.set_test_executor(self.executor)
        self.client = TestClient(webui_server.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        bartender_print.set_runner(None)

    def _run_pass_cycle(self, cycle_id_out=None):
        self.adapter.step_script = deque([4, 6, 65535])
        ok, error, cycle_id = self.executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok, error)
        return cycle_id

    def test_manual_between_insert_and_auto_submit_is_busy_single_submit(self):
        real_save = database.save_record_ex
        inserted = threading.Event()
        release = threading.Event()

        def blocking_save(**kwargs):
            result = real_save(**kwargs)
            inserted.set()
            release.wait(8.0)
            return result

        database.save_record_ex = blocking_save
        try:
            cycle_id = self._run_pass_cycle()
            self.assertTrue(inserted.wait(4.0), "record must be inserted first")
            record = database.find_record_by_cycle(cycle_id)
            self.assertIsNotNone(record)
            manual = self.client.post("/api/print_label",
                                      json={"record_id": record["id"]}).json()
            self.assertEqual(manual.get("state"), "busy",
                             "manual reprint must not pass the pending auto job")
            self.assertEqual(len(self.runner.calls), 0)
        finally:
            database.save_record_ex = real_save
            release.set()
        self.assertTrue(wait_idle(self.executor))
        self.assertEqual(len(self.runner.calls), 1)
        record = database.find_record_by_cycle(cycle_id)
        self.assertEqual(record["print_state"], "submitted")

    def test_manual_during_auto_processing_is_busy(self):
        runner_gate = threading.Event()
        runner_entered = threading.Event()
        original_run = self.runner.run

        def blocking_run(cmd, **kwargs):
            runner_entered.set()
            runner_gate.wait(8.0)
            return original_run(cmd, **kwargs)

        self.runner.run = blocking_run
        cycle_id = self._run_pass_cycle()
        self.assertTrue(runner_entered.wait(4.0))
        record = database.find_record_by_cycle(cycle_id)
        manual = self.client.post("/api/print_label",
                                  json={"record_id": record["id"]}).json()
        self.assertEqual(manual.get("state"), "busy")
        self.assertEqual(len(self.runner.calls), 0)
        runner_gate.set()
        self.assertTrue(wait_idle(self.executor))
        self.assertEqual(len(self.runner.calls), 1)

    def test_explicit_manual_reprint_after_auto_submit_adds_one(self):
        cycle_id = self._run_pass_cycle()
        self.assertTrue(wait_idle(self.executor))
        self.assertEqual(len(self.runner.calls), 1)
        record = database.find_record_by_cycle(cycle_id)
        response = self.client.post("/api/print_label",
                                    json={"record_id": record["id"]}).json()
        self.assertTrue(response["success"], response)
        self.assertTrue(response["printed"])
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(response["serial_number"], record["serial_number"])
        self.assertEqual(database.get_record(record["id"])["print_state"],
                         "submitted")

    def test_restart_recovery_marks_pending_unknown_without_auto_retry(self):
        record_id, _ = database.save_record_ex(
            qr_code="Q", pressure1=1.0, leak1=0.1, pressure2=None, leak2=None,
            test_result="PASS", product_model="M1", operator=None,
            serial_number="SN-RESTART-1", cycle_id="restart-cycle",
            print_state="auto_pending")
        self.assertEqual(len(self.runner.calls), 0)
        database.recover_pending_prints()
        record = database.get_record(record_id)
        self.assertEqual(record["print_state"], "unknown")
        self.assertEqual(len(self.runner.calls), 0, "no automatic retry on restart")
        response = self.client.post("/api/print_label",
                                    json={"record_id": record_id}).json()
        self.assertTrue(response["success"], response)
        self.assertEqual(len(self.runner.calls), 1)

    def test_auto_failure_state_persisted_and_manual_allowed(self):
        self.runner.returncode = 1
        cycle_id = self._run_pass_cycle()
        self.assertTrue(wait_idle(self.executor))
        record = database.find_record_by_cycle(cycle_id)
        self.assertEqual(record["print_state"], "failed")
        self.runner.returncode = 0
        response = self.client.post("/api/print_label",
                                    json={"record_id": record["id"]}).json()
        self.assertTrue(response["success"], response)
        self.assertEqual(len(self.runner.calls), 2)

    def test_save_failure_never_prints(self):
        from unittest import mock
        with mock.patch.object(database, "save_record_ex",
                               side_effect=RuntimeError("db down")):
            cycle_id = self._run_pass_cycle()
            self.assertTrue(wait_idle(self.executor))
        self.assertEqual(len(self.runner.calls), 0)
        self.assertIsNone(database.find_record_by_cycle(cycle_id))


class Com8DefaultTests(unittest.TestCase):
    def test_instrument_default_is_com8(self):
        self.assertEqual(modbus_utils.SERIAL_PORT, "COM8")
        self.assertEqual(modbus_utils.BAUDRATE, 9600)
        self.assertEqual(modbus_utils.PARITY, "E")

    def test_scanner_port_is_independent_com7(self):
        self.assertEqual(serial_scanner.SCAN_PORT, "COM7")
        self.assertEqual(serial_scanner.SCAN_BAUDRATE, 9600)
        self.assertEqual(serial_scanner.SCAN_PARITY, "N")

    def test_env_overrides_are_independent(self):
        import subprocess
        import sys
        project = Path(__file__).resolve().parent.parent
        code = (
            "import modbus_utils, serial_scanner;"
            "print(modbus_utils.SERIAL_PORT, serial_scanner.SCAN_PORT)"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(project)
        env["ATEQ_DISABLE_HARDWARE"] = "1"
        env["ATEQ_SERIAL_PORT"] = "COMX"
        env["ATEQ_BAUDRATE"] = "19200"
        env["SCAN_SERIAL_PORT"] = "COMY"
        env["ATEQ_SCANNER_LOG"] = str(Path(_TMP) / "scanner2.log")
        result = subprocess.run([sys.executable, "-c", code], cwd=str(project),
                                env=env, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("COMX COMY", result.stdout)

    def test_fake_serial_receives_com8_construction_parameters(self):
        """Port construction must use COM8 with 9600/8E1 without touching hardware."""
        import serial as real_serial
        import importlib

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

        original = real_serial.Serial
        real_serial.Serial = FakeSerial
        try:
            importlib.reload(modbus_utils)
            modbus_utils._send_raw_serial("FF030030000D0000", timeout=0.1)
            # the fake never answers a valid frame; we only assert construction args
            self.assertEqual(captured.get("port"), "COM8")
            self.assertEqual(captured.get("baudrate"), 9600)
            self.assertEqual(captured.get("bytesize"), 8)
            self.assertEqual(captured.get("parity"), real_serial.PARITY_EVEN)
            self.assertEqual(captured.get("stopbits"), 1)
        finally:
            real_serial.Serial = original
            importlib.reload(modbus_utils)
            modbus_utils.clear_transport_override()


if __name__ == "__main__":
    unittest.main()
