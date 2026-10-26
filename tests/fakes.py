#!/usr/bin/env python3
"""Deterministic fakes for offline ATEQ tests.

Boundary-level only: fake RTU device/transport, fake clock, scripted device adapter,
fake print runner. Business functions under test are never mocked to return success.
"""

from __future__ import annotations

from collections import deque
import threading
import time

import serial

from modbus_utils import modbus_crc


def swap16(value: int) -> int:
    value &= 0xFFFF
    return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)


class FakeRTUDevice:
    """A minimal Modbus RTU instrument model for FC03/05/06/16."""

    def __init__(self, station: int = 255):
        self.station = station
        self.program = 1                  # 1-based program number
        self.step_code = 65535
        self.status = 0
        self.pressure = 0.0
        self.leak = 0.0
        self.pressure_unit_code = 12000   # kPa per existing table
        self.leak_unit_code = 51000       # mL/min
        self.open_failure = False
        self.open_failure_once = False
        self.silent = False
        self.write_timeout = False
        self.write_program_steps: set[int] = set()
        self.commands: list[tuple[int, str]] = []
        self.on_frame = None              # callable(frame_hex) for gating tests
        # Parameter (program times) support: program -> {param_id: milliseconds}
        self.param_programs = {
            1: {0x0001: 7000, 0x0002: 2000, 0x0003: 5000},
            2: {0x0001: 3000, 0x0002: 1500, 0x0003: 4000},
        }
        self.selected_param_program = 1
        self._seq = 0
        self._lock = threading.Lock()

    # -- state helpers -------------------------------------------------
    def set_pressure(self, value: float) -> None:
        self.pressure = value

    def set_leak(self, value: float) -> None:
        self.leak = value

    def enter_step(self, step_code: int, status: int | None = None) -> None:
        self.step_code = step_code
        if status is not None:
            self.status = status

    def complete(self, status: int) -> None:
        self.step_code = 65535
        self.status = status

    # -- frame handling ------------------------------------------------
    def handle(self, frame_hex: str, timeout: float = 3):
        with self._lock:
            self._seq += 1
            seq = self._seq
            self.commands.append((seq, frame_hex.upper()))
        if self.open_failure or self.open_failure_once:
            self.open_failure_once = False
            raise serial.SerialException("fake port open failure")
        if self.write_timeout:
            raise serial.SerialTimeoutException("fake write timeout")
        if self.on_frame is not None:
            self.on_frame(frame_hex)
        if self.silent:
            return None

        frame = bytes.fromhex(frame_hex)
        if len(frame) < 4:
            return None
        station, func = frame[0], frame[1]
        if station != self.station:
            return None

        if func == 0x03:
            address = int.from_bytes(frame[2:4], "big")
            count = int.from_bytes(frame[4:6], "big")
            if address == 0x0030 and count == 13:
                return self._registers_response()
            if address == 0x0000 and count == 9:
                return self._param_response()
            return None
        if func == 0x06:
            address = int.from_bytes(frame[2:4], "big")
            value = int.from_bytes(frame[4:6], "big")
            if address == 0x0200:
                if self.step_code in (4, 5, 6):
                    return self._exception(func, 0x06)
                self.program = swap16(value) + 1
                return frame_hex.upper()
            if address == 0x0000:
                return frame_hex.upper()
            return self._exception(func, 0x02)
        if func == 0x05:
            address = int.from_bytes(frame[2:4], "big")
            value = int.from_bytes(frame[4:6], "big")
            if address == 0x0001 and value == 0xFF00:
                if self.step_code not in (4, 5, 6):
                    self.step_code = 4
                    self.status = 0
                return frame_hex.upper()
            if address == 0x0001 and value == 0x0000:
                return frame_hex.upper()
            if address == 0x0000 and value == 0xFF00:
                self.step_code = 65535
                self.status = 0
                return frame_hex.upper()
            return frame_hex.upper()
        if func == 0x10:
            address = int.from_bytes(frame[2:4], "big")
            if address == 0x0000:
                return self._echo(func, frame)
            if address == 0x3004:
                count = int.from_bytes(frame[4:6], "big")
                byte_count = frame[6]
                payload = frame[7:7 + byte_count]
                if count >= 1 and len(payload) >= 2:
                    # payload value is (program-1) little-endian; decode back to 1-based
                    self.selected_param_program = (payload[0] | (payload[1] << 8)) + 1
                return self._echo(func, frame)
            return self._exception(func, 0x02)
        return self._exception(func, 0x01)

    def _registers_response(self) -> str:
        regs = [0] * 13
        regs[0] = swap16(self.program - 1)
        regs[3] = swap16(self.status)
        regs[4] = swap16(self.step_code)
        p_raw = int(round(self.pressure * 1000.0)) & 0xFFFFFFFF
        regs[5] = swap16(p_raw & 0xFFFF)
        regs[6] = swap16((p_raw >> 16) & 0xFFFF)
        regs[7] = swap16(self.pressure_unit_code & 0xFFFF)
        regs[8] = swap16((self.pressure_unit_code >> 16) & 0xFFFF)
        l_raw = int(round(self.leak * 1000.0)) & 0xFFFFFFFF
        regs[9] = swap16(l_raw & 0xFFFF)
        regs[10] = swap16((l_raw >> 16) & 0xFFFF)
        regs[11] = swap16(self.leak_unit_code & 0xFFFF)
        regs[12] = swap16((self.leak_unit_code >> 16) & 0xFFFF)
        data = b"".join(r.to_bytes(2, "big") for r in regs)
        body = bytes([self.station, 0x03, len(data)]) + data
        return (body + bytes.fromhex(modbus_crc(body.hex()))).hex().upper()

    def _param_response(self) -> str:
        values = self.param_programs.get(self.selected_param_program, {})
        data = bytearray()
        for param_id in (0x0001, 0x0002, 0x0003):
            value_ms = int(values.get(param_id, 0))
            low = value_ms & 0xFFFF
            high = (value_ms >> 16) & 0xFFFF
            data += swap16(param_id).to_bytes(2, "big")
            data += swap16(low).to_bytes(2, "big")
            data += swap16(high).to_bytes(2, "big")
        body = bytes([self.station, 0x03, len(data)]) + bytes(data)
        return (body + bytes.fromhex(modbus_crc(body.hex()))).hex().upper()

    def _exception(self, func: int, code: int) -> str:
        body = bytes([self.station, func | 0x80, code])
        return (body + bytes.fromhex(modbus_crc(body.hex()))).hex().upper()

    @staticmethod
    def _echo(func: int, frame: bytes) -> str:
        body = bytes([frame[0], func]) + frame[2:6]
        return (body + bytes.fromhex(modbus_crc(body.hex()))).hex().upper()


class FakeClock:
    """Monotonic clock with manual control; sleep advances instead of blocking."""

    def __init__(self, start: float = 10_000.0):
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += float(seconds)


class ScriptedAdapter:
    """Device adapter used by TestExecutor tests.

    Records every wire-level action; callers script stepcodes/samples/readbacks and
    may gate calls with Events to create deterministic interleavings.
    """

    def __init__(self, clock: FakeClock | None = None):
        self.clock = clock or FakeClock()
        self.write_program_calls: list[int] = []
        self.start_calls = 0
        self.reset_calls = 0
        self.read_step_calls = 0
        self.read_sample_calls = 0
        self.events: list[tuple[str, float, object]] = []
        self.step_script: deque = deque()
        self.default_step = 65535
        self.sample_script: deque = deque()
        self.default_sample = None
        self.readback_values: deque = deque()
        self.default_readback = None
        self.readback_none = False
        self.write_ok = True
        self.write_message = "Program written and verified"
        self.start_ok = True
        self.reset_ok = True
        self.write_gate: threading.Event | None = None
        self.write_entered = threading.Event()
        self.read_program_gate: threading.Event | None = None
        self.read_program_entered = threading.Event()
        self.before_write = None          # callable(program) hook
        self.after_write = None           # callable(program) hook (after recording)
        self.before_start = None
        self.after_read_step = None       # callable(count, value)
        self.after_read_sample = None     # callable(count, value)
        self.sample_error = None          # error code returned by read_sample_ex
        self.step_return_override = None  # fixed step code (e.g. idle proof in tests)
        self._lock = threading.Lock()

    def _record(self, name: str, value=None):
        with self._lock:
            self.events.append((name, self.clock.now(), value))

    def write_program(self, program_number, cancel_check=None):
        if cancel_check is not None and cancel_check():
            return False, "cancelled before program write"
        self.write_entered.set()
        if self.write_gate is not None:
            self.write_gate.wait(5.0)
        if self.before_write is not None:
            self.before_write(program_number)
        self.write_program_calls.append(program_number)
        self._record("write_program", program_number)
        if self.after_write is not None:
            self.after_write(program_number)
        if not self.write_ok:
            return False, self.write_message
        return True, self.write_message

    def read_current_program(self):
        self.read_program_entered.set()
        if self.read_program_gate is not None:
            self.read_program_gate.wait(10.0)
        self._record("read_current_program", None)
        if self.readback_values:
            value = self.readback_values.popleft()
        else:
            value = self.default_readback
        if value is None and self.readback_none:
            return None
        if value is None and self.write_program_calls:
            return self.write_program_calls[-1]
        return value

    def start_test(self, cancel_check=None):
        if cancel_check is not None and cancel_check():
            return False
        self.start_calls += 1
        self._record("start_test", None)
        if self.before_start is not None:
            self.before_start()
        return self.start_ok

    def reset_device(self):
        self.reset_calls += 1
        self._record("reset_device", None)
        return self.reset_ok

    def read_step_code(self):
        self.read_step_calls += 1
        with self._lock:
            if self.step_return_override is not None:
                value = self.step_return_override
            elif self.step_script:
                value = self.step_script.popleft()
            else:
                value = self.default_step
        self._record("read_step_code", value)
        if self.after_read_step is not None:
            self.after_read_step(self.read_step_calls, value)
        return value

    def read_sample(self):
        self.read_sample_calls += 1
        with self._lock:
            if self.sample_script:
                value = self.sample_script.popleft()
            else:
                value = self.default_sample
        self._record("read_sample", value)
        if self.after_read_sample is not None:
            self.after_read_sample(self.read_sample_calls, value)
        return value

    def read_sample_ex(self):
        """Production adapter parity: return (sample, error_code)."""
        return self.read_sample(), self.sample_error


def sample(step_code=6, result="PASS", status=0x0021, pressure=100.0, leak=0.5,
           pressure_unit="kPa", leak_unit="mL/min", actual_program=1):
    return {
        "step_code": step_code,
        "status": status,
        "test_result": result,
        "pressure": pressure,
        "leak": leak,
        "pressure_unit": pressure_unit,
        "leak_unit": leak_unit,
        "actual_program": actual_program,
    }


def reset_scanner_state(scanner_module):
    scanner_module.stop_serial_scanner()
    scanner_module.set_event_callback(None)
    scanner_module.clear_binding()
    with scanner_module._LOCK:
        scanner_module._QUEUE.clear()
        scanner_module._EVENTS.clear()
        scanner_module._STATE.seq = 0
        scanner_module._STATE.overflowed = False
        scanner_module._STATE.dropped_count = 0
        scanner_module._STATE.last_result = ""


class FakeScanner:
    """In-memory scanner module double (raw event layer stays real code)."""

    def __init__(self):
        self.events = {}
        self.binding = None
        self.cleared = []
        self._seq = 0

    def add_event(self, code, device_verified=True):
        self._seq += 1
        event_id = f"session:{self._seq}"
        event = {
            "event_id": event_id,
            "session_id": "session",
            "seq": self._seq,
            "code": code,
            "device_verified": device_verified,
            "processed": False,
            "result": "PENDING",
        }
        self.events[event_id] = event
        return event

    def get_event(self, event_id):
        return self.events.get(event_id)

    def set_binding(self, event_id, source=None):
        event = self.events.get(event_id)
        if event is None:
            return None
        self.binding = dict(event)
        event["result"] = "BOUND"
        return dict(self.binding)

    def get_binding(self):
        return dict(self.binding) if self.binding else None

    def consume_binding(self):
        binding = self.binding
        self.binding = None
        if binding:
            self.events[binding["event_id"]]["processed"] = True
        return dict(binding) if binding else None

    def clear_binding(self, reason=""):
        self.cleared.append(reason)
        self.binding = None

    def mark_event_result(self, event_id, result, started=False, message="",
                          processed=True):
        event = self.events.get(event_id)
        if event is None:
            return None
        event["result"] = result
        event["started"] = started
        event["message"] = message
        event["processed"] = processed
        return dict(event)


class FakeRecorder:
    def __init__(self, fail=False, serial="20260101-MODEL-00001"):
        self.fail = fail
        self.serial = serial
        self.calls = []

    def __call__(self, cycle):
        self.calls.append(cycle.cycle_id)
        if self.fail:
            return {"record_id": None, "serial_number": None, "created": False,
                    "error": "record save failed: fake"}
        return {"record_id": 101, "serial_number": self.serial, "created": True,
                "error": None}


class FakePrinter:
    def __init__(self, state="submitted", success=True, disabled=False):
        self.state = state
        self.success = success
        self.disabled = disabled
        self.calls = []

    def __call__(self, cycle):
        self.calls.append(cycle.cycle_id)
        return {"success": self.success, "printed": self.success,
                "state": self.state, "message": "fake print",
                "disabled": self.disabled}


class FakePrintRunner:
    """Injected into bartender_print so tests never touch D:/BarTender."""

    def __init__(self, returncode=0, timeout=False, delay=0.0):
        self.returncode = returncode
        self.timeout = timeout
        self.delay = delay
        self.calls: list[list[str]] = []
        self._lock = threading.Lock()

    def run(self, cmd, **kwargs):
        with self._lock:
            self.calls.append(list(cmd))
        if self.timeout:
            raise __import__("subprocess").TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))
        if self.delay:
            time.sleep(self.delay)
        return __import__("subprocess").CompletedProcess(cmd, self.returncode, "", "")
