#!/usr/bin/env python3
"""Slice A: modbus_utils frame validation, typed errors, atomic transactions."""

import threading
import unittest

import serial

import modbus_utils
from modbus_utils import (
    ERROR_CRC_MISMATCH,
    ERROR_EXCEPTION_RESPONSE,
    ERROR_OK,
    ERROR_PORT_OPEN_FAILED,
    ERROR_PROTOCOL,
    ERROR_READ_TIMEOUT,
    ERROR_WRITE_TIMEOUT,
    STATION_ID,
    modbus_crc,
    read_holding_registers,
    send_raw,
    send_raw_ex,
)
from tests.fakes import FakeRTUDevice


class ModbusUtilsTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeRTUDevice(station=STATION_ID)
        modbus_utils.set_transport_override(self.device.handle)

    def tearDown(self):
        modbus_utils.clear_transport_override()

    def test_crc_is_deterministic_and_hex(self):
        crc = modbus_crc("01030030000D")
        self.assertEqual(len(crc), 4)
        int(crc, 16)
        self.assertEqual(crc, modbus_crc("01030030000D"))

    def test_valid_read_returns_frame(self):
        result = send_raw_ex(f"{STATION_ID:02X}030030000D" + "0000")
        # CRC in the request is intentionally rebuilt by read_holding_registers below.
        response = read_holding_registers(0x0030, 13)
        self.assertIsNotNone(response)
        self.assertTrue(response.startswith(f"{STATION_ID:02X}03"))

    def test_exception_frame_is_not_truthy_success(self):
        body = bytes([STATION_ID, 0x86, 0x02])
        exception_frame = (body + bytes.fromhex(modbus_crc(body.hex()))).hex().upper()
        modbus_utils.set_transport_override(lambda frame, timeout: exception_frame)
        result = send_raw_ex("FF0602000001" + "0000")
        self.assertIsNone(result["response"])
        self.assertEqual(result["error_code"], ERROR_EXCEPTION_RESPONSE)
        self.assertIn("非法数据地址", result["message"])
        self.assertIsNone(send_raw("FF0602000001" + "0000"))

    def test_standard_exception_codes_translate_to_chinese(self):
        expected = {
            0x01: "非法功能码",
            0x02: "非法数据地址",
            0x03: "非法数据值",
            0x04: "从站设备故障",
            0x05: "确认",
            0x06: "从站设备忙",
        }
        for code, fragment in expected.items():
            message = modbus_utils.modbus_exception_message(code)
            self.assertIn(fragment, message, f"code 0x{code:02X}: {message}")
            self.assertIn(f"0x{code:02X}", message)
        unknown = modbus_utils.modbus_exception_message(0x7F)
        self.assertIn("未收录", unknown)
        self.assertIn("0x7F", unknown)

    def test_crc_mismatch_is_reported(self):
        good = self.device._registers_response()
        bad = good[:-2] + ("00" if good[-2:] != "00" else "11")
        modbus_utils.set_transport_override(lambda frame, timeout: bad)
        result = send_raw_ex("FF030030000D" + "0000")
        self.assertEqual(result["error_code"], ERROR_CRC_MISMATCH)
        self.assertIsNone(result["response"])

    def test_short_frame_is_protocol_error(self):
        modbus_utils.set_transport_override(lambda frame, timeout: "FF0300")
        result = send_raw_ex("FF030030000D" + "0000")
        self.assertEqual(result["error_code"], ERROR_PROTOCOL)

    def test_silent_device_is_read_timeout_and_opened(self):
        self.device.silent = True
        result = send_raw_ex("FF030030000D" + "0000")
        self.assertEqual(result["error_code"], ERROR_READ_TIMEOUT)
        self.assertTrue(result["opened"])

    def test_open_failure_is_distinct(self):
        self.device.open_failure_once = True

        def failing(frame, timeout):
            raise serial.SerialException("cannot open fake port")

        modbus_utils.set_transport_override(failing)
        result = send_raw_ex("FF030030000D" + "0000")
        self.assertEqual(result["error_code"], ERROR_PORT_OPEN_FAILED)
        self.assertFalse(result["opened"])

    def test_write_timeout_is_distinct(self):
        def raising_transport(frame, timeout_value):
            raise serial.SerialTimeoutException("write timed out")

        modbus_utils.set_transport_override(raising_transport)
        result = send_raw_ex("FF030030000D" + "0000")
        self.assertEqual(result["error_code"], ERROR_WRITE_TIMEOUT)

    def test_multi_frame_transaction_is_atomic(self):
        observed = []

        def handler(frame_hex, timeout):
            observed.append(threading.current_thread().name)
            return self.device.handle(frame_hex, timeout)

        modbus_utils.set_transport_override(handler)

        def worker():
            with modbus_utils.transaction():
                send_raw_ex("FF030030000D" + "0000")
                send_raw_ex("FF030030000D" + "0000")

        threads = [threading.Thread(target=worker, name=f"tx-{i}") for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Each transaction's two frames must appear adjacent in the global order.
        self.assertEqual(len(observed), 16)
        for i in range(0, len(observed), 2):
            self.assertEqual(observed[i], observed[i + 1])

    def test_defaults_are_field_contract(self):
        # User-confirmed field fact (2026-10-01): instrument moved to COM8.
        self.assertEqual(modbus_utils.SERIAL_PORT, "COM8")
        self.assertEqual(modbus_utils.BAUDRATE, 9600)
        self.assertEqual(modbus_utils.PARITY, "E")
        self.assertEqual(STATION_ID, 255)


if __name__ == "__main__":
    unittest.main()
