#!/usr/bin/env python3
"""Five BarTender-associated text files, independent of printer submission.

A newly saved test overwrites all five files and starts a fresh clearing timer.
The files are emptied, never deleted.  A stale timer from an earlier test cannot
clear data written by a later test.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import threading

_LOCK = threading.RLock()
_TIMER = None
_GENERATION = 0

FILE_PRODUCT = "产品名称.txt"
FILE_CODE = "代号.txt"
FILE_OPERATOR = "检验员.txt"
FILE_DATETIME = "日期时间.txt"
FILE_LEAK = "泄漏量.txt"
FILENAMES = (FILE_PRODUCT, FILE_CODE, FILE_OPERATOR, FILE_DATETIME, FILE_LEAK)


def _directory() -> Path:
    configured = os.environ.get("ATEQ_ASSOCIATED_LABEL_DIR")
    if configured:
        return Path(configured)
    if os.environ.get("ATEQ_DISABLE_HARDWARE") == "1":
        # Offline regressions must never write the production D: root.  Existing
        # test suites already isolate the BarTender data directory this way.
        return Path(os.environ.get("ATEQ_LABEL_DIR", "offline_label_data"))
    return Path(r"D:\\")


def _clear_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get(
            "ATEQ_LABEL_FILE_CLEAR_SECONDS", "10")))
    except (TypeError, ValueError):
        return 10.0


def _encoding() -> str:
    return os.environ.get("LABEL_DATA_ENCODING", "gbk")


def _text(value) -> str:
    return "" if value is None else str(value)


def _number(value) -> str:
    if isinstance(value, float):
        return format(value, ".15g")
    return _text(value)


def _phase_text(number: int, phase) -> str:
    value = _number(getattr(phase, "leak", None))
    unit = _text(getattr(phase, "leak_unit", None)).strip()
    if value and unit:
        return f"P{number}={value} {unit}"
    return f"P{number}={value}" if value else ""


def leak_text(cycle) -> str:
    """Keep both results for a dual-program cycle, with their original units."""
    p1 = _phase_text(1, cycle.p1)
    p2 = _phase_text(2, cycle.p2) if cycle.p2.required else ""
    if p1 and p2:
        return f"{p1};{p2}"
    return p1 or p2


def write_saved_cycle(cycle, *, directory=None, clear_seconds=None,
                      encoding=None, saved_at=None) -> dict:
    """Synchronously write one saved cycle; schedule an emptying, not deletion.

    The caller must pass only a successfully persisted, newly finalized cycle.
    Any filesystem/encoding exception propagates to that caller for logging and
    must not be represented as a database or printer failure.
    """
    global _TIMER, _GENERATION
    context = cycle.context or {}
    timestamp = datetime.fromtimestamp(saved_at).strftime("%Y-%m-%d %H:%M:%S") \
        if isinstance(saved_at, (int, float)) else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    files = {
        FILE_PRODUCT: _text(context.get("product_model")),
        FILE_CODE: _text(cycle.serial_number),
        FILE_OPERATOR: _text(context.get("operator")),
        FILE_DATETIME: timestamp,
        FILE_LEAK: leak_text(cycle),
    }
    target = Path(directory) if directory is not None else _directory()
    delay = _clear_seconds() if clear_seconds is None else max(0.0, float(clear_seconds))
    codec = encoding or _encoding()
    target.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        # Claim a new generation before doing IO.  An old timer can therefore
        # never erase this result, even if its callback was already waking up.
        if _TIMER is not None:
            _TIMER.cancel()
        _GENERATION += 1
        generation = _GENERATION
        for filename in FILENAMES:
            (target / filename).write_text(
                files[filename], encoding=codec, errors="replace")
        timer = threading.Timer(delay, _clear_generation, args=(target, generation, codec))
        timer.daemon = True
        _TIMER = timer
        timer.start()
    return {"files": [str(target / name) for name in FILENAMES],
            "clear_seconds": delay, "values": files}


def _clear_generation(directory: Path, generation: int, encoding: str) -> None:
    global _TIMER
    try:
        with _LOCK:
            if generation != _GENERATION:
                return
            for filename in FILENAMES:
                (directory / filename).write_text(
                    "", encoding=encoding, errors="replace")
            _TIMER = None
    except Exception as exc:
        # A timer exception must not kill the process or affect the saved record.
        print(f"[ATEQ] associated label file clear failed: {exc}")


def cancel_pending_clear() -> None:
    """Test/shutdown hook: cancel the timer without changing file contents."""
    global _TIMER
    with _LOCK:
        if _TIMER is not None:
            _TIMER.cancel()
            _TIMER = None
