#!/usr/bin/env python3
"""ATEQ program/cycle executor.

Plan refs: F01..F11, PLAN_FINAL amendments 2,3,4,7,8,12,14.

Design:
- one CycleManager (cycle_state.py) owns the active cycle; this module performs IO
- one instrument supervisor thread (sampling cache + hardware-cycle detection +
  priority reset execution); explicit lifecycle, no import-time side effects
- one software worker thread per admitted cycle; all device commands go through
  command_lock and re-check the cycle cancel event after acquiring it
- program identity is always a fresh readback; the written_program1 cache is never
  trusted as instrument truth
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path

from database import save_test_record
from modbus_utils import (
    ERROR_OK,
    STATION_ID,
    commit_barrier,
    modbus_crc,
    modbus_exception_message,
    modbus_lock,
    read_holding_registers,
    read_holding_registers_ex,
    send_guard,
    send_raw,
)
from cycle_state import (
    PHASE_CHAINING_P2,
    PHASE_FINALIZING,
    PHASE_IDLE,
    PHASE_PREPARING,
    PHASE_RUNNING_P1,
    PHASE_RUNNING_P2,
    RESULT_ABORTED,
    RESULT_ERROR,
    RESULT_FAIL,
    RESULT_INTERRUPTED,
    RESULT_PASS,
    RESULT_UNKNOWN,
    RESET_CONFIRMED,
    RESET_FAILED,
    RESET_PENDING,
    STATUS_CYCLE_END_BIT,
    STATUS_FAIL_MASK,
    STATUS_KNOWN_BITS,
    STATUS_PASS_BIT,
    CycleCancelled,
    CycleManager,
    CycleRecord,
)

WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502

POLL_INTERVAL = float(os.environ.get('ATEQ_POLL_INTERVAL', '0.1'))
SUPERVISOR_INTERVAL = float(os.environ.get('ATEQ_SUPERVISOR_INTERVAL', '0.3'))
PHASE_TIMEOUT = float(os.environ.get('ATEQ_PHASE_TIMEOUT', '600'))
CHAIN_TIMEOUT = float(os.environ.get('ATEQ_CHAIN_TIMEOUT', '60'))
SAMPLE_STALE_SECONDS = float(os.environ.get('ATEQ_SAMPLE_STALE', '2.0'))
COMM_FAILURE_LIMIT = int(os.environ.get('ATEQ_COMM_FAILURE_LIMIT', '30'))
SCANNER_ENABLED = os.environ.get('ATEQ_SCANNER_ENABLED', '1') != '0'
HARDWARE_DISABLED = os.environ.get('ATEQ_DISABLE_HARDWARE', '0') == '1'


def _line_settings_snapshot():
    """Persisted line settings (scanner_enabled/scanner_port); {} when unavailable."""
    try:
        import line_runtime
        return line_runtime.get_line_settings()
    except Exception:
        return {}


def _persisted_scanner_enabled():
    return bool(_line_settings_snapshot().get("scanner_enabled", True))


def _persisted_scanner_port():
    return str(_line_settings_snapshot().get("scanner_port") or "")

# 单位表：按 ATEQ 手册 UNIT TABLE（第10章，十六进制代码对应十进制标识）。
# 未列出的代码显示 Code(nnnn)，绝不猜单位。
UNIT_TABLE = {
    0: "cm³/s", 1000: "cm³/min", 2000: "cm³/h", 3000: "mm³/h",
    4000: "Calibrated Pa", 5000: "Calibrated Pa/s",
    6000: "Pa", 7000: "High res Pa", 8000: "Pa/s", 9000: "High res Pa/s",
    11000: "Bar", 12000: "kPa", 13000: "PSI", 14000: "mBar", 15000: "MPa",
    43000: "D mode Pa", 44000: "Low res Pa", 45000: "Low res Pa/s",
    46000: "in³/s", 47000: "in³/min", 48000: "in³/h",
    49000: "ft³/h", 58000: "USA cm³/s", 59000: "USA cm³/min", 60000: "USA cm³/h",
    50000: "mL/s", 51000: "mL/min", 52000: "mL/h",
    76000: "ft³/s", 77000: "ft³/min",
}

ACTIVE_STEP_MIN = 4
ACTIVE_STEP_MAX = 100
STEP_COMPLETE = 65535
# 现场协议修订（2026-10-26 用户确认）：
# 压力只取同一周期最后一次 StepCode 6 采样，绝不用 65535 或运行旧值回填；
# 泄漏优先取最后一次 StepCode 7 采样，缺失时才回退到同周期 65535 终止采样。
STEP_LEAK = 7
STEP_PRESSURE = 6


def unit_name(code):
    if code is None:
        return None
    return UNIT_TABLE.get(code, f"Code({code})")


def swap_bytes(value):
    value &= 0xFFFF
    return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)


def _parse_status(status):
    if status is None:
        return None
    return {
        'status': status,
        'pass_bit': bool(status & STATUS_PASS_BIT),
        'fail_flags': status & STATUS_FAIL_MASK,
        'cycle_end': bool(status & STATUS_CYCLE_END_BIT),
        'unknown_bits': status & ~STATUS_KNOWN_BITS,
        'keyboard_lock': bool(status & 0x8000),
    }


def _cycle_evidence_fields(step, sample):
    """Same-cycle measurement evidence keyed by the OBSERVED StepCode.

    - StepCode 6: refresh pressure (the last 6 sample wins)
    - StepCode 7: refresh leak (the last 7 sample wins; 65535 is only a fallback)
    """
    fields = {}
    if not isinstance(sample, dict) or step is None:
        return fields
    if step == STEP_LEAK and sample.get('leak') is not None:
        fields['leak_step7'] = sample.get('leak')
        fields['leak_step7_unit'] = sample.get('leak_unit')
    elif step == STEP_PRESSURE and sample.get('pressure') is not None:
        fields['pressure_step6'] = sample.get('pressure')
        fields['pressure_step6_unit'] = sample.get('pressure_unit')
    return fields


def _read_realtime_registers():
    """Read the validated 13-register realtime block used by the reference adapter."""
    response = read_holding_registers(0x0030, 13)
    if not response:
        raise RuntimeError("ATEQ realtime read timed out or returned no response")
    return _parse_realtime_response(response)


def _parse_realtime_response(response):
    """Validate and decode a realtime frame (raises RuntimeError on any violation)."""
    response = str(response).upper()
    try:
        frame = bytes.fromhex(response)
    except ValueError as exc:
        raise RuntimeError("ATEQ returned an invalid hex frame") from exc

    if len(frame) == 5 and frame[0] == STATION_ID and frame[1] == 0x83:
        if response[-4:] != modbus_crc(response[:-4]):
            raise RuntimeError("ATEQ exception response CRC mismatch")
        raise RuntimeError(f"ATEQ {modbus_exception_message(frame[2])}")

    expected_length = 5 + 13 * 2
    if len(frame) != expected_length:
        raise RuntimeError(f"ATEQ realtime response length {len(frame)} != {expected_length}")
    if frame[0] != STATION_ID or frame[1] != 0x03:
        raise RuntimeError("ATEQ response unit or function does not match request")
    if frame[2] != 26:
        raise RuntimeError("ATEQ realtime register byte count is invalid")
    if response[-4:] != modbus_crc(response[:-4]):
        raise RuntimeError("ATEQ realtime response CRC mismatch")

    return [int.from_bytes(frame[3 + i * 2:5 + i * 2], "big") for i in range(13)]


def _decode_pressure_leak(registers):
    pressure_raw = ((registers[6] & 0xFF) << 24) | ((registers[6] >> 8) << 16) | \
                   ((registers[5] & 0xFF) << 8) | (registers[5] >> 8)
    if pressure_raw & 0x80000000:
        pressure_raw -= 0x100000000
    pressure = pressure_raw / 1000.0

    # Full 32-bit little-endian word pair: low word in registers[7], high in [8].
    pressure_unit_code = ((registers[8] & 0xFF) << 24) | ((registers[8] >> 8) << 16) | \
                         ((registers[7] & 0xFF) << 8) | (registers[7] >> 8)

    leak_raw = ((registers[10] & 0xFF) << 24) | ((registers[10] >> 8) << 16) | \
               ((registers[9] & 0xFF) << 8) | (registers[9] >> 8)
    if leak_raw & 0x80000000:
        leak_raw -= 0x100000000
    leak = leak_raw / 1000.0

    leak_unit_code = ((registers[12] & 0xFF) << 24) | ((registers[12] >> 8) << 16) | \
                     ((registers[11] & 0xFF) << 8) | (registers[11] >> 8)
    return pressure, pressure_unit_code, leak, leak_unit_code


def read_current_program():
    """Read the instrument program number (1..255) or None when unreadable."""
    try:
        return swap_bytes(_read_realtime_registers()[0]) + 1
    except Exception as exc:
        print(f"[ATEQ] Current program read failed: {exc}")
        return None


def write_program(program_number, cancel_check=None):
    """Write program number and verify by readback; refuses while a test is active.

    cancel_check is re-evaluated after bus ownership and immediately before the FC06
    transmit: once stop has returned, no program frame may commit for that cycle.
    """
    try:
        number = int(str(program_number).strip())
    except (TypeError, ValueError):
        return False, "Program number must be an integer from 1 to 255"
    if isinstance(program_number, bool) or not 1 <= number <= 255:
        return False, "Program number must be an integer from 1 to 255"

    try:
        registers = _read_realtime_registers()
    except Exception as exc:
        return False, str(exc)
    if cancel_check is not None and cancel_check():
        return False, "cancelled before program write"
    step_code = swap_bytes(registers[4])
    if ACTIVE_STEP_MIN <= step_code <= ACTIVE_STEP_MAX:
        return False, f"ATEQ is active (StepCode {step_code}); program write refused"

    write_val = swap_bytes(number - 1)
    cmd = f"{STATION_ID:02X}06{0x0200:04X}{write_val:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    # Cancel predicate is bound to the actual send: it is evaluated inside the
    # write-commit critical section, immediately before the transport write.
    with send_guard(cancel_check):
        resp = send_raw(full_cmd)
    if cancel_check is not None and cancel_check():
        if resp is None:
            return False, "cancelled before program write"
        # The frame is already committed (in flight); stop the sequence here.
        return True, f"Program {number} written (cancel observed before readback)"
    if resp and resp.upper() == full_cmd.upper():
        readback = read_current_program()
        if readback == number:
            return True, f"Program {number} written and verified"
        return False, f"ATEQ program readback mismatch: expected {number}, got {readback}"
    return False, f"ATEQ program write failed (no valid echo): {resp}"


def read_step_code():
    """Read the step code or None when unreadable."""
    try:
        return swap_bytes(_read_realtime_registers()[4])
    except Exception as exc:
        print(f"[ATEQ] StepCode read failed: {exc}")
        return None


def _sample_from_registers(registers):
    pressure, pressure_unit_code, leak, leak_unit_code = _decode_pressure_leak(registers)
    status = swap_bytes(registers[3])
    parsed = _parse_status(status)
    test_result = 'PASS' if parsed['pass_bit'] and not parsed['fail_flags'] else 'FAIL'
    return {
        'pressure': pressure,
        'pressure_unit': unit_name(pressure_unit_code),
        'leak': leak,
        'leak_unit': unit_name(leak_unit_code),
        'test_result': test_result,
        'status': status,
        'step_code': swap_bytes(registers[4]),
        'actual_program': swap_bytes(registers[0]) + 1,
    }


def read_test_data():
    """Read a validated standard sample (no fabricated values)."""
    try:
        registers = _read_realtime_registers()
    except Exception as exc:
        print(f"[ATEQ] read_test_data failed: {exc}")
        return None
    return _sample_from_registers(registers)


def read_test_data_ex():
    """Read a sample plus typed transport error (production adapter path)."""
    result = read_holding_registers_ex(0x0030, 13)
    error_code = result.get('error_code')
    if error_code != ERROR_OK or not result.get('response'):
        return None, (error_code or 'READ_TIMEOUT')
    try:
        registers = _parse_realtime_response(result['response'])
    except Exception:
        return None, 'PROTOCOL_ERROR'
    return _sample_from_registers(registers), None


def read_status():
    """Read the status word or None when unreadable."""
    try:
        return swap_bytes(_read_realtime_registers()[3])
    except Exception as exc:
        print(f"[ATEQ] Status read failed: {exc}")
        return None


def is_test_complete():
    """Legacy compat: ONLY StepCode 65535 is the terminal/idle marker.

    StepCode 0 is the instrument's abnormal-reset state and 65525 is not
    recognized as terminal at all (protocol amendment 2026-10-01).
    """
    step = read_step_code()
    return step == STEP_COMPLETE


def wait_for_test_complete(timeout=120, poll_interval=0.5):
    """Legacy compat: poll until idle/completed step code."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if is_test_complete():
            return True
        time.sleep(poll_interval)
    return False


def start_test(cancel_check=None):
    """Pulse the start coil; returns True only on a valid echo of the ON pulse.

    Classification: the ON pulse (0x0001 FF00) is a NEW start and is refused when the
    cycle is already cancelled; the OFF pulse (0x0001 0000) is mandatory in-flight
    cleanup for a committed ON and is always sent within the same bus transaction.
    """
    with modbus_lock:
        if cancel_check is not None and cancel_check():
            return False
        cmd = f"{STATION_ID:02X}05{0x01:04X}FF00"
        full_cmd = cmd + modbus_crc(cmd)
        # ON commit is guarded; if the guard rejects it, no OFF cleanup is needed.
        with send_guard(cancel_check):
            resp = send_raw(full_cmd)
        if resp is None and cancel_check is not None and cancel_check():
            return False
        time.sleep(0.1)
        # OFF is mandatory in-flight cleanup for any committed/attempted ON and is
        # never subject to the cancel predicate.
        off_cmd = f"{STATION_ID:02X}05{0x01:04X}0000"
        send_raw(off_cmd + modbus_crc(off_cmd))
    return resp is not None and resp.startswith(f"{STATION_ID:02X}05")


def reset_device():
    """Pulse the reset coil; returns True only on a valid echo."""
    cmd = f"{STATION_ID:02X}05{0x00:04X}FF00"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd)
    return resp is not None and resp.startswith(f"{STATION_ID:02X}05")


class DeviceAdapter:
    """Production IO boundary used by TestExecutor (fakeable in tests)."""

    def read_sample(self):
        return read_test_data()

    def read_sample_ex(self):
        return read_test_data_ex()

    def read_step_code(self):
        return read_step_code()

    def read_current_program(self):
        return read_current_program()

    def write_program(self, program_number, cancel_check=None):
        return write_program(program_number, cancel_check=cancel_check)

    def start_test(self, cancel_check=None):
        return start_test(cancel_check=cancel_check)

    def reset_device(self):
        return reset_device()


def _default_record_saver(cycle: CycleRecord) -> dict:
    """Save one idempotent record per cycle; allocate serial only when needed."""
    import database
    import serial_generator

    existing = database.find_record_by_cycle(cycle.cycle_id)
    if existing is not None:
        return {
            'record_id': existing.get('id'),
            'serial_number': existing.get('serial_number'),
            'created': False,
            'error': None,
        }

    context = cycle.context or {}
    serial_number = None
    serial_error = None
    try:
        serial_info = serial_generator.get_generator().get_next_serial_number(
            context.get('product_model') or 'UNKNOWN',
            context.get('qr_code'))
        serial_number = serial_info.serial_number
    except Exception as exc:
        serial_error = f"serial generation failed: {exc}"
        print(f"[ATEQ] {serial_error}")

    p1 = cycle.p1
    p2 = cycle.p2
    try:
        import database
        record_id, created = database.save_record_ex(
            qr_code=context.get('qr_code'),
            pressure1=p1.pressure,
            leak1=p1.leak,
            pressure2=p2.pressure,
            leak2=p2.leak,
            test_result=cycle.overall_result or RESULT_UNKNOWN,
            product_model=context.get('product_model'),
            operator=context.get('operator'),
            serial_number=serial_number,
            cycle_id=cycle.cycle_id,
            program1_actual=p1.actual_program,
            program2_actual=p2.actual_program,
            program1_result=p1.result,
            program2_result=p2.result,
            pressure1_unit=p1.pressure_unit,
            leak1_unit=p1.leak_unit,
            pressure2_unit=p2.pressure_unit,
            leak2_unit=p2.leak_unit,
            terminal_reason=cycle.terminal_reason,
            started_at=cycle.started_at,
            finished_at=cycle.finished_at,
            label_template=context.get('label_template'),
            supplier_code=context.get('supplier_code'),
            # The insert itself establishes the durable auto-print admission:
            # a visible PASS record is immediately unclaimable by manual reprint.
            print_state=('auto_pending'
                         if (cycle.overall_result == RESULT_PASS and serial_number)
                         else 'skipped'),
        )
        existing = None if created else database.get_record(record_id)
        return {
            'record_id': record_id,
            'serial_number': serial_number if created
            else (existing or {}).get('serial_number'),
            'created': created,
            'error': None,
            'serial_error': serial_error,
        }
    except Exception as exc:
        return {
            'record_id': None,
            'serial_number': serial_number,
            'created': False,
            'error': f"record save failed: {exc}",
            'serial_error': serial_error,
        }


class BusUnavailable(Exception):
    """Raised when the serialized bus could not be acquired in time."""


class _BusToken:
    __slots__ = ("priority", "seq")

    def __init__(self, priority, seq):
        self.priority = bool(priority)
        self.seq = seq


class _BusGate:
    """Deterministic priority ticket arbiter for the single serial bus.

    - waiters register in an ordered queue (priority tokens before normal tokens,
      FIFO within a class) while holding only the queue condition
    - a waiter physically acquires `mutex` only when it is first in the queue; if a
      priority token arrives while it was blocked on the mutex, it re-queues instead
      of jumping ahead of the reset
    - timeout/cancellation removes the token and wakes the queue; no path waits on
      the physical mutex while holding the condition lock
    - `mutex` stays a plain RLock so external holders (tests/params-like code) are
      ordered fairly, and `wait_for_queue` provides a deterministic test barrier
    """

    def __init__(self):
        self.mutex = threading.RLock()
        self._cond = threading.Condition()
        self._queue = []
        self._seq = 0

    def queue_length(self):
        with self._cond:
            return len(self._queue)

    def wait_for_queue(self, count, timeout=2.0):
        """Barrier: wait until at least `count` waiters are registered."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while len(self._queue) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return len(self._queue) >= count

    def _enqueue(self, priority):
        self._seq += 1
        token = _BusToken(priority, self._seq)
        if priority:
            index = 0
            while index < len(self._queue) and self._queue[index].priority:
                index += 1
            self._queue.insert(index, token)
        else:
            self._queue.append(token)
        self._cond.notify_all()
        return token

    def _remove_token(self, token):
        try:
            self._queue.remove(token)
        except ValueError:
            pass
        self._cond.notify_all()

    @contextlib.contextmanager
    def hold(self, priority=False, timeout=5.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            token = self._enqueue(priority)
            while True:
                if self._queue and self._queue[0] is token:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._remove_token(token)
                    yield False
                    return
                self._cond.wait(remaining)

        while True:
            # Physically acquire only when we are first in line; a priority token can
            # arrive while we wait on the mutex and must be handed the bus first.
            remaining = max(0.0, deadline - time.monotonic())
            acquired = self.mutex.acquire(timeout=remaining)
            if not acquired:
                with self._cond:
                    self._remove_token(token)
                yield False
                return
            with self._cond:
                if self._queue and self._queue[0] is token:
                    break
                # A higher-priority waiter arrived: release and let it through.
            self.mutex.release()

        try:
            yield True
        finally:
            self.mutex.release()
            with self._cond:
                self._remove_token(token)


class TestExecutor:
    """Owns the cycle manager, device IO and background supervisor."""

    def __init__(self, io=None, clock=None, sleeper=None,
                 manager: CycleManager | None = None,
                 recorder=None, printer=None, settings_provider=None,
                 scanner_module=None, persist_enabled=True):
        self._clock = clock or time.time
        self._sleep = sleeper or time.sleep
        self._io = io or DeviceAdapter()
        self._manager = manager or CycleManager(persist_enabled=persist_enabled)
        self._command_lock = threading.RLock()
        self._supervisor_lock = threading.RLock()
        self._supervisor_thread = None
        self._supervisor_stop = threading.Event()
        self._supervisor_enabled = False
        self._reset_requested = threading.Event()
        self._reset_worker = None
        self._pending_context = {}
        self._pending_lock = threading.RLock()
        self._sample_lock = threading.RLock()
        self._sample_cache = None
        self._prepare_state = {'ready': False, 'message': '未准备', 'program': None,
                               'actual': None, 'at': None}
        self._needs_p1_prepare = threading.Event()
        self._status_callback = None
        self._completion_callback = None
        self._recorder = recorder or _default_record_saver
        self._printer = printer or self._default_print
        self._settings_provider = settings_provider or self._default_settings
        self._scanner = scanner_module
        self._started = False
        self._start_lock = threading.Lock()
        self._finalize_guard_lock = threading.Lock()
        self._finalizing_cycles: set = set()
        self._finalized_cycles: set = set()
        self._bus_gate = _BusGate()
        self._command_lock = self._bus_gate.mutex
        self._last_good_sample = None
        self._hardware_synced = False
        self._reset_retry_at = 0.0
        self._reset_attempts = 0

    # ------------------------------------------------------------- lifecycle
    def _default_settings(self):
        try:
            from line_runtime import get_line_settings
            return get_line_settings()
        except Exception:
            return {'scan_required': True, 'printer_enabled': True, 'scan_start': False}

    def _default_print(self, cycle: CycleRecord) -> dict:
        try:
            from line_runtime import print_label_if_enabled
            context = cycle.context or {}
            return print_label_if_enabled(
                template_name=context.get('label_template') or '',
                serial_number=cycle.serial_number or '',
                product_model=context.get('product_model') or '',
                qr_code=context.get('qr_code') or (cycle.serial_number or ''),
                supplier_code=context.get('supplier_code') or '',
                record_id=cycle.record_id,
                cancel_check=cycle.cancel_event.is_set,
            )
        except Exception as exc:
            return {'success': False, 'printed': False, 'state': 'failed',
                    'message': str(exc)}

    def _get_scanner(self):
        if self._scanner is None:
            import serial_scanner
            self._scanner = serial_scanner
        return self._scanner

    def ensure_started(self):
        """Explicit startup: recover interrupted state, no hardware unless enabled."""
        with self._start_lock:
            if self._started:
                return
            self._started = True
        persisted = self._manager.read_persisted()
        if isinstance(persisted, dict) and persisted.get('active'):
            payload = self._manager.note_interrupted(persisted)
            if payload is not None:
                save_error = None
                try:
                    self._save_interrupted_record(persisted, payload)
                except Exception as exc:
                    save_error = str(exc)
                    print(f"[ATEQ] interrupted recovery record failed: {exc}")
                self._write_interrupted_evidence(persisted, payload, save_error)

    def _write_interrupted_evidence(self, persisted, payload, save_error):
        """Keep retryable evidence even when the interrupted record could not be saved."""
        try:
            path = self._manager.interrupted_evidence_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                'persisted': persisted,
                'payload': payload,
                'record_save_error': save_error,
                'at': self._clock(),
            }, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as exc:
            print(f"[ATEQ] interrupted evidence write failed: {exc}")

    def _save_interrupted_record(self, persisted, payload):
        import database
        active = persisted.get('active') or {}
        context = active.get('context') or {}
        existing = database.find_record_by_cycle(payload['cycle_id'])
        if existing is None:
            database.save_test_record(
                qr_code=context.get('qr_code'),
                pressure1=None,
                leak1=None,
                pressure2=None,
                leak2=None,
                test_result=RESULT_UNKNOWN,
                product_model=context.get('product_model'),
                operator=context.get('operator'),
                serial_number=None,
                cycle_id=payload['cycle_id'],
                terminal_reason=RESULT_INTERRUPTED,
                started_at=active.get('started_at'),
            )

    def set_status_callback(self, callback):
        self._status_callback = callback

    def set_completion_callback(self, callback):
        """Register a lightweight terminal-cycle observer (for example PLC output).

        The callback runs after the CycleManager has published the final payload and
        must not perform blocking IO.  Exceptions cannot change the saved outcome.
        """
        self._completion_callback = callback

    def _notify_completion(self, cycle):
        callback = self._completion_callback
        if callback is None:
            return
        try:
            callback({
                'cycle_id': cycle.cycle_id,
                'trigger': cycle.trigger,
                'overall_result': cycle.overall_result,
                'terminal_reason': cycle.terminal_reason,
                'record_saved': bool(cycle.record_saved),
            })
        except Exception as exc:
            print(f"[ATEQ] completion callback failed: {exc}")

    def _update_status(self, phase, message, program=None):
        if self._status_callback:
            try:
                self._status_callback({
                    'phase': phase,
                    'message': message,
                    'program': program,
                })
            except Exception:
                pass

    # --------------------------------------------------------------- context
    def set_pending_test_context(self, product_model=None, operator=None, qr_code=None,
                                 label_template=None, supplier_code='', program1=None,
                                 program2=None, switch_chamber=None,
                                 scan_required=None, scan_start=None, replace=False):
        with self._pending_lock:
            if replace:
                # Explicit full replacement of the NEXT context (None clears fields).
                context = {
                    'product_model': product_model or None,
                    'operator': operator or None,
                    'qr_code': qr_code or None,
                    'label_template': label_template or None,
                    'supplier_code': supplier_code or '',
                    'program1': self._to_program(program1),
                    'program2': self._to_program(program2) or 0,
                    'switch_chamber': bool(switch_chamber) if switch_chamber is not None else None,
                    'scan_required': bool(scan_required) if scan_required is not None else None,
                    'scan_start': bool(scan_start) if scan_start is not None else None,
                }
                self._pending_context = context
                return dict(context)
            context = dict(self._pending_context)
            if product_model is not None:
                context['product_model'] = product_model or None
            if operator is not None:
                context['operator'] = operator or None
            if qr_code is not None:
                context['qr_code'] = qr_code or None
            if label_template is not None:
                context['label_template'] = label_template or None
            if supplier_code is not None:
                context['supplier_code'] = supplier_code or ''
            if program1 is not None:
                context['program1'] = self._to_program(program1)
            if program2 is not None:
                context['program2'] = self._to_program(program2) or 0
            if switch_chamber is not None:
                context['switch_chamber'] = bool(switch_chamber)
            if scan_required is not None:
                context['scan_required'] = bool(scan_required)
            if scan_start is not None:
                context['scan_start'] = bool(scan_start)
            self._pending_context = context
        return dict(context)

    def get_pending_context(self):
        with self._pending_lock:
            return dict(self._pending_context)

    def _clear_pending_qr(self):
        """One part per binding: clear the NEXT context QR after consume/end."""
        with self._pending_lock:
            if self._pending_context.get('qr_code'):
                self._pending_context['qr_code'] = None

    @staticmethod
    def _to_program(value):
        try:
            if isinstance(value, bool):
                return None
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        if 1 <= number <= 255:
            return number
        return None

    def arm_next_context(self, context, write_program_now=True):
        """Explicitly replace the NEXT cycle context; never mutates an active cycle."""
        previous = self.get_pending_context()
        self.set_pending_test_context(
            product_model=context.get('product_model'),
            operator=context.get('operator'),
            qr_code=context.get('qr_code'),
            label_template=context.get('label_template'),
            supplier_code=context.get('supplier_code', ''),
            program1=context.get('program1'),
            program2=context.get('program2'),
            switch_chamber=context.get('switch_chamber'),
            scan_required=context.get('scan_required'),
            scan_start=context.get('scan_start'),
            replace=True,
        )
        current = self.get_pending_context()
        if (previous.get('product_model'), previous.get('program1'),
                previous.get('program2')) != (current.get('product_model'),
                                              current.get('program1'),
                                              current.get('program2')):
            # Context changed: old readiness proof is no longer valid.
            self._prepare_state = {'ready': False, 'message': '配置已变更，需重新准备',
                                   'program': current.get('program1'),
                                   'actual': None, 'at': self._clock()}
            self._needs_p1_prepare.set()
        if self._manager.is_busy():
            return {'deferred': True, 'busy': True, 'prepared': False,
                    'message': '测试中：仅更新下一周期配置'}
        if self._manager.reset_inhibits_admission():
            return {'deferred': True, 'busy': False, 'prepared': False,
                    'message': '复位处理中，暂不准备'}
        if write_program_now:
            self._prepare_next_p1(force=True)
        prepared = bool(self._prepare_state.get('ready'))
        return {'deferred': False, 'busy': False, 'prepared': prepared,
                'prepare_state': dict(self._prepare_state)}

    def arm_sequence(self, program1=None, program2=0, product_model=None, operator=None,
                     qr_code=None, label_template=None, supplier_code='',
                     scan_required=False, scan_start=False, write_program_now=True):
        """Legacy compat wrapper around arm_next_context."""
        return self.arm_next_context(
            {
                'program1': program1,
                'program2': program2,
                'product_model': product_model,
                'operator': operator,
                'qr_code': qr_code,
                'label_template': label_template,
                'supplier_code': supplier_code,
                'scan_required': scan_required,
                'scan_start': scan_start,
            },
            write_program_now=write_program_now,
        )

    # ----------------------------------------------------------- admission
    def start_cycle(self, context, trigger='software', scan_event=None,
                    admission_violation=None):
        """Admit and run one cycle. Returns (ok, error_code, cycle_id)."""
        program1 = self._to_program(context.get('program1'))
        program2 = self._to_program(context.get('program2')) or 0
        if program1 is None:
            return False, 'PROGRAM1_INVALID', None
        if context.get('program2') not in (None, '', 0, '0') and program2 == 0:
            return False, 'PROGRAM2_INVALID', None
        frozen = {
            'product_model': context.get('product_model') or '',
            'operator': context.get('operator') or '',
            'qr_code': context.get('qr_code') or '',
            'label_template': context.get('label_template') or '',
            'supplier_code': context.get('supplier_code') or '',
            'program1': program1,
            'program2': program2,
            'switch_chamber': context.get('switch_chamber'),
            'scan_required': bool(context.get('scan_required')),
            'scan_start': bool(context.get('scan_start')),
            'scan_source': context.get('scan_source'),
            'device_verified': bool(context.get('device_verified')),
        }
        required = [program1] + ([program2] if program2 else [])
        cycle, error = self._manager.admit(frozen, trigger, required,
                                           scan_binding=scan_event,
                                           admission_violation=admission_violation)
        if cycle is None:
            return False, error or 'BUSY', None
        self._clear_pending_qr()
        worker = threading.Thread(target=self._run_cycle, args=(cycle,),
                                  name=f"ATEQCycle-{cycle.cycle_id[:8]}", daemon=True)
        worker.start()
        return True, 'OK', cycle.cycle_id

    def execute_async(self, program1, program2=0):
        """Legacy compat entry."""
        context = self.get_pending_context()
        context['program1'] = program1
        context['program2'] = program2
        ok, error, cycle_id = self.start_cycle(context, trigger='software')
        if ok:
            return True, 'Test started'
        return False, {'BUSY': 'Test already running'}.get(error, error)

    def execute_test_sequence(self, program1, program2=0):
        """Legacy compat: synchronous admission check only (worker runs in background)."""
        ok, message = self.execute_async(program1, program2)
        return {'success': ok, 'error': None if ok else message,
                'program1_result': None, 'program2_result': None}

    # ------------------------------------------------------------- stop/reset
    def request_stop(self, reason='user'):
        info = self._manager.request_stop(reason)
        if info.get('cancel_accepted'):
            # Linearize STOP against the transport write boundary: after this barrier
            # no new program-write/start-ON frame can commit for the cancelled cycle.
            commit_barrier()
            self._clear_pending_qr()
            self._schedule_reset(info.get('cycle_id'))
        reset = self._manager.get_reset_state()
        confirmed = (reset.get('state') == RESET_CONFIRMED and
                     reset.get('cycle_id') == info.get('cycle_id'))
        return {
            'cancel_accepted': bool(info.get('cancel_accepted')),
            'reset_confirmed': bool(confirmed),
            'reset_pending': bool(info.get('cancel_accepted') and not confirmed),
            'reset_state': reset.get('state'),
            'reset_error': reset.get('error'),
            'cycle_id': info.get('cycle_id'),
            'message': info.get('message', ''),
        }

    def stop(self):
        """Legacy compat."""
        return self.request_stop('user')

    def _schedule_reset(self, cycle_id):
        if not self._manager.reset_pending():
            self._manager.set_reset_state(RESET_PENDING, cycle_id)
        self._reset_attempts = 0
        self._reset_retry_at = 0.0
        self._reset_requested.set()
        if self._supervisor_enabled and self._supervisor_thread is not None \
                and self._supervisor_thread.is_alive():
            return
        worker = self._reset_worker
        if worker is None or not worker.is_alive():
            self._reset_worker = threading.Thread(
                target=self._perform_reset, name='ATEQResetOnce', daemon=True)
            self._reset_worker.start()

    def _perform_reset(self):
        """Priority reset: pulse, then require idle proof before 'confirmed'."""
        self._reset_requested.clear()
        reset = self._manager.get_reset_state()
        cycle_id = reset.get('cycle_id')
        self._reset_attempts += 1
        try:
            with self._bus_gate.hold(priority=True, timeout=5.0) as acquired:
                if not acquired:
                    self._manager.set_reset_state(
                        RESET_FAILED, cycle_id, error='RESET_TIMEOUT: bus busy')
                    self._reset_retry_at = self._clock() + 2.0
                    return
                ok = self._io.reset_device()
                step = None
                if ok:
                    try:
                        step = self._io.read_step_code()
                    except Exception:
                        step = None
            if ok and step in (0, STEP_COMPLETE):
                self._manager.set_reset_state(RESET_CONFIRMED, cycle_id)
                self._reset_attempts = 0
                self._reset_retry_at = 0.0
                self._hardware_synced = (step == STEP_COMPLETE)
            elif ok:
                self._manager.set_reset_state(
                    RESET_FAILED, cycle_id,
                    error=f'instrument still active (step {step})')
                self._reset_retry_at = self._clock() + 2.0
            else:
                self._manager.set_reset_state(
                    RESET_FAILED, cycle_id, error='reset coil did not acknowledge')
                self._reset_retry_at = self._clock() + 2.0
        except Exception as exc:
            self._manager.set_reset_state(RESET_FAILED, cycle_id, error=str(exc))
            self._reset_retry_at = self._clock() + 2.0

    # ----------------------------------------------------------- supervisor
    def start_instrument_supervisor(self, start_scanner=True):
        with self._supervisor_lock:
            if self._supervisor_enabled:
                return
            self._supervisor_enabled = True
            self._supervisor_stop.clear()
            self._supervisor_thread = threading.Thread(
                target=self._supervisor_loop, name='ATEQInstrumentSupervisor',
                daemon=True)
            self._supervisor_thread.start()
        if start_scanner and SCANNER_ENABLED and not HARDWARE_DISABLED \
                and _persisted_scanner_enabled():
            try:
                scanner = self._get_scanner()
                scanner.set_event_callback(self.handle_scan_event)
                port = _persisted_scanner_port()
                if port:
                    setter = getattr(scanner, "set_scan_port", None)
                    if setter is not None:
                        setter(port)
                scanner.start_serial_scanner()
            except Exception as exc:
                print(f"[ATEQ] scanner startup failed: {exc}")

    def stop_instrument_supervisor(self):
        self._supervisor_stop.set()
        with self._supervisor_lock:
            thread = self._supervisor_thread
            self._supervisor_thread = None
            self._supervisor_enabled = False
        if thread is not None and thread.is_alive():
            thread.join(2.0)
        try:
            self._get_scanner().stop_serial_scanner()
        except Exception:
            pass

    def supervisor_running(self):
        thread = self._supervisor_thread
        return bool(self._supervisor_enabled and thread is not None and thread.is_alive())

    def _supervisor_loop(self):
        while not self._supervisor_stop.is_set():
            try:
                if self._reset_requested.is_set():
                    self._perform_reset()
                    continue
                reset_state = self._manager.get_reset_state()["state"]
                if (reset_state == RESET_FAILED and self._reset_attempts < 5
                        and self._clock() >= self._reset_retry_at):
                    self._perform_reset()
                if (self._needs_p1_prepare.is_set() and self._manager.get_active() is None
                        and not self._manager.reset_inhibits_admission()):
                    self._prepare_next_p1()
                sample, error_code, message = self._read_sample_with_error()
                self._update_sample_cache(sample, error_code, message)
                self._handle_hardware_sample(sample)
                self._check_hardware_watchdog(self._manager.get_active())
            except Exception as exc:
                print(f"[ATEQ] supervisor error: {exc}")
            self._supervisor_stop.wait(SUPERVISOR_INTERVAL)

    def _read_sample_with_error(self):
        """Read a sample through the typed adapter path (never swallow error codes)."""
        fn = getattr(self._io, 'read_sample_ex', None)
        if fn is not None:
            try:
                sample, error_code = fn()
                return sample, (error_code or None), ''
            except Exception as exc:
                return None, 'IO_ERROR', str(exc)
        try:
            sample = self._io.read_sample()
        except Exception as exc:
            return None, 'IO_ERROR', str(exc)
        return sample, (None if isinstance(sample, dict) else 'READ_TIMEOUT'), ''

    def _update_sample_cache(self, sample, error_code=None, message=''):
        with self._sample_lock:
            now = self._clock()
            if isinstance(sample, dict) and not error_code:
                self._last_good_sample = {'sample': dict(sample), 'sampled_at': now}
            if sample is None and not error_code:
                error_code = 'READ_TIMEOUT'
            self._sample_cache = {
                'sample': dict(sample) if isinstance(sample, dict) else None,
                'sampled_at': now,
                'error_code': error_code,
                'message': message,
            }

    def _sample_snapshot(self):
        with self._sample_lock:
            cache = self._sample_cache
            last_good = self._last_good_sample
        if cache is None:
            return {'pressure': None, 'leak': None, 'pressure_unit': None,
                    'leak_unit': None, 'step_code': None, 'status': None,
                    'status_text': '无采样', 'sampled_at': None, 'stale': True,
                    'error_code': 'NO_SAMPLE', 'message': '采样服务未启动',
                    'last_good': None}
        sample = cache.get('sample') or {}
        error_code = cache.get('error_code')
        stale = bool(error_code) or (
            (self._clock() - (cache.get('sampled_at') or 0)) > SAMPLE_STALE_SECONDS)
        status = sample.get('status')
        parsed = _parse_status(status) if status is not None else None
        last_good_payload = None
        if last_good and error_code:
            last = last_good.get('sample') or {}
            last_good_payload = {
                'pressure': last.get('pressure'),
                'leak': last.get('leak'),
                'pressure_unit': last.get('pressure_unit'),
                'leak_unit': last.get('leak_unit'),
                'step_code': last.get('step_code'),
                'status': last.get('status'),
                'sampled_at': last_good.get('sampled_at'),
                'stale': True,
            }
        return {
            'pressure': sample.get('pressure') if not error_code else None,
            'leak': sample.get('leak') if not error_code else None,
            'pressure_unit': sample.get('pressure_unit') if not error_code else None,
            'leak_unit': sample.get('leak_unit') if not error_code else None,
            'step_code': sample.get('step_code') if not error_code else None,
            'status': status if not error_code else None,
            'status_text': (self._status_text(parsed) if not error_code
                            else f'采样失败({error_code})'),
            'sampled_at': cache.get('sampled_at'),
            'stale': bool(stale),
            'error_code': error_code,
            'message': cache.get('message', ''),
            'last_good': last_good_payload,
        }

    @staticmethod
    def _status_text(parsed):
        if parsed is None:
            return '无状态'
        bits = []
        if parsed['keyboard_lock']:
            bits.append('键盘锁定')
        if parsed['cycle_end']:
            bits.append('循环结束')
        if parsed['fail_flags']:
            bits.append('失败标志')
        if parsed['pass_bit']:
            bits.append('测试通过')
        if parsed['unknown_bits']:
            bits.append(f"未确认标志位(0x{parsed['unknown_bits']:04X})")
        return ' | '.join(bits) if bits else '待机中'

    def runtime_snapshot(self):
        snap = self._manager.snapshot()
        snap['sample'] = self._sample_snapshot()
        snap['prepare'] = dict(self._prepare_state)
        snap['supervisor'] = {
            'running': self.supervisor_running(),
            'hardware_disabled': HARDWARE_DISABLED,
        }
        snap['running'] = bool(snap.get('busy'))
        return snap

    def get_runtime_state(self):
        return self.runtime_snapshot()

    @contextlib.contextmanager
    def params_reservation(self):
        """Reserve IDLE for a whole multi-frame transaction; released on any exit."""
        acquired = self._manager.reserve_external()
        try:
            yield acquired
        finally:
            if acquired:
                self._manager.release_external()

    @contextlib.contextmanager
    def _bus(self, cycle=None, priority=False, timeout=5.0, check_cancel=True):
        """Serialized bus entry: after ownership is granted, cancellation is re-checked
        immediately before any transport write/start."""
        with self._bus_gate.hold(priority=priority, timeout=timeout) as acquired:
            if not acquired:
                raise BusUnavailable('bus acquisition timeout')
            if check_cancel and cycle is not None:
                self._manager.check_cancelled(cycle)
            yield

    @contextlib.contextmanager
    def run_bus_transaction(self, timeout=5.0):
        """External multi-frame transaction (params) under the same priority gate."""
        with self._bus_gate.hold(priority=False, timeout=timeout) as acquired:
            if not acquired:
                raise BusUnavailable('bus acquisition timeout')
            yield acquired

    def wait_until_idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._manager.is_busy():
                return True
            time.sleep(0.01)
        return not self._manager.is_busy()

    # -------------------------------------------------------- scan handling
    def handle_scan_event(self, event):
        """Backend consumer: process each device scan event exactly once."""
        if not isinstance(event, dict):
            return
        event_id = event.get('event_id')
        code = event.get('code') or ''
        if not event_id or not code:
            return
        scanner = self._get_scanner()
        if not _persisted_scanner_enabled():
            # Disabled scanner: no autostart, no binding, no stale admission.
            scanner.mark_event_result(event_id, 'REJECTED',
                                      message='扫码枪已禁用，事件未处理')
            return
        if self._manager.is_busy():
            scanner.mark_event_result(event_id, 'BUSY', message='测试中，扫码已记录但未启动')
            return

        settings = self._settings_provider() or {}
        scan_required = settings.get('scan_required', True)
        scan_start = settings.get('scan_start', False)
        existing = scanner.get_event(event_id)
        if existing and existing.get('processed'):
            return

        scanner.set_binding(event_id)
        context = self.get_pending_context()
        context['qr_code'] = code
        context['scan_required'] = scan_required
        context['scan_start'] = scan_start
        context['scan_source'] = 'device'
        context['device_verified'] = bool(event.get('device_verified'))
        if not scan_start:
            scanner.mark_event_result(event_id, 'BOUND',
                                      message='扫码已暂存，等待手动/硬件启动')
            return
        if not context.get('product_model') or not context.get('program1'):
            scanner.mark_event_result(event_id, 'NO_PRODUCT',
                                      message='未选择产品，扫码已暂存')
            return
        binding = scanner.consume_binding()
        ok, error, cycle_id = self.start_cycle(context, trigger='scan',
                                               scan_event=binding or event)
        if ok:
            scanner.mark_event_result(event_id, 'AUTO_STARTED', started=True,
                                      message=f'扫码启动周期 {cycle_id}')
        elif error == 'BUSY':
            scanner.mark_event_result(event_id, 'BUSY', message='测试中，扫码已消费未启动')
        else:
            # Consumed events are never restored: a newer event must not be clobbered
            # by an older one, and a rejected event stays rejected.
            scanner.mark_event_result(event_id, error or 'REJECTED',
                                      message=f'扫码启动被拒绝: {error}')

    def start_plc_cycle(self):
        """Admit a PLC M0.0 request through the same product/scan/cycle gates.

        The pending context is restored from the server-side active product at WebUI
        startup.  PLC pulses never supply program numbers or bypass scan admission.
        Returns the same (ok, error, cycle_id) tuple as start_cycle().
        """
        context = self.get_pending_context()
        if not context.get('product_model') or self._to_program(context.get('program1')) is None:
            return False, 'NO_PRODUCT', None

        settings = self._settings_provider() or {}
        scan_required = bool(settings.get('scan_required', True))
        scanner = self._get_scanner()
        binding = scanner.get_binding() if scanner is not None else None
        qr = str(context.get('qr_code') or '').strip()
        scan_source = None
        device_verified = False

        if scan_required:
            if binding is None:
                if not qr:
                    return False, 'SCAN_REQUIRED', None
                binding = scanner.create_manual_binding(qr) if scanner is not None else None
                if binding is None:
                    return False, 'SCANNER_UNAVAILABLE', None
                scan_source = 'manual'
            else:
                if qr and qr != binding.get('code'):
                    return False, 'QR_MISMATCH', None
                qr = binding.get('code') or ''
                scan_source = binding.get('source')
                device_verified = bool(binding.get('device_verified'))
            binding = scanner.consume_binding() if scanner is not None else None
        elif binding is not None:
            if qr and qr != binding.get('code'):
                return False, 'QR_MISMATCH', None
            qr = binding.get('code') or qr
            scan_source = binding.get('source')
            device_verified = bool(binding.get('device_verified'))
            binding = scanner.consume_binding()
        else:
            scan_source = 'manual' if qr else None

        context.update({
            'qr_code': qr,
            'scan_required': scan_required,
            'scan_start': bool(settings.get('scan_start', False)),
            'scan_source': scan_source,
            'device_verified': device_verified,
        })
        return self.start_cycle(context, trigger='plc', scan_event=binding)

    # ------------------------------------------------------ hardware cycles
    def _handle_hardware_sample(self, sample):
        if not isinstance(sample, dict):
            return
        step = sample.get('step_code')
        if step is None:
            return
        if step == STEP_COMPLETE:
            # Confirmed idle observation is required before a transition is trusted
            # as a fresh external cycle (startup mid-cycle must stay unverified).
            # Protocol amendment 2026-10-01: ONLY 65535 is idle; step 0 is abnormal
            # reset and 65525 is never terminal.
            self._hardware_synced = True
        active = self._manager.get_active()
        if step is not None and ACTIVE_STEP_MIN <= step <= ACTIVE_STEP_MAX:
            if active is None:
                self._admit_external_cycle(sample)
            else:
                self._attach_activity(active, sample)
            return
        # Only purely hardware-triggered cycles are finalized by the supervisor;
        # software/scan cycles are owned by their worker thread.
        if active is None or active.trigger != 'hardware':
            self._check_chain_timeout(active)
            return
        if step == STEP_COMPLETE:
            phase_key = self._phase_key(active)
            if phase_key is not None:
                snapshot = active.p1 if phase_key == 'p1' else active.p2
                if snapshot.activity_observed:
                    self._finish_external_in_thread(active, 'complete')
            self._check_chain_timeout(active)
        elif step == 0:
            phase_key = self._phase_key(active)
            if phase_key is not None:
                snapshot = active.p1 if phase_key == 'p1' else active.p2
                if snapshot.activity_observed:
                    self._abort_external_phase(active, 'ABNORMAL_RESET')
        else:
            self._check_chain_timeout(active)

    @staticmethod
    def _phase_key(cycle):
        if cycle.phase in (PHASE_PREPARING, PHASE_RUNNING_P1):
            return 'p1'
        if cycle.phase in (PHASE_CHAINING_P2, PHASE_RUNNING_P2):
            return 'p2'
        return None

    def _admit_external_cycle(self, sample):
        step = sample.get('step_code')
        context = self.get_pending_context()
        settings = self._settings_provider() or {}
        scan_required = settings.get('scan_required', True)
        context['scan_required'] = scan_required
        context['scan_start'] = settings.get('scan_start', False)
        context['scan_source'] = 'hardware'
        context['device_verified'] = False

        violation = None
        scanner = self._get_scanner()
        # Consume the binding atomically BEFORE admission; the exact consumed code
        # identifies the part even when scan is optional.
        binding = scanner.consume_binding()
        if scan_required and binding is None:
            violation = 'SCAN_REQUIRED'
        if not self._hardware_synced:
            violation = violation or 'INSTRUMENT_STATE_UNVERIFIED'
        if violation is None and (not context.get('product_model')
                                  or self._to_program(context.get('program1')) is None):
            violation = 'MODEL_UNKNOWN'
        actual = self._safe_read_program()
        expected = self._to_program(context.get('program1'))
        if violation is None and actual is not None and expected is not None and actual != expected:
            violation = 'PROGRAM_MISMATCH'

        if violation == 'SCAN_REQUIRED':
            # 该扫码没扫码而硬件启动了仪器: 拒绝该周期，并通过 Modbus
            # 发复位代码把仪器停回空闲；复位确认前新周期不准准入
            if not self._manager.reset_pending():
                self._update_status(
                    'error',
                    '需要扫码但未扫码: 硬件启动已被拒绝，正在通过 Modbus 复位仪器')
                self._schedule_reset(None)
            return

        program1 = self._to_program(context.get('program1'))
        program2 = self._to_program(context.get('program2')) or 0
        binding_code = (binding or {}).get('code') if binding else None
        binding_source = (binding or {}).get('source') if binding else None
        binding_verified = bool((binding or {}).get('device_verified')) if binding else False
        frozen = {
            'product_model': context.get('product_model') or '',
            'operator': context.get('operator') or '',
            # Hardware cycles only ever take the consumed binding's code; a stale
            # pending QR must never identify a new part.
            'qr_code': binding_code or '',
            'label_template': context.get('label_template') or '',
            'supplier_code': context.get('supplier_code') or '',
            'program1': program1,
            'program2': program2,
            'switch_chamber': context.get('switch_chamber'),
            'scan_required': scan_required,
            'scan_start': settings.get('scan_start', False),
            'scan_source': binding_source or 'hardware',
            'device_verified': binding_verified,
        }
        required = []
        if program1 is not None:
            required.append(program1)
            if program2:
                required.append(program2)
        cycle, error = self._manager.admit(frozen, 'hardware', required,
                                           scan_binding=binding,
                                           admission_violation=violation)
        if cycle is None:
            if binding is not None:
                scanner.mark_event_result(binding['event_id'], error or 'REJECTED',
                                          message=f'硬件周期准入被拒绝: {error}')
            return
        self._clear_pending_qr()
        now = self._clock()
        identity_verified = (actual is not None and program1 is not None
                             and actual == program1 and violation is None)
        self._manager.set_cycle_field(cycle, started_at=now, phase=PHASE_RUNNING_P1)
        self._manager.update_phase(
            cycle, 'p1', status='running', started_at=now,
            activity_observed=True, actual_program=actual,
            program_identity_verified=identity_verified,
            pressure=sample.get('pressure'), leak=sample.get('leak'),
            pressure_unit=sample.get('pressure_unit'),
            leak_unit=sample.get('leak_unit'),
            sampled_at=self._clock(), status_word=sample.get('status'),
            **_cycle_evidence_fields(step, sample))
        self._update_status('hardware_running', '检测到硬件启动', program1)

    def _expected_program(self, cycle, snapshot):
        programs = cycle.required_programs or []
        if snapshot is cycle.p1:
            return programs[0] if programs else cycle.context.get('program1')
        return programs[1] if len(programs) > 1 else cycle.context.get('program2')

    def _attach_activity(self, cycle, sample):
        phase_key = self._phase_key(cycle)
        if phase_key is None:
            return
        snapshot = cycle.p1 if phase_key == 'p1' else cycle.p2
        step = sample.get('step_code')
        fields = {'activity_observed': True}
        expected = self._expected_program(cycle, snapshot)
        actual = sample.get('actual_program')
        if actual is not None and expected is not None:
            if actual != expected:
                # Program mutation invalidates stage identity permanently.
                fields['program_identity_verified'] = False
                fields['identity_mismatch_program'] = actual
                fields['actual_program'] = actual
            elif (snapshot.program_identity_verified is not False
                  and cycle.admission_violation is None):
                fields['actual_program'] = actual
                fields['program_identity_verified'] = True
        if step is not None and 4 <= step <= 6:
            fields.update({
                'pressure': sample.get('pressure'),
                'leak': sample.get('leak'),
                'pressure_unit': sample.get('pressure_unit'),
                'leak_unit': sample.get('leak_unit'),
                'sampled_at': self._clock(),
                'status_word': sample.get('status'),
            })
        fields.update(_cycle_evidence_fields(step, sample))
        if phase_key == 'p2' and cycle.phase == PHASE_CHAINING_P2:
            fields['status'] = 'running'
            fields['started_at'] = self._clock()
            self._manager.set_phase(cycle, PHASE_RUNNING_P2)
        self._manager.update_phase(cycle, phase_key, **fields)

    def _abort_external_phase(self, cycle, reason):
        if not self._manager.is_active(cycle):
            return
        phase_key = self._phase_key(cycle) or 'p1'
        self._manager.update_phase(
            cycle, phase_key, status='failed', result=RESULT_ABORTED,
            terminal_reason=reason, finished_at=self._clock())
        self._finalize_cycle_in_thread(cycle, RESULT_ABORTED, reason)

    def _finish_external_in_thread(self, cycle, tag):
        if not self._manager.is_active(cycle):
            return
        with self._finalize_guard_lock:
            if cycle.cycle_id in self._finalizing_cycles:
                return
            self._finalizing_cycles.add(cycle.cycle_id)
        thread = threading.Thread(target=self._finalize_external_phase,
                                  args=(cycle,), name=f"ATEQFinalize-{tag}", daemon=True)
        thread.start()

    def _finalize_external_phase(self, cycle):
        try:
            if not self._manager.is_active(cycle):
                return
            phase_key = self._phase_key(cycle)
            if phase_key is None:
                return
            snapshot = cycle.p1 if phase_key == 'p1' else cycle.p2
            if snapshot.status in ('completed', 'failed', 'skipped'):
                return
            sample = self._read_completion_sample()
            result, reason = self._evaluate_phase(cycle, snapshot, sample)
            self._manager.update_phase(
                cycle, phase_key,
                status='completed' if result == RESULT_PASS else 'failed',
                result=result, terminal_reason=reason, finished_at=self._clock())
            if result != RESULT_PASS:
                if phase_key == 'p1' and cycle.p2.required:
                    self._manager.update_phase(cycle, 'p2', status='skipped',
                                               skip_reason=f'PROGRAM1_{result}')
                self._finalize_and_finish(cycle, result, reason)
                return
            if phase_key == 'p2':
                self._finalize_and_finish(cycle, RESULT_PASS, 'OK')
                return
            # p1 PASS -> chain program 2 when required
            if cycle.p2.required:
                self._chain_program2(cycle, external=True)
            else:
                self._finalize_and_finish(cycle, RESULT_PASS, 'OK')
        finally:
            with self._finalize_guard_lock:
                self._finalizing_cycles.discard(cycle.cycle_id)

    def _chain_program2(self, cycle, external=False):
        program2 = cycle.context.get('program2')
        if not program2:
            self._finalize_and_finish(cycle, RESULT_PASS, 'OK')
            return
        if self._manager.cancel_requested(cycle):
            self._finalize_and_finish(cycle, RESULT_ABORTED, 'CANCELLED')
            return
        try:
            self._manager.set_phase(cycle, PHASE_CHAINING_P2)
            # Write + readback under one bus ownership; cancellation is re-checked after
            # ownership is granted and again before the start pulse.
            with self._bus(cycle):
                ok, message = self._io.write_program(
                    program2,
                    cancel_check=cycle.cancel_event.is_set)
                readback = self._io.read_current_program() if ok else None
            if self._manager.cancel_requested(cycle):
                self._manager.update_phase(cycle, 'p2', status='failed',
                                           result=RESULT_ABORTED,
                                           terminal_reason='CANCELLED')
                self._finalize_and_finish(cycle, RESULT_ABORTED, 'CANCELLED')
                return
            verified = (readback == program2)
            if not ok:
                self._manager.update_phase(cycle, 'p2', status='failed',
                                           result=RESULT_ERROR,
                                           terminal_reason='CHAIN_WRITE_FAILED',
                                           skip_reason=message)
                self._finalize_and_finish(cycle, RESULT_ERROR, 'CHAIN_WRITE_FAILED')
                return
            if not verified:
                # 链测程序2同样要求显示程序号与配置一致，一致才允许启动
                self._update_status(
                    'error',
                    f'程序2程序号不一致: 仪器显示 {readback}，配置 {program2}，已拒绝启动并下发复位',
                    program2)
                self._manager.update_phase(cycle, 'p2', status='failed',
                                           result=RESULT_ERROR,
                                           terminal_reason='CHAIN_PROGRAM_MISMATCH')
                self._finalize_and_finish(cycle, RESULT_ERROR, 'CHAIN_PROGRAM_MISMATCH')
                # 复位仪器清除残留状态；复位确认前新周期不准准入
                self._schedule_reset(cycle.cycle_id)
                return
            with self._bus(cycle):
                started = self._io.start_test(
                    cancel_check=cycle.cancel_event.is_set)
            if not started:
                if self._manager.cancel_requested(cycle):
                    self._finalize_and_finish(cycle, RESULT_ABORTED, 'CANCELLED')
                    return
                self._manager.update_phase(cycle, 'p2', status='failed',
                                           result=RESULT_ERROR,
                                           terminal_reason='CHAIN_START_FAILED')
                self._finalize_and_finish(cycle, RESULT_ERROR, 'CHAIN_START_FAILED')
                return
            now = self._clock()
            self._manager.update_phase(
                cycle, 'p2', status='running', started_at=now,
                actual_program=readback, program_identity_verified=verified)
            if external:
                # Stay in CHAINING_P2 until real activity is observed so the chain
                # deadline can expire instead of remaining busy forever.
                setattr(cycle, '_chain_deadline', now + CHAIN_TIMEOUT)
            else:
                self._manager.set_cycle_field(cycle, phase=PHASE_RUNNING_P2)
        except BusUnavailable:
            if self._manager.is_active(cycle):
                self._manager.update_phase(cycle, 'p2', status='failed',
                                           result=RESULT_ERROR,
                                           terminal_reason='BUS_TIMEOUT')
                self._finalize_and_finish(cycle, RESULT_ERROR, 'BUS_TIMEOUT')
        except CycleCancelled:
            if self._manager.is_active(cycle):
                self._finalize_and_finish(cycle, RESULT_ABORTED, 'CANCELLED')
        except Exception:
            if self._manager.is_active(cycle):
                self._manager.update_phase(cycle, 'p2', status='failed',
                                           result=RESULT_ERROR,
                                           terminal_reason='CHAIN_EXCEPTION')
                self._finalize_and_finish(cycle, RESULT_ERROR, 'CHAIN_EXCEPTION')

    def _check_chain_timeout(self, active):
        if active is None:
            return
        if active.phase != PHASE_CHAINING_P2:
            return
        deadline = getattr(active, '_chain_deadline', 0)
        if deadline and self._clock() > deadline:
            # Unknown hardware behavior: never report a product-quality FAIL.
            self._manager.update_phase(active, 'p2', status='skipped',
                                       skip_reason='CHAIN_TIMEOUT')
            self._finalize_cycle_in_thread(active, RESULT_UNKNOWN, 'CHAIN_TIMEOUT')

    def _finalize_cycle_in_thread(self, cycle, result, reason):
        """Async finalize for supervisor-owned paths (never blocks sampling)."""
        if not self._manager.is_active(cycle):
            return
        with self._finalize_guard_lock:
            if cycle.cycle_id in self._finalizing_cycles:
                return
            self._finalizing_cycles.add(cycle.cycle_id)

        def run():
            try:
                self._finalize_and_finish(cycle, result, reason)
            finally:
                with self._finalize_guard_lock:
                    self._finalizing_cycles.discard(cycle.cycle_id)

        threading.Thread(target=run, name=f"ATEQFinalize-{cycle.cycle_id[:8]}",
                         daemon=True).start()

    def _check_hardware_watchdog(self, active):
        """Phase/communication deadlines for supervisor-owned hardware cycles."""
        if active is None or active.trigger != 'hardware':
            return
        if not self._manager.is_active(active):
            return
        phase_key = self._phase_key(active)
        if phase_key is None:
            return
        snapshot = active.p1 if phase_key == 'p1' else active.p2
        if not snapshot.activity_observed:
            return
        started = snapshot.started_at or active.started_at
        if started is not None and self._clock() - started > PHASE_TIMEOUT:
            self._manager.update_phase(
                active, phase_key, status='failed', result=RESULT_UNKNOWN,
                terminal_reason='PHASE_TIMEOUT', finished_at=self._clock())
            if phase_key == 'p1' and active.p2.required:
                self._manager.update_phase(active, 'p2', status='skipped',
                                           skip_reason='PHASE_TIMEOUT')
            self._finalize_cycle_in_thread(active, RESULT_UNKNOWN, 'PHASE_TIMEOUT')

    # --------------------------------------------------------- software run
    def _run_cycle(self, cycle: CycleRecord):
        try:
            self._manager.set_phase(cycle, PHASE_PREPARING)
            self._update_status('preparing', '准备程序1', cycle.context.get('program1'))
            ok, code, message, readback, verified = self._prepare_phase(
                cycle, 'p1', cycle.context.get('program1'))
            if not ok:
                self._manager.update_phase(cycle, 'p1', status='failed',
                                           result=RESULT_ERROR, terminal_reason=code)
                self._finalize_and_finish(cycle, RESULT_ERROR, code)
                return
            self._manager.update_phase(
                cycle, 'p1', actual_program=readback,
                program_identity_verified=verified)
            if readback is not None and readback != cycle.context.get('program1'):
                # 明确不一致: 仪器显示的程序号与配置不同，拒绝 Modbus 启动
                self._update_status(
                    'error',
                    f'程序1程序号不一致: 仪器显示 {readback}，配置 '
                    f'{cycle.context.get("program1")}，已拒绝启动并下发复位',
                    cycle.context.get('program1'))
                self._manager.update_phase(cycle, 'p1', status='failed',
                                           result=RESULT_ERROR,
                                           terminal_reason='PROGRAM_MISMATCH')
                self._finalize_and_finish(cycle, RESULT_ERROR, 'PROGRAM_MISMATCH')
                # 复位仪器清除残留状态；复位确认前新周期不准准入
                self._schedule_reset(cycle.cycle_id)
                return
            # 回读失败(readback=None)无法确认身份: 周期照常执行，
            # 结果在终点判定为 PROGRAM_UNVERIFIED（不可能 PASS/打印）
            if not self._start_phase(cycle, 'p1'):
                self._manager.update_phase(cycle, 'p1', status='failed',
                                           result=RESULT_ERROR,
                                           terminal_reason='START_FAILED')
                self._finalize_and_finish(cycle, RESULT_ERROR, 'START_FAILED')
                return
            now = self._clock()
            self._manager.update_phase(cycle, 'p1', status='running', started_at=now)
            self._manager.set_cycle_field(cycle, started_at=now, phase=PHASE_RUNNING_P1)
            self._update_status('running_program1', '程序1运行中', cycle.context.get('program1'))

            kind, sample, step = self._wait_phase(cycle, 'p1')
            result, reason = self._outcome_from_wait(cycle, 'p1', kind, sample, step)
            self._manager.update_phase(
                cycle, 'p1', status='completed' if result == RESULT_PASS else 'failed',
                result=result, terminal_reason=reason, finished_at=self._clock())
            if result != RESULT_PASS:
                if cycle.p2.required:
                    self._manager.update_phase(cycle, 'p2', status='skipped',
                                               skip_reason=f'PROGRAM1_{result}')
                self._finalize_and_finish(cycle, result, reason)
                return
            if not cycle.p2.required:
                self._finalize_and_finish(cycle, RESULT_PASS, 'OK')
                return
            self._chain_program2(cycle, external=False)
            if not self._manager.is_active(cycle):
                return
            if cycle.p2.status == 'running':
                kind, sample, step = self._wait_phase(
                    cycle, 'p2', require_activity=True,
                    start_deadline=self._clock() + CHAIN_TIMEOUT)
                if kind == 'chain_timeout':
                    # Never-started P2 is unknown, not a product-quality FAIL.
                    self._manager.update_phase(cycle, 'p2', status='skipped',
                                               skip_reason='CHAIN_TIMEOUT')
                    self._finalize_and_finish(cycle, RESULT_UNKNOWN, 'CHAIN_TIMEOUT')
                    return
                result, reason = self._outcome_from_wait(cycle, 'p2', kind, sample, step)
                self._manager.update_phase(
                    cycle, 'p2',
                    status='completed' if result == RESULT_PASS else 'failed',
                    result=result, terminal_reason=reason, finished_at=self._clock())
                self._finalize_and_finish(cycle, result, reason)
            elif cycle.p2.status == 'skipped':
                self._finalize_and_finish(cycle, cycle.p1.result or RESULT_FAIL,
                                          cycle.p2.skip_reason or 'P2_SKIPPED')
            # if p2 write/start failed, _chain_program2 already finalized
        except CycleCancelled:
            if self._manager.is_active(cycle):
                reason = 'CANCELLED' if cycle.stop_requested else 'SUPERSEDED'
                self._finalize_and_finish(cycle, RESULT_ABORTED, reason)
        except BusUnavailable:
            if self._manager.is_active(cycle):
                self._finalize_and_finish(cycle, RESULT_ERROR, 'BUS_TIMEOUT')
        except Exception:
            if self._manager.is_active(cycle):
                self._finalize_and_finish(cycle, RESULT_ERROR, 'EXECUTOR_EXCEPTION')

    def _prepare_phase(self, cycle, key, program):
        with self._bus(cycle):
            ok, message = self._io.write_program(
                program,
                # lock-free predicate: evaluated inside the send-commit critical section
                cancel_check=cycle.cancel_event.is_set)
            if not ok:
                if self._manager.cancel_requested(cycle):
                    raise CycleCancelled('stop requested')
                return False, 'PREPARE_WRITE_FAILED', message, None, False
            readback = self._io.read_current_program()
            verified = (readback == program)
            return True, 'OK', message, readback, verified

    def _start_phase(self, cycle, key):
        with self._bus(cycle):
            started = bool(self._io.start_test(
                cancel_check=cycle.cancel_event.is_set))
            if not started and self._manager.cancel_requested(cycle):
                raise CycleCancelled('stop requested')
            return started

    def _wait_phase(self, cycle, key, require_activity=False,
                    start_deadline=None):
        deadline = self._clock() + PHASE_TIMEOUT
        failures = 0
        while True:
            self._manager.check_cancelled(cycle)
            snapshot = cycle.p1 if key == 'p1' else cycle.p2
            expected = self._expected_program(cycle, snapshot)
            if require_activity and not snapshot.activity_observed and \
                    start_deadline is not None and self._clock() > start_deadline:
                return 'chain_timeout', None, None
            if self._clock() > deadline:
                return 'timeout', None, None
            with self._bus(cycle):
                step = self._io.read_step_code()
            if step is None:
                failures += 1
                if failures >= COMM_FAILURE_LIMIT:
                    return 'comm_error', None, None
                self._sleep(POLL_INTERVAL)
                continue
            failures = 0
            if ACTIVE_STEP_MIN <= step <= ACTIVE_STEP_MAX:
                if not snapshot.activity_observed:
                    self._manager.update_phase(cycle, key, activity_observed=True,
                                               started_at=self._clock())
                if 4 <= step <= 7:
                    with self._bus(cycle):
                        sample = self._io.read_sample()
                    if isinstance(sample, dict):
                        # Same-cycle evidence: 6 captures pressure, 7 captures leak.
                        # A 65535 leak fallback is resolved only at cycle completion.
                        fields = _cycle_evidence_fields(step, sample)
                        if 4 <= step <= 6:
                            fields.update({
                                'pressure': sample.get('pressure'),
                                'leak': sample.get('leak'),
                                'pressure_unit': sample.get('pressure_unit'),
                                'leak_unit': sample.get('leak_unit'),
                                'sampled_at': self._clock(),
                                'status_word': sample.get('status'),
                            })
                        actual = sample.get('actual_program')
                        if actual is not None and expected is not None:
                            fields['actual_program'] = actual
                            if actual != expected:
                                fields['program_identity_verified'] = False
                                fields['identity_mismatch_program'] = actual
                        self._manager.update_phase(cycle, key, **fields)
            elif step in (0, STEP_COMPLETE) and snapshot.activity_observed:
                sample = self._read_completion_sample(cycle)
                return 'complete', sample, step
            elif step == 0 and not snapshot.activity_observed and require_activity:
                return 'abnormal_reset', None, step
            self._sleep(POLL_INTERVAL)

    def _read_completion_sample(self, cycle=None):
        sample = None
        for _ in range(3):
            if cycle is not None:
                with self._bus(cycle):
                    sample = self._io.read_sample()
            else:
                sample = self._io.read_sample()
            if isinstance(sample, dict):
                break
            self._sleep(0.05)
        return sample

    def _outcome_from_wait(self, cycle, key, kind, sample, step):
        snapshot = cycle.p1 if key == 'p1' else cycle.p2
        if kind == 'timeout':
            return RESULT_UNKNOWN, 'PHASE_TIMEOUT'
        if kind == 'comm_error':
            return RESULT_ERROR, 'COMMUNICATION_ERROR'
        if kind == 'abnormal_reset':
            return RESULT_ABORTED, 'ABNORMAL_RESET'
        if kind != 'complete':
            return RESULT_UNKNOWN, 'UNKNOWN_STATE'
        if step == 0:
            return RESULT_ABORTED, 'ABNORMAL_RESET'
        return self._evaluate_phase(cycle, snapshot, sample)

    def _evaluate_phase(self, cycle, snapshot, sample):
        """Evidence for one phase; PASS requires terminal sample ownership + identity."""
        if cycle.admission_violation:
            return RESULT_ERROR, cycle.admission_violation
        if not snapshot.activity_observed:
            return RESULT_UNKNOWN, 'NO_ACTIVITY_OBSERVED'
        if not isinstance(sample, dict):
            return RESULT_UNKNOWN, 'MISSING_SAMPLE'
        status = sample.get('status')
        parsed = _parse_status(status)
        if parsed is None:
            return RESULT_UNKNOWN, 'MISSING_STATUS'
        # Terminal sample must identify the exact expected program of THIS phase.
        expected = self._expected_program(cycle, snapshot)
        actual = sample.get('actual_program')
        if expected is None:
            return RESULT_UNKNOWN, 'PROGRAM_UNKNOWN'
        if actual is None:
            return RESULT_UNKNOWN, 'PROGRAM_UNVERIFIED'
        if actual != expected:
            return RESULT_UNKNOWN, 'PROGRAM_MISMATCH'
        if not snapshot.program_identity_verified:
            return RESULT_UNKNOWN, 'PROGRAM_UNVERIFIED'
        # Field protocol confirmed 2026-10-26:
        # - pressure: LAST valid StepCode 6 sample only; no 65535 fallback.
        # - leak: LAST valid StepCode 7 sample; fall back to THIS terminal
        #   (65535) sample only when no valid StepCode 7 leak exists.
        pressure = snapshot.pressure_step6
        pressure_unit = snapshot.pressure_step6_unit
        leak = snapshot.leak_step7
        leak_unit = snapshot.leak_step7_unit
        if leak is None:
            leak = sample.get('leak')
            leak_unit = sample.get('leak_unit')
        # Install the resolved same-cycle evidence before any early return so a
        # FAIL/UNKNOWN payload can never display stale running measurements.
        self._manager.update_phase(
            cycle, 'p1' if snapshot is cycle.p1 else 'p2',
            pressure=pressure, pressure_unit=pressure_unit,
            leak=leak, leak_unit=leak_unit)
        if pressure is None:
            return RESULT_UNKNOWN, 'MISSING_PRESSURE_SAMPLE'
        if leak is None:
            return RESULT_UNKNOWN, 'MISSING_STEP7_LEAK'
        if not parsed['cycle_end']:
            return RESULT_UNKNOWN, 'CYCLE_END_NOT_OBSERVED'
        if parsed['fail_flags'] or not parsed['pass_bit']:
            return RESULT_FAIL, 'STATUS_FAIL'
        if parsed['unknown_bits']:
            return RESULT_UNKNOWN, 'UNKNOWN_STATUS_BITS'
        self._manager.update_phase(
            cycle, 'p1' if snapshot is cycle.p1 else 'p2',
            pressure=pressure, leak=leak,
            pressure_unit=pressure_unit, leak_unit=leak_unit,
            sampled_at=self._clock(), status_word=status)
        return RESULT_PASS, 'OK'

    # ------------------------------------------------------------- finalize
    def _finalize_and_finish(self, cycle, result, reason):
        """Exactly-once finalize: outcome before save, identity before print."""
        with self._finalize_guard_lock:
            if cycle.cycle_id in self._finalized_cycles:
                return
        if not self._manager.is_active(cycle):
            with self._finalize_guard_lock:
                self._finalized_cycles.add(cycle.cycle_id)
            return
        with self._finalize_guard_lock:
            if cycle.cycle_id in self._finalized_cycles:
                return
            self._finalized_cycles.add(cycle.cycle_id)

        self._manager.set_phase(cycle, PHASE_FINALIZING)
        # Recorder must observe the final outcome, reason and time.
        self._manager.set_cycle_field(cycle, overall_result=result,
                                      terminal_reason=reason,
                                      finished_at=self._clock())
        try:
            record = self._recorder(cycle) or {}
        except Exception as exc:
            record = {'record_id': None, 'serial_number': None, 'created': False,
                      'error': f'recorder failed: {exc}'}
        record_id = record.get('record_id')
        serial_number = record.get('serial_number')
        created = bool(record.get('created'))
        record_saved = bool(record_id) and not record.get('error')
        # Saved identity must be installed before the printer runs.
        self._manager.set_cycle_field(
            cycle, record_id=record_id, serial_number=serial_number,
            record_saved=record_saved, record_error=record.get('error'))

        # These five associated-label files belong to persistence, not printing:
        # disabled/failed printing must not suppress them.  An idempotent existing
        # row must not republish stale label data either.
        if record_saved and created:
            try:
                import label_data_files
                label_result = label_data_files.write_saved_cycle(
                    cycle, saved_at=cycle.finished_at)
                self._manager.set_cycle_field(
                    cycle, label_files_state='written', label_files_message='')
                print('[ATEQ] associated label files written; clear in '
                      f"{label_result['clear_seconds']:g}s")
            except Exception as exc:
                # Never misreport this independent filesystem error as a failed
                # database save or a BarTender submission failure.
                self._manager.set_cycle_field(
                    cycle, label_files_state='failed', label_files_message=str(exc))
                print(f'[ATEQ] associated label file write failed: {exc}')

        # Durable print admission: the insert already marked a PASS record as
        # 'auto_pending', so no manual reprint can slip in before this submission.
        print_state = 'skipped'
        print_message = ''
        try:
            if result != RESULT_PASS:
                print_message = '未通过或未取得合格证据，不打印'
            elif not record_saved:
                print_message = record.get('error') or '记录保存失败，不打印'
            elif not serial_number:
                print_message = '缺少有效序列号，不打印'
            elif not created:
                print_message = '记录已存在（幂等），不重复打印'
            elif cycle.cancel_event.is_set():
                print_state = 'cancelled'
                print_message = '停止请求已受理，未提交打印'
            else:
                try:
                    print_result = self._printer(cycle) or {}
                    print_state = print_result.get('state') or (
                        'submitted' if print_result.get('printed') else 'failed')
                    print_message = print_result.get('message', '')
                    if print_result.get('disabled'):
                        print_state = 'disabled'
                except Exception as exc:
                    print_state = 'failed'
                    print_message = f'printer failed: {exc}'
        finally:
            try:
                import database
                if record_id:
                    database.update_print_status(record_id, print_state, print_message)
            except Exception:
                pass

        cycle_fields = {'print_state': print_state, 'print_message': print_message}
        self._manager.set_cycle_field(cycle, **cycle_fields)

        self._manager.finish(cycle, result, reason, cycle_fields)
        self._notify_completion(cycle)
        self._update_status('finished', f'周期结束: {result} ({reason})')
        try:
            self._get_scanner().clear_binding('cycle finished')
        except Exception:
            pass
        self._clear_pending_qr()
        if cycle.context.get('program2') or cycle.trigger in ('hardware', 'scan', 'plc'):
            self._needs_p1_prepare.set()

    # ------------------------------------------------------ readiness prep
    def _prepare_next_p1(self, force=False):
        context = self.get_pending_context()
        program1 = self._to_program(context.get('program1'))
        if program1 is None:
            self._prepare_state = {'ready': False, 'message': '未选择产品程序',
                                   'program': None, 'actual': None,
                                   'at': self._clock()}
            self._needs_p1_prepare.clear()
            return self._prepare_state
        if self._manager.is_busy() or self._manager.reset_inhibits_admission():
            return self._prepare_state
        # Reserve admission for the whole idle-proof/write/readback so a new cycle
        # cannot start between the busy check and the program write.
        if not self._manager.reserve_external():
            self._prepare_state = {'ready': False, 'message': '设备忙，未准备',
                                   'program': program1, 'actual': None,
                                   'at': self._clock()}
            return self._prepare_state
        try:
            with self._bus(None, timeout=5.0):
                actual = self._io.read_current_program()
                if actual == program1:
                    self._prepare_state = {'ready': True, 'message': '已就绪',
                                           'program': program1, 'actual': actual,
                                           'at': self._clock()}
                    self._needs_p1_prepare.clear()
                    return self._prepare_state
                ok, message = self._io.write_program(program1)
                readback = self._io.read_current_program() if ok else None
                ready = ok and readback == program1
                self._prepare_state = {
                    'ready': ready,
                    'message': '已就绪' if ready else f'未就绪: {message}',
                    'program': program1,
                    'actual': readback,
                    'at': self._clock(),
                }
                if ready:
                    self._needs_p1_prepare.clear()
        except Exception as exc:
            self._prepare_state = {'ready': False, 'message': f'未就绪: {exc}',
                                   'program': program1, 'actual': None,
                                   'at': self._clock()}
        finally:
            self._manager.release_external()
        return self._prepare_state

    def _safe_read_program(self):
        try:
            return self._io.read_current_program()
        except Exception:
            return None


# ------------------------------------------------------------------ singleton
_EXECUTOR = None
_EXECUTOR_LOCK = threading.Lock()


def get_test_executor():
    """Lazy singleton; importing this module never creates hardware state."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = TestExecutor()
        return _EXECUTOR


def set_test_executor(executor):
    """Test/embedding hook: replace the process executor explicitly."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        _EXECUTOR = executor
    return executor


def __getattr__(name):
    # PEP 562 compat for `from program_selector import test_executor`.
    if name == 'test_executor':
        return get_test_executor()
    raise AttributeError(name)
