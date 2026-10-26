#!/usr/bin/env python3
"""Offline tests for S7 handshake/runtime integration; no PLC or ATEQ IO."""

from collections import deque
import threading
import time
import unittest

from cycle_state import CycleManager, RESULT_PASS
from plc_runtime import PlcRuntime
from plc_s7_smart import HandshakeMonitor
from program_selector import TestExecutor
from tests.fakes import FakeClock, FakePrinter, FakeRecorder, FakeScanner, ScriptedAdapter, sample


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class FakePlc:
    def __init__(self):
        self.ip = "fake"
        self.port = 102
        self.pulse_seconds = 0.0
        self.pulses = []
        self.cleared = False
        self.disconnected = False

    def pulse_result(self, ok):
        self.pulses.append(bool(ok))
        return {"elapsed_ms": 0}

    def clear_outputs(self):
        self.cleared = True

    def disconnect(self):
        self.disconnected = True


class FakeMonitor:
    def __init__(self, plc, on_start, on_reset, on_error):
        self.plc = plc
        self.on_start = on_start
        self.on_reset = on_reset
        self.on_error = on_error
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        return None


class PlcRuntimeTests(unittest.TestCase):
    def test_definitive_results_are_serialized_and_other_results_do_not_pulse(self):
        plc = FakePlc()
        starts = []
        resets = []
        runtime = PlcRuntime(
            on_start=lambda: starts.append(1) or (True, "OK", "cycle"),
            on_reset=lambda: resets.append(1) or {"cancel_accepted": True},
            plc=plc,
            monitor_factory=FakeMonitor,
        )
        runtime.start()
        try:
            self.assertTrue(runtime.enqueue_result(
                {"cycle_id": "p1", "overall_result": "PASS", "terminal_reason": "OK"}))
            self.assertFalse(runtime.enqueue_result(
                {"cycle_id": "u1", "overall_result": "UNKNOWN"}))
            self.assertFalse(runtime.enqueue_result(
                {"cycle_id": "a1", "overall_result": "ABORTED"}))
            self.assertFalse(runtime.enqueue_result(
                {"cycle_id": "e1", "overall_result": "ERROR"}))
            self.assertTrue(runtime.enqueue_result(
                {"cycle_id": "f1", "overall_result": "FAIL", "terminal_reason": "STATUS_FAIL"}))
            self.assertTrue(wait_until(lambda: len(plc.pulses) == 2))
            self.assertEqual(plc.pulses, [True, False])

            runtime._monitor.on_start()
            runtime._monitor.on_reset()
            self.assertEqual(starts, [1])
            self.assertEqual(resets, [1])
        finally:
            runtime.stop()
        self.assertTrue(runtime._monitor is None)
        self.assertTrue(plc.cleared)
        self.assertTrue(plc.disconnected)


class ReconnectingPlc:
    def __init__(self):
        self.connected = False
        self.connects = 0

    def is_connected(self):
        return self.connected

    def ensure_connected(self):
        self.connects += 1
        self.connected = True

    def disconnect(self):
        self.connected = False

    def read_byte(self):
        return 1  # Start is already ON immediately after reconnect: baseline only.


class HandshakeReconnectTests(unittest.TestCase):
    def test_first_sample_after_reconnect_does_not_create_start_edge(self):
        plc = ReconnectingPlc()
        starts = []
        monitor = HandshakeMonitor(plc, on_start=lambda: starts.append(1), poll_ms=1)
        monitor.start()
        try:
            self.assertTrue(wait_until(lambda: monitor.stats()["polls"] >= 3))
        finally:
            monitor.stop()
            monitor.join(1.0)
        self.assertGreaterEqual(plc.connects, 1)
        self.assertEqual(starts, [])


class PlcExecutorTests(unittest.TestCase):
    def make(self, scan_required=False, adapter_sample=None):
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.step_script = deque([4, 6, 65535])
        adapter.default_step = 65535
        adapter.default_sample = adapter_sample or sample()
        scanner = FakeScanner()
        executor = TestExecutor(
            io=adapter,
            clock=clock.now,
            sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(),
            printer=FakePrinter(),
            settings_provider=lambda: {
                "scan_required": scan_required,
                "scan_start": False,
                "printer_enabled": True,
            },
            scanner_module=scanner,
            persist_enabled=False,
        )
        executor.set_pending_test_context(
            product_model="MODEL", program1=1, program2=0, replace=True)
        return executor, scanner

    def test_missing_product_and_required_scan_reject_plc_start(self):
        executor, _ = self.make(scan_required=True)
        executor.set_pending_test_context(replace=True)
        self.assertEqual(executor.start_plc_cycle(), (False, "NO_PRODUCT", None))

        executor.set_pending_test_context(
            product_model="MODEL", program1=1, program2=0, replace=True)
        self.assertEqual(executor.start_plc_cycle(), (False, "SCAN_REQUIRED", None))
        self.assertFalse(executor._manager.is_busy())

    def test_plc_start_consumes_scan_and_completion_callback_observes_final_result(self):
        executor, scanner = self.make(scan_required=True)
        event = scanner.add_event("PART-001")
        scanner.set_binding(event["event_id"])
        completions = []
        executor.set_completion_callback(completions.append)

        ok, error, cycle_id = executor.start_plc_cycle()
        self.assertTrue(ok, error)
        self.assertIsNotNone(cycle_id)
        self.assertTrue(wait_until(lambda: not executor._manager.is_busy()))

        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertEqual(payload["trigger"], "plc")
        self.assertEqual(payload["overall_result"], RESULT_PASS)
        self.assertEqual(payload["qr_code"], "PART-001")
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]["cycle_id"], cycle_id)
        self.assertEqual(completions[0]["overall_result"], RESULT_PASS)
        self.assertTrue(completions[0]["record_saved"])
        self.assertIsNone(scanner.get_binding())

    def test_callback_exception_does_not_change_cycle_result(self):
        executor, _ = self.make()

        def broken_callback(_):
            raise RuntimeError("PLC output unavailable")

        executor.set_completion_callback(broken_callback)
        ok, error, _ = executor.start_plc_cycle()
        self.assertTrue(ok, error)
        self.assertTrue(wait_until(lambda: not executor._manager.is_busy()))
        self.assertEqual(executor.runtime_snapshot()["completion_payload"]["overall_result"],
                         RESULT_PASS)


if __name__ == "__main__":
    unittest.main()
