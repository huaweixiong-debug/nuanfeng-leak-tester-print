#!/usr/bin/env python3
"""Serial barcode/QR scanner support.

Plan refs: F06, PLAN_FINAL amendments 4, 11.
- every accepted payload becomes a numbered event (session_id, seq, event_id)
- full raw payload is preserved byte-for-byte after delimiter/whitespace strip
- bounded event queue with explicit overflow; overflow invalidates any binding
  derived from a dropped event (no ambiguous pending code can be trusted)
- backend consumer callback processes events autonomously (no browser required)
- explicit start/stop lifecycle; importing this module performs no I/O
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, asdict, field
from datetime import datetime
import logging
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable, Optional

import serial


ROOT = Path(__file__).resolve().parent
LOG_PATH = Path(os.environ.get("ATEQ_SCANNER_LOG", ROOT / "serial_scanner.log"))

SCAN_PORT = os.environ.get("SCAN_SERIAL_PORT", "COM7")
SCAN_BAUDRATE = int(os.environ.get("SCAN_SERIAL_BAUDRATE", "9600"))
SCAN_BYTESIZE = int(os.environ.get("SCAN_SERIAL_BYTESIZE", "8"))
SCAN_PARITY = os.environ.get("SCAN_SERIAL_PARITY", "N").upper()
SCAN_STOPBITS = float(os.environ.get("SCAN_SERIAL_STOPBITS", "1"))
SCAN_TIMEOUT = float(os.environ.get("SCAN_SERIAL_TIMEOUT", "0.05"))
SCAN_IDLE_SECONDS = float(os.environ.get("SCAN_IDLE_SECONDS", "0.45"))
SCAN_RECONNECT_SECONDS = float(os.environ.get("SCAN_RECONNECT_SECONDS", "2.0"))
SCAN_QUEUE_MAX = int(os.environ.get("SCAN_QUEUE_MAX", "64"))
SCAN_EVENT_HISTORY_MAX = int(os.environ.get("SCAN_EVENT_HISTORY_MAX", "256"))

logger = logging.getLogger("SerialScanner")
if not logger.handlers:
    if os.environ.get("ATEQ_DISABLE_HARDWARE") == "1":
        # Tests must not create log artifacts at import time.
        logger.addHandler(logging.NullHandler())
    else:
        try:
            Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s - %(levelname)s - %(message)s"))
            logger.addHandler(handler)
        except Exception:  # logging must never break scanning
            logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.INFO)


SCAN_DELIMITERS = {10, 13, 9}
SCAN_STRIP_BYTES = b"\x00\x02\x03\r\n\t "

# 事件处理结果（业务层/接口层使用）
RESULT_PENDING = "PENDING"
RESULT_BUSY = "BUSY"
RESULT_BOUND = "BOUND"
RESULT_AUTO_STARTED = "AUTO_STARTED"
RESULT_NO_PRODUCT = "NO_PRODUCT"
RESULT_MANUAL = "MANUAL"
RESULT_REJECTED = "REJECTED"
RESULT_UNKNOWN = "UNKNOWN"


@dataclass
class ScanState:
    seq: int = 0
    code: str = ""
    raw_hex: str = ""
    timestamp: str = ""
    port: str = SCAN_PORT
    connected: bool = False
    error: str = ""
    session_id: str = ""
    event_id: str = ""
    pending_count: int = 0
    dropped_count: int = 0
    overflowed: bool = False
    processed_seq: int = 0
    last_result: str = ""


@dataclass
class ScanEvent:
    session_id: str
    seq: int
    code: str
    raw_hex: str
    timestamp: str
    event_id: str
    source: str = "device"          # device | manual
    device_verified: bool = False   # manual typed QR is never device verified
    result: str = RESULT_PENDING
    started: bool = False
    message: str = ""
    processed: bool = False


_LOCK = threading.RLock()
_STATE = ScanState()
_QUEUE: deque = deque()
_EVENTS: "OrderedDict[str, ScanEvent]" = OrderedDict()
_BINDING: Optional[ScanEvent] = None
_BINDING_INVALID = False
_CONSUMER: Optional[Callable[[dict], None]] = None
_STARTED = False
_STOP_EVENT = threading.Event()
_THREAD: Optional[threading.Thread] = None
_PORT_OVERRIDE: Optional[str] = None
# Set when a stop timed out with the worker still alive (mixed old/new config);
# sticky until process restart: no new worker may start, no manual/forged event
# may create an admission credential.
_RECONFIG_PENDING = False


def set_scan_port(port: Optional[str]) -> None:
    """Runtime scanner port override (persisted UI setting). None/"" reverts to
    the SCAN_SERIAL_PORT environment default."""
    global _PORT_OVERRIDE
    with _LOCK:
        _PORT_OVERRIDE = str(port).strip().upper() if port else None


def get_scan_port() -> str:
    with _LOCK:
        return _PORT_OVERRIDE or SCAN_PORT


def _new_session_id() -> str:
    return uuid.uuid4().hex


_STATE.session_id = _new_session_id()


def set_event_callback(callback: Optional[Callable[[dict], None]]) -> None:
    """Register the backend consumer called once per accepted scan event."""
    global _CONSUMER
    with _LOCK:
        _CONSUMER = callback


def _serial_parity(value: str):
    return {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "M": serial.PARITY_MARK,
        "S": serial.PARITY_SPACE,
    }.get((value or "N").upper(), serial.PARITY_NONE)


def _clean_scan_bytes(raw: bytes) -> bytes:
    return raw.strip(SCAN_STRIP_BYTES)


def _decode_scan(raw: bytes) -> str:
    cleaned = _clean_scan_bytes(raw)
    for encoding in ("utf-8", "gbk", "latin1"):
        try:
            return cleaned.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return cleaned.decode("latin1", errors="replace").strip()


def _publish_event(code: str, raw_bytes: bytes, source: str = "device",
                   device_verified: bool = True) -> Optional[ScanEvent]:
    """Create, queue and dispatch one scan event. Raw payload is never altered."""
    if not code:
        return None
    with _LOCK:
        if _RECONFIG_PENDING:
            # Old-config worker (or a manual injection) must not admit events while
            # the scanner configuration is in an unresolved state.
            return None
        _STATE.seq += 1
        seq = _STATE.seq
        event = ScanEvent(
            session_id=_STATE.session_id,
            seq=seq,
            code=code,
            raw_hex=raw_bytes.hex(" ").upper(),
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            event_id=f"{_STATE.session_id}:{seq}",
            source=source,
            device_verified=bool(device_verified),
        )
        _QUEUE.append(event)
        _EVENTS[event.event_id] = event
        while len(_EVENTS) > SCAN_EVENT_HISTORY_MAX:
            _EVENTS.popitem(last=False)

        while len(_QUEUE) > SCAN_QUEUE_MAX:
            dropped = _QUEUE.popleft()
            _STATE.dropped_count += 1
            _STATE.overflowed = True
            dropped.result = RESULT_REJECTED
            dropped.message = "队列溢出，事件被丢弃；请重新扫码"
            # A binding derived from a dropped event must never be usable as a
            # trusted scan credential.
            global _BINDING, _BINDING_INVALID
            if _BINDING is not None and _BINDING.event_id == dropped.event_id:
                _BINDING = None
                _BINDING_INVALID = True
            logger.warning("Scan queue overflow, dropped event %s", dropped.event_id)

        if source == "device":
            _STATE.code = code
            _STATE.raw_hex = event.raw_hex
            _STATE.timestamp = event.timestamp
        _STATE.event_id = event.event_id
        _STATE.pending_count = sum(1 for e in _QUEUE if not e.processed)
        _STATE.connected = True
        _STATE.error = ""
        _STATE.port = get_scan_port()
        consumer = _CONSUMER

    logger.info("SCAN %s len=%s source=%s code=%s raw=%s",
                event.event_id, len(code), source, code, event.raw_hex)

    if consumer is not None:
        try:
            consumer(_event_dict(event))
        except Exception as exc:  # consumer failure must not kill the scanner loop
            logger.warning("Scan consumer failed for %s: %s", event.event_id, exc)
    return event


def _event_dict(event: ScanEvent) -> dict:
    return asdict(event)


def publish_scan_for_test(raw: bytes, source: str = "device") -> Optional[ScanEvent]:
    """Raw-layer test hook: feed bytes exactly as the serial loop would."""
    return _publish_event(_decode_scan(raw), _clean_scan_bytes(raw), source=source,
                          device_verified=(source == "device"))


def _consume_chunk(buffer: bytearray, chunk: bytes) -> None:
    for byte in chunk:
        if byte in SCAN_DELIMITERS:
            if buffer:
                raw = bytes(buffer)
                buffer.clear()
                _publish_event(_decode_scan(raw), _clean_scan_bytes(raw))
            continue
        buffer.append(byte)


def get_event(event_id: str) -> Optional[dict]:
    with _LOCK:
        event = _EVENTS.get(event_id)
        return _event_dict(event) if event else None


def mark_event_result(event_id: str, result: str, *, started: bool = False,
                      message: str = "", processed: bool = True) -> Optional[dict]:
    with _LOCK:
        event = _EVENTS.get(event_id)
        if event is None:
            return None
        event.result = result
        event.started = bool(started)
        event.message = message
        event.processed = bool(processed)
        _STATE.last_result = result
        if processed:
            _STATE.processed_seq = max(_STATE.processed_seq, event.seq)
        _STATE.pending_count = sum(1 for e in _QUEUE if not e.processed)
        return _event_dict(event)


def _forget_queue_event(event_id: str) -> None:
    for ev in list(_QUEUE):
        if ev.event_id == event_id:
            try:
                _QUEUE.remove(ev)
            except ValueError:
                pass
            return


def set_binding(event_id: str, source: Optional[str] = None) -> Optional[dict]:
    """Mark an existing event as the single unconsumed admission credential."""
    global _BINDING, _BINDING_INVALID
    with _LOCK:
        event = _EVENTS.get(event_id)
        if event is None:
            return None
        if _BINDING is not None and _BINDING.event_id != event_id:
            _BINDING.processed = True
            _forget_queue_event(_BINDING.event_id)
        if source is not None:
            event.source = source
            event.device_verified = (source == "device")
        _BINDING = event
        _BINDING_INVALID = False
        event.result = RESULT_BOUND
        _STATE.last_result = RESULT_BOUND
        return _event_dict(event)


def create_manual_binding(code: str) -> Optional[dict]:
    """Frozen compatibility rule: a typed/legacy qr_code creates a manual binding.

    It is explicitly NOT device verified; it never bypasses scan_required semantics
    beyond being a single admission credential.
    """
    if not code or not str(code).strip():
        return None
    event = _publish_event(str(code).strip(), str(code).strip().encode("utf-8"),
                           source="manual", device_verified=False)
    if event is None:
        return None
    return set_binding(event.event_id, source="manual")


def get_binding() -> Optional[dict]:
    with _LOCK:
        if _BINDING is None or _BINDING_INVALID:
            return None
        return _event_dict(_BINDING)


def has_unconsumed_binding() -> bool:
    return get_binding() is not None


def consume_binding() -> Optional[dict]:
    global _BINDING, _BINDING_INVALID
    with _LOCK:
        if _BINDING is None or _BINDING_INVALID:
            return None
        event = _BINDING
        _BINDING = None
        _BINDING_INVALID = False
        event.processed = True
        _forget_queue_event(event.event_id)
        _STATE.processed_seq = max(_STATE.processed_seq, event.seq)
        _STATE.pending_count = sum(1 for e in _QUEUE if not e.processed)
        return _event_dict(event)


def clear_binding(reason: str = "") -> None:
    global _BINDING, _BINDING_INVALID
    with _LOCK:
        if _BINDING is not None:
            _BINDING.result = RESULT_REJECTED
            _BINDING.message = reason or "扫码绑定已清除"
            _BINDING.processed = True
            _forget_queue_event(_BINDING.event_id)
        _BINDING = None
        _BINDING_INVALID = False


def binding_invalidated() -> bool:
    with _LOCK:
        return bool(_BINDING_INVALID)


def get_scanner_state() -> dict[str, Any]:
    with _LOCK:
        data = asdict(_STATE)
        data["pending_count"] = sum(1 for e in _QUEUE if not e.processed)
        binding = None if (_BINDING is None or _BINDING_INVALID) else {
            "event_id": _BINDING.event_id,
            "code": _BINDING.code,
            "source": _BINDING.source,
            "device_verified": _BINDING.device_verified,
            "seq": _BINDING.seq,
        }
    data.update(
        {
            "port": get_scan_port(),
            "reconfig_pending": _RECONFIG_PENDING,
            "baudrate": SCAN_BAUDRATE,
            "bytesize": SCAN_BYTESIZE,
            "parity": SCAN_PARITY,
            "stopbits": SCAN_STOPBITS,
            "binding": binding,
            "binding_invalid": binding_invalidated(),
            "queue_max": SCAN_QUEUE_MAX,
        }
    )
    return data


def start_serial_scanner() -> bool:
    """Start the reader thread. Returns True only when a live worker owns the port.

    Refuses while a stop is in flight, while the old worker is still alive, or
    after a failed stop (reconfig pending) so two workers can never overlap on
    different ports.
    """
    global _STARTED, _THREAD
    with _LOCK:
        if _RECONFIG_PENDING:
            return False
        if _STARTED and _THREAD is not None and _THREAD.is_alive():
            if _STOP_EVENT.is_set():
                return False
            return True
        if _STARTED:
            # Stale retained state with a dead worker: clear before restarting.
            _STARTED = False
            _THREAD = None
        _STOP_EVENT.clear()
        _STARTED = True
        _THREAD = threading.Thread(target=_scanner_loop, name="SerialScanner", daemon=True)
        _THREAD.start()
        return True


def stop_serial_scanner(timeout: float = 2.0) -> bool:
    """Stop the reader thread; True only when termination is confirmed.

    On timeout the thread state is retained truthfully (running=True), the sticky
    reconfig-pending flag is set, and the stop token is left set so the old worker
    keeps exiting instead of being revived by a later start.
    """
    global _STARTED, _THREAD, _RECONFIG_PENDING
    with _LOCK:
        _STOP_EVENT.set()
        thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout)
    with _LOCK:
        alive = thread is not None and thread.is_alive()
        if alive:
            _RECONFIG_PENDING = True
            _STARTED = True
            _THREAD = thread
            return False
        if _THREAD is thread:
            _THREAD = None
        _STARTED = False
        return True


def reconfig_pending() -> bool:
    with _LOCK:
        return bool(_RECONFIG_PENDING)


def clear_pending_events(reason: str = "") -> None:
    """Drop unconsumed events after a scanner disable/port change.

    Prevents an old-port event or binding from being admitted under the new
    configuration.
    """
    with _LOCK:
        _QUEUE.clear()
        _EVENTS.clear()
        _STATE.pending_count = 0
    clear_binding(reason)


def scanner_running() -> bool:
    with _LOCK:
        return bool(_STARTED)


def _scanner_loop() -> None:
    while not _STOP_EVENT.is_set():
        try:
            _read_serial_forever()
        except Exception as exc:
            if _STOP_EVENT.is_set():
                break
            with _LOCK:
                _STATE.connected = False
                _STATE.error = str(exc)
                _STATE.port = get_scan_port()
            logger.warning("Scanner serial loop failed: %s", exc)
            _STOP_EVENT.wait(SCAN_RECONNECT_SECONDS)
    with _LOCK:
        _STATE.connected = False


def _read_serial_forever() -> None:
    buffer = bytearray()
    last_byte_time = 0.0
    port = get_scan_port()

    with serial.Serial(
        port=port,
        baudrate=SCAN_BAUDRATE,
        bytesize=SCAN_BYTESIZE,
        parity=_serial_parity(SCAN_PARITY),
        stopbits=SCAN_STOPBITS,
        timeout=SCAN_TIMEOUT,
    ) as ser:
        ser.reset_input_buffer()
        with _LOCK:
            _STATE.connected = True
            _STATE.error = ""
            _STATE.port = port
        logger.info(
            "Scanner opened: %s %s %s%s%s",
            port,
            SCAN_BAUDRATE,
            SCAN_BYTESIZE,
            SCAN_PARITY,
            SCAN_STOPBITS,
        )

        while not _STOP_EVENT.is_set():
            chunk = ser.read(64)
            now = time.monotonic()

            if chunk:
                if buffer and last_byte_time and now - last_byte_time >= SCAN_IDLE_SECONDS:
                    raw = bytes(buffer)
                    buffer.clear()
                    _publish_event(_decode_scan(raw), _clean_scan_bytes(raw))
                _consume_chunk(buffer, chunk)
                last_byte_time = now
                continue

            if buffer and last_byte_time and now - last_byte_time >= SCAN_IDLE_SECONDS:
                raw = bytes(buffer)
                buffer.clear()
                _publish_event(_decode_scan(raw), _clean_scan_bytes(raw))
