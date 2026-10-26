#!/usr/bin/env python3
"""
ATEQ Modbus 通信工具模块
提供线程安全的 Modbus RTU 串口通信功能

Plan refs: F08/F09, PLAN_FINAL amendments 2,3,9,14.
- one re-entrant transaction lock for single frames AND multi-frame sequences
- typed errors instead of swallowed None (port open vs silence vs CRC vs exception)
- Modbus exception frames are NEVER returned as truthy success
- injectable transport for deterministic offline tests
"""

from __future__ import annotations

import atexit
import contextlib
import os
import socket
import threading
import time

import serial

# 配置参数（现场最新确认 2026-10-01：仪器已改接 COM8 / 9600 / 8E1 / 站号255；
# 扫码枪独立保持 COM7 / 9600 / 8N1）
WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502
STATION_ID = int(os.environ.get('ATEQ_STATION_ID', '255'))
if not 1 <= STATION_ID <= 255:
    raise ValueError('ATEQ_STATION_ID must be between 1 and 255')
SERIAL_PORT = os.environ.get('ATEQ_SERIAL_PORT', 'COM8')
BAUDRATE = int(os.environ.get('ATEQ_BAUDRATE', '9600'))
BYTESIZE = int(os.environ.get('ATEQ_BYTESIZE', '8'))
PARITY = os.environ.get('ATEQ_PARITY', 'E')
STOPBITS = int(os.environ.get('ATEQ_STOPBITS', '1'))
TRANSPORT = os.environ.get('ATEQ_MODBUS_TRANSPORT', 'serial').lower()

# 错误码（对外契约）
ERROR_OK = 'OK'
ERROR_PORT_OPEN_FAILED = 'PORT_OPEN_FAILED'
ERROR_WRITE_TIMEOUT = 'WRITE_TIMEOUT'
ERROR_READ_TIMEOUT = 'READ_TIMEOUT'
ERROR_CRC_MISMATCH = 'CRC_MISMATCH'
ERROR_PROTOCOL = 'PROTOCOL_ERROR'
ERROR_EXCEPTION_RESPONSE = 'EXCEPTION_RESPONSE'
ERROR_HANDLE_CLOSED = 'HANDLE_CLOSED'
ERROR_CANCELLED = 'CANCELLED'

# Modbus 标准异常码 → 中文可读消息（现场确认 2026-10-01）。
# 异常帧始终是类型化失败，绝不作为成功响应返回。
MODBUS_EXCEPTION_MESSAGES = {
    0x01: '非法功能码',
    0x02: '非法数据地址',
    0x03: '非法数据值',
    0x04: '从站设备故障',
    0x05: '确认（设备处理中，请稍后重试）',
    0x06: '从站设备忙',
    0x08: '内存奇偶校验错误',
    0x0A: '网关路径不可用',
    0x0B: '网关目标设备无响应',
}


def modbus_exception_message(code):
    """把标准 Modbus 异常码翻译为中文消息；未知码保留原值，便于现场排查。"""
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return f'未知 Modbus 异常码 {code!r}'
    code_int &= 0xFF
    text = MODBUS_EXCEPTION_MESSAGES.get(code_int)
    if text is None:
        return f'Modbus 异常码 {code_int}（0x{code_int:02X}）: 未收录，请现场记录'
    return f'Modbus 异常码 {code_int}（0x{code_int:02X}）: {text}'

# 全局可重入互斥锁: 单帧收发与多帧事务共用同一把锁。
# 上层可用 transaction() 包住多帧序列，避免与其它参数/命令交错。
modbus_lock = threading.RLock()
_serial_connection = None
_transport_override = None
_PORT_OVERRIDE = None
_override_lock = threading.Lock()

# 发送提交仲裁器（B1）:
# - 所有帧的传输阶段（写请求字节 / fake transport 调用）都在 _write_commit_lock 内，
#   取消谓词与提交在同一临界区内判定，因此 STOP 与命令提交存在唯一线性化点；
# - 响应读取在锁外进行，不把取消/状态锁持到串口读超时结束；
# - STOP 调用 commit_barrier() 取得同一把锁，保证“STOP 返回后不再有新的写/启动帧提交”；
# - 线程本地 send_guard 让取消谓词随实际发送路径生效（即使中间包装函数丢弃 kwargs）。
_write_commit_lock = threading.Lock()
_send_guard_tls = threading.local()


def _guard_stack():
    stack = getattr(_send_guard_tls, 'stack', None)
    if stack is None:
        stack = []
        _send_guard_tls.stack = stack
    return stack


@contextlib.contextmanager
def send_guard(predicate):
    """Bind a cancellation predicate to sends made by the current thread.

    The predicate is evaluated inside the write-commit critical section, immediately
    before the transport write. Pass None for a no-op guard.
    """
    stack = _guard_stack()
    stack.append(predicate)
    try:
        yield
    finally:
        if stack:
            stack.pop()


def _active_guard():
    stack = getattr(_send_guard_tls, 'stack', None)
    for predicate in reversed(stack or []):
        if predicate is not None:
            return predicate
    return None


def _guard_cancelled(cancel_check=None):
    predicate = cancel_check if cancel_check is not None else _active_guard()
    if predicate is None:
        return False
    try:
        return bool(predicate())
    except Exception:
        # Fail closed: a broken cancellation predicate must never be treated as
        # permission to send a state-changing frame.
        return True


def commit_barrier():
    """Linearize STOP against the transport write boundary (short, write-only lock)."""
    with _write_commit_lock:
        return True


class ModbusFrameError(Exception):
    """框架校验错误，携带错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def set_transport_override(fn) -> None:
    """测试注入：fn(frame_hex, timeout) -> response_hex | None | raise serial.*。

    设置后不再触碰真实串口。生产代码不调用。
    """
    global _transport_override
    with _override_lock:
        _transport_override = fn


def clear_transport_override() -> None:
    global _transport_override
    with _override_lock:
        _transport_override = None


def _get_transport_override():
    with _override_lock:
        return _transport_override


def _close_serial_connection():
    global _serial_connection
    connection, _serial_connection = _serial_connection, None
    if connection is not None:
        try:
            if connection.is_open:
                connection.close()
        except Exception:
            pass


def close_modbus_connection():
    """Close the persistent ATEQ serial handle during shutdown or reconfiguration."""
    with modbus_lock:
        _close_serial_connection()


def set_instrument_port(port):
    """Runtime instrument port override (persisted UI setting).

    None/"" reverts to the ATEQ_SERIAL_PORT environment default. Any change drops
    the current handle so the next transaction reopens on the configured port.
    Never guesses a port.
    """
    global _PORT_OVERRIDE
    text = str(port).strip().upper() if port else None
    _PORT_OVERRIDE = text
    close_modbus_connection()


def get_instrument_port():
    return _PORT_OVERRIDE or SERIAL_PORT


atexit.register(close_modbus_connection)


def modbus_crc(data_hex):
    """计算 Modbus RTU CRC16 校验码"""
    data = bytes.fromhex(data_hex)
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return f"{crc & 0xFF:02X}{(crc >> 8) & 0xFF:02X}"


@contextlib.contextmanager
def transaction():
    """多帧事务互斥：与单帧收发共享同一把可重入锁。"""
    with modbus_lock:
        yield


def _result(response, error_code, message, opened, started):
    return {
        'response': response,
        'error_code': error_code,
        'message': message,
        'opened': bool(opened),
        'duration_ms': int((time.monotonic() - started) * 1000),
    }


def send_raw_ex(data_hex, timeout=3, cancel_check=None):
    """发送原始帧并返回带错误码的结果字典（线程安全，永不抛出协议异常）。

    取消判定与实际传输写发生在同一个 `_write_commit_lock` 临界区内：若 STOP 的
    commit_barrier 先获得该锁并置位取消，则本帧绝不写出；响应读取在锁外执行。
    """
    started = time.monotonic()
    with modbus_lock:
        override = _get_transport_override()
        opened = True
        response = None
        try:
            if override is not None:
                # fake transport: commit decision happens under the write-commit lock
                # (the transport call itself may be arbitrarily slow and must not hold
                # the gate; a decided frame is considered committed/in-flight).
                with _write_commit_lock:
                    if _guard_cancelled(cancel_check):
                        return _result(None, ERROR_CANCELLED,
                                       'cancelled before transmit', True, started)
                response = override(data_hex, timeout)
            elif TRANSPORT == 'tcp':
                response = _send_raw_tcp_committed(data_hex, timeout, cancel_check,
                                                   started)
                if isinstance(response, dict):
                    # Transport-level typed result (e.g. CANCELLED) short-circuits
                    # response validation and preserves the send_raw_ex contract.
                    return response
            else:
                written = _serial_commit_write(data_hex, timeout, cancel_check,
                                               started)
                if isinstance(written, dict):
                    # Typed transport result (CANCELLED): never treat as a frame.
                    return written
                opened, expected_length = written
                ser = _serial_connection
                response = ser.read(expected_length)
                response = response.hex().upper() if response else None
        except serial.SerialTimeoutException as exc:
            _close_serial_connection()
            return _result(None, ERROR_WRITE_TIMEOUT, str(exc), True, started)
        except serial.SerialException as exc:
            _close_serial_connection()
            return _result(None, ERROR_PORT_OPEN_FAILED, str(exc), False, started)
        except ValueError as exc:
            return _result(None, ERROR_PROTOCOL, f'invalid frame: {exc}', False, started)
        except Exception as exc:  # defensive: never leak an unexpected type upward
            _close_serial_connection()
            return _result(None, ERROR_PROTOCOL, str(exc), False, started)

        if not response:
            if opened:
                return _result(None, ERROR_READ_TIMEOUT, 'no response from instrument', opened, started)
            return _result(None, ERROR_PORT_OPEN_FAILED, 'serial port not open', opened, started)

        try:
            validated, error_code, message = _validate_response(data_hex, response)
        except ModbusFrameError as exc:
            return _result(None, exc.code, exc.message, opened, started)
        except (ValueError, IndexError) as exc:
            return _result(None, ERROR_PROTOCOL, f'invalid response: {exc}', opened, started)

        if error_code == ERROR_EXCEPTION_RESPONSE:
            # FC exception responses must never be treated as a successful value.
            return _result(None, error_code, message, opened, started)
        return _result(validated, ERROR_OK, '', opened, started)


def send_raw(data_hex, timeout=3, cancel_check=None):
    """发送原始十六进制数据并返回响应；任何错误（含异常帧/取消）返回 None。

    取消谓词在传输写提交临界区内、实际写之前判定（见 send_raw_ex）。
    """
    return send_raw_ex(data_hex, timeout, cancel_check=cancel_check)['response']


def _validate_response(request_hex, response_hex):
    """Validate unit, function, length, echo and CRC.

    Returns (response_hex, error_code, message); exception frames keep their own code
    but are never reported as OK.
    """
    request = bytes.fromhex(request_hex)
    response = bytes.fromhex(response_hex)
    response_hex = response_hex.upper()

    if len(response) == 5 and response[0] == request[0] and response[1] == (request[1] | 0x80):
        if response_hex[-4:] != modbus_crc(response_hex[:-4]):
            raise ModbusFrameError(ERROR_CRC_MISMATCH, 'exception response CRC mismatch')
        return None, ERROR_EXCEPTION_RESPONSE, modbus_exception_message(response[2])

    if len(response) != _expected_response_length(request):
        raise ModbusFrameError(
            ERROR_PROTOCOL, f'response length {len(response)} is invalid')
    if response[0] != request[0] or response[1] != request[1]:
        raise ModbusFrameError(ERROR_PROTOCOL, 'response unit or function does not match request')
    if request[1] in (0x03, 0x04, 0x01, 0x02):
        if len(response) < 5 or response[2] != len(response) - 5:
            raise ModbusFrameError(ERROR_PROTOCOL, 'response byte count is invalid')
    elif response[:6] != request[:6]:
        raise ModbusFrameError(ERROR_PROTOCOL, 'write response echo does not match request')
    if response_hex[-4:] != modbus_crc(response_hex[:-4]):
        raise ModbusFrameError(ERROR_CRC_MISMATCH, 'response CRC mismatch')
    return response_hex, ERROR_OK, ''


def _send_raw_tcp_committed(data_hex, timeout, cancel_check, started):
    """TCP bridge: send under the commit lock, receive outside; CANCELLED is a dict."""
    if _guard_cancelled(cancel_check):
        return _result(None, ERROR_CANCELLED, 'cancelled before transmit', True, started)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((WINDOWS_HOST_IP, TCP_PORT))
        with _write_commit_lock:
            if _guard_cancelled(cancel_check):
                return _result(None, ERROR_CANCELLED, 'cancelled before transmit',
                               True, started)
            sock.sendall(bytes.fromhex(data_hex))
        response = sock.recv(1024)
        return response.hex().upper() if response else None
    except Exception:
        return None
    finally:
        sock.close()


def _send_raw_tcp(data_hex, timeout=3):
    """通过旧 TCP 桥接发送 Modbus RTU 帧（兼容包装）。"""
    return _send_raw_tcp_committed(data_hex, timeout, None, time.monotonic())


def _ensure_serial_open(timeout):
    global _serial_connection
    if _serial_connection is None or not _serial_connection.is_open:
        _close_serial_connection()
        _serial_connection = serial.Serial(
            port=get_instrument_port(),
            baudrate=BAUDRATE,
            bytesize=BYTESIZE,
            parity=_serial_parity(PARITY),
            stopbits=STOPBITS,
            timeout=timeout,
            write_timeout=timeout,
        )


def _serial_commit_write(data_hex, timeout, cancel_check, started):
    """Write the request bytes under the commit lock; reads happen outside.

    Returns (opened, expected_length) or a result dict when cancelled.
    """
    data = bytes.fromhex(data_hex)
    expected_length = _expected_response_length(data)
    _ensure_serial_open(timeout)
    try:
        with _write_commit_lock:
            if _guard_cancelled(cancel_check):
                return _result(None, ERROR_CANCELLED, 'cancelled before transmit',
                               True, started)
            ser = _serial_connection
            ser.timeout = timeout
            if hasattr(ser, 'write_timeout'):
                ser.write_timeout = timeout
            ser.reset_input_buffer()
            written = ser.write(data)
            if written != len(data):
                raise serial.SerialTimeoutException('incomplete Modbus request write')
            ser.flush()
        return True, expected_length
    except serial.SerialException:
        _close_serial_connection()
        raise
    except Exception:
        _close_serial_connection()
        raise


def _send_raw_serial(data_hex, timeout=3):
    """Reuse one serial handle for serialized Modbus RTU transactions (compat).

    Returns (response_hex | None, opened). Raises serial.SerialException /
    SerialTimeoutException so callers can classify open vs write failures.
    """
    written = _serial_commit_write(data_hex, timeout, None, time.monotonic())
    if isinstance(written, dict):
        return None, bool(written.get('opened'))
    opened, expected_length = written
    try:
        response = _serial_connection.read(expected_length)
        return (response.hex().upper() if response else None), opened
    except serial.SerialException:
        _close_serial_connection()
        raise
    except Exception:
        _close_serial_connection()
        raise


def _serial_parity(value):
    value = (value or 'E').upper()
    return {
        'E': serial.PARITY_EVEN,
        'N': serial.PARITY_NONE,
        'O': serial.PARITY_ODD,
    }.get(value, serial.PARITY_EVEN)


def _expected_response_length(request):
    if len(request) < 6:
        return 1024

    function_code = request[1]
    if function_code in (0x05, 0x06, 0x0F, 0x10):
        return 8

    if function_code in (0x03, 0x04):
        count = int.from_bytes(request[4:6], byteorder='big')
        return 5 + count * 2

    if function_code in (0x01, 0x02):
        count = int.from_bytes(request[4:6], byteorder='big')
        return 5 + ((count + 7) // 8)

    return 1024


def _coil_value(value):
    return 0xFF00 if value else 0x0000


def _register_value(value):
    return int(value) & 0xFFFF


def read_holding_registers(address, count):
    """读取保持寄存器（线程安全）"""
    cmd = f"{STATION_ID:02X}03{address:04X}{count:04X}"
    return send_raw(cmd + modbus_crc(cmd))


def read_holding_registers_ex(address, count, timeout=3):
    """Read holding registers with typed error information."""
    cmd = f"{STATION_ID:02X}03{address:04X}{count:04X}"
    return send_raw_ex(cmd + modbus_crc(cmd), timeout=timeout)


def write_single_register(address, value):
    """写单个寄存器（线程安全）"""
    cmd = f"{STATION_ID:02X}06{address:04X}{_register_value(value):04X}"
    return send_raw(cmd + modbus_crc(cmd))


def write_single_coil(address, value):
    """写单个线圈（线程安全）"""
    cmd = f"{STATION_ID:02X}05{address:04X}{_coil_value(value):04X}"
    return send_raw(cmd + modbus_crc(cmd))
