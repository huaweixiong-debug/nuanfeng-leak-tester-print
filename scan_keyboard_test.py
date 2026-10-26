#!/usr/bin/env python3
"""Standalone keyboard-wedge scanner test.

Most barcode/QR scanners work like a USB keyboard. This tool keeps one window
focused, captures printable keys, and treats Enter, Tab, or a short idle period
as the end of one scan.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk


ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "scan_keyboard_test.log"
IDLE_MS = int(os.environ.get("SCAN_IDLE_MS", "450"))


class ScannerTestApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Barcode / QR Scanner Keyboard Test")
        self.root.geometry("760x520")
        self.root.minsize(640, 420)

        self.buffer = ""
        self.last_key_at = 0.0
        self.idle_job: str | None = None
        self.scan_count = 0

        self.current_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready. Click the input box, then scan a code.")
        self.count_var = tk.StringVar(value="Scans: 0")

        self._build_ui()
        self.root.bind_all("<KeyPress>", self._on_key_press, add="+")
        self.root.after(250, self._focus_input)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(
            container,
            text="Scanner Keyboard Test",
            font=("Segoe UI", 18, "bold"),
        )
        title.pack(anchor="w")

        hint = ttk.Label(
            container,
            text=(
                "This window tests whether the scanner sends keyboard input. "
                "Scan a QR/barcode while the input box is focused."
            ),
            wraplength=700,
        )
        hint.pack(anchor="w", pady=(6, 16))

        ttk.Label(container, text="Live input").pack(anchor="w")
        self.input_entry = ttk.Entry(container, textvariable=self.current_var, font=("Consolas", 18))
        self.input_entry.pack(fill=tk.X, pady=(4, 10))

        toolbar = ttk.Frame(container)
        toolbar.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(toolbar, text="Focus input", command=self._focus_input).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Finish current", command=lambda: self._finish_scan("manual")).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(toolbar, text="Clear", command=self._clear_current).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(toolbar, textvariable=self.count_var).pack(side=tk.RIGHT)

        ttk.Label(container, textvariable=self.status_var).pack(anchor="w", pady=(0, 8))

        log_frame = ttk.LabelFrame(container, text="Scan log")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(log_frame, height=12, font=("Consolas", 11), wrap=tk.NONE)
        y_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        x_scroll = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self._append_log("Scanner test started. Waiting for input.")

    def _focus_input(self) -> None:
        self.root.lift()
        self.input_entry.focus_force()
        self.input_entry.icursor(tk.END)
        self.status_var.set("Input focused. Scan now.")

    def _on_key_press(self, event: tk.Event) -> str | None:
        keysym = str(event.keysym)

        if keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R"):
            return None

        if keysym in ("Return", "KP_Enter"):
            self._finish_scan("enter")
            return "break"

        if keysym == "Tab":
            self._finish_scan("tab")
            return "break"

        if keysym == "Escape":
            self._clear_current()
            return "break"

        if keysym == "BackSpace":
            self.buffer = self.buffer[:-1]
            self.current_var.set(self.buffer)
            self._schedule_idle_finish()
            return "break"

        char = getattr(event, "char", "")
        if char and char.isprintable():
            self.buffer += char
            self.current_var.set(self.buffer)
            self.input_entry.icursor(tk.END)
            self.status_var.set(f"Receiving... length={len(self.buffer)}")
            self._schedule_idle_finish()
            return "break"

        return None

    def _schedule_idle_finish(self) -> None:
        if self.idle_job is not None:
            self.root.after_cancel(self.idle_job)
        self.idle_job = self.root.after(IDLE_MS, lambda: self._finish_scan("idle"))

    def _finish_scan(self, reason: str) -> None:
        if self.idle_job is not None:
            self.root.after_cancel(self.idle_job)
            self.idle_job = None

        code = self.buffer.strip()
        if not code:
            self.status_var.set("No scan data captured.")
            self._focus_input()
            return

        self.scan_count += 1
        self.count_var.set(f"Scans: {self.scan_count}")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"[{timestamp}] #{self.scan_count} len={len(code)} end={reason} code={code}"
        self._append_log(message)
        LOG_PATH.write_text("", encoding="utf-8") if not LOG_PATH.exists() else None
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(message + "\n")

        self.status_var.set(f"Captured scan #{self.scan_count}. length={len(code)}, end={reason}")
        self.buffer = ""
        self.current_var.set("")
        self._focus_input()

    def _clear_current(self) -> None:
        if self.idle_job is not None:
            self.root.after_cancel(self.idle_job)
            self.idle_job = None
        self.buffer = ""
        self.current_var.set("")
        self.status_var.set("Cleared. Scan again.")
        self._focus_input()

    def _append_log(self, message: str) -> None:
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    ScannerTestApp().run()
