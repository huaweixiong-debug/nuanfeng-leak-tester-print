#!/usr/bin/env python3
"""Terminal/measurement sourcing rules, field correction confirmed 2026-10-26.

User-confirmed rules under test (fake transport / temp DB / fake printer only):
- ONLY StepCode 65535 is terminal/idle; 65525 and 0 are never terminal results.
- 65535 alone is never a result: same-cycle activity + valid terminal status.
- pressure: last valid same-cycle StepCode 6 sample only; no 65535 fallback.
- leak: last valid same-cycle StepCode 7 sample, otherwise the 65535 sample.
- Modbus exceptions translate to Chinese while staying typed failures.
"""

from collections import deque
import os
from pathlib import Path
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_amendment_")
os.environ.setdefault("ATEQ_TEST_DB", str(Path(_TMP) / "test.db"))
os.environ.setdefault("ATEQ_RUNTIME_STATE", str(Path(_TMP) / "state.json"))

import modbus_utils  # noqa: E402
import program_selector  # noqa: E402
from cycle_state import (  # noqa: E402
    RESET_CONFIRMED,
    RESULT_PASS,
    RESULT_UNKNOWN,
    CycleManager,
)
from program_selector import TestExecutor  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeClock,
    FakePrinter,
    FakeRecorder,
    FakeRTUDevice,
    FakeScanner,
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


class AmendmentCycleTests(unittest.TestCase):
    def make(self, step_script=None, samples=None):
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.step_script = deque(step_script or [])
        adapter.default_step = 65535
        adapter.default_sample = sample()
        adapter.default_readback = 1
        if samples:
            adapter.sample_script = deque(samples)
        executor = TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=FakePrinter(),
            settings_provider=lambda: {'scan_required': False,
                                       'scan_start': False,
                                       'printer_enabled': False},
            scanner_module=FakeScanner(), persist_enabled=False,
        )
        return executor, adapter

    def run_single(self, executor, samples):
        ok, error, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok, error)
        self.assertTrue(wait_idle(executor), 'cycle did not finish')
        return executor.runtime_snapshot()['completion_payload']

    def test_pressure_uses_last_step6_and_leak_uses_last_step7(self):
        samples = [
            sample(step_code=4, pressure=10.0, leak=1.0, status=0x20),
            sample(step_code=6, pressure=50.0, leak=0.5, status=0x20),
            sample(step_code=6, pressure=60.0, leak=0.6,
                   pressure_unit='Bar', status=0x20),
            sample(step_code=6, pressure=None, leak=0.7, status=0x20),
            sample(step_code=7, pressure=111.0, leak=9.9, status=0x20),
            sample(step_code=7, pressure=112.0, leak=10.1,
                   leak_unit='cm³/s', status=0x20),
            sample(step_code=7, pressure=113.0, leak=None, status=0x20),
            sample(step_code=65535, pressure=999.0, leak=777.0,
                   pressure_unit='PSI', leak_unit='mL/min', status=0x21),
        ]
        steps = [4, 6, 6, 6, 7, 7, 7, 65535]
        executor, _ = self.make(step_script=steps, samples=samples)
        payload = self.run_single(executor, samples)
        self.assertEqual(payload['overall_result'], RESULT_PASS,
                         payload['terminal_reason'])
        self.assertEqual(payload['pressure1'], 60.0,
                         'last valid StepCode 6 pressure must win')
        self.assertEqual(payload['pressure1_unit'], 'Bar')
        self.assertEqual(payload['leak1'], 10.1,
                         'last valid StepCode 7 leak must win')
        self.assertEqual(payload['leak1_unit'], 'cm³/s')
        self.assertNotEqual(payload['leak1'], 777.0,
                            '65535 must not override valid StepCode 7 evidence')

    def test_absent_step6_pressure_is_unknown_without_65535_fallback(self):
        samples = [
            sample(step_code=4, pressure=10.0, leak=1.0, status=0x20),
            sample(step_code=7, pressure=111.0, leak=9.9, status=0x20),
            sample(step_code=65535, pressure=222.0, leak=333.0, status=0x21),
        ]
        executor, _ = self.make(step_script=[4, 7, 65535], samples=samples)
        payload = self.run_single(executor, samples)
        self.assertEqual(payload['overall_result'], RESULT_UNKNOWN)
        self.assertEqual(payload['terminal_reason'], 'MISSING_PRESSURE_SAMPLE')
        self.assertIsNone(payload['pressure1'],
                          '65535 pressure must never replace StepCode 6')
        self.assertEqual(payload['leak1'], 9.9)

    def test_absent_step7_leak_falls_back_to_65535_with_its_unit(self):
        samples = [
            sample(step_code=4, pressure=10.0, leak=1.0, status=0x20),
            sample(step_code=6, pressure=50.0, leak=0.25,
                   pressure_unit='kPa', status=0x20),
            sample(step_code=65535, pressure=222.0, leak=999.0,
                   leak_unit='cm³/s', status=0x21),
        ]
        executor, _ = self.make(step_script=[4, 6, 65535], samples=samples)
        payload = self.run_single(executor, samples)
        self.assertEqual(payload['overall_result'], RESULT_PASS,
                         payload['terminal_reason'])
        self.assertEqual(payload['pressure1'], 50.0)
        self.assertEqual(payload['pressure1_unit'], 'kPa')
        self.assertEqual(payload['leak1'], 999.0)
        self.assertEqual(payload['leak1_unit'], 'cm³/s')

    def test_absent_step7_and_terminal_leak_is_unknown(self):
        samples = [
            sample(step_code=6, pressure=50.0, leak=0.25, status=0x20),
            sample(step_code=65535, pressure=222.0, leak=None, status=0x21),
        ]
        executor, _ = self.make(step_script=[6, 65535], samples=samples)
        payload = self.run_single(executor, samples)
        self.assertEqual(payload['overall_result'], RESULT_UNKNOWN)
        self.assertEqual(payload['terminal_reason'], 'MISSING_STEP7_LEAK')
        self.assertIsNone(payload['leak1'])

    def test_phase_evidence_never_crosses_cycles_or_phases(self):
        samples = [
            sample(step_code=4, pressure=10.0, leak=9.0, actual_program=1),
            sample(step_code=6, pressure=50.0, leak=1.0, actual_program=1),
            sample(step_code=7, pressure=111.0, leak=9.9, actual_program=1),
            sample(step_code=65535, pressure=1000.0, leak=900.0,
                   actual_program=1, status=0x21),
            sample(step_code=4, pressure=20.0, leak=8.0, actual_program=2),
            sample(step_code=6, pressure=70.0, leak=2.0, actual_program=2),
            sample(step_code=7, pressure=222.0, leak=12.0, actual_program=2),
            sample(step_code=65535, pressure=333.0, leak=888.0,
                   actual_program=2, status=0x21),
        ]
        executor, adapter = self.make(
            step_script=[4, 6, 7, 65535, 4, 6, 7, 65535], samples=samples)
        adapter.default_readback = None  # readback follows the last write (P2=2)
        ok, error, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 2}, 'software')
        self.assertTrue(ok, error)
        self.assertTrue(wait_idle(executor))
        payload = executor.runtime_snapshot()['completion_payload']
        self.assertEqual(payload['overall_result'], RESULT_PASS)
        self.assertEqual(payload['pressure1'], 50.0)
        self.assertEqual(payload['leak1'], 9.9)
        self.assertEqual(payload['pressure2'], 70.0,
                         'P2 must not inherit P1 StepCode 6 pressure')
        self.assertEqual(payload['leak2'], 12.0,
                         'P2 must not inherit P1 StepCode 7 leak')

    def test_65535_idle_frame_alone_is_never_a_result(self):
        executor, adapter = self.make()
        adapter.default_readback = 1
        executor.set_pending_test_context(product_model='M1', program1=1)
        executor._handle_hardware_sample(
            {'step_code': 65535, 'status': 0x21, 'actual_program': 1,
             'pressure': 1.0, 'leak': 2.0})
        self.assertIsNone(executor._manager.get_active())
        self.assertIsNone(executor.runtime_snapshot()['completion_payload'])

        # An admitted cycle with no observed activity must not finalize on 65535.
        cycle, error = executor._manager.admit(
            {'product_model': 'M1', 'program1': 1}, 'hardware', [1])
        self.assertIsNotNone(cycle, error)
        executor._manager.set_phase(cycle, 'RUNNING_P1')
        executor._handle_hardware_sample(
            {'step_code': 65535, 'status': 0x21, 'actual_program': 1,
             'pressure': 1.0, 'leak': 2.0})
        self.assertIs(executor._manager.get_active(), cycle)
        self.assertIsNone(executor.runtime_snapshot()['completion_payload'])

    def test_step0_is_not_verified_idle_evidence(self):
        executor, adapter = self.make()
        adapter.default_readback = 1
        executor._handle_hardware_sample({'step_code': 0, 'status': 0})
        executor.set_pending_test_context(product_model='M1', program1=1)
        executor._handle_hardware_sample(
            {'step_code': 6, 'actual_program': 1, 'status': 1,
             'pressure': 2.0, 'leak': 3.0})
        cycle = executor._manager.get_active()
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle.admission_violation,
                         'INSTRUMENT_STATE_UNVERIFIED')

    def test_reset_confirmed_at_step0_does_not_sync_until_65535(self):
        executor, adapter = self.make()
        adapter.default_readback = 1
        manager = executor._manager

        executor._hardware_synced = True
        adapter.default_step = 0
        manager.set_reset_state('pending', 'c1')
        executor._perform_reset()
        self.assertEqual(manager.get_reset_state()['state'], RESET_CONFIRMED)
        self.assertFalse(executor._hardware_synced,
                         'reset confirmed at step 0 must clear the stale hardware sync')

        executor.set_pending_test_context(product_model='M1', program1=1)
        executor._handle_hardware_sample(
            {'step_code': 6, 'actual_program': 1, 'status': 1,
             'pressure': 2.0, 'leak': 3.0})
        unsynced = manager.get_active()
        self.assertIsNotNone(unsynced)
        self.assertEqual(unsynced.admission_violation,
                         'INSTRUMENT_STATE_UNVERIFIED')

        adapter.default_step = 65535
        executor._handle_hardware_sample({'step_code': 65535, 'status': 0})
        self.assertTrue(executor._hardware_synced)
        self.assertTrue(wait_idle(executor))
        executor.set_pending_test_context(product_model='M1', program1=1)
        executor._handle_hardware_sample(
            {'step_code': 6, 'actual_program': 1, 'status': 1,
             'pressure': 2.0, 'leak': 3.0})
        synced = manager.get_active()
        self.assertIsNotNone(synced)
        self.assertIsNone(synced.admission_violation)

    def test_hardware_65535_finalize_keeps_step6_pressure_and_step7_leak(self):
        executor, adapter = self.make()
        adapter.default_sample = sample(step_code=65535, pressure=987.0,
                                        leak=654.0, actual_program=1,
                                        status=0x21)
        adapter.default_readback = 1
        executor.set_pending_test_context(product_model='M1', program1=1)
        executor._handle_hardware_sample({'step_code': 65535, 'status': 0})
        executor._handle_hardware_sample(
            {'step_code': 6, 'actual_program': 1, 'status': 1,
             'pressure': 50.0, 'leak': 0.4})
        executor._handle_hardware_sample(
            {'step_code': 6, 'actual_program': 1, 'status': 1,
             'pressure': 60.0, 'leak': 0.6})
        executor._handle_hardware_sample(
            {'step_code': 7, 'actual_program': 1, 'status': 1,
             'pressure': 123.0, 'leak': 9.9})
        executor._handle_hardware_sample(
            {'step_code': 7, 'actual_program': 1, 'status': 1,
             'pressure': 124.0, 'leak': 10.2})
        executor._handle_hardware_sample(
            {'step_code': 65535, 'actual_program': 1, 'status': 0x21,
             'pressure': 987.0, 'leak': 654.0})
        self.assertTrue(wait_idle(executor))
        payload = executor.runtime_snapshot()['completion_payload']
        self.assertEqual(payload['overall_result'], RESULT_PASS,
                         payload['terminal_reason'])
        self.assertEqual(payload['pressure1'], 60.0,
                         'last StepCode 6 sample is the pressure source')
        self.assertEqual(payload['leak1'], 10.2,
                         'last StepCode 7 sample is the preferred leak source')


class AmendmentTransportTests(unittest.TestCase):
    def test_is_test_complete_recognizes_only_65535(self):
        device = FakeRTUDevice(station=modbus_utils.STATION_ID)
        modbus_utils.set_transport_override(device.handle)
        try:
            device.enter_step(65525)
            self.assertFalse(program_selector.is_test_complete())
            device.enter_step(0)
            self.assertFalse(program_selector.is_test_complete())
            device.enter_step(65535)
            self.assertTrue(program_selector.is_test_complete())
        finally:
            modbus_utils.clear_transport_override()

    def test_realtime_exception_message_is_chinese(self):
        body = bytes([modbus_utils.STATION_ID, 0x83, 0x02])
        frame = (body + bytes.fromhex(
            modbus_utils.modbus_crc(body.hex()))).hex().upper()
        with self.assertRaises(RuntimeError) as caught:
            program_selector._parse_realtime_response(frame)
        self.assertIn('非法数据地址', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
