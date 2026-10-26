#!/usr/bin/env python3
"""Slice B: unified cycle evidence rules (F02/F03/F04/F05)."""

from collections import deque
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

os.environ.setdefault("ATEQ_TEST_DB", str(Path(tempfile.mkdtemp(prefix="ateq_cycle_")) / "test.db"))

from cycle_state import CycleManager, RESULT_ABORTED, RESULT_FAIL, RESULT_PASS, RESULT_UNKNOWN
from program_selector import TestExecutor
from tests.fakes import (
    FakeClock,
    FakePrinter,
    FakeRecorder,
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


class CycleExecutionTests(unittest.TestCase):
    def make(self, step_script=None, adapter_sample="default", recorder=None,
             printer=None, write_ok=True):
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.step_script = deque(step_script or [])
        adapter.default_step = 65535
        adapter.default_sample = sample() if adapter_sample == "default" else adapter_sample
        adapter.write_ok = write_ok
        scan = FakeScanner()
        executor = TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=recorder or FakeRecorder(),
            printer=printer or FakePrinter(),
            settings_provider=lambda: {'scan_required': False,
                                       'printer_enabled': True,
                                       'scan_start': False},
            scanner_module=scan, persist_enabled=False,
        )
        return executor, adapter, clock, scan

    def test_single_program_pass_full_evidence(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535])
        ok, error, cycle_id = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok, error)
        self.assertTrue(wait_idle(executor), 'cycle did not finish')
        snap = executor.runtime_snapshot()
        cp = snap['completion_payload']
        self.assertEqual(cp['overall_result'], RESULT_PASS)
        self.assertEqual(cp['actual_program1'], 1)
        self.assertTrue(cp['record_saved'])
        self.assertEqual(cp['print_state'], 'submitted')
        self.assertEqual(adapter.write_program_calls, [1])
        self.assertEqual(adapter.start_calls, 1)

    def test_two_programs_pass_and_p1_retained(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535, 4, 6, 65535])
        p1_sample = sample(actual_program=1)
        p2_sample = sample(actual_program=2)
        adapter.sample_script = deque([p1_sample, p1_sample, p1_sample,
                                       p2_sample, p2_sample, p2_sample])
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 2}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        cp = executor.runtime_snapshot()['completion_payload']
        self.assertEqual(cp['overall_result'], RESULT_PASS)
        self.assertEqual(cp['result1'], RESULT_PASS)
        self.assertEqual(cp['result2'], RESULT_PASS)
        self.assertIsNotNone(cp['pressure1'])
        self.assertEqual(adapter.write_program_calls, [1, 2])
        self.assertEqual(adapter.start_calls, 2)

    def test_p2_missing_never_pass(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535])
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 2}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        cp = executor.runtime_snapshot()['completion_payload']
        self.assertNotEqual(cp['overall_result'], RESULT_PASS)
        # never-started P2 is unknown hardware behavior, not a product-quality FAIL
        self.assertEqual(cp['overall_result'], RESULT_UNKNOWN)
        self.assertEqual(cp['terminal_reason'], 'CHAIN_TIMEOUT')
        self.assertIsNone(cp['result2'])
        self.assertEqual(adapter.write_program_calls, [1, 2])

    def test_p1_fail_skips_p2_and_never_prints(self):
        fail_sample = sample(result='FAIL', status=0x0022)
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535],
                                            adapter_sample=fail_sample)
        printer = executor._printer
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 2}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        cp = executor.runtime_snapshot()['completion_payload']
        self.assertEqual(cp['overall_result'], RESULT_FAIL)
        self.assertEqual(cp['result1'], RESULT_FAIL)
        self.assertIsNone(cp['result2'])
        self.assertEqual(adapter.write_program_calls, [1])
        self.assertEqual(printer.calls, [])
        self.assertEqual(cp['print_state'], 'skipped')

    def test_abnormal_reset_is_aborted_not_pass(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 0])
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        cp = executor.runtime_snapshot()['completion_payload']
        self.assertEqual(cp['overall_result'], RESULT_ABORTED)
        self.assertEqual(cp['terminal_reason'], 'ABNORMAL_RESET')
        self.assertIsNotNone(cp['pressure1'])

    def test_missing_sample_never_pass_and_no_fabricated_zero(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535],
                                            adapter_sample=None)
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        cp = executor.runtime_snapshot()['completion_payload']
        self.assertEqual(cp['overall_result'], RESULT_UNKNOWN)
        self.assertIsNone(cp['pressure1'])
        self.assertIsNone(cp['leak1'])

    def test_unknown_status_bits_block_pass(self):
        unknown_sample = sample(status=0x0021 | 0x0040)
        executor, _, _, _ = self.make(step_script=[4, 6, 65535],
                                      adapter_sample=unknown_sample)
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        cp = executor.runtime_snapshot()['completion_payload']
        self.assertEqual(cp['overall_result'], RESULT_UNKNOWN)
        self.assertEqual(cp['terminal_reason'], 'UNKNOWN_STATUS_BITS')

    def test_unverified_program_blocks_pass(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535])
        adapter.readback_none = True
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        cp = executor.runtime_snapshot()['completion_payload']
        self.assertEqual(cp['overall_result'], RESULT_UNKNOWN)
        self.assertEqual(cp['terminal_reason'], 'PROGRAM_UNVERIFIED')

    def test_write_failure_never_starts_or_prints(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535],
                                            write_ok=False)
        printer = executor._printer
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        cp = executor.runtime_snapshot()['completion_payload']
        self.assertEqual(cp['overall_result'], 'ERROR')
        self.assertEqual(adapter.start_calls, 0)
        self.assertEqual(printer.calls, [])

    def test_concurrent_executor_admission_single_winner(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535])
        adapter.write_gate = threading.Event()
        results = []
        lock = threading.Lock()

        def start():
            ok, error, _ = executor.start_cycle(
                {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
            with lock:
                results.append((ok, error))

        threads = [threading.Thread(target=start) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for ok, _ in results if ok), 1)
        self.assertEqual(sum(1 for ok, _ in results if not ok), 7)
        adapter.write_gate.set()
        self.assertTrue(wait_idle(executor))

    def test_required_programs_come_from_frozen_context(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535])
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 3, 'program2': 0}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        cp = executor.runtime_snapshot()['completion_payload']
        self.assertEqual(cp['program1'], 3)
        self.assertEqual(adapter.write_program_calls, [3])

    def test_bool_program_rejected(self):
        executor, _, _, _ = self.make(step_script=[4, 6, 65535])
        ok, error, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': True, 'program2': 0}, 'software')
        self.assertFalse(ok)
        self.assertEqual(error, 'PROGRAM1_INVALID')

    def test_arm_next_context_defers_while_busy(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535])
        adapter.write_gate = threading.Event()
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok)
        self.assertTrue(adapter.write_entered.wait(2.0))
        active = executor._manager.get_active()
        result = executor.arm_next_context(
            {'product_model': 'M2', 'program1': 5, 'program2': 0})
        self.assertTrue(result['busy'])
        self.assertTrue(result['deferred'])
        # active cycle context unchanged; arm issued no additional wire command
        # (the only write is the in-flight one blocked on the gate)
        self.assertEqual(active.context['product_model'], 'M1')
        self.assertEqual(adapter.write_program_calls, [])
        self.assertEqual(executor.get_pending_context()['product_model'], 'M2')
        adapter.write_gate.set()
        adapter.write_gate = None
        self.assertTrue(wait_idle(executor))
        self.assertEqual(adapter.write_program_calls, [1])

    def test_arm_next_context_idle_prepares_program(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535])
        adapter.readback_values = deque([2])  # instrument currently on program 2
        result = executor.arm_next_context(
            {'product_model': 'M1', 'program1': 1, 'program2': 0})
        self.assertFalse(result['busy'])
        self.assertTrue(result['prepared'], result)
        self.assertEqual(adapter.write_program_calls, [1])

    def test_stop_before_final_print_skips_print(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535])
        printer = executor._printer
        cycle, error = executor._manager.admit(
            {'product_model': 'M1', 'program1': 1, 'program2': 0},
            'software', [1])
        self.assertIsNotNone(cycle)
        cycle.cancel_event.set()
        executor._finalize_and_finish(cycle, RESULT_PASS, 'OK')
        self.assertEqual(printer.calls, [])
        payload = executor.runtime_snapshot()['completion_payload']
        self.assertIn(payload['print_state'], ('skipped', 'cancelled'))
        self.assertIn('停止', payload['print_message'])

    def test_hardware_readiness_prepares_p1_after_p2_terminal(self):
        executor, adapter, _, _ = self.make(step_script=[4, 6, 65535])
        adapter.readback_values = deque([2])  # instrument left on program 2
        executor.set_pending_test_context(product_model='M1', program1=1, program2=2)
        cycle, error = executor._manager.admit(
            {'product_model': 'M1', 'program1': 1, 'program2': 2},
            'hardware', [1, 2])
        executor._finalize_and_finish(cycle, RESULT_PASS, 'OK')
        self.assertTrue(executor._needs_p1_prepare.is_set())
        state = executor._prepare_next_p1()
        self.assertTrue(state['ready'], state)
        self.assertEqual(adapter.write_program_calls, [1])

    def test_startup_during_active_instrument_cycle_is_interrupted(self):
        import database
        state_dir = tempfile.mkdtemp(prefix="ateq_interrupt_")
        state_path = Path(state_dir) / "state.json"
        os.environ["ATEQ_TEST_DB"] = str(Path(state_dir) / "test.db")
        database._INITIALIZED = False
        database.init_database()

        persisted_manager = CycleManager(persist_path=str(state_path),
                                         persist_enabled=True)
        cycle, _ = persisted_manager.admit(
            {'product_model': 'M1', 'program1': 1, 'program2': 0, 'qr_code': 'Q'},
            'hardware', [1])
        persisted_manager.set_phase(cycle, 'RUNNING_P1')

        manager = CycleManager(persist_path=str(state_path), persist_enabled=True)
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        recorder = FakeRecorder()
        executor = TestExecutor(io=adapter, clock=clock.now, sleeper=clock.sleep,
                                manager=manager, recorder=recorder,
                                printer=FakePrinter(), scanner_module=FakeScanner(),
                                persist_enabled=False)
        executor.ensure_started()
        payload = executor.runtime_snapshot()['completion_payload']
        self.assertIsNotNone(payload)
        self.assertEqual(payload['overall_result'], RESULT_UNKNOWN)
        self.assertEqual(payload['terminal_reason'], 'INTERRUPTED')
        self.assertEqual(adapter.start_calls, 0)  # no auto restart
        record = database.find_record_by_cycle(payload['cycle_id'])
        self.assertIsNotNone(record)
        self.assertEqual(record['test_result'], 'UNKNOWN')
        self.assertEqual(record['terminal_reason'], 'INTERRUPTED')


if __name__ == '__main__':
    unittest.main()
