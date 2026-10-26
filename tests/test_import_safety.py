#!/usr/bin/env python3
"""Slice A: importing the modules must not touch hardware, threads or host processes."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent

IMPORT_SCRIPT = r"""
import threading
import serial

def _boom(*args, **kwargs):
    raise AssertionError('serial port was opened at import time')

serial.Serial = _boom
serial.SerialException = serial.SerialException

before = {t.name for t in threading.enumerate()}
import modbus_utils
import serial_scanner
import cycle_state
import program_selector
import database
import webui_server
after = {t.name for t in threading.enumerate()}

new_threads = after - before
assert not [n for n in new_threads if 'Scanner' in n], new_threads
assert not [n for n in new_threads if 'Supervisor' in n], new_threads
assert not [n for n in new_threads if n.startswith('ATEQ')], new_threads
assert program_selector.get_test_executor() is program_selector.get_test_executor()
print('IMPORT_OK')
"""


class ImportSafetyTests(unittest.TestCase):
    def test_import_starts_no_hardware_or_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env['PYTHONPATH'] = str(PROJECT)
            env['ATEQ_DISABLE_HARDWARE'] = '1'
            env['ATEQ_TEST_DB'] = str(Path(tmp) / 'test.db')
            env['ATEQ_SERIAL_DB'] = str(Path(tmp) / 'serial.db')
            env['ATEQ_RUNTIME_STATE'] = str(Path(tmp) / 'state.json')
            env['ATEQ_SCANNER_LOG'] = str(Path(tmp) / 'scanner.log')
            env['ATEQ_LINE_SETTINGS'] = str(Path(tmp) / 'line_settings.json')
            env['ATEQ_PRODUCT_CONFIG'] = str(Path(tmp) / 'products.json')
            result = subprocess.run(
                [sys.executable, '-c', IMPORT_SCRIPT],
                cwd=str(PROJECT), env=env, capture_output=True, text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn('IMPORT_OK', result.stdout)
            self.assertFalse((Path(tmp) / 'state.json').exists())
            # importing webui_server must not initialize databases or kill processes
            self.assertFalse((Path(tmp) / 'test.db').exists())
            self.assertFalse((Path(tmp) / 'products.json').exists())

    def test_scanner_import_creates_no_log_file_under_test_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "scanner.log"
            env = dict(os.environ)
            env['PYTHONPATH'] = str(PROJECT)
            env['ATEQ_DISABLE_HARDWARE'] = '1'
            env['ATEQ_SCANNER_LOG'] = str(log_path)
            result = subprocess.run(
                [sys.executable, '-c', 'import serial_scanner; print("OK")'],
                cwd=str(PROJECT), env=env, capture_output=True, text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse(log_path.exists(),
                             "test env import must not open the scanner log")

    def test_program_selector_module_import_creates_no_singleton_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env['PYTHONPATH'] = str(PROJECT)
            env['ATEQ_RUNTIME_STATE'] = str(Path(tmp) / 'state.json')
            result = subprocess.run(
                [sys.executable, '-c', 'import program_selector; print("OK")'],
                cwd=str(PROJECT), env=env, capture_output=True, text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertFalse((Path(tmp) / 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
