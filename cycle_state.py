#!/usr/bin/env python3
"""Single-cycle state model for the ATEQ executor.

Plan refs: F01/F02/F03/F05/F07, PLAN_FINAL amendments 2,3,7.
- one active cycle at a time; admission is atomic under admission_lock
- per-cycle cancel event + cycle identity on every mutation
- snapshots copied under state_lock; no IO under any lock here
- persistence serializer writes the newest snapshot with a unique temp file
- no imports of serial/modbus: fully unit-testable

Lock order (documented partial order, no inversions):
    admission_lock -> command_lock -> modbus_lock
    state_lock is a leaf lock: acquire it last, never acquire anything else
    while holding it, and never hold it across IO.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import copy
import json
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Optional

PHASE_IDLE = "IDLE"
PHASE_PREPARING = "PREPARING"
PHASE_RUNNING_P1 = "RUNNING_P1"
PHASE_CHAINING_P2 = "CHAINING_P2"
PHASE_RUNNING_P2 = "RUNNING_P2"
PHASE_FINALIZING = "FINALIZING"
PHASE_SAVED = "SAVED"

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_UNKNOWN = "UNKNOWN"
RESULT_ABORTED = "ABORTED"
RESULT_ERROR = "ERROR"
RESULT_INTERRUPTED = "INTERRUPTED"

TERMINAL_PHASES = {PHASE_SAVED, "FAILED", "ABORTED", "ERROR", "INTERRUPTED"}

RESET_IDLE = "idle"
RESET_PENDING = "pending"
RESET_CONFIRMED = "confirmed"
RESET_FAILED = "failed"

# Status word contract (PLAN_FINAL amendment 8):
# bit0 PASS, bits1-4 conservative failure mask, bit5 cycle end, bit15 keyboard lock.
STATUS_PASS_BIT = 0x0001
STATUS_FAIL_MASK = 0x001E
STATUS_CYCLE_END_BIT = 0x0020
STATUS_KNOWN_BITS = STATUS_PASS_BIT | STATUS_FAIL_MASK | STATUS_CYCLE_END_BIT | 0x8000


class CycleCancelled(Exception):
    def __init__(self, reason: str = "cancelled"):
        super().__init__(reason)
        self.reason = reason


@dataclass
class PhaseSnapshot:
    required: bool = False
    status: str = "pending"          # pending|running|completed|skipped|failed
    pressure: Optional[float] = None
    leak: Optional[float] = None
    pressure_unit: Optional[str] = None
    leak_unit: Optional[str] = None
    result: Optional[str] = None
    actual_program: Optional[int] = None
    program_identity_verified: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    terminal_reason: Optional[str] = None
    skip_reason: Optional[str] = None
    sampled_at: Optional[float] = None
    cycle_end_verified: bool = False
    activity_observed: bool = False
    status_word: Optional[int] = None
    unit_unknown: bool = False
    # Same-cycle evidence (field protocol confirmed 2026-10-26):
    # pressure only from the last valid StepCode 6 sample; never from 65535.
    # leak from the last valid StepCode 7 sample, with same-cycle 65535 fallback.
    pressure_step6: Optional[float] = None
    pressure_step6_unit: Optional[str] = None
    leak_step7: Optional[float] = None
    leak_step7_unit: Optional[str] = None


@dataclass
class CycleRecord:
    cycle_id: str
    trigger: str
    context: dict
    required_programs: list = field(default_factory=list)
    created_at: float = 0.0
    phase: str = PHASE_IDLE
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    stop_requested: bool = False
    scan_binding: Optional[dict] = None
    p1: PhaseSnapshot = field(default_factory=PhaseSnapshot)
    p2: PhaseSnapshot = field(default_factory=PhaseSnapshot)
    terminal_reason: Optional[str] = None
    overall_result: Optional[str] = None
    admission_violation: Optional[str] = None
    record_id: Optional[int] = None
    serial_number: Optional[str] = None
    record_saved: bool = False
    record_error: Optional[str] = None
    print_state: str = "pending"     # pending|disabled|skipped|submitted|failed|timeout|busy|unknown
    print_message: str = ""


def _phase_copy(phase: PhaseSnapshot) -> dict:
    return asdict(phase)


def _cycle_copy(cycle: CycleRecord) -> dict:
    data = {
        "cycle_id": cycle.cycle_id,
        "trigger": cycle.trigger,
        "context": copy.deepcopy(cycle.context),
        "required_programs": list(cycle.required_programs),
        "created_at": cycle.created_at,
        "phase": cycle.phase,
        "started_at": cycle.started_at,
        "finished_at": cycle.finished_at,
        "stop_requested": cycle.stop_requested,
        "terminal_reason": cycle.terminal_reason,
        "overall_result": cycle.overall_result,
        "admission_violation": cycle.admission_violation,
        "record_id": cycle.record_id,
        "serial_number": cycle.serial_number,
        "record_saved": cycle.record_saved,
        "record_error": cycle.record_error,
        "print_state": cycle.print_state,
        "print_message": cycle.print_message,
        "scan_binding": copy.deepcopy(cycle.scan_binding),
        "p1": _phase_copy(cycle.p1),
        "p2": _phase_copy(cycle.p2),
    }
    return data


class CycleManager:
    """Owns the single active cycle, completion payload and reset status."""

    def __init__(self, persist_path: Optional[str] = None,
                 persist_enabled: bool = True):
        self._admission_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._active: Optional[CycleRecord] = None
        self._last_cycle_id: Optional[str] = None
        self._completion_sequence = 0
        self._completion_payload: Optional[dict] = None
        self._reset_state = {
            "state": RESET_IDLE,
            "cycle_id": None,
            "at": None,
            "error": None,
        }
        self._persist_path = Path(
            persist_path
            or os.environ.get("ATEQ_RUNTIME_STATE", "")
            or (Path(__file__).resolve().parent / "test_executor_state.json")
        )
        self._persist_enabled = persist_enabled
        self._external_reserved = False
        self._persist_error: Optional[str] = None

    # ------------------------------------------------------------------ state
    def reset_inhibits_admission(self) -> bool:
        """A pending OR failed reset blocks new cycles until idle is proven."""
        with self._state_lock:
            return self._reset_state["state"] in (RESET_PENDING, RESET_FAILED)

    def persist_health(self) -> dict:
        return {
            "enabled": self._persist_enabled,
            "ok": self._persist_error is None,
            "error": self._persist_error,
            "path": str(self._persist_path),
        }

    def reserve_external(self) -> bool:
        """Reserve the instrument for an external multi-frame transaction (e.g. params)."""
        with self._admission_lock:
            with self._state_lock:
                if self._active is not None or self._external_reserved:
                    return False
                if self._reset_state["state"] in (RESET_PENDING, RESET_FAILED):
                    return False
                if self._persist_enabled and self._persist_error:
                    return False
                self._external_reserved = True
                return True

    def release_external(self) -> None:
        with self._admission_lock:
            self._external_reserved = False

    def external_reserved(self) -> bool:
        with self._state_lock:
            return self._external_reserved

    def admit(self, context: dict, trigger: str, required_programs: list,
              scan_binding: Optional[dict] = None,
              now: Optional[float] = None,
              admission_violation: Optional[str] = None) -> tuple[Optional[CycleRecord], Optional[str]]:
        with self._admission_lock:
            with self._state_lock:
                if self._active is not None:
                    return None, "BUSY"
                if self._external_reserved:
                    return None, "DEVICE_BUSY"
                if self._reset_state["state"] == RESET_PENDING:
                    return None, "RESET_PENDING"
                if self._reset_state["state"] == RESET_FAILED:
                    return None, "RESET_FAILED"
                if self._persist_enabled and self._persist_error:
                    return None, "PERSISTENCE_UNAVAILABLE"
            cycle = CycleRecord(
                cycle_id=uuid.uuid4().hex,
                trigger=trigger,
                context=copy.deepcopy(context or {}),
                required_programs=list(required_programs or []),
                created_at=now if now is not None else time.time(),
                phase=PHASE_PREPARING,
                scan_binding=copy.deepcopy(scan_binding),
                admission_violation=admission_violation,
            )
            p1 = cycle.required_programs[0] if len(cycle.required_programs) > 0 else context.get("program1")
            p2 = cycle.required_programs[1] if len(cycle.required_programs) > 1 else None
            cycle.p1.required = p1 is not None
            cycle.p2.required = p2 is not None
            with self._state_lock:
                self._active = cycle
                self._last_cycle_id = cycle.cycle_id
            self.persist()
            if self._persist_enabled and self._persist_error:
                # Durable state could not be established: refuse the admission.
                with self._state_lock:
                    self._active = None
                return None, "PERSISTENCE_UNAVAILABLE"
            return cycle, None

    def get_active(self) -> Optional[CycleRecord]:
        with self._state_lock:
            return self._active

    def is_active(self, cycle: Optional[CycleRecord]) -> bool:
        with self._state_lock:
            return cycle is not None and self._active is cycle

    def is_busy(self) -> bool:
        with self._state_lock:
            return self._active is not None and self._active.phase not in TERMINAL_PHASES

    def set_phase(self, cycle: CycleRecord, phase: str) -> bool:
        with self._state_lock:
            if self._active is not cycle:
                return False
            cycle.phase = phase
        self.persist()
        return True

    def update_phase(self, cycle: CycleRecord, key: str, **fields) -> bool:
        with self._state_lock:
            if self._active is not cycle:
                return False
            snapshot = cycle.p1 if key == "p1" else cycle.p2
            for name, value in fields.items():
                setattr(snapshot, name, value)
        self.persist()
        return True

    def set_cycle_field(self, cycle: CycleRecord, **fields) -> bool:
        with self._state_lock:
            if self._active is not cycle:
                return False
            for name, value in fields.items():
                setattr(cycle, name, value)
        self.persist()
        return True

    def get_cycle_field(self, cycle: CycleRecord, name: str, default=None):
        with self._state_lock:
            return getattr(cycle, name, default)

    # ------------------------------------------------------------ cancel/stop
    def check_cancelled(self, cycle: CycleRecord) -> None:
        with self._state_lock:
            if cycle.cancel_event.is_set():
                raise CycleCancelled("stop requested")
            if self._active is not cycle:
                raise CycleCancelled("cycle superseded")

    def cancel_requested(self, cycle: CycleRecord) -> bool:
        with self._state_lock:
            return cycle.cancel_event.is_set() or self._active is not cycle

    def request_stop(self, reason: str = "user", mark_reset: bool = True) -> dict:
        """Cancel the active cycle and atomically mark reset-pending before returning.

        The reset inhibit is installed under the same state lock as the cancel event so
        no new admission can slip in between cancellation and reset scheduling.
        """
        with self._state_lock:
            cycle = self._active
            if cycle is None:
                return {
                    "cancel_accepted": False,
                    "cycle_id": None,
                    "phase_before": None,
                    "reason": reason,
                    "message": "没有正在执行的周期",
                }
            phase_before = cycle.phase
            cycle.cancel_event.set()
            cycle.stop_requested = True
            if mark_reset:
                self._reset_state = {
                    "state": RESET_PENDING,
                    "cycle_id": cycle.cycle_id,
                    "at": time.time(),
                    "error": None,
                }
        self.persist()
        return {
            "cancel_accepted": True,
            "cycle_id": cycle.cycle_id,
            "phase_before": phase_before,
            "reason": reason,
            "message": "取消已受理",
        }

    # --------------------------------------------------------------- finalize
    def finish(self, cycle: CycleRecord, result: str, reason: str,
               cycle_fields: Optional[dict] = None) -> bool:
        """Atomically finalize the active cycle exactly once."""
        with self._state_lock:
            if self._active is not cycle:
                return False
            if cycle_fields:
                for name, value in cycle_fields.items():
                    setattr(cycle, name, value)
            cycle.overall_result = result
            cycle.terminal_reason = reason
            cycle.finished_at = time.time()
            cycle.phase = PHASE_SAVED if result == RESULT_PASS else (
                terminal_phase_for(result))
            payload = self._build_completion_payload(cycle)
            self._completion_payload = payload
            self._completion_sequence += 1
            self._last_cycle_id = cycle.cycle_id
            self._active = None
        self.persist()
        return True

    def _build_completion_payload(self, cycle: CycleRecord) -> dict:
        p1 = _phase_copy(cycle.p1)
        p2 = _phase_copy(cycle.p2)
        return {
            "cycle_id": cycle.cycle_id,
            "completion_sequence": self._completion_sequence + 1,
            "trigger": cycle.trigger,
            "qr_code": cycle.context.get("qr_code"),
            "scan_source": cycle.context.get("scan_source"),
            "device_verified": cycle.context.get("device_verified"),
            "program1": cycle.context.get("program1"),
            "program2": cycle.context.get("program2"),
            "actual_program1": p1.get("actual_program"),
            "actual_program2": p2.get("actual_program"),
            "pressure1": p1.get("pressure"),
            "leak1": p1.get("leak"),
            "pressure1_unit": p1.get("pressure_unit"),
            "leak1_unit": p1.get("leak_unit"),
            "result1": p1.get("result"),
            "program1_pass": p1.get("result") == RESULT_PASS,
            "pressure2": p2.get("pressure"),
            "leak2": p2.get("leak"),
            "pressure2_unit": p2.get("pressure_unit"),
            "leak2_unit": p2.get("leak_unit"),
            "result2": p2.get("result"),
            "program2_pass": p2.get("result") == RESULT_PASS,
            "overall_result": cycle.overall_result,
            "serial_number": cycle.serial_number,
            "record_id": cycle.record_id,
            "record_saved": cycle.record_saved,
            "record_error": cycle.record_error,
            "print_state": cycle.print_state,
            "print_attempted": cycle.print_state not in ("pending", "disabled", "skipped"),
            "print_success": cycle.print_state in ("submitted",),
            "print_message": cycle.print_message,
            "terminal_reason": cycle.terminal_reason,
            "admission_violation": cycle.admission_violation,
            "phase": cycle.phase,
        }

    # ---------------------------------------------------------------- reset
    def set_reset_state(self, state: str, cycle_id: Optional[str] = None,
                        error: Optional[str] = None, at: Optional[float] = None) -> None:
        normalized = str(state or RESET_IDLE).strip().lower()
        with self._state_lock:
            self._reset_state = {
                "state": normalized,
                "cycle_id": cycle_id,
                "at": at if at is not None else time.time(),
                "error": error,
            }
        self.persist()

    def get_reset_state(self) -> dict:
        with self._state_lock:
            return dict(self._reset_state)

    def reset_pending(self) -> bool:
        with self._state_lock:
            return self._reset_state["state"] == RESET_PENDING

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        with self._state_lock:
            cycle = self._active
            cycle_id = cycle.cycle_id if cycle is not None else self._last_cycle_id
            payload = copy.deepcopy(self._completion_payload)
            if payload is not None and payload.get("cycle_id") != cycle_id:
                payload = None
            return {
                "cycle_id": cycle_id,
                "busy": cycle is not None and cycle.phase not in TERMINAL_PHASES,
                "running": cycle is not None and cycle.phase in (
                    PHASE_PREPARING, PHASE_RUNNING_P1, PHASE_CHAINING_P2, PHASE_RUNNING_P2),
                "phase": cycle.phase if cycle is not None else (payload or {}).get("phase"),
                "trigger": cycle.trigger if cycle is not None else None,
                "required_programs": list(cycle.required_programs) if cycle else [],
                "terminal_reason": cycle.terminal_reason if cycle else (payload or {}).get("terminal_reason"),
                "admission_violation": cycle.admission_violation if cycle else None,
                "p1": _phase_copy(cycle.p1) if cycle else None,
                "p2": _phase_copy(cycle.p2) if cycle else None,
                "completion_sequence": self._completion_sequence,
                "completion_payload": payload,
                "reset": dict(self._reset_state),
                "cycle_context": copy.deepcopy(cycle.context) if cycle else None,
                "persist": self.persist_health(),
            }

    # ------------------------------------------------------------ persistence
    def persist(self) -> None:
        """Serialize the newest snapshot with a unique temp file + atomic replace."""
        if not self._persist_enabled:
            return
        with self._persist_lock:
            with self._state_lock:
                cycle = self._active
                state = {
                    "version": 2,
                    "updated_at": time.time(),
                    "cycle_id": cycle.cycle_id if cycle else self._last_cycle_id,
                    "active": _cycle_copy(cycle) if cycle is not None else None,
                    "completion_sequence": self._completion_sequence,
                    "completion_payload": copy.deepcopy(self._completion_payload),
                    "last_cycle_id": self._last_cycle_id,
                    "reset": dict(self._reset_state),
                }
            try:
                self._persist_path.parent.mkdir(parents=True, exist_ok=True)
                temp_file = self._persist_path.with_suffix(
                    f".{uuid.uuid4().hex}.tmp")
                temp_file.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                os.replace(temp_file, self._persist_path)
                self._persist_error = None
            except Exception as exc:
                # Durable state failure is surfaced and inhibits new admission.
                self._persist_error = f"{type(exc).__name__}: {exc}"

    def interrupted_evidence_path(self) -> Path:
        return self._persist_path.with_suffix(".interrupted.json")

    def read_persisted(self) -> Optional[dict]:
        if not self._persist_enabled:
            # Disabled persistence means no disk state at all (no reads, no writes).
            return None
        try:
            if not self._persist_path.exists():
                return None
            return json.loads(self._persist_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def note_interrupted(self, persisted: dict, result: str = RESULT_UNKNOWN,
                         reason: str = RESULT_INTERRUPTED) -> Optional[dict]:
        """Mark a persisted unfinished cycle as INTERRUPTED without restarting it."""
        with self._state_lock:
            active = (persisted or {}).get("active")
            if not isinstance(active, dict):
                return None
            if self._active is not None:
                return None
            cycle_id = active.get("cycle_id") or uuid.uuid4().hex
            payload = {
                "cycle_id": cycle_id,
                "completion_sequence": self._completion_sequence + 1,
                "trigger": active.get("trigger"),
                "program1": (active.get("context") or {}).get("program1"),
                "program2": (active.get("context") or {}).get("program2"),
                "actual_program1": None,
                "actual_program2": None,
                "pressure1": None,
                "leak1": None,
                "result1": None,
                "program1_pass": False,
                "pressure2": None,
                "leak2": None,
                "result2": None,
                "program2_pass": False,
                "overall_result": result,
                "serial_number": None,
                "record_id": None,
                "record_saved": False,
                "record_error": None,
                "print_state": "skipped",
                "print_attempted": False,
                "print_success": False,
                "print_message": "重启恢复：周期未完成，不自动重打",
                "terminal_reason": reason,
                "admission_violation": None,
                "phase": RESULT_INTERRUPTED,
                "interrupted": True,
            }
            self._completion_payload = payload
            self._completion_sequence += 1
            self._last_cycle_id = cycle_id
        self.persist()
        return payload

    def restore_completion(self) -> None:
        """Reload completion sequence/payload from disk (no active cycle)."""
        if not self._persist_enabled:
            return
        persisted = self.read_persisted()
        if not isinstance(persisted, dict):
            return
        with self._state_lock:
            try:
                self._completion_sequence = int(persisted.get("completion_sequence") or 0)
            except Exception:
                self._completion_sequence = 0
            payload = persisted.get("completion_payload")
            self._completion_payload = payload if isinstance(payload, dict) else None
            self._last_cycle_id = persisted.get("last_cycle_id")
            reset = persisted.get("reset")
            if isinstance(reset, dict):
                self._reset_state = {
                    "state": reset.get("state", RESET_IDLE),
                    "cycle_id": reset.get("cycle_id"),
                    "at": reset.get("at"),
                    "error": reset.get("error"),
                }


def terminal_phase_for(result: str) -> str:
    """Terminal phase label for non-PASS results."""
    return {
        RESULT_PASS: PHASE_SAVED,
        RESULT_FAIL: "FAILED",
        RESULT_ABORTED: "ABORTED",
        RESULT_ERROR: "ERROR",
        RESULT_INTERRUPTED: RESULT_INTERRUPTED,
    }.get(result, "ERROR")
