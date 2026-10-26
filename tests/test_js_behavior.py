#!/usr/bin/env python3
"""Slice E: embedded JS behavioral checks (barcode cursor, print UI, P1 retention).

Runs extracted pure functions in Node; no browser, server or hardware.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.extract_js import extract_script


PROJECT = Path(__file__).resolve().parent.parent


def extract_function(script: str, name: str) -> str:
    marker = f"function {name}("
    start = script.index(marker)
    brace = script.index("{", start)
    depth = 0
    for index in range(brace, len(script)):
        char = script[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return script[start:index + 1]
    raise AssertionError(f"unterminated function {name}")


class JsBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = extract_script(
            (PROJECT / "webui_server.py").read_text(encoding="utf-8"))

    def test_p1_snapshot_not_overwritten_by_realtime(self):
        # P1 retention: no realtime-to-result overwrite path may exist.
        self.assertNotIn("syncProgram1ResultFromRealtime", self.script)
        self.assertNotIn("pendingProgram1RealtimeFinalize", self.script)
        self.assertIn("result.program1_pressure", self.script)
        # live monitor may only write the monitor card values
        self.assertNotIn(
            "document.getElementById('program1-pressure').textContent = pressureValue",
            self.script)

    def test_scanner_cursor_never_reset_to_zero(self):
        self.assertNotIn("lastScannerSeq = 0", self.script)
        self.assertIn("scannerCursorAdvance", self.script)
        self.assertIn("last_scanner_session", self.script)

    def test_manual_print_requires_record_id(self):
        self.assertIn("没有可补打的已保存PASS记录", self.script)
        self.assertIn("record_id: recordId", self.script)

    def test_leak_curve_uses_step6_not_final_value_steps(self):
        charts = extract_function(self.script, "updateCharts")
        self.assertIn(
            "const isTestStart = stepCode === 6 && lastStepCode !== 6;", charts)
        self.assertIn("if (stepCode === 6) {", charts)
        self.assertNotIn("if (stepCode === 7 || stepCode === 65535)", charts)
        self.assertNotIn("const isLeakStart = stepCode === 7", charts)
        # The live monitor may show step 6 changes and the final 7/65535 value;
        # this is deliberately independent from the curve acquisition window.
        self.assertIn(
            "const captureLeakRealtime = data.step_code === 6 || "
            "data.step_code === 7 || data.step_code === 65535;", self.script)

    def test_node_behavioral_harness(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        harness = "\n".join([
            "const assert = require('assert');",
            extract_function(self.script, "scannerCursorAdvance"),
            extract_function(self.script, "resolvePrintUi"),
            # barcode cursor: same session, newer seq -> new event
            "assert.strictEqual(scannerCursorAdvance('s1', 5, 's1', 4).isNew, true);",
            # same event replayed -> not new, seq does not regress
            "const replay = scannerCursorAdvance('s1', 4, 's1', 4);",
            "assert.strictEqual(replay.isNew, false);",
            "assert.strictEqual(replay.seq, 4);",
            # backend restart: new session accepted even with seq starting at 1
            "assert.strictEqual(scannerCursorAdvance('s2', 1, 's1', 99).isNew, true);",
            # completion must never reset the cursor (seq 0 is not an event)
            "assert.strictEqual(scannerCursorAdvance('s2', 0, 's2', 7).isNew, false);",
            # manual print UI: printed clears QR
            "assert.strictEqual(resolvePrintUi({printed: true, serial_number: 'SN'}).clearQr, true);",
            # disabled printer never clears QR and never says printed
            "assert.strictEqual(resolvePrintUi({printed: false, state: 'disabled'}).clearQr, false);",
            "assert.strictEqual(resolvePrintUi({printed: false, state: 'disabled'}).notify, 'warning');",
            # busy reprint keeps the code
            "assert.strictEqual(resolvePrintUi({printed: false, state: 'busy'}).clearQr, false);",
            # failure path
            "assert.strictEqual(resolvePrintUi({printed: false, state: 'failed'}).notify, 'error');",
            "assert.strictEqual(resolvePrintUi(null).clearQr, false);",
            # A busy software/hardware cycle must not suppress realtime pressure.
            "const monitorElements = Object.fromEntries(['pressure-value', 'pressure-unit', 'leak-value', 'leak-unit', 'device-status', 'monitor-status'].map(id => [id, {textContent: '', className: ''}]));",
            "const document = {getElementById: id => monitorElements[id]};",
            "let lastValidPressure = null;",
            "let lastValidPressureUnit = null;",
            "let lastValidLeak = null;",
            "let lastValidLeakUnit = null;",
            "let lastStepcode = 0;",
            "let monitorDataPolling = false;",
            "let monitorResponse = {success: true, busy: true, step_code: 6, pressure: 12.3456, pressure_unit: 'kPa', leak: -0.1234, leak_unit: 'mL/min', status: 0, status_text: '测试'};",
            "const fetch = async () => ({json: async () => monitorResponse});",
            "const clearStatusLights = () => {};",
            "const updateStatusLights = () => {};",
            "async " + extract_function(self.script, "updateMonitorData"),
            "(async () => {",
            "  await updateMonitorData();",
            "  assert.strictEqual(monitorElements['pressure-value'].textContent, '12.346');",
            "  assert.strictEqual(monitorElements['pressure-unit'].textContent, 'kPa');",
            "  assert.strictEqual(monitorDataPolling, false);",
            "  monitorResponse = {...monitorResponse, step_code: 65535, pressure: -0.08};",
            "  await updateMonitorData();",
            "  assert.strictEqual(monitorElements['pressure-value'].textContent, '-0.080');",
            "  console.log('JS_BEHAVIOR_OK');",
            "})().catch(error => { console.error(error); process.exit(1); });",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "harness.js"
            harness_path.write_text(harness, encoding="utf-8")
            result = subprocess.run([node, str(harness_path)],
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("JS_BEHAVIOR_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
