#!/usr/bin/env python3
"""Associated five-file export: persistence boundary and timer regressions."""

from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.TemporaryDirectory(prefix="ateq_associated_labels_")
os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")
os.environ["ATEQ_TEST_DB"] = str(Path(_TMP.name) / "test.db")
os.environ["ATEQ_SERIAL_DB"] = str(Path(_TMP.name) / "serial.db")
os.environ["ATEQ_RUNTIME_STATE"] = str(Path(_TMP.name) / "state.json")
os.environ["ATEQ_LABEL_DIR"] = str(Path(_TMP.name) / "print-data")
os.environ["ATEQ_ASSOCIATED_LABEL_DIR"] = str(Path(_TMP.name) / "associated")
os.environ["ATEQ_LABEL_FILE_CLEAR_SECONDS"] = "10"

import label_data_files  # noqa: E402
import program_selector  # noqa: E402
from cycle_state import CycleManager  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeClock, FakePrinter, FakeRecorder, FakeScanner, ScriptedAdapter, sample)


class FakeTimer:
    instances = []

    def __init__(self, interval, callback, args=()):
        self.interval = interval
        self.callback = callback
        self.args = args
        self.started = False
        self.cancelled = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # Manual firing also works after cancel so stale-callback races are tested.
        self.callback(*self.args)


def make_cycle(executor, *, program2=0, serial="20261026-M1-00001"):
    cycle, _ = executor._manager.admit({
        "product_model": "暖风芯体",
        "operator": "张三",
        "program1": 1,
        "program2": program2,
    }, "software", [1] + ([2] if program2 else []))
    cycle.serial_number = serial
    executor._manager.update_phase(
        cycle, "p1", pressure=15.0, pressure_unit="kPa",
        leak=0.0123, leak_unit="mL/min")
    if program2:
        executor._manager.update_phase(
            cycle, "p2", pressure=16.0, pressure_unit="kPa",
            leak=-2.5, leak_unit="Pa/s")
    return cycle


def make_executor(*, recorder=None, printer=None):
    clock = FakeClock()
    adapter = ScriptedAdapter(clock)
    adapter.default_step = 65535
    adapter.default_sample = sample()
    executor = program_selector.TestExecutor(
        io=adapter, clock=clock.now, sleeper=clock.sleep,
        manager=CycleManager(persist_enabled=False),
        recorder=recorder or FakeRecorder(),
        printer=printer or FakePrinter(disabled=True),
        settings_provider=lambda: {"scan_required": False, "scan_start": False,
                                   "printer_enabled": False},
        scanner_module=FakeScanner(), persist_enabled=False)
    return executor


class LabelDataFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(
            prefix="associated-case-", dir=_TMP.name))
        FakeTimer.instances = []
        self.timer_patch = mock.patch.object(label_data_files.threading, "Timer", FakeTimer)
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.addCleanup(label_data_files.cancel_pending_clear)

    def cycle(self, **kwargs):
        return make_cycle(make_executor(), **kwargs)

    def read(self, filename):
        return (self.directory / filename).read_text(encoding="gbk")

    def test_saved_cycle_writes_exact_five_values_in_gbk(self):
        cycle = self.cycle(program2=2)
        saved_at = datetime(2026, 10, 26, 14, 13, 5).timestamp()
        result = label_data_files.write_saved_cycle(
            cycle, directory=self.directory, saved_at=saved_at)
        self.assertEqual(self.read("产品名称.txt"), "暖风芯体")
        self.assertEqual(self.read("代号.txt"), "20261026-M1-00001")
        self.assertEqual(self.read("检验员.txt"), "张三")
        self.assertEqual(self.read("日期时间.txt"), "2026-10-26 14:13:05")
        self.assertEqual(self.read("泄漏量.txt"),
                         "P1=0.0123 mL/min;P2=-2.5 Pa/s")
        self.assertEqual(len(result["files"]), 5)
        self.assertEqual(FakeTimer.instances[0].interval, 10.0)
        self.assertTrue(FakeTimer.instances[0].started)

    def test_single_program_writes_only_its_own_leak(self):
        label_data_files.write_saved_cycle(self.cycle(), directory=self.directory)
        self.assertEqual(self.read("泄漏量.txt"), "P1=0.0123 mL/min")

    def test_ten_second_callback_empties_but_does_not_delete_files(self):
        label_data_files.write_saved_cycle(self.cycle(), directory=self.directory)
        timer = FakeTimer.instances[0]
        self.assertEqual(timer.interval, 10.0)
        timer.fire()
        for filename in label_data_files.FILENAMES:
            path = self.directory / filename
            self.assertTrue(path.exists(), filename)
            self.assertEqual(path.read_bytes(), b"", filename)

    def test_old_timer_cannot_clear_a_newer_cycle(self):
        first = self.cycle(serial="FIRST")
        label_data_files.write_saved_cycle(first, directory=self.directory)
        old_timer = FakeTimer.instances[0]
        second = self.cycle(serial="SECOND")
        label_data_files.write_saved_cycle(second, directory=self.directory)
        new_timer = FakeTimer.instances[1]
        self.assertTrue(old_timer.cancelled)
        old_timer.fire()
        self.assertEqual(self.read("代号.txt"), "SECOND")
        new_timer.fire()
        self.assertEqual(self.read("代号.txt"), "")


class FinalizationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(
            prefix="associated-integration-", dir=_TMP.name))
        self.env_patch = mock.patch.dict(os.environ, {
            "ATEQ_ASSOCIATED_LABEL_DIR": str(self.directory),
            "ATEQ_LABEL_FILE_CLEAR_SECONDS": "10",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(label_data_files.cancel_pending_clear)
        FakeTimer.instances = []
        self.timer_patch = mock.patch.object(label_data_files.threading, "Timer", FakeTimer)
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)

    def test_disabled_printer_still_writes_after_save(self):
        executor = make_executor(printer=FakePrinter(disabled=True))
        cycle = make_cycle(executor)
        executor._finalize_and_finish(cycle, "PASS", "OK")
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertTrue(payload["record_saved"])
        self.assertEqual(payload["print_state"], "disabled")
        self.assertEqual((self.directory / "代号.txt").read_text(encoding="gbk"),
                         cycle.serial_number)
        self.assertEqual(cycle.label_files_state, "written")

    def test_failed_save_does_not_write(self):
        executor = make_executor(recorder=FakeRecorder(fail=True))
        cycle = make_cycle(executor)
        with mock.patch.object(label_data_files, "write_saved_cycle") as writer:
            executor._finalize_and_finish(cycle, "PASS", "OK")
        writer.assert_not_called()
        self.assertFalse(executor.runtime_snapshot()["completion_payload"]["record_saved"])

    def test_idempotent_existing_record_does_not_republish(self):
        recorder = lambda _cycle: {"record_id": 101, "serial_number": "OLD",
                                   "created": False, "error": None}
        executor = make_executor(recorder=recorder)
        cycle = make_cycle(executor)
        with mock.patch.object(label_data_files, "write_saved_cycle") as writer:
            executor._finalize_and_finish(cycle, "PASS", "OK")
        writer.assert_not_called()

    def test_file_error_does_not_change_save_or_print_result(self):
        executor = make_executor(printer=FakePrinter())
        cycle = make_cycle(executor)
        with mock.patch.object(label_data_files, "write_saved_cycle",
                               side_effect=OSError("disk locked")):
            executor._finalize_and_finish(cycle, "PASS", "OK")
        payload = executor.runtime_snapshot()["completion_payload"]
        self.assertTrue(payload["record_saved"])
        self.assertIsNone(payload["record_error"])
        self.assertEqual(payload["print_state"], "submitted")
        self.assertEqual(cycle.label_files_state, "failed")
        self.assertIn("disk locked", cycle.label_files_message)


if __name__ == "__main__":
    unittest.main()
