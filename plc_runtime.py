#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Connect the S7-200 SMART handshake to the ATEQ cycle executor.

The PLC owns M0.0/M0.1 (two-second start/reset pulses).  This runtime owns
M0.2/M0.3 and emits one two-second pulse for each definitive PASS/FAIL cycle.
UNKNOWN, ABORTED and ERROR are not product-quality verdicts and emit no pulse.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

from plc_s7_smart import HandshakeMonitor, S7SmartPlc


logger = logging.getLogger("S7SmartRuntime")


class PlcRuntime:
    """Own PLC monitoring and a serialized, non-blocking result-pulse worker."""

    def __init__(
        self,
        on_start: Callable[[], object],
        on_reset: Callable[[], object],
        plc: Optional[S7SmartPlc] = None,
        monitor_factory=HandshakeMonitor,
        result_queue_size: int = 8,
    ) -> None:
        self._on_start = on_start
        self._on_reset = on_reset
        self._plc = plc or S7SmartPlc()
        self._monitor_factory = monitor_factory
        self._monitor = None
        self._results = queue.Queue(maxsize=max(int(result_queue_size), 1))
        self._stop_event = threading.Event()
        self._result_thread = None
        self._started = False
        self._lifecycle_lock = threading.Lock()

    @staticmethod
    def _report_callback_result(kind: str, result: object) -> None:
        if isinstance(result, tuple) and len(result) >= 2:
            ok, detail = bool(result[0]), result[1]
            if not ok:
                logger.warning("PLC %s rejected: %s", kind, detail)
        elif isinstance(result, dict) and kind == "start" and not result.get("success", True):
            logger.warning("PLC start rejected: %s", result)

    def _start_callback(self) -> None:
        try:
            result = self._on_start()
            self._report_callback_result("start", result)
            logger.info("PLC start callback completed: %s", result)
        except Exception as exc:
            logger.exception("PLC start callback failed: %s", exc)

    def _reset_callback(self) -> None:
        try:
            result = self._on_reset()
            logger.info("PLC reset callback completed: %s", result)
        except Exception as exc:
            logger.exception("PLC reset callback failed: %s", exc)

    def _monitor_error(self, exc: BaseException) -> None:
        logger.warning("PLC monitor connection/read error: %s", exc)

    def start(self) -> None:
        """Start both workers; connection retries continue in the monitor."""
        with self._lifecycle_lock:
            if self._started:
                return
            self._stop_event.clear()
            self._result_thread = threading.Thread(
                target=self._result_loop, name="PlcResultPulse", daemon=True)
            self._result_thread.start()
            self._monitor = self._monitor_factory(
                self._plc,
                on_start=self._start_callback,
                on_reset=self._reset_callback,
                on_error=self._monitor_error,
            )
            try:
                self._monitor.start()
            except Exception:
                self._stop_event.set()
                self._results.put_nowait(None)
                self._result_thread.join(1.0)
                self._result_thread = None
                self._monitor = None
                raise
            self._started = True
        logger.info("PLC runtime started: %s:%s", self._plc.ip, self._plc.port)

    def enqueue_result(self, completion: dict) -> bool:
        """Completion callback: return immediately; never block cycle finalization."""
        if not isinstance(completion, dict):
            return False
        result = completion.get("overall_result")
        cycle_id = completion.get("cycle_id")
        reason = completion.get("terminal_reason")
        if result not in ("PASS", "FAIL"):
            logger.info("No PLC result pulse for cycle=%s result=%s reason=%s",
                        cycle_id, result, reason)
            return False
        try:
            self._results.put_nowait((cycle_id, result, reason))
            return True
        except queue.Full:
            logger.error("PLC result queue full; pulse dropped cycle=%s result=%s",
                         cycle_id, result)
            return False

    def _result_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._results.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            cycle_id, result, reason = item
            try:
                pulse = self._plc.pulse_result(ok=(result == "PASS"))
                logger.info("PLC result pulse completed cycle=%s result=%s reason=%s elapsed=%sms",
                            cycle_id, result, reason, pulse.get("elapsed_ms"))
            except Exception as exc:
                # Do not retry automatically: a lost response cannot prove whether the
                # first output write reached the PLC, and a retry could double-pulse it.
                logger.error("PLC result pulse failed cycle=%s result=%s: %s",
                             cycle_id, result, exc)

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            self._started = False
            self._stop_event.set()
            monitor = self._monitor
            self._monitor = None
            result_thread = self._result_thread
            self._result_thread = None
        if monitor is not None:
            monitor.stop()
            monitor.join(2.0)
        try:
            self._results.put_nowait(None)
        except queue.Full:
            pass
        if result_thread is not None:
            result_thread.join(max(float(self._plc.pulse_seconds) + 1.0, 2.0))
        try:
            self._plc.clear_outputs()
        except Exception:
            pass
        self._plc.disconnect()
        logger.info("PLC runtime stopped")
