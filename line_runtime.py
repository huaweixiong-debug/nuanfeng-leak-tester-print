#!/usr/bin/env python3
"""
Runtime settings for scan gating and label printing.

Plan refs: F06/F10, PLAN_FINAL amendments 4,10.
- atomic settings write
- one print path for automatic and manual reprint; rich state result
- disabled printers are never attempted; "printed" only means submitted
- per-record in-flight tracking: concurrent duplicate jobs are rejected BUSY
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parent

# Bool switches + optional serial ports. Empty port string means "use the
# environment/default fallback" (kept backward compatible with old files that
# only carried the three booleans).
BOOL_KEYS = ("scan_required", "printer_enabled", "scan_start", "scanner_enabled")
PORT_KEYS = ("scanner_port", "instrument_port")

DEFAULT_SETTINGS = {
    "scan_required": True,
    "printer_enabled": True,
    "scan_start": False,
    "scanner_enabled": True,
    "scanner_port": "",
    "instrument_port": "",
}
_LOCK = threading.RLock()
_INFLIGHT = set()

_COM_RE = re.compile(r"^COM[1-9][0-9]{0,3}$")

# Actual defaults used by the device modules when the persisted port is empty.
SCANNER_DEFAULT_PORT = os.environ.get("SCAN_SERIAL_PORT", "COM7").strip().upper()
INSTRUMENT_DEFAULT_PORT = os.environ.get("ATEQ_SERIAL_PORT", "COM8").strip().upper()


def effective_ports(settings: dict[str, Any]) -> tuple[str, str]:
    """Resolve persisted ports to the actual ports the modules would use."""
    scanner = str(settings.get("scanner_port") or "").strip().upper() or SCANNER_DEFAULT_PORT
    instrument = str(settings.get("instrument_port") or "").strip().upper() or INSTRUMENT_DEFAULT_PORT
    return scanner, instrument


class SettingsError(ValueError):
    """Validation failure: nothing is written when this is raised."""


def normalize_port(value: Any, *, allow_empty: bool = True) -> str:
    """Canonicalise a Windows COM port name.

    Accepts None/"" (unset, when allow_empty) and COM1..COM9999 case-insensitively;
    raises SettingsError on anything else. Never guesses a port.
    """
    text = "" if value is None else str(value).strip().upper()
    if not text:
        if allow_empty:
            return ""
        raise SettingsError("串口号不能为空")
    if not _COM_RE.match(text):
        raise SettingsError(f"串口号格式无效: {value!r}（应形如 COM3）")
    return text


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off", ""):
            return False
    return bool(value)


logger = logging.getLogger("LineRuntime")
if not logger.handlers:
    if os.environ.get("ATEQ_DISABLE_HARDWARE") == "1":
        logger.addHandler(logging.NullHandler())
    else:
        try:
            logger.addHandler(logging.FileHandler(ROOT / "line_runtime.log", encoding="utf-8"))
        except Exception:
            logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.INFO)


def _settings_path() -> Path:
    return Path(os.environ.get("ATEQ_LINE_SETTINGS", str(ROOT / "line_settings.json")))


def _read_settings_unlocked() -> dict[str, Any]:
    path = _settings_path()
    if not path.exists():
        return dict(DEFAULT_SETTINGS)

    try:
        with path.open("r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as exc:
        logger.warning("Failed to read settings, using defaults: %s", exc)
        return dict(DEFAULT_SETTINGS)

    settings = dict(DEFAULT_SETTINGS)
    for key in BOOL_KEYS:
        if key in raw:
            settings[key] = _coerce_bool(raw[key])
    for key in PORT_KEYS:
        if key in raw:
            try:
                settings[key] = normalize_port(raw[key])
            except SettingsError:
                settings[key] = ""
    return settings


def _write_settings_unlocked(settings: dict[str, Any]) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def get_line_settings() -> dict[str, Any]:
    with _LOCK:
        return _read_settings_unlocked()


def update_line_settings(
    scan_required: bool | None = None,
    printer_enabled: bool | None = None,
    scan_start: bool | None = None,
    scanner_enabled: bool | None = None,
    scanner_port: str | None = None,
    instrument_port: str | None = None,
) -> dict[str, Any]:
    """Validate then atomically persist all-or-nothing.

    Raises SettingsError (subclass of ValueError) on malformed ports, duplicate
    effective ports, or contradictory scanner disable; nothing is written.
    """
    with _LOCK:
        settings = _read_settings_unlocked()

        for name, value in (
            ("scan_required", scan_required),
            ("printer_enabled", printer_enabled),
            ("scan_start", scan_start),
            ("scanner_enabled", scanner_enabled),
        ):
            if value is not None:
                settings[name] = bool(value)

        # A provided port of None/"" clears to the environment/default fallback.
        if scanner_port is not None:
            settings["scanner_port"] = normalize_port(scanner_port)
        if instrument_port is not None:
            settings["instrument_port"] = normalize_port(instrument_port)

        # A disabled scanner cannot leave a required/automatic scan behind.
        if not settings["scanner_enabled"]:
            if scan_start is True:
                raise SettingsError("扫码枪已禁用，不能开启「扫码启动」")
            if scan_required is True:
                raise SettingsError("扫码枪已禁用，不能开启「需要扫码」")
            settings["scan_start"] = False
            settings["scan_required"] = False

        # Conflict is decided on EFFECTIVE ports (persisted value or env default),
        # and only while the scanner is enabled: a disabled scanner never uses its
        # configured port, so an equal string must not block instrument settings.
        scanner_eff, instrument_eff = effective_ports(settings)
        if settings["scanner_enabled"] and scanner_eff == instrument_eff:
            raise SettingsError(
                f"仪器与扫码枪不能使用同一串口: {scanner_eff}（含默认端口回退）")

        _write_settings_unlocked(settings)
        logger.info("Updated line settings: %s", settings)
        return settings


def claim_print(record_id) -> bool:
    """Reserve a record for printing; False when another job is in flight."""
    if record_id is None:
        return True
    with _LOCK:
        if record_id in _INFLIGHT:
            return False
        _INFLIGHT.add(record_id)
        return True


def release_print(record_id) -> None:
    if record_id is None:
        return
    with _LOCK:
        _INFLIGHT.discard(record_id)


def print_inflight(record_id) -> bool:
    if record_id is None:
        return False
    with _LOCK:
        return record_id in _INFLIGHT


def print_label_if_enabled(
    template_name: str,
    serial_number: str,
    product_model: str,
    qr_code: str | None = None,
    supplier_code: str = "",
    sequence_code: str | None = None,
    record_id=None,
    cancel_check=None,
) -> dict[str, Any]:
    """Submit a saved-record print. The CALLER owns the per-record claim
    (claim_print/release_print) so the claim covers save -> submission and manual
    reprints cannot race an automatic job."""
    settings = get_line_settings()
    if not settings["printer_enabled"]:
        return {"success": True, "printed": False, "disabled": True,
                "state": "disabled", "message": "打印机使用已关闭",
                "serial_number": serial_number, "template": template_name,
                "duration_ms": 0}

    if not serial_number:
        return {"success": False, "printed": False, "disabled": False,
                "state": "failed", "message": "缺少有效序列号，已阻止打印",
                "serial_number": serial_number, "template": template_name,
                "duration_ms": 0}

    if not sequence_code:
        tail = str(serial_number or "").rsplit("-", 1)[-1]
        sequence_code = tail if tail.isdigit() else str(serial_number or "")
    template_name = (template_name or "").strip()

    try:
        from bartender_print import BarTenderPrinter
        result = BarTenderPrinter().print_fixed_label_files_ex(
            product_model=product_model or "",
            supplier_code=supplier_code or "",
            sequence_code=sequence_code or "",
            template_path=template_name or "",
            cancel_check=cancel_check,
        )
    except Exception as exc:
        logger.exception("Print failed")
        return {"success": False, "printed": False, "disabled": False,
                "state": "failed", "message": str(exc),
                "serial_number": serial_number, "template": template_name,
                "duration_ms": 0}

    return {
        "success": bool(result.get("success")),
        "printed": bool(result.get("printed")),
        "disabled": False,
        "state": result.get("state", "unknown"),
        "message": result.get("message", ""),
        "serial_number": serial_number,
        "template": template_name,
        "duration_ms": result.get("duration_ms"),
        "paper_confirmed": False,
    }


if __name__ == "__main__":
    print(json.dumps(get_line_settings(), ensure_ascii=False))
