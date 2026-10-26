#!/usr/bin/env python3
"""Third targeted repair round: send-commit linearization at the real write boundary.

Regression shapes mirror the Codex barrier: a pause inserted between the caller and the
original transport send, with STOP returning before the pause is released.
"""

from collections import deque
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_round3_")
os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")
os.environ["ATEQ_TEST_DB"] = str(Path(_TMP) / "test.db")
os.environ["ATEQ_SERIAL_DB"] = str(Path(_TMP) / "serial.db")
os.environ["ATEQ_RUNTIME_STATE"] = str(Path(_TMP) / "state.json")
os.environ["ATEQ_SCANNER_LOG"] = str(Path(_TMP) / "scanner.log")
os.environ["ATEQ_LINE_SETTINGS"] = str(Path(_TMP) / "line_settings.json")
os.environ["ATEQ_PRODUCT_CONFIG"] = str(Path(_TMP) / "products.json")

import modbus_utils  # noqa: E402
import program_selector  # noqa: E402
import serial_scanner  # noqa: E402
from cycle_state import CycleManager  # noqa: E402
from program_selector import TestExecutor  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeClock,
    FakePrinter,
    FakeRecorder,
    FakeRTUDevice,
)


def wait_idle(executor, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not executor._manager.is_busy():
            return True
        time.sleep(0.005)
    return not executor._manager.is_busy()


class SendCommitBarrierTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeRTUDevice(station=modbus_utils.STATION_ID)
        modbus_utils.set_transport_override(self.device.handle)
        self.original_send = program_selector.send_raw
        self.clock = FakeClock()
        self.executor = TestExecutor(
            io=program_selector.DeviceAdapter(),
            clock=self.clock.now, sleeper=self.clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=FakePrinter(),
            scanner_module=serial_scanner, persist_enabled=False)

    def tearDown(self):
        program_selector.send_raw = self.original_send
        modbus_utils.clear_transport_override()

    def _frames(self, prefix, address=None):
        found = []
        for _seq, frame in self.device.commands:
            upper = frame.upper()
            if not upper.startswith(prefix):
                continue
            if address is not None and upper[4:8] != address:
                continue
            found.append(frame)
        return found

    def test_stop_wins_fc06_send_boundary(self):
        """Reviewer barrier: pause is entered before the original FC06 send."""
        at_send = threading.Event()
        release = threading.Event()
        original = self.original_send

        def barrier_send(frame, *args, **kwargs):
            if frame.upper().startswith(f"{modbus_utils.STATION_ID:02X}06"):
                at_send.set()
                release.wait(8.0)
            return original(frame, *args, **kwargs)

        program_selector.send_raw = barrier_send
        ok, error, _ = self.executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok, error)
        self.assertTrue(at_send.wait(3.0), "must reach the FC06 dispatch barrier")
        response = self.executor.request_stop()
        self.assertTrue(response["cancel_accepted"])
        release.set()
        self.assertTrue(wait_idle(self.executor))
        writes = self._frames(f"{modbus_utils.STATION_ID:02X}06", address="0200")
        self.assertEqual(writes, [], "FC06 must not commit after STOP returned")
        payload = self.executor.runtime_snapshot()["completion_payload"]
        self.assertEqual(payload["overall_result"], "ABORTED")

    def test_stop_wins_start_on_send_boundary(self):
        at_on = threading.Event()
        release = threading.Event()
        original = self.original_send

        def barrier_send(frame, *args, **kwargs):
            upper = frame.upper()
            if (upper.startswith(f"{modbus_utils.STATION_ID:02X}05")
                    and upper[4:8] == "0001" and upper[8:12] == "FF00"):
                at_on.set()
                release.wait(8.0)
            return original(frame, *args, **kwargs)

        program_selector.send_raw = barrier_send
        ok, error, _ = self.executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok, error)
        self.assertTrue(at_on.wait(3.0), "must reach the start-ON barrier")
        response = self.executor.request_stop()
        self.assertTrue(response["cancel_accepted"])
        release.set()
        self.assertTrue(wait_idle(self.executor))
        self.assertEqual(self._frames(f"{modbus_utils.STATION_ID:02X}05", "0001"), [])
        payload = self.executor.runtime_snapshot()["completion_payload"]
        self.assertEqual(payload["overall_result"], "ABORTED")

    def test_committed_fc06_then_stop_is_in_flight(self):
        """Inverse order: the frame commits before STOP; no new ON afterwards."""
        committed = threading.Event()
        release = threading.Event()
        original = self.original_send
        commit_times = []

        def barrier_send(frame, *args, **kwargs):
            if frame.upper().startswith(f"{modbus_utils.STATION_ID:02X}06"):
                response = original(frame, *args, **kwargs)  # commit first
                commit_times.append(time.monotonic())
                committed.set()
                release.wait(8.0)
                return response
            return original(frame, *args, **kwargs)

        program_selector.send_raw = barrier_send
        ok, error, _ = self.executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok, error)
        self.assertTrue(committed.wait(3.0))
        started = time.monotonic()
        response = self.executor.request_stop()
        stop_time = time.monotonic()
        self.assertTrue(response["cancel_accepted"])
        self.assertGreaterEqual(stop_time, commit_times[0],
                                "STOP must linearize after the committed frame")
        self.assertLess(stop_time - started, 1.0)
        release.set()
        self.assertTrue(wait_idle(self.executor))
        self.assertEqual(len(self._frames(f"{modbus_utils.STATION_ID:02X}06", "0200")), 1)
        self.assertEqual(self._frames(f"{modbus_utils.STATION_ID:02X}05", "0001"), [],
                         "no start ON may follow a committed program write + STOP")
        self.assertEqual(self.executor.runtime_snapshot()["completion_payload"]["overall_result"],
                         "ABORTED")

    def test_committed_on_still_gets_off_cleanup(self):
        on_committed = threading.Event()
        release = threading.Event()
        original = self.original_send

        def barrier_send(frame, *args, **kwargs):
            upper = frame.upper()
            if (upper.startswith(f"{modbus_utils.STATION_ID:02X}05")
                    and upper[4:8] == "0001" and upper[8:12] == "FF00"):
                response = original(frame, *args, **kwargs)
                on_committed.set()
                release.wait(8.0)
                return response
            return original(frame, *args, **kwargs)

        program_selector.send_raw = barrier_send
        ok, error, _ = self.executor.start_cycle(
            {"product_model": "M1", "program1": 1, "program2": 0}, "software")
        self.assertTrue(ok, error)
        self.assertTrue(on_committed.wait(3.0))
        response = self.executor.request_stop()
        self.assertTrue(response["cancel_accepted"])
        release.set()
        self.assertTrue(wait_idle(self.executor))
        on_frames = [f for _s, f in self.device.commands
                     if f.upper().startswith(f"{modbus_utils.STATION_ID:02X}05")
                     and f.upper()[4:8] == "0001"]
        on_pulses = [f for f in on_frames if f.upper()[8:12] == "FF00"]
        off_pulses = [f for f in on_frames if f.upper()[8:12] == "0000"]
        self.assertEqual(len(on_pulses), 1, "exactly one committed ON pulse")
        self.assertGreaterEqual(len(off_pulses), 1,
                                "OFF cleanup is mandatory for a committed ON")
        payload = self.executor.runtime_snapshot()["completion_payload"]
        self.assertEqual(payload["overall_result"], "ABORTED")

    def test_direct_guard_rejects_without_transport_call(self):
        with modbus_utils.send_guard(lambda: True):
            response = modbus_utils.send_raw(
                f"{modbus_utils.STATION_ID:02X}0602000001" + "0000")
        self.assertIsNone(response)
        self.assertEqual(self.device.commands, [])
        modbus_utils.commit_barrier()  # must not deadlock
        with modbus_utils.send_guard(lambda: False):
            result = modbus_utils.send_raw_ex(
                f"{modbus_utils.STATION_ID:02X}0602000001" + "0000")
        self.assertIn(result["error_code"], ("OK", "CRC_MISMATCH", "PROTOCOL_ERROR"))


class TcpCancelAndFailClosedTests(unittest.TestCase):
    """Codex TCP cancel-path finding + fail-closed guard predicate."""

    def setUp(self):
        self.original_transport = modbus_utils.TRANSPORT
        self.device = FakeRTUDevice(station=modbus_utils.STATION_ID)
        modbus_utils.set_transport_override(None)

    def tearDown(self):
        modbus_utils.TRANSPORT = self.original_transport
        modbus_utils.clear_transport_override()

    def test_tcp_cancelled_guard_returns_typed_result_without_socket(self):
        import socket as socket_module
        original_socket = socket_module.socket
        socket_calls = []

        def forbidden_socket(*args, **kwargs):
            socket_calls.append(1)
            raise AssertionError("no socket may be created for a cancelled send")

        modbus_utils.TRANSPORT = 'tcp'
        socket_module.socket = forbidden_socket
        try:
            with modbus_utils.send_guard(lambda: True):
                result = modbus_utils.send_raw_ex('FF0602000001')
                response = modbus_utils.send_raw('FF0602000001')
        finally:
            socket_module.socket = original_socket
        self.assertEqual(result['error_code'], 'CANCELLED')
        self.assertIsNone(result['response'])
        self.assertIsNone(response)
        self.assertEqual(socket_calls, [], "cancelled TCP send must not touch sockets")

    def test_guard_predicate_exception_fails_closed(self):
        calls = []
        modbus_utils.set_transport_override(
            lambda frame, timeout: (calls.append(frame), None)[1])

        def broken_predicate():
            raise RuntimeError("broken cancel predicate")

        with modbus_utils.send_guard(broken_predicate):
            result = modbus_utils.send_raw_ex('FF0602000001')
        self.assertEqual(result['error_code'], 'CANCELLED')
        self.assertIsNone(result['response'])
        self.assertEqual(calls, [], "broken predicate must not permit a send")

    def test_guard_predicate_exception_fails_closed_for_tcp(self):
        import socket as socket_module
        original_socket = socket_module.socket
        socket_calls = []

        def forbidden_socket(*args, **kwargs):
            socket_calls.append(1)
            raise AssertionError("no socket may be created")

        def broken_predicate():
            raise RuntimeError("broken cancel predicate")

        modbus_utils.TRANSPORT = 'tcp'
        socket_module.socket = forbidden_socket
        try:
            with modbus_utils.send_guard(broken_predicate):
                result = modbus_utils.send_raw_ex('FF0602000001')
        finally:
            socket_module.socket = original_socket
        self.assertEqual(result['error_code'], 'CANCELLED')
        self.assertEqual(socket_calls, [])

    def test_explicit_cancel_check_kwarg_also_short_circuits(self):
        calls = []
        modbus_utils.set_transport_override(
            lambda frame, timeout: (calls.append(frame), None)[1])
        result = modbus_utils.send_raw_ex('FF0602000001',
                                          cancel_check=lambda: True)
        self.assertEqual(result['error_code'], 'CANCELLED')
        self.assertEqual(calls, [])

    def test_non_cancelled_override_path_still_sends(self):
        calls = []
        frame = (f"{modbus_utils.STATION_ID:02X}0602000001"
                 + modbus_utils.modbus_crc(f"{modbus_utils.STATION_ID:02X}0602000001"))

        def transport(_frame, _timeout):
            calls.append(_frame)
            return frame.upper()

        modbus_utils.set_transport_override(transport)
        with modbus_utils.send_guard(lambda: False):
            result = modbus_utils.send_raw_ex(frame)
        self.assertEqual(result['error_code'], 'OK')
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
