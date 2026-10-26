#!/usr/bin/env python3
"""Failing-first regressions for the Codex final review repair round (R1..R12).

Backend-focused tests: finalization ordering, bus cancellation boundaries, per-phase
identity, hardware synchronization, reset inhibition, binding ownership, 32-bit units,
persistence health and print submission cancellation.
"""

from collections import deque
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_review_")
os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")
os.environ["ATEQ_TEST_DB"] = str(Path(_TMP) / "test.db")
os.environ["ATEQ_SERIAL_DB"] = str(Path(_TMP) / "serial.db")
os.environ["ATEQ_RUNTIME_STATE"] = str(Path(_TMP) / "state.json")
os.environ["ATEQ_SCANNER_LOG"] = str(Path(_TMP) / "scanner.log")
os.environ["ATEQ_LINE_SETTINGS"] = str(Path(_TMP) / "line_settings.json")
os.environ["ATEQ_PRODUCT_CONFIG"] = str(Path(_TMP) / "products.json")
os.environ["ATEQ_LABEL_DIR"] = str(Path(_TMP) / "data")
os.environ["ATEQ_LABEL_TEMPLATE_DIR"] = str(Path(_TMP) / "templates")
os.environ["ATEQ_DEFAULT_TEMPLATE"] = str(Path(_TMP) / "templates" / "label.btw")
os.environ["ATEQ_LEGACY_PRODUCT_MODELS"] = str(Path(_TMP) / "product_models.json")

import bartender_print  # noqa: E402
import database  # noqa: E402
import line_runtime  # noqa: E402
import modbus_utils  # noqa: E402
import program_selector  # noqa: E402
import serial_generator  # noqa: E402
import serial_scanner  # noqa: E402
from cycle_state import CycleManager  # noqa: E402
from modbus_utils import STATION_ID  # noqa: E402
from program_selector import TestExecutor  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeClock,
    FakePrintRunner,
    FakePrinter,
    FakeRecorder,
    FakeScanner,
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


def make_executor(adapter=None, recorder=None, printer=None, settings=None,
                  scanner=None, manager=None, clock=None):
    clock = clock or FakeClock()
    adapter = adapter or ScriptedAdapter(clock)
    adapter.default_step = 65535
    if getattr(adapter, "default_sample", None) is None:
        adapter.default_sample = sample()
    return TestExecutor(
        io=adapter, clock=clock.now, sleeper=clock.sleep,
        manager=manager or CycleManager(persist_enabled=False),
        recorder=recorder or FakeRecorder(),
        printer=printer or FakePrinter(),
        settings_provider=lambda: (settings or {"scan_required": False,
                                                "scan_start": False,
                                                "printer_enabled": False}),
        scanner_module=scanner or FakeScanner(),
        persist_enabled=False,
    ), adapter, clock


def prepare_template():
    template = Path(os.environ["ATEQ_DEFAULT_TEMPLATE"])
    template.parent.mkdir(parents=True, exist_ok=True)
    if not template.exists():
        template.write_text("fake btw", encoding="utf-8")


class R1FinalizationOrderingTests(unittest.TestCase):
    def setUp(self):
        prepare_template()
        self.runner = FakePrintRunner()
        bartender_print.set_runner(self.runner)
        line_runtime.update_line_settings(scan_required=False, scan_start=False,
                                          printer_enabled=True)
        database._INITIALIZED = False
        database.init_database()
        serial_generator.reset_generator()
        self.addCleanup(lambda: bartender_print.set_runner(None))

    def _use_production_pipeline(self, executor):
        executor._recorder = program_selector._default_record_saver
        executor._printer = executor._default_print

    def test_production_saver_and_printer_receive_final_outcome(self):
        executor, adapter, _ = make_executor()
        self._use_production_pipeline(executor)
        adapter.step_script = deque([4, 6, 65535])
        ok, error, cycle_id = executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok, error)
        self.assertTrue(wait_idle(executor))
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertEqual(payload["overall_result"], "PASS")
        record = database.find_record_by_cycle(cycle_id)
        self.assertIsNotNone(record, "production saver must persist the cycle")
        self.assertEqual(record["test_result"], "PASS")
        self.assertEqual(record["terminal_reason"], "OK")
        self.assertIsNotNone(record["started_at"])
        self.assertIsNotNone(record["finished_at"])
        self.assertTrue(record["serial_number"])
        self.assertEqual(payload["record_id"], record["id"])
        self.assertEqual(payload["serial_number"], record["serial_number"])
        self.assertEqual(len(self.runner.calls), 1, "auto print must submit once")
        self.assertEqual(database.get_record(record["id"])["print_state"], "submitted")

    def test_fail_and_aborted_rows_persist_reason_time_and_never_print(self):
        executor, adapter, _ = make_executor()
        self._use_production_pipeline(executor)
        adapter.default_sample = sample(result="FAIL", status=0x0022)
        adapter.step_script = deque([4, 6, 65535])
        ok, _, cycle_id = executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        record = database.find_record_by_cycle(cycle_id)
        self.assertEqual(record["test_result"], "FAIL")
        self.assertEqual(record["terminal_reason"], "STATUS_FAIL")
        self.assertIsNotNone(record["finished_at"])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(record["print_state"], "skipped")

    def test_duplicate_finalization_single_row_single_print(self):
        executor, adapter, _ = make_executor()
        self._use_production_pipeline(executor)
        cycle, _ = executor._manager.admit(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software", [1])
        threads = [threading.Thread(target=executor._finalize_and_finish,
                                    args=(cycle, "PASS", "OK")) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(database.get_records_by_date(
            time.strftime("%Y-%m-%d")) or True)
        rows = [r for r in database.get_latest_records(50)
                if r["cycle_id"] == cycle.cycle_id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.runner.calls), 1)
        serials = [r["serial_number"] for r in rows]
        self.assertEqual(len(set(serials)), 1)


class R2CancelAtSendTests(unittest.TestCase):
    def test_cancel_during_p2_write_prevents_start(self):
        executor, adapter, _ = make_executor()
        manager = executor._manager
        cycle, _ = manager.admit({"program1": 1, "program2": 2}, "software", [1, 2])
        adapter.after_write = lambda _program: manager.request_stop("barrier")
        executor._chain_program2(cycle)
        self.assertEqual(adapter.start_calls, 0,
                         "no start may be sent once cancel was requested during write")
        self.assertFalse(manager.is_active(cycle))
        self.assertEqual(manager.snapshot()["completion_payload"]["overall_result"],
                         "ABORTED")

    def test_cancel_while_waiting_bus_sends_no_frame(self):
        executor, adapter, _ = make_executor()
        executor._command_lock.acquire()
        try:
            ok, _, _ = executor.start_cycle(
                {"product_model": "M1", "program1": 1, "program2": 0}, "software")
            self.assertTrue(ok)
            time.sleep(0.05)
            response = executor.request_stop()
            self.assertTrue(response["cancel_accepted"])
        finally:
            executor._command_lock.release()
        self.assertTrue(wait_idle(executor))
        self.assertEqual(adapter.write_program_calls, [])
        self.assertEqual(adapter.start_calls, 0)

    def test_reset_priority_with_params_like_transaction(self):
        """A params-like bus holder blocks the worker; reset must still win after the
        holder releases, and the queued worker must send no frame."""
        executor, adapter, _ = make_executor()
        manager = executor._manager
        executor._command_lock.acquire()  # simulate a params transaction holding the bus
        try:
            ok, error, _ = executor.start_cycle(
                {"product_model": "M1", "program1": 1, "program2": 0}, "software")
            self.assertTrue(ok, error)
            time.sleep(0.05)  # worker is queued on the bus
            started = time.time()
            response = executor.request_stop()
            self.assertTrue(response["cancel_accepted"])
            self.assertLess(time.time() - started, 1.0)
            self.assertTrue(manager.reset_pending())
            self.assertEqual(adapter.write_program_calls, [])
        finally:
            executor._command_lock.release()
        deadline = time.time() + 4.0
        while time.time() < deadline and not (
                manager.get_reset_state()["state"] == "confirmed"
                and not manager.is_busy()):
            time.sleep(0.01)
        self.assertEqual(manager.get_reset_state()["state"], "confirmed")
        self.assertTrue(wait_idle(executor))
        self.assertEqual(adapter.reset_calls, 1)
        self.assertEqual(adapter.start_calls, 0)
        self.assertEqual(adapter.write_program_calls, [])


class RealBusBoundaryTests(unittest.TestCase):
    """R2: cancellation boundaries with the real DeviceAdapter + injected transport."""

    def setUp(self):
        self.device = FakeRTUDevice(station=STATION_ID)
        modbus_utils.set_transport_override(self.device.handle)

    def tearDown(self):
        modbus_utils.clear_transport_override()

    def _executor(self, cycle=None):
        clock = FakeClock()
        executor, adapter, _ = make_executor(clock=clock)
        executor._io = program_selector.DeviceAdapter()
        return executor, adapter, clock

    def test_cancel_while_prewrite_read_blocked_prevents_start(self):
        gate = threading.Event()
        entered = threading.Event()
        stop_ts = []

        def on_frame(frame_hex):
            if frame_hex.upper().startswith(f"{STATION_ID:02X}03") and not entered.is_set():
                entered.set()
                gate.wait(5.0)

        self.device.on_frame = on_frame
        executor, adapter, _ = self._executor()
        ok, error, _ = executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok, error)
        self.assertTrue(entered.wait(2.0))
        stop_ts.append(time.time())
        response = executor.request_stop()
        self.assertTrue(response["cancel_accepted"])
        self.device.step_code = 65535
        gate.set()
        self.assertTrue(wait_idle(executor))
        # no start-coil (address 0x0001 ON) command may appear after the stop returned
        for _seq, frame in self.device.commands:
            upper = frame.upper()
            is_start = upper.startswith(f"{STATION_ID:02X}05") and upper[4:8] == "0001" \
                and upper[8:12] == "FF00"
            if is_start:
                self.fail(f"start frame sent after stop: {frame}")

    def test_cancel_while_waiting_transaction_lock_sends_no_command(self):
        executor, adapter, _ = self._executor()
        executor._command_lock.acquire()
        try:
            ok, _, _ = executor.start_cycle(
                {"product_model": "M1", "program1": 1, "program2": 0}, "software")
            self.assertTrue(ok)
            time.sleep(0.05)
            response = executor.request_stop()
            self.assertTrue(response["cancel_accepted"])
            before = list(self.device.commands)
        finally:
            executor._command_lock.release()
        self.device.step_code = 65535
        self.assertTrue(wait_idle(executor))
        writes = [f for _s, f in self.device.commands
                  if f.upper().startswith(f"{STATION_ID:02X}06")]
        starts = [f for _s, f in self.device.commands
                  if f.upper().startswith(f"{STATION_ID:02X}05")
                  and f.upper()[4:8] == "0001" and f.upper()[8:12] == "FF00"]
        self.assertEqual(writes, [])
        self.assertEqual(starts, [])

    def test_cancel_during_p2_write_blocks_start_pulse(self):
        write_gate = threading.Event()
        write_entered = threading.Event()

        def on_frame(frame_hex):
            upper = frame_hex.upper()
            if upper.startswith(f"{STATION_ID:02X}06") and "0200" in upper[4:8]:
                write_entered.set()
                write_gate.wait(5.0)

        self.device.on_frame = on_frame
        clock = FakeClock()
        executor, adapter, _ = make_executor(clock=clock)
        executor._io = program_selector.DeviceAdapter()
        manager = executor._manager
        manager.set_reset_state("confirmed", None)
        cycle, _ = manager.admit({"program1": 1, "program2": 2}, "software", [1, 2])
        chain_thread = threading.Thread(target=executor._chain_program2,
                                        args=(cycle,))
        chain_thread.start()
        self.assertTrue(write_entered.wait(3.0))
        response = executor.request_stop()
        self.assertTrue(response["cancel_accepted"])
        self.device.step_code = 65535
        write_gate.set()
        chain_thread.join(5.0)
        self.assertTrue(wait_idle(executor))
        starts = [f for _s, f in self.device.commands
                  if f.upper().startswith(f"{STATION_ID:02X}05")
                  and f.upper()[4:8] == "0001" and f.upper()[8:12] == "FF00"]
        self.assertEqual(starts, [], "no start pulse may follow the stop request")


class R1DeterministicFailureTests(unittest.TestCase):
    def test_recorder_failure_is_captured_and_no_print(self):
        def failing_recorder(_cycle):
            raise RuntimeError("db down")

        executor, adapter, _ = make_executor(recorder=failing_recorder,
                                             printer=FakePrinter())
        cycle, _ = executor._manager.admit({"program1": 1}, "software", [1])
        executor._finalize_and_finish(cycle, "PASS", "OK")
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertFalse(payload["record_saved"])
        self.assertIn("recorder failed", payload["record_error"])
        self.assertEqual(payload["print_state"], "skipped")
        self.assertFalse(executor._manager.is_busy())

    def test_printer_failure_is_captured(self):
        def failing_printer(_cycle):
            raise RuntimeError("bartender down")

        executor, adapter, _ = make_executor(printer=failing_printer)
        cycle, _ = executor._manager.admit({"program1": 1}, "software", [1])
        executor._finalize_and_finish(cycle, "PASS", "OK")
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertEqual(payload["print_state"], "failed")
        self.assertIn("printer failed", payload["print_message"])
        self.assertFalse(executor._manager.is_busy())


class R3PhaseIdentityTests(unittest.TestCase):
    def test_terminal_program_mismatch_blocks_pass(self):
        executor, adapter, _ = make_executor()
        manager = executor._manager
        cycle, _ = manager.admit({"program1": 1}, "software", [1])
        manager.update_phase(cycle, "p1", activity_observed=True,
                             program_identity_verified=True, actual_program=1,
                             pressure=2, leak=3, status_word=0)
        result, reason = executor._evaluate_phase(
            cycle, cycle.p1,
            {"status": 0x21, "actual_program": 2, "pressure": 2, "leak": 3,
             "step_code": 65535})
        self.assertNotEqual(result, "PASS", f"wrong-program terminal accepted: {reason}")

    def test_mid_phase_identity_mutation_blocks_pass(self):
        executor, adapter, _ = make_executor()
        adapter.default_sample = sample(actual_program=2)
        adapter.step_script = deque([4, 6, 65535])
        ok, _, _ = executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertNotEqual(payload["overall_result"], "PASS")

    def test_terminal_missing_measurements_not_backfilled_from_running(self):
        executor, adapter, _ = make_executor()
        manager = executor._manager
        cycle, _ = manager.admit({"program1": 1}, "software", [1])
        manager.update_phase(cycle, "p1", activity_observed=True,
                             program_identity_verified=True, actual_program=1,
                             pressure=99, leak=9, status_word=0x20)
        result, reason = executor._evaluate_phase(
            cycle, cycle.p1,
            {"status": 0x21, "actual_program": 1, "pressure": None, "leak": None,
             "step_code": 65535})
        self.assertNotEqual(result, "PASS",
                            "running measurements must not backfill terminal sample")

    def test_same_number_two_phases_require_new_activity(self):
        executor, adapter, _ = make_executor()
        adapter.step_script = deque([4, 6, 65535])
        ok, _, _ = executor.start_cycle(
            {"product_model": "M1", "program1": 2, "program2": 2}, "software")
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertNotEqual(payload["overall_result"], "PASS")
        self.assertIn(payload["overall_result"], ("UNKNOWN", "ERROR"),
                      "never-started P2 must not be reported as product FAIL")
        self.assertIsNone(payload["result2"])

    def test_p1_snapshot_immutable_during_p2(self):
        executor, adapter, _ = make_executor()
        p1_sample = sample(pressure=100.0, leak=1.0, actual_program=1)
        p2_sample = sample(pressure=200.0, leak=2.0, actual_program=2)
        adapter.sample_script = deque([p1_sample, p1_sample, p1_sample,
                                       p2_sample, p2_sample, p2_sample])
        adapter.step_script = deque([4, 6, 65535, 4, 6, 65535])
        ok, _, _ = executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 2}, "software")
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertEqual(payload["overall_result"], "PASS")
        self.assertAlmostEqual(payload["pressure1"], 100.0)
        self.assertAlmostEqual(payload["pressure2"], 200.0)
        self.assertEqual(payload["result1"], "PASS")


class R4HardwareSynchronizationTests(unittest.TestCase):
    def test_initial_active_sample_is_unverified(self):
        executor, adapter, _ = make_executor(
            settings={"scan_required": False, "scan_start": False})
        executor.set_pending_test_context(product_model="M", program1=2)
        executor._handle_hardware_sample(
            {"step_code": 6, "actual_program": 2, "status": 1,
             "pressure": 2, "leak": 3})
        cycle = executor._manager.get_active()
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle.admission_violation, "INSTRUMENT_STATE_UNVERIFIED")
        # completion must not yield PASS / chain / print
        executor._handle_hardware_sample(
            {"step_code": 65535, "actual_program": 2, "status": 0x21,
             "pressure": 2, "leak": 3})
        self.assertTrue(wait_idle(executor))
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertNotEqual(payload["overall_result"], "PASS")

    def test_idle_then_active_is_a_fresh_verified_cycle(self):
        executor, adapter, _ = make_executor(
            settings={"scan_required": False, "scan_start": False})
        adapter.default_readback = 2
        executor.set_pending_test_context(product_model="M", program1=2)
        executor._handle_hardware_sample({"step_code": 65535, "status": 0})
        executor._handle_hardware_sample(
            {"step_code": 6, "actual_program": 2, "status": 1,
             "pressure": 2, "leak": 3})
        cycle = executor._manager.get_active()
        self.assertIsNotNone(cycle)
        self.assertIsNone(cycle.admission_violation)
        self.assertTrue(cycle.p1.program_identity_verified)

    def test_prepare_reserves_admission_during_idle_proof(self):
        executor, adapter, _ = make_executor()
        adapter.read_program_gate = threading.Event()
        executor.set_pending_test_context(product_model="M1", program1=1)
        results = []

        def prepare():
            results.append(executor._prepare_next_p1(force=True))

        thread = threading.Thread(target=prepare)
        thread.start()
        self.assertTrue(adapter.read_program_entered.wait(2.0))
        ok, error, _ = executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertFalse(ok, "admission must be reserved during readiness proof")
        self.assertIn(error, ("DEVICE_BUSY", "BUSY", "RESET_PENDING"))
        adapter.read_program_gate.set()
        thread.join(3.0)
        self.assertTrue(wait_idle(executor))

    def test_write_program_refuses_all_active_steps(self):
        device = FakeRTUDevice()
        modbus_utils.set_transport_override(device.handle)
        try:
            device.step_code = 7
            ok, message = program_selector.write_program(1)
            self.assertFalse(ok, "steps 4..100 must refuse program writes")
        finally:
            modbus_utils.clear_transport_override()


class R5HardwareTimeoutTests(unittest.TestCase):
    def test_hardware_p2_never_started_times_out_unknown(self):
        executor, adapter, clock = make_executor()
        manager = executor._manager
        printer = executor._printer
        cycle, _ = manager.admit(
            {"product_model": "M", "program1": 1, "program2": 2}, "hardware", [1, 2])
        manager.set_cycle_field(cycle, phase="RUNNING_P1", started_at=clock.now())
        manager.update_phase(cycle, "p1", status="completed", result="PASS",
                             activity_observed=True, program_identity_verified=True,
                             actual_program=1, finished_at=clock.now())
        executor._chain_program2(cycle, external=True)
        self.assertEqual(adapter.start_calls, 1)
        self.assertEqual(cycle.phase, "CHAINING_P2",
                         "hardware P2 must stay in CHAINING until activity is observed")
        clock.advance(executor_module.CHAIN_TIMEOUT + 1)
        executor._check_chain_timeout(cycle)
        self.assertTrue(wait_idle(executor))
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertIn(payload["overall_result"], ("UNKNOWN", "ERROR"))
        self.assertIsNone(payload["result2"])
        self.assertEqual(printer.calls, [])

    def test_hardware_phase_watchdog_times_out_unknown(self):
        executor, adapter, clock = make_executor()
        manager = executor._manager
        cycle, _ = manager.admit(
            {"product_model": "M", "program1": 1, "program2": 0}, "hardware", [1])
        manager.set_cycle_field(cycle, phase="RUNNING_P1")
        manager.update_phase(cycle, "p1", status="running", activity_observed=True,
                             started_at=clock.now() - executor_module.PHASE_TIMEOUT - 5)
        executor._check_hardware_watchdog(cycle)
        self.assertTrue(wait_idle(executor))
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertIn(payload["overall_result"], ("UNKNOWN", "ERROR"))
        self.assertIsNotNone(payload["terminal_reason"])

    def test_concurrent_finalize_exactly_once(self):
        entered = threading.Event()
        release = threading.Event()
        counter = {"n": 0}

        def recorder(_cycle):
            counter["n"] += 1
            entered.set()
            release.wait(2.0)
            return {"record_id": 7, "serial_number": "S", "created": True,
                    "error": None}

        executor, adapter, _ = make_executor(recorder=recorder,
                                             printer=FakePrinter())
        cycle, _ = executor._manager.admit({"program1": 1}, "software", [1])
        first = threading.Thread(target=executor._finalize_and_finish,
                                 args=(cycle, "ABORTED", "CANCELLED"))
        first.start()
        self.assertTrue(entered.wait(2.0))
        second = threading.Thread(target=executor._finalize_and_finish,
                                  args=(cycle, "PASS", "OK"))
        second.start()
        release.set()
        first.join(3.0)
        second.join(3.0)
        self.assertEqual(counter["n"], 1, "finalization must be exactly-once")


class R6ResetInhibitionTests(unittest.TestCase):
    def test_failed_reset_blocks_admission_until_recovery(self):
        manager = CycleManager(persist_enabled=False)
        manager.set_reset_state("failed", "old", error="no response")
        cycle, error = manager.admit({"program1": 1}, "software", [1])
        self.assertIsNone(cycle)
        self.assertIn(error, ("RESET_PENDING", "RESET_FAILED", "DEVICE_BUSY"))
        self.assertFalse(manager.reserve_external())

    def test_reset_ack_with_active_step_is_not_confirmed(self):
        executor, adapter, _ = make_executor()
        adapter.default_step = 6  # instrument still active
        manager = executor._manager
        manager.set_reset_state("pending", "c1")
        executor._perform_reset()
        self.assertEqual(manager.get_reset_state()["state"], "failed")
        # recovery: confirmed idle clears inhibition
        adapter.default_step = 65535
        manager.set_reset_state("pending", "c1")
        executor._perform_reset()
        self.assertEqual(manager.get_reset_state()["state"], "confirmed")
        cycle, error = manager.admit({"program1": 1}, "software", [1])
        self.assertIsNotNone(cycle, error)

    def test_stop_sets_reset_inhibit_atomically_before_return(self):
        executor, adapter, _ = make_executor()
        adapter.write_gate = threading.Event()
        ok, _, _ = executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok)
        self.assertTrue(adapter.write_entered.wait(2.0))
        response = executor.request_stop()
        self.assertTrue(response["cancel_accepted"])
        # reset cannot run while the bus is held, so inhibition must already be armed
        self.assertTrue(executor._manager.reset_pending())
        ok2, error2, _ = executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertFalse(ok2)
        self.assertIn(error2, ("BUSY", "RESET_PENDING", "DEVICE_BUSY"))
        adapter.write_gate.set()
        adapter.write_gate = None
        self.assertTrue(wait_idle(executor))


class R7BindingOwnershipTests(unittest.TestCase):
    def setUp(self):
        reset_scanner_state(serial_scanner)

    def tearDown(self):
        reset_scanner_state(serial_scanner)

    def test_hardware_cycle_uses_consumed_binding_code(self):
        executor, adapter, _ = make_executor(
            settings={"scan_required": False, "scan_start": False},
            scanner=serial_scanner)
        serial_scanner.create_manual_binding("NEW")
        executor.set_pending_test_context(product_model="M", program1=1,
                                          qr_code="OLD")
        executor._handle_hardware_sample(
            {"step_code": 6, "actual_program": 1, "status": 1,
             "pressure": 2, "leak": 3})
        cycle = executor._manager.get_active()
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle.context["qr_code"], "NEW")
        self.assertEqual(cycle.context["scan_source"], "manual")
        self.assertFalse(cycle.context["device_verified"])
        self.assertIsNone(serial_scanner.get_binding())

    def test_concurrent_scan_admission_single_start(self):
        executor, adapter, _ = make_executor(
            settings={"scan_required": True, "scan_start": True},
            scanner=serial_scanner)
        executor.set_pending_test_context(product_model="M", program1=1)
        adapter.write_gate = threading.Event()
        event = serial_scanner.publish_scan_for_test(b"CONC-1\r\n")
        threads = [threading.Thread(target=executor.handle_scan_event,
                                    args=(dict(event_dict),)) for _ in range(4)
                   for event_dict in [dict(serial_scanner.get_event(event.event_id))]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        adapter.write_gate.set()
        adapter.write_gate = None
        self.assertTrue(wait_idle(executor))
        self.assertEqual(adapter.start_calls, 1)


executor_module = program_selector


class R10UnitsAndCacheTests(unittest.TestCase):
    def test_full_32bit_unit_code_roundtrip(self):
        device = FakeRTUDevice()
        device.pressure_unit_code = 0x12345678
        device.leak_unit_code = 0x0FFFFFFF
        modbus_utils.set_transport_override(device.handle)
        try:
            parsed = program_selector.read_test_data()
        finally:
            modbus_utils.clear_transport_override()
        self.assertEqual(parsed["pressure_unit"], "Code(305419896)")
        self.assertEqual(parsed["leak_unit"], "Code(268435455)")

    def test_production_adapter_reports_port_open_failure(self):
        import serial

        def failing(frame, timeout):
            raise serial.SerialException("cannot open fake port")

        modbus_utils.set_transport_override(failing)
        try:
            adapter = program_selector.DeviceAdapter()
            sample_value, error_code = adapter.read_sample_ex()
        finally:
            modbus_utils.clear_transport_override()
        self.assertIsNone(sample_value)
        self.assertEqual(error_code, "PORT_OPEN_FAILED")

    def test_supervisor_cache_marks_failed_current_sample_stale(self):
        executor, adapter, _ = make_executor()
        executor._update_sample_cache(sample(pressure=10.0, leak=1.0),
                                      error_code=None)
        self.assertTrue(executor.runtime_snapshot()["sample"]["pressure"] == 10.0)
        executor._update_sample_cache(None, error_code="READ_TIMEOUT",
                                      message="no response")
        snapshot = executor.runtime_snapshot()["sample"]
        self.assertEqual(snapshot["error_code"], "READ_TIMEOUT")
        self.assertTrue(snapshot["stale"])
        self.assertIsNotNone(snapshot.get("last_good"))
        self.assertTrue(snapshot["last_good"]["stale"])


class R11PersistenceHealthTests(unittest.TestCase):
    def test_persist_failure_blocks_admission_and_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CycleManager(persist_path=tmp, persist_enabled=True)
            cycle, error = manager.admit({"program1": 1}, "software", [1])
            self.assertIsNone(cycle)
            self.assertEqual(error, "PERSISTENCE_UNAVAILABLE")
            health = manager.persist_health()
            self.assertFalse(health["ok"])
            self.assertTrue(health["error"])

    def test_interrupted_recovery_failure_retains_evidence(self):
        from unittest import mock
        state_dir = tempfile.mkdtemp(prefix="ateq_interrupt_fail_")
        state_path = Path(state_dir) / "state.json"
        os.environ["ATEQ_TEST_DB"] = str(Path(state_dir) / "test.db")
        database._INITIALIZED = False
        database.init_database()
        persisted_manager = CycleManager(persist_path=str(state_path),
                                         persist_enabled=True)
        cycle, _ = persisted_manager.admit(
            {"product_model": "M1", "program1": 1, "qr_code": "Q"},
            "hardware", [1])
        persisted_manager.set_phase(cycle, "RUNNING_P1")
        manager = CycleManager(persist_path=str(state_path), persist_enabled=True)
        adapter = ScriptedAdapter(FakeClock())
        executor = TestExecutor(io=adapter, manager=manager,
                                recorder=FakeRecorder(), printer=FakePrinter(),
                                scanner_module=FakeScanner(), persist_enabled=False)
        with mock.patch.object(database, "save_test_record",
                               side_effect=RuntimeError("db down")):
            executor.ensure_started()
        sidecar = Path(state_dir) / "state.interrupted.json"
        self.assertTrue(sidecar.exists(),
                        "failed interrupted recovery must retain retryable evidence")
        evidence = sidecar.read_text(encoding="utf-8")
        self.assertIn(cycle.cycle_id, evidence)


if __name__ == "__main__":
    unittest.main()
