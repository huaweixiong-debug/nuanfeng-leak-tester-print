#!/usr/bin/env python3
"""
BarTender 标签打印模块
通过调用 BarTender 命令行工具打印标签

Plan refs: F10, PLAN_FINAL amendments 10.
- one module print lock covering data files + BarTender process read
- injectable runner and injectable paths for offline tests
- finite subprocess timeout; return code 0 means "submitted", not "paper printed"
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Optional, Dict, Any

if os.environ.get('ATEQ_DISABLE_HARDWARE') == '1':
    logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()])
else:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('bartender_print.log', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
logger = logging.getLogger('BarTenderPrint')

_PRINT_LOCK = threading.RLock()
_RUNNER = None
_PRINTER_ENUMERATOR = None

# Frozen port identifier for the line printer (Zebra ZT231 on USB001). The queue
# name is resolved from the Windows spooler per submission; the default printer is
# never used as a fallback.
PRINTER_PORT = "USB001"


def set_runner(runner) -> None:
    """Test hook: runner.run(cmd, **kwargs) replaces subprocess.run."""
    global _RUNNER
    _RUNNER = runner


def get_runner():
    return _RUNNER


def set_printer_enumerator(enumerator) -> None:
    """Test hook: enumerator() -> [{"name": ..., "port": ...}, ...]."""
    global _PRINTER_ENUMERATOR
    _PRINTER_ENUMERATOR = enumerator


def get_printer_enumerator():
    return _PRINTER_ENUMERATOR


def _enumerate_printers_os() -> list[dict]:
    """Windows spooler enumeration (name + port) via stdlib ctypes; [] on failure."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)

        class PRINTER_INFO_2(ctypes.Structure):
            _fields_ = [
                ("pServerName", wintypes.LPWSTR),
                ("pPrinterName", wintypes.LPWSTR),
                ("pShareName", wintypes.LPWSTR),
                ("pPortName", wintypes.LPWSTR),
                ("pDriverName", wintypes.LPWSTR),
                ("pComment", wintypes.LPWSTR),
                ("pLocation", wintypes.LPWSTR),
                ("pDevMode", ctypes.c_void_p),
                ("pSepFile", wintypes.LPWSTR),
                ("pPrintProcessor", wintypes.LPWSTR),
                ("pDatatype", wintypes.LPWSTR),
                ("pParameters", wintypes.LPWSTR),
                ("pSecurityDescriptor", ctypes.c_void_p),
                ("Attributes", wintypes.DWORD),
                ("Priority", wintypes.DWORD),
                ("DefaultPriority", wintypes.DWORD),
                ("StartTime", wintypes.DWORD),
                ("UntilTime", wintypes.DWORD),
                ("Status", wintypes.DWORD),
                ("cJobs", wintypes.DWORD),
                ("AveragePPM", wintypes.DWORD),
            ]

        winspool.EnumPrintersW.restype = wintypes.BOOL
        winspool.EnumPrintersW.argtypes = [
            wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]

        printer_enum_local = 0x00000002
        needed = wintypes.DWORD(0)
        returned = wintypes.DWORD(0)
        winspool.EnumPrintersW(printer_enum_local, None, 2, None, 0,
                               ctypes.byref(needed), ctypes.byref(returned))
        if needed.value == 0:
            return []
        buffer = ctypes.create_string_buffer(needed.value)
        ok = winspool.EnumPrintersW(printer_enum_local, None, 2, buffer,
                                    needed.value, ctypes.byref(needed),
                                    ctypes.byref(returned))
        if not ok:
            return []
        entries = ctypes.cast(buffer, ctypes.POINTER(PRINTER_INFO_2))
        printers = []
        for index in range(returned.value):
            info = entries[index]
            name = str(info.pPrinterName or "").strip()
            port = str(info.pPortName or "").strip()
            if name:
                printers.append({"name": name, "port": port})
        return printers
    except Exception as exc:
        logger.warning("Printer enumeration failed: %s", exc)
        return []


def enumerate_windows_printers() -> list[dict]:
    """Queue list ({name, port}) with an explicit test/offline override."""
    enumerator = get_printer_enumerator()
    if enumerator is not None:
        try:
            return list(enumerator() or [])
        except Exception as exc:
            logger.warning("Injected printer enumerator failed: %s", exc)
            return []
    if os.environ.get("ATEQ_DISABLE_HARDWARE") == "1":
        # Deterministic offline stub: disabled-hardware/test mode never touches the
        # spooler. Tests that need absent/ambiguous queues inject an enumerator.
        return [{"name": "Zebra ZT231", "port": "USB001"}]
    return _enumerate_printers_os()


def resolve_printer_by_port(port_name: str = PRINTER_PORT) -> dict:
    """Resolve exactly one Windows queue whose port matches (e.g. USB001).

    Never falls back to the default printer: zero matches -> not_found, more than
    one -> ambiguous. Both are hard failures for the caller.
    """
    port = str(port_name or "").strip().upper()
    printers = enumerate_windows_printers()
    matches = [p for p in printers
               if str(p.get("port") or "").strip().upper() == port]
    if len(matches) == 1:
        return {"ok": True, "name": str(matches[0].get("name") or ""), "port": port,
                "reason": "", "matches": matches}
    if not matches:
        return {"ok": False, "name": "", "port": port, "reason": "not_found",
                "matches": []}
    return {"ok": False, "name": "", "port": port, "reason": "ambiguous",
            "matches": matches}


def _timeout_seconds() -> float:
    try:
        return float(os.environ.get('BARTRENDER_TIMEOUT_SECONDS', '60'))
    except Exception:
        return 60.0


def _default_exe() -> str:
    return os.environ.get(
        'BARTENDER_EXE', r"C:\Program Files\Seagull\BarTender Suite\bartend.exe")


def _default_data_dir() -> Path:
    return Path(os.environ.get('ATEQ_LABEL_DIR', r"D:\data"))


def _default_template() -> Path:
    return Path(os.environ.get('ATEQ_DEFAULT_TEMPLATE', str(_default_data_dir() / "label.btw")))


def _default_template_folder() -> Path:
    return Path(os.environ.get(
        'ATEQ_LABEL_TEMPLATE_DIR', str(Path(__file__).parent / "templates")))


class BarTenderPrinter:
    """BarTender 标签打印机类"""

    BARTENDER_EXE = r"C:\Program Files\Seagull\BarTender Suite\bartend.exe"
    FIXED_DATA_DIR = Path(r"D:\data")
    FIXED_TEMPLATE = Path(r"D:\data\label.btw")

    def __init__(self, template_folder: Optional[str] = None):
        self.exe_path = os.environ.get('BARTENDER_EXE', self.BARTENDER_EXE)
        self.data_dir = Path(os.environ.get('ATEQ_LABEL_DIR', str(self.FIXED_DATA_DIR)))
        self.default_template = Path(os.environ.get(
            'ATEQ_DEFAULT_TEMPLATE', str(self.data_dir / "label.btw")))
        if template_folder is None:
            self.template_folder = _default_template_folder()
        else:
            self.template_folder = Path(template_folder)

        logger.info(f"初始化 BarTender 打印机，模板文件夹: {self.template_folder}")

    def _get_template_path(self, template_name: str) -> str:
        if template_name.endswith('.btw') or template_name.endswith('.BTW'):
            template_path = self.template_folder / template_name
        else:
            template_path = self.template_folder / f"{template_name}.btw"
        return str(template_path)

    def _run(self, cmd, timeout: Optional[float] = None):
        runner = get_runner()
        if runner is not None:
            return runner.run(cmd, timeout=timeout if timeout is not None else _timeout_seconds())
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=timeout if timeout is not None else _timeout_seconds(),
        )

    def print_label(self,
                    template_name: str,
                    copies: int = 1,
                    variables: Optional[Dict[str, str]] = None,
                    printer_name: Optional[str] = None,
                    wait: bool = True) -> bool:
        template_path = self._get_template_path(template_name)
        if not os.path.exists(template_path):
            logger.error("❌ 模板文件不存在: %s", template_path)
            return False

        cmd = [self.exe_path, "/F", template_path]
        if copies > 1:
            cmd.extend(["/C", str(copies)])
        if printer_name:
            # BarTender selects the printer with /PRN=<queue>; /P is the print action.
            cmd.append(f"/PRN={printer_name}")
        if variables:
            for var_name, var_value in variables.items():
                cmd.extend(["/D", f"{var_name}={var_value}"])
        cmd.append("/X")
        if wait:
            cmd.append("/W")

        logger.info("📄 准备打印标签: %s", template_name)
        try:
            result = self._run(cmd)
            if result.returncode == 0:
                logger.info("✅ 标签打印命令已提交: %s", template_name)
                return True
            logger.error("❌ 标签打印失败，返回码: %s", result.returncode)
            return False
        except subprocess.TimeoutExpired:
            logger.error("❌ 打印超时: %s", template_name)
            return False
        except Exception as e:
            logger.error("❌ 打印异常: %s", e)
            return False

    def print_with_data(self,
                        template_name: str,
                        serial_number: str,
                        product_model: str,
                        qr_code: Optional[str] = None,
                        date: Optional[str] = None,
                        copies: int = 1) -> bool:
        from datetime import datetime
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        if qr_code is None:
            qr_code = serial_number
        variables = {
            "SerialNumber": serial_number,
            "ProductModel": product_model,
            "QRCode": qr_code,
            "Date": date
        }
        return self.print_label(template_name, copies=copies, variables=variables)

    def print_fixed_label_files_ex(
        self,
        product_model: str,
        supplier_code: str,
        sequence_code: str,
        template_path: str | None = None,
        wait: bool = True,
        timeout: Optional[float] = None,
        cancel_check=None,
    ) -> dict:
        """Write D:\\data files and run BarTender under one lock; returns rich state.

        cancel_check is re-evaluated AFTER the shared print lock is held and
        immediately before any file/process submission, so a stop that arrives while
        waiting for the lock still cancels a not-yet-submitted job.
        """
        started = time.monotonic()
        with _PRINT_LOCK:
            if cancel_check is not None and cancel_check():
                return {"success": False, "printed": False, "state": "cancelled",
                        "message": "停止请求已受理，未提交打印",
                        "duration_ms": int((time.monotonic() - started) * 1000)}
            template_text = str(template_path or "").strip()
            if template_text:
                selected_template = Path(template_text)
                if not selected_template.is_absolute():
                    if selected_template.suffix.lower() == ".btw":
                        selected_template = self.template_folder / selected_template
                    else:
                        selected_template = self.template_folder / f"{template_text}.btw"
            else:
                selected_template = self.default_template
            if not selected_template.exists():
                logger.error("BarTender template does not exist: %s", selected_template)
                return {"success": False, "printed": False, "state": "failed",
                        "message": f"模板不存在: {selected_template}",
                        "duration_ms": int((time.monotonic() - started) * 1000)}

            resolution = resolve_printer_by_port(PRINTER_PORT)
            if not resolution["ok"]:
                if resolution["reason"] == "not_found":
                    message = (f"未找到使用端口 {PRINTER_PORT} 的打印机，已阻止打印"
                               f"（不会回落到默认打印机）")
                else:
                    names = ", ".join(str(p.get("name") or "")
                                      for p in resolution["matches"])
                    message = (f"端口 {PRINTER_PORT} 匹配到多台打印机，无法确定队列: {names}")
                logger.error("Printer resolution failed: %s", message)
                return {"success": False, "printed": False, "state": "failed",
                        "message": message,
                        "duration_ms": int((time.monotonic() - started) * 1000)}

            sequence_code = str(sequence_code or "").strip()
            if sequence_code.isdigit():
                sequence_code = f"{int(sequence_code):05d}"

            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                return {"success": False, "printed": False, "state": "failed",
                        "message": f"标签数据目录不可用: {exc}",
                        "duration_ms": int((time.monotonic() - started) * 1000)}

            if cancel_check is not None and cancel_check():
                return {"success": False, "printed": False, "state": "cancelled",
                        "message": "停止请求已受理，未提交打印",
                        "duration_ms": int((time.monotonic() - started) * 1000)}
            files = {
                "产品型号.txt": product_model or "",
                "供应商代码.txt": supplier_code or "",
                "序号.txt": sequence_code,
            }
            encoding = os.environ.get("LABEL_DATA_ENCODING", "gbk")
            try:
                for filename, value in files.items():
                    (self.data_dir / filename).write_text(
                        str(value), encoding=encoding, errors="replace")
            except Exception as exc:
                return {"success": False, "printed": False, "state": "failed",
                        "message": f"标签数据写入失败: {exc}",
                        "duration_ms": int((time.monotonic() - started) * 1000)}

            if cancel_check is not None and cancel_check():
                return {"success": False, "printed": False, "state": "cancelled",
                        "message": "停止请求已受理，未提交打印",
                        "duration_ms": int((time.monotonic() - started) * 1000)}
            cmd = [self.exe_path, str(selected_template),
                   f"/PRN={resolution['name']}", "/P", "/X"]
            logger.info(
                "Fixed label data: product_model=%s, supplier_code=%s, sequence=%s, template=%s",
                product_model, supplier_code, sequence_code, selected_template)
            try:
                result = self._run(cmd, timeout=timeout)
                if getattr(result, 'returncode', 1) == 0:
                    return {"success": True, "printed": True, "state": "submitted",
                            "message": "BarTender 命令已完成（不代表纸张已出）",
                            "duration_ms": int((time.monotonic() - started) * 1000)}
                logger.error("Fixed BarTender print failed, return code: %s",
                             getattr(result, 'returncode', None))
                return {"success": False, "printed": False, "state": "failed",
                        "message": f"BarTender 返回码 {getattr(result, 'returncode', None)}",
                        "duration_ms": int((time.monotonic() - started) * 1000)}
            except subprocess.TimeoutExpired:
                logger.error("Fixed BarTender print timed out")
                return {"success": False, "printed": False, "state": "timeout",
                        "message": f"BarTender 超时({timeout or _timeout_seconds()}s)",
                        "duration_ms": int((time.monotonic() - started) * 1000)}
            except Exception as e:
                logger.error("Fixed BarTender print exception: %s", e)
                return {"success": False, "printed": False, "state": "failed",
                        "message": str(e),
                        "duration_ms": int((time.monotonic() - started) * 1000)}

    def print_fixed_label_files(
        self,
        product_model: str,
        supplier_code: str,
        sequence_code: str,
        template_path: str | None = None,
        wait: bool = True,
    ) -> bool:
        result = self.print_fixed_label_files_ex(
            product_model=product_model,
            supplier_code=supplier_code,
            sequence_code=sequence_code,
            template_path=template_path,
            wait=wait,
        )
        return bool(result.get("success") and result.get("printed"))

    def preview_label(self, template_name: str) -> bool:
        template_path = self._get_template_path(template_name)
        if not os.path.exists(template_path):
            logger.error("❌ 模板文件不存在: %s", template_path)
            return False
        cmd = [self.exe_path, "/F", template_path]
        try:
            subprocess.Popen(cmd)
            logger.info("👁️ 已打开标签预览: %s", template_name)
            return True
        except Exception as e:
            logger.error("❌ 打开预览失败: %s", e)
            return False


def test_print():
    """测试打印功能"""
    print("=" * 60)
    print("BarTender 标签打印测试")
    print("=" * 60)
    printer = BarTenderPrinter()
    template_name = "test_label"
    print(f"\n📄 测试打印标签: {template_name}")
    success = printer.print_with_data(
        template_name=template_name,
        serial_number="20260322-MODEL001-0001",
        product_model="MODEL001",
        qr_code="20260322-MODEL001-0001",
        copies=1
    )
    if success:
        print("\n✅ 打印测试成功")
    else:
        print("\n❌ 打印测试失败")
    print("\n" + "=" * 60)


if __name__ == '__main__':
    test_print()
