#!/usr/bin/env python3
"""Slice A/B: exact program/pressure/leak/unit decoding with crafted frames.

Covers PLAN_FINAL amendment 8: signed values, byte/word order, unknown unit codes,
status bit contract (bit0 PASS, bit5 cycle end, conservative fail mask, unknown bits).
"""

import unittest

import modbus_utils
import program_selector
from modbus_utils import STATION_ID
from tests.fakes import FakeRTUDevice


class CraftedFrameTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeRTUDevice(station=STATION_ID)
        modbus_utils.set_transport_override(self.device.handle)

    def tearDown(self):
        modbus_utils.clear_transport_override()

    def test_positive_values_units_and_actual_program(self):
        self.device.program = 17
        self.device.pressure = 123.456
        self.device.leak = 7.89
        self.device.pressure_unit_code = 12000
        self.device.leak_unit_code = 51000
        sample = program_selector.read_test_data()
        self.assertAlmostEqual(sample["pressure"], 123.456, places=3)
        self.assertAlmostEqual(sample["leak"], 7.89, places=3)
        self.assertEqual(sample["pressure_unit"], "kPa")
        self.assertEqual(sample["leak_unit"], "mL/min")
        self.assertEqual(sample["actual_program"], 17)

    def test_signed_negative_values(self):
        self.device.pressure = -45.5
        self.device.leak = -0.125
        sample = program_selector.read_test_data()
        self.assertAlmostEqual(sample["pressure"], -45.5, places=3)
        self.assertAlmostEqual(sample["leak"], -0.125, places=3)

    def test_unknown_unit_codes_are_preserved(self):
        self.device.pressure_unit_code = 9999
        self.device.leak_unit_code = 7777
        sample = program_selector.read_test_data()
        self.assertEqual(sample["pressure_unit"], "Code(9999)")
        self.assertEqual(sample["leak_unit"], "Code(7777)")

    def test_program_255_roundtrip(self):
        self.device.program = 255
        self.assertEqual(program_selector.read_current_program(), 255)

    def test_step_code_and_status_roundtrip(self):
        self.device.enter_step(6, status=0x0021)
        self.assertEqual(program_selector.read_step_code(), 6)
        self.assertEqual(program_selector.read_status(), 0x0021)
        sample = program_selector.read_test_data()
        self.assertEqual(sample["step_code"], 6)
        self.assertEqual(sample["test_result"], "PASS")

    def test_status_bits_contract(self):
        parsed = program_selector._parse_status(0x0021)
        self.assertTrue(parsed["pass_bit"])
        self.assertTrue(parsed["cycle_end"])
        self.assertEqual(parsed["fail_flags"], 0)
        self.assertEqual(parsed["unknown_bits"], 0)
        parsed_fail = program_selector._parse_status(0x0002)
        self.assertEqual(parsed_fail["fail_flags"], 0x0002)
        self.assertFalse(parsed_fail["pass_bit"])
        parsed_unknown = program_selector._parse_status(0x0040)
        self.assertEqual(parsed_unknown["unknown_bits"], 0x0040)
        parsed_keyboard = program_selector._parse_status(0x8000)
        self.assertTrue(parsed_keyboard["keyboard_lock"])
        self.assertEqual(parsed_keyboard["unknown_bits"], 0)

    def test_webui_formatter_uses_validated_frame_only(self):
        import webui_server
        self.device.pressure = 10.0
        self.device.leak = 1.0
        frame = program_selector.read_holding_registers(0x0030, 13)
        if frame is None:
            # read_holding_registers goes through the override as well
            frame = self.device.handle(
                f"{STATION_ID:02X}030030000D" +
                modbus_utils.modbus_crc(f"{STATION_ID:02X}030030000D"), 3)
        parsed = webui_server.parse_realtime_data(frame)
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed["pressure"], 10.0, places=3)
        self.assertEqual(parsed["pressure_unit"], "kPa")
        self.assertIn("status_bits", parsed)
        # no padding: short/invalid frames yield None, never zero-filled data
        self.assertIsNone(webui_server.parse_realtime_data(frame[:20]))
        self.assertIsNone(webui_server.parse_realtime_data(""))

    def test_communication_failure_produces_no_sample(self):
        self.device.silent = True
        self.assertIsNone(program_selector.read_test_data())
        self.assertIsNone(program_selector.read_step_code())


if __name__ == "__main__":
    unittest.main()
