#!/usr/bin/env python3
"""Slice B: CycleManager admission, identity, snapshots and interruption."""

import json
from pathlib import Path
import tempfile
import threading
import unittest

from cycle_state import (
    CycleManager,
    CycleCancelled,
    PHASE_RUNNING_P1,
    RESULT_PASS,
    RESULT_UNKNOWN,
)


class CycleManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = CycleManager(persist_enabled=False)

    def test_concurrent_admission_has_exactly_one_winner(self):
        results = []
        lock = threading.Lock()

        def worker():
            cycle, error = self.manager.admit({'program1': 1}, 'software', [1])
            with lock:
                results.append((cycle, error))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in results if r[0] is not None]
        losers = [r for r in results if r[0] is None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 15)
        self.assertTrue(all(error == 'BUSY' for _, error in losers))

    def test_old_cycle_cannot_mutate_after_superseded(self):
        cycle1, _ = self.manager.admit({'program1': 1}, 'software', [1])
        self.manager.finish(cycle1, RESULT_PASS, 'OK')
        cycle2, _ = self.manager.admit({'program1': 2}, 'software', [2])
        self.assertFalse(self.manager.update_phase(cycle2, 'p1', pressure=1.0) is None)
        # stale cycle1 mutations are rejected
        self.assertFalse(self.manager.update_phase(cycle1, 'p1', pressure=9.9))
        self.assertFalse(self.manager.set_cycle_field(cycle1, terminal_reason='tampered'))
        with self.assertRaises(CycleCancelled):
            self.manager.check_cancelled(cycle1)

    def test_cancel_event_and_cancelled_check(self):
        cycle, _ = self.manager.admit({'program1': 1}, 'software', [1])
        info = self.manager.request_stop()
        self.assertTrue(info['cancel_accepted'])
        self.assertTrue(cycle.cancel_event.is_set())
        with self.assertRaises(CycleCancelled):
            self.manager.check_cancelled(cycle)

    def test_completion_payload_is_cycle_bound(self):
        cycle, _ = self.manager.admit({'program1': 1, 'program2': 0},
                                      'software', [1])
        self.manager.finish(cycle, RESULT_PASS, 'OK',
                            {'record_id': 7, 'serial_number': 'SN-1',
                             'record_saved': True, 'print_state': 'submitted'})
        snap = self.manager.snapshot()
        self.assertFalse(snap['busy'])
        self.assertEqual(snap['completion_payload']['cycle_id'], cycle.cycle_id)
        self.assertEqual(snap['completion_sequence'], 1)
        # new cycle hides the previous payload
        cycle2, _ = self.manager.admit({'program1': 2}, 'software', [2])
        snap2 = self.manager.snapshot()
        self.assertIsNone(snap2['completion_payload'])
        self.assertEqual(snap2['cycle_id'], cycle2.cycle_id)
        self.assertTrue(snap2['busy'])

    def test_disabled_persistence_never_touches_disk(self):
        self.assertIsNone(self.manager.read_persisted())
        cycle, _ = self.manager.admit({'program1': 1}, 'software', [1])
        self.manager.persist()
        self.assertIsNone(self.manager.read_persisted())

    def test_interrupted_recovery_marks_unknown_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / 'state.json'
            manager = CycleManager(persist_path=str(state_path), persist_enabled=True)
            cycle, _ = manager.admit({'program1': 1, 'product_model': 'M'},
                                     'hardware', [1])
            manager.set_phase(cycle, PHASE_RUNNING_P1)
            persisted = manager.read_persisted()
            self.assertIsNotNone(persisted)
            self.assertTrue(persisted.get('active'))
            # No stray temp files remain after the atomic replace.
            self.assertEqual(list(Path(tmp).glob('*.tmp')), [])

            manager2 = CycleManager(persist_path=str(state_path), persist_enabled=True)
            payload = manager2.note_interrupted(persisted)
            self.assertIsNotNone(payload)
            self.assertEqual(payload['overall_result'], RESULT_UNKNOWN)
            self.assertEqual(payload['terminal_reason'], 'INTERRUPTED')
            self.assertEqual(payload['print_state'], 'skipped')
            self.assertFalse(manager2.is_busy())
            # persisted state no longer carries an active cycle
            self.assertIsNone(manager2.read_persisted().get('active'))
            self.assertEqual(json.loads(state_path.read_text(encoding='utf-8'))['active'], None)

    def test_persistence_keeps_newest_snapshot_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / 'state.json'
            manager = CycleManager(persist_path=str(state_path), persist_enabled=True)
            first, _ = manager.admit({'program1': 1}, 'software', [1])
            manager.set_phase(first, PHASE_RUNNING_P1)
            manager.finish(first, RESULT_PASS, 'OK')
            second, _ = manager.admit({'program1': 2}, 'software', [2])
            manager.set_phase(second, PHASE_RUNNING_P1)
            persisted = manager.read_persisted()
            self.assertIsNotNone(persisted.get('active'))
            self.assertEqual(persisted['active']['cycle_id'], second.cycle_id)
            self.assertEqual(persisted['last_cycle_id'], second.cycle_id)
            self.assertEqual(list(Path(tmp).glob('*.tmp')), [])

    def test_reset_pending_blocks_admission(self):
        self.manager.set_reset_state('pending', 'some-cycle')
        cycle, error = self.manager.admit({'program1': 1}, 'software', [1])
        self.assertIsNone(cycle)
        self.assertEqual(error, 'RESET_PENDING')
        self.manager.set_reset_state('confirmed', 'some-cycle')
        cycle, error = self.manager.admit({'program1': 1}, 'software', [1])
        self.assertIsNotNone(cycle)


if __name__ == '__main__':
    unittest.main()
