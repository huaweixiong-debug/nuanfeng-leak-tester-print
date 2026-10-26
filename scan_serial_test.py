#!/usr/bin/env python3
"""Standalone COM scanner test for serial QR/barcode scanners."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import time

import serial


ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "scan_serial_test.log"

PORT = os.environ.get("SCAN_SERIAL_PORT", "COM4")
BAUDRATE = int(os.environ.get("SCAN_SERIAL_BAUDRATE", "9600"))
BYTESIZE = int(os.environ.get("SCAN_SERIAL_BYTESIZE", "8"))
PARITY_TEXT = os.environ.get("SCAN_SERIAL_PARITY", "N").upper()
STOPBITS = float(os.environ.get("SCAN_SERIAL_STOPBITS", "1"))
TIMEOUT = float(os.environ.get("SCAN_SERIAL_TIMEOUT", "0.05"))
IDLE_SECONDS = float(os.environ.get("SCAN_IDLE_SECONDS", "0.45"))


def serial_parity(value: str):
    return {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "M": serial.PARITY_MARK,
        "S": serial.PARITY_SPACE,
    }.get((value or "N").upper(), serial.PARITY_NONE)


def decode_scan(raw: bytes) -> str:
    cleaned = raw.strip(b"\x00\x02\x03\r\n\t ")
    for encoding in ("utf-8", "gbk", "latin1"):
        try:
            return cleaned.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return cleaned.decode("latin1", errors="replace").strip()


def print_scan(index: int, raw: bytes, reason: str) -> None:
    code = decode_scan(raw)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"[{timestamp}] #{index} len={len(code)} end={reason} "
        f"hex={raw.hex(' ').upper()} code={code}"
    )
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    print("Serial barcode / QR scanner test")
    print(f"Port: {PORT}, {BAUDRATE}, {BYTESIZE}{PARITY_TEXT}{STOPBITS:g}")
    print(f"Idle end: {IDLE_SECONDS:.2f}s")
    print("Close any serial debug tool first, then scan a code. Press Ctrl+C to exit.")
    print()

    try:
        ser = serial.Serial(
            port=PORT,
            baudrate=BAUDRATE,
            bytesize=BYTESIZE,
            parity=serial_parity(PARITY_TEXT),
            stopbits=STOPBITS,
            timeout=TIMEOUT,
        )
    except Exception as exc:
        print(f"Could not open {PORT}: {exc}")
        print("If your debug tool is still open, close it because only one program can use COM4.")
        return 1

    count = 0
    buffer = bytearray()
    last_byte_time = 0.0

    try:
        with ser:
            ser.reset_input_buffer()
            print(f"{PORT} opened. Waiting for scan data...")
            while True:
                chunk = ser.read(64)
                now = time.monotonic()

                if chunk:
                    print(f"RX {len(chunk)} byte(s): {chunk.hex(' ').upper()}", flush=True)
                    buffer.extend(chunk)
                    last_byte_time = now
                    if any(byte in chunk for byte in (10, 13, 9)):
                        count += 1
                        print_scan(count, bytes(buffer), "terminator")
                        buffer.clear()
                    continue

                if buffer and last_byte_time and now - last_byte_time >= IDLE_SECONDS:
                    count += 1
                    print_scan(count, bytes(buffer), "idle")
                    buffer.clear()
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
