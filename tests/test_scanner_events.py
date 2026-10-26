#!/usr/bin/env python3
"""Slice C: scanner raw preservation, bounded events, binding/overflow semantics."""

import time
import unittest

import serial_scanner as scanner


class ScannerEventTests(unittest.TestCase):
    def setUp(self):
        scanner.stop_serial_scanner()
        scanner.set_event_callback(None)
        scanner.clear_binding()
        with scanner._LOCK:
            scanner._QUEUE.clear()
            scanner._EVENTS.clear()
            scanner._STATE.seq = 0
            scanner._STATE.overflowed = False
            scanner._STATE.dropped_count = 0
            scanner._STATE.last_result = ""

    def tearDown(self):
        scanner.stop_serial_scanner()
        scanner.set_event_callback(None)
        scanner.clear_binding()

    def test_repeated_payload_is_preserved_verbatim(self):
        event = scanner.publish_scan_for_test(b"ABCABC\r\n")
        self.assertEqual(event.code, "ABCABC")
        self.assertEqual(bytes.fromhex(event.raw_hex.replace(" ", "")), b"ABCABC")

    def test_event_ids_use_session_and_seq(self):
        first = scanner.publish_scan_for_test(b"P1\r\n")
        second = scanner.publish_scan_for_test(b"P2\r\n")
        self.assertEqual(first.event_id, f"{first.session_id}:{first.seq}")
        self.assertEqual(second.seq, first.seq + 1)
        self.assertNotEqual(first.event_id, second.event_id)

    def test_queue_is_bounded_and_overflow_is_visible(self):
        original_max = scanner.SCAN_QUEUE_MAX
        scanner.SCAN_QUEUE_MAX = 3
        try:
            events = [scanner.publish_scan_for_test(f"P{i}\r\n".encode()) for i in range(6)]
            state = scanner.get_scanner_state()
            self.assertTrue(state["overflowed"])
            self.assertGreaterEqual(state["dropped_count"], 3)
            # newest event is always kept and readable
            self.assertIsNotNone(scanner.get_event(events[-1].event_id))
        finally:
            scanner.SCAN_QUEUE_MAX = original_max

    def test_overflow_invalidates_binding_from_dropped_event(self):
        original_max = scanner.SCAN_QUEUE_MAX
        scanner.SCAN_QUEUE_MAX = 2
        try:
            first = scanner.publish_scan_for_test(b"OLD\r\n")
            scanner.set_binding(first.event_id)
            self.assertIsNotNone(scanner.get_binding())
            scanner.publish_scan_for_test(b"N1\r\n")
            scanner.publish_scan_for_test(b"N2\r\n")
            scanner.publish_scan_for_test(b"N3\r\n")
            self.assertIsNone(scanner.get_binding())
            self.assertTrue(scanner.binding_invalidated())
        finally:
            scanner.SCAN_QUEUE_MAX = original_max

    def test_binding_consume_is_single_shot(self):
        event = scanner.publish_scan_for_test(b"PART-1\r\n")
        scanner.set_binding(event.event_id)
        consumed = scanner.consume_binding()
        self.assertEqual(consumed["code"], "PART-1")
        self.assertIsNone(scanner.consume_binding())
        self.assertFalse(scanner.has_unconsumed_binding())

    def test_manual_binding_is_not_device_verified(self):
        binding = scanner.create_manual_binding("TYPED-123")
        self.assertIsNotNone(binding)
        self.assertEqual(binding["source"], "manual")
        self.assertFalse(binding["device_verified"])

    def test_event_callback_receives_each_event_once(self):
        seen = []
        scanner.set_event_callback(lambda event: seen.append(event["event_id"]))
        first = scanner.publish_scan_for_test(b"S1\r\n")
        second = scanner.publish_scan_for_test(b"S2\r\n")
        self.assertEqual(seen, [first.event_id, second.event_id])

    def test_explicit_lifecycle_start_stop_without_serial_port(self):
        import serial as real_serial

        class IdleSerial:
            is_open = True

            def __init__(self, *args, **kwargs):
                self.timeout = kwargs.get("timeout", 0.05)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def reset_input_buffer(self):
                pass

            def read(self, size):
                time.sleep(0.01)
                return b""

        original = real_serial.Serial
        real_serial.Serial = IdleSerial
        try:
            scanner.start_serial_scanner()
            scanner.start_serial_scanner()  # idempotent
            self.assertTrue(scanner.scanner_running())
            scanner.stop_serial_scanner(timeout=2.0)
            self.assertFalse(scanner.scanner_running())
        finally:
            real_serial.Serial = original


if __name__ == "__main__":
    unittest.main()
