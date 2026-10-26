#!/usr/bin/env python3
"""Slice B: stop linearization and reset scheduling (F01, amendment 2)."""

from collections import deque
import threading
import time
import unittest

from cycle_state import CycleManager, RESET_CONFIRMED, RESULT_ABORTED
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


def wait_reset(executor, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if executor._manager.get_reset_state()['state'] in (RESET_CONFIRMED, 'failed'):
            return executor._manager.get_reset_state()
        time.sleep(0.005)
    return executor._manager.get_reset_state()


class StopLinearizationTests(unittest.TestCase):
    def make(self, step_script=None):
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.step_script = deque(step_script or [4, 6, 65535])
        adapter.default_step = 65535
        adapter.default_sample = sample()
        executor = TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=FakePrinter(),
            settings_provider=lambda: {'scan_required': False,
                                       'printer_enabled': True,
                                       'scan_start': False},
            scanner_module=FakeScanner(), persist_enabled=False,
        )
        return executor, adapter, clock

    def test_stop_returns_fast_even_when_command_lock_held(self):
        executor, adapter, _ = self.make()
        adapter.write_gate = threading.Event()
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok)
        self.assertTrue(adapter.write_entered.wait(2.0))
        adapter.step_return_override = 65535  # deterministic idle proof for reset
        started = time.time()
        response = executor.request_stop()
        elapsed = time.time() - started
        self.assertLess(elapsed, 1.0)
        self.assertTrue(response['cancel_accepted'])
        self.assertFalse(response['reset_confirmed'])
        self.assertTrue(response['reset_pending'])
        self.assertEqual(adapter.reset_calls, 0)
        # busy must remain until the worker can no longer issue commands
        self.assertTrue(executor._manager.is_busy())
        adapter.write_gate.set()
        state = wait_reset(executor)
        self.assertEqual(state['state'], RESET_CONFIRMED)
        self.assertEqual(adapter.reset_calls, 1)
        self.assertTrue(wait_idle(executor))

    def test_cancel_before_start_sends_no_start_command(self):
        executor, adapter, clock = self.make()
        stop_at = []

        def hook(_program):
            stop_at.append(clock.now())
            adapter.step_return_override = 65535
            executor.request_stop()

        adapter.before_write = hook
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        self.assertEqual(adapter.start_calls, 0)
        snap = executor.runtime_snapshot()
        self.assertEqual(snap['completion_payload']['overall_result'], RESULT_ABORTED)
        start_events = [e for e in adapter.events if e[0] == 'start_test']
        self.assertEqual(start_events, [])
        state = wait_reset(executor)
        self.assertEqual(state['state'], RESET_CONFIRMED)

    def test_cancel_during_p1_prevents_p2_commands(self):
        executor, adapter, clock = self.make(step_script=[4, 6, 65535, 4, 6, 65535])
        stop_at = []

        def hook(_count, _value):
            if not stop_at:
                stop_at.append(clock.now())
                executor.request_stop()

        adapter.after_read_sample = hook
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 2}, 'software')
        self.assertTrue(ok)
        self.assertTrue(wait_idle(executor))
        self.assertEqual(adapter.write_program_calls, [1])
        self.assertEqual(adapter.start_calls, 1)
        snap = executor.runtime_snapshot()
        self.assertEqual(snap['completion_payload']['overall_result'], RESULT_ABORTED)
        self.assertIsNone(snap['completion_payload']['result2'])
        # Nothing may be commanded after the stop timestamp except reset.
        for name, ts, value in adapter.events:
            if name in ('write_program', 'start_test'):
                self.assertLessEqual(ts, stop_at[0], f'{name} after stop')

    def test_admission_blocked_until_reset_confirmed(self):
        executor, adapter, _ = self.make()

        def stop_hook(_p):
            adapter.step_return_override = 65535
            executor.request_stop()

        adapter.before_write = stop_hook
        ok, _, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        self.assertTrue(ok)
        # Immediately after stop the reset is pending; a new cycle must not be admitted.
        deadline = time.time() + 2.0
        saw_pending = False
        while time.time() < deadline and not saw_pending:
            if executor._manager.reset_pending():
                saw_pending = True
                break
            time.sleep(0.002)
        self.assertTrue(saw_pending)
        ok2, error2, _ = executor.start_cycle(
            {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
        if not ok2:
            self.assertIn(error2, ('BUSY', 'RESET_PENDING'))
        state = wait_reset(executor)
        self.assertEqual(state['state'], RESET_CONFIRMED)
        self.assertTrue(wait_idle(executor))

    def test_supervisor_reset_waits_for_inflight_command_without_deadlock(self):
        executor, adapter, _ = self.make()
        adapter.default_sample = sample(step_code=65535, status=0)
        adapter.write_gate = threading.Event()
        executor.start_instrument_supervisor(start_scanner=False)
        try:
            ok, _, _ = executor.start_cycle(
                {'product_model': 'M1', 'program1': 1, 'program2': 0}, 'software')
            self.assertTrue(ok)
            self.assertTrue(adapter.write_entered.wait(2.0))
            adapter.step_return_override = 65535
            started = time.time()
            response = executor.request_stop()
            self.assertLess(time.time() - started, 1.0)
            self.assertTrue(response['reset_pending'])
            # Reset must not run while the in-flight command holds the lock.
            time.sleep(0.2)
            self.assertEqual(adapter.reset_calls, 0)
            adapter.write_gate.set()
            state = wait_reset(executor, timeout=5.0)
            self.assertEqual(state['state'], RESET_CONFIRMED)
            self.assertGreaterEqual(adapter.reset_calls, 1)
        finally:
            adapter.write_gate.set()
            executor.stop_instrument_supervisor()

    def test_no_start_after_stop_across_many_steps(self):
        for stop_after in (1, 2, 3):
            executor, adapter, clock = self.make(
                step_script=[4, 6, 65535, 4, 6, 65535])
            stop_at = []

            def hook(count, _value, stop_after=stop_after, stop_at=stop_at,
                     executor=executor, clock=clock):
                if count == stop_after and not stop_at:
                    stop_at.append(clock.now())
                    executor.request_stop()

            adapter.after_read_step = hook
            ok, _, _ = executor.start_cycle(
                {'product_model': 'M1', 'program1': 1, 'program2': 2}, 'software')
            self.assertTrue(ok)
            self.assertTrue(wait_idle(executor))
            for name, ts, _value in adapter.events:
                if name in ('write_program', 'start_test') and stop_at:
                    self.assertLessEqual(ts, stop_at[0], f'{name} after stop')


if __name__ == '__main__':
    unittest.main()
