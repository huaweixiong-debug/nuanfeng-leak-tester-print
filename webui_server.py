from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
import contextlib
import subprocess
import os
import logging
import re
import threading
import time
from pathlib import Path

# 启动时终止之前的进程（仅在 __main__ 显式启动时执行，导入不产生副作用）
PORT = 8001
PROJECT_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("ATEQWebUI")
def stop_previous_server(port: int) -> None:
    if os.name != "nt":
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True,
                timeout=5,
            )
            pids = [p for p in result.stdout.decode().split() if p.isdigit()]
            for pid in pids:
                subprocess.run(
                    ["kill", "-9", pid],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
        except Exception:
            pass
        return

    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            timeout=5,
        )
    except Exception as exc:
        print(f"Port cleanup skipped: {exc}")
        return

    # netstat 在中文 Windows 下输出 GBK，按字节捕获并忽略无法解码的字符
    netstat_text = (result.stdout or b"").decode("utf-8", errors="ignore")
    current_pid = str(os.getpid())
    pids: set[str] = set()
    for line in netstat_text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP":
            local_addr = parts[1]
            state = parts[3].upper()
            pid = parts[4]
            if local_addr.endswith(f":{port}") and state == "LISTENING" and pid != current_pid:
                pids.add(pid)

    for pid in pids:
        subprocess.run(
            ["taskkill", "/F", "/PID", pid],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def stop_stale_project_python_processes() -> None:
    """Stop old project Python processes that may still hold the serial port."""
    if os.name != "nt":
        return

    project_path = str(PROJECT_ROOT).lower().replace("'", "''")
    script = f"""
$CurrentPid = {os.getpid()}
# venv 的 python.exe 是启动器，真实解释器是它的子进程；
# 不排除父进程会把派生自己的启动器误判为陈旧进程并杀掉。
$ParentPid = (Get-CimInstance Win32_Process -Filter "ProcessId=$CurrentPid").ParentProcessId
$ProjectPath = '{project_path}'
$Needles = @('webui_server.py', 'scan_serial_test.py', 'serial_scanner.py')
Get-CimInstance Win32_Process | ForEach-Object {{
    $proc = $_
    if ($proc.ProcessId -eq $CurrentPid) {{ return }}
    if ($proc.ProcessId -eq $ParentPid) {{ return }}
    if ($proc.Name -notmatch '^(python|pythonw)\\.exe$') {{ return }}
    $cmd = [string]$proc.CommandLine
    if (-not $cmd) {{ return }}
    $lower = $cmd.ToLowerInvariant()
    # 只按脚本名匹配。按项目路径匹配会误杀命令行里恰好带有本目录的无关
    # Python 进程（如 IDE 语言服务、自动化工具宿主）。
    $matched = $false
    foreach ($needle in $Needles) {{
        if ($lower.Contains($needle)) {{
            $matched = $true
            break
        }}
    }}
    if ($matched) {{
        try {{
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Output ("Stopped stale scanner/webui process PID=" + $proc.ProcessId)
        }} catch {{
            Write-Output ("Could not stop PID=" + $proc.ProcessId + ": " + $_.Exception.Message)
        }}
    }}
}}
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=8,
        )
        output = (result.stdout or result.stderr or "").strip()
        if output:
            print(output)
    except Exception as exc:
        print(f"serial stale process cleanup skipped: {exc}")


# 导入数据库模块（导入本模块不启动进程清理/数据库/硬件）
from database import init_database, save_test_record, get_latest_records, get_statistics_by_date, get_records_by_date, query_records, get_all_product_models
from serial_generator import generate_serial_number, get_generator

# 导入程序选择模块（惰性单例；导入不启动线程/硬件）
from program_selector import (
    get_test_executor,
    write_program,
    read_current_program,
    start_test,
    reset_device,
    is_test_complete,
)
from modbus_utils import (
    send_raw,
    send_raw_ex,
    modbus_crc,
    STATION_ID,
    WINDOWS_HOST_IP,
    TCP_PORT,
    read_holding_registers,
    transaction as modbus_transaction,
)

LINE_RUNTIME_IMPORT_ERROR = None
try:
    from line_runtime import (
        get_line_settings,
        update_line_settings,
    )
except Exception as exc:
    LINE_RUNTIME_IMPORT_ERROR = exc
    get_line_settings = None
    update_line_settings = None

SERIAL_SCANNER_IMPORT_ERROR = None
try:
    from serial_scanner import get_scanner_state, start_serial_scanner
except Exception as exc:
    SERIAL_SCANNER_IMPORT_ERROR = exc
    get_scanner_state = None
    start_serial_scanner = None


STARTUP_ERROR = None
_PLC_RUNTIME = None


def _hardware_enabled():
    return os.environ.get("ATEQ_DISABLE_HARDWARE", "0") != "1"


def _plc_enabled():
    """PLC handshake is part of hardware runtime and can be explicitly disabled."""
    return _hardware_enabled() and os.environ.get("ATEQ_PLC_ENABLED", "1") != "0"


def _startup_unavailable():
    return STARTUP_ERROR


def _apply_persisted_device_settings_at_startup():
    """Apply persisted scanner/instrument configuration before any device open.

    Without this, a saved instrument COM would be lost on service restart until
    the settings were saved again (only the API live-apply called set_instrument_port).
    """
    try:
        settings = _line_settings()
    except Exception as exc:
        logger.error("persisted device settings unavailable at startup: %s", exc)
        return
    try:
        import modbus_utils
        modbus_utils.set_instrument_port(settings.get("instrument_port") or None)
    except Exception as exc:
        logger.error("instrument port startup apply failed: %s", exc)
    try:
        scanner = _scanner_module()
        if scanner is not None:
            scanner.set_scan_port(settings.get("scanner_port") or None)
            if not settings.get("scanner_enabled", True):
                scanner.stop_serial_scanner()
                clear_events = getattr(scanner, "clear_pending_events", None)
                if clear_events is not None:
                    clear_events("scanner disabled at startup")
                else:
                    scanner.clear_binding("scanner disabled at startup")
    except Exception as exc:
        logger.error("scanner settings startup apply failed: %s", exc)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # 显式服务生命周期：数据库初始化失败时不得启动任何硬件/控制线程。
    global STARTUP_ERROR, _PLC_RUNTIME
    STARTUP_ERROR = None
    # Persisted device configuration must be effective before the first serial open.
    _apply_persisted_device_settings_at_startup()
    try:
        if _hardware_enabled() or os.environ.get("ATEQ_INIT_DB") == "1":
            init_database()
            try:
                import database
                recovered = database.recover_pending_prints()
                if recovered:
                    logger.warning("recovered %s pending print states as unknown",
                                   recovered)
            except Exception as exc:
                logger.error("print state recovery failed: %s", exc)
    except Exception as exc:
        STARTUP_ERROR = f"database init failed: {exc}"
        logger.error(STARTUP_ERROR)
    if STARTUP_ERROR is None:
        try:
            # Restore the persisted active product before any hardware cycle can be
            # admitted; the supervisor prepares program 1 on its first pass.
            if not get_test_executor().get_pending_context().get("product_model"):
                _rearm_active_product(write_program_now=False)
        except Exception as exc:
            logger.error("active product restore failed: %s", exc)
    if STARTUP_ERROR is None and _hardware_enabled():
        try:
            executor = get_test_executor()
            executor.ensure_started()
            executor.start_instrument_supervisor(start_scanner=True)
        except Exception as exc:
            logger.error("instrument lifecycle start failed: %s", exc)
        if _plc_enabled():
            try:
                from plc_runtime import PlcRuntime
                _PLC_RUNTIME = PlcRuntime(
                    on_start=executor.start_plc_cycle,
                    on_reset=lambda: executor.request_stop('plc'),
                )
                _PLC_RUNTIME.start()
                executor.set_completion_callback(_PLC_RUNTIME.enqueue_result)
            except Exception as exc:
                # PLC failure must not kill the existing ATEQ WebUI service.  The
                # monitor retries transient connection errors after a successful start.
                logger.error("PLC lifecycle start failed: %s", exc)
                _PLC_RUNTIME = None
                executor.set_completion_callback(None)
    yield
    try:
        executor = get_test_executor()
        executor.set_completion_callback(None)
        if _PLC_RUNTIME is not None:
            _PLC_RUNTIME.stop()
            _PLC_RUNTIME = None
    except Exception as exc:
        logger.error("PLC lifecycle stop failed: %s", exc)
    try:
        get_test_executor().stop_instrument_supervisor()
    except Exception:
        pass


app = FastAPI(title="ATEQ 仪器控制", lifespan=lifespan)

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ATEQ 仪器控制</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/interactjs/dist/interact.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: radial-gradient(ellipse at 10% 0%, rgba(59, 130, 246, 0.12), transparent 42%),
                        linear-gradient(145deg, #111827 0%, #172338 52%, #102b4b 100%);
            min-height: 100vh;
            font-family: 'Segoe UI', system-ui, sans-serif;
            color: #e5e7eb;
            overflow-x: hidden;
        }
        .control-card {
            position: absolute;
            touch-action: none;
            user-select: none;
            box-shadow: 0 12px 32px rgba(2, 8, 23, 0.22);
            border-color: rgba(100, 116, 139, 0.48) !important;
        }
        .control-card.dragging {
            opacity: 0.9;
        }
        .drag-handle {
            cursor: grab;
        }
        .drag-handle:active {
            cursor: grabbing;
        }
        .resize-handle {
            position: absolute;
            bottom: 0;
            right: 0;
            width: 20px;
            height: 20px;
            cursor: se-resize;
            background: linear-gradient(135deg, transparent 50%, rgba(255,255,255,0.3) 50%);
            border-radius: 0 0 16px 0;
        }
        .btn {
            transition: all 0.2s ease;
        }
        .btn:hover {
            transform: translateY(-2px);
        }
        .btn-start {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            box-shadow: 0 4px 15px rgba(16, 185, 129, 0.4);
        }
        .btn-start:hover {
            box-shadow: 0 6px 20px rgba(16, 185, 129, 0.6);
        }
        .btn-stop {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            box-shadow: 0 4px 15px rgba(239, 68, 68, 0.4);
        }
        .btn-stop:hover {
            box-shadow: 0 6px 20px rgba(239, 68, 68, 0.6);
        }
        
        /* 标签页样式 */
        .tab-btn {
            transition: all 0.2s ease;
        }
        .tab-btn:hover {
            background-color: rgba(255, 255, 255, 0.05);
        }
        .tab-btn.active {
            color: white;
            border-bottom-color: #3b82f6;
        }
        .tab-pane {
            display: none;
        }
        .tab-pane.active {
            display: block;
        }
        .settings-locked .control-card {
            opacity: 0.45;
            filter: grayscale(0.6);
        }
        #page-settings {
            position: relative;
            display: none;
            flex-direction: column;
            gap: 12px;
        }
        #page-settings.active {
            display: flex;
        }
        #page-settings .control-card {
            position: relative;
            left: auto;
            top: auto;
        }
        .settings-lock-overlay {
            display: none;
            position: absolute;
            inset: 20px;
            z-index: 60;
            align-items: center;
            justify-content: center;
            pointer-events: auto;
            background: rgba(15, 23, 42, 0.22);
        }
        .settings-locked .settings-lock-overlay {
            display: flex;
        }
        .settings-lock-panel {
            position: absolute;
            top: 24px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(30, 41, 59, 0.95);
            border: 1px solid rgba(148, 163, 184, 0.4);
            border-radius: 12px;
            padding: 14px 22px;
            text-align: center;
            box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4);
            pointer-events: none;
        }
        #settings-auth-bar {
            position: relative;
            z-index: 80;
            width: 100%;
            margin: 0;
            flex-shrink: 0;
        }
        .settings-locked #settings-auth-bar {
            position: absolute;
            left: 50%;
            top: 50%;
            transform: translate(-50%, -50%);
            margin: 0;
            width: min(860px, calc(100vw - 48px));
            cursor: grab;
            box-shadow: 0 18px 40px rgba(0, 0, 0, 0.38);
        }
        #settings-auth-bar.dragging {
            cursor: grabbing;
        }
        #records-display-table,
        #query-results-table {
            width: 100%;
            table-layout: fixed;
        }
        #records-display-table th,
        #records-display-table td,
        #query-results-table th,
        #query-results-table td {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .records-header-cell {
            position: relative;
            user-select: none;
            padding-right: 10px;
        }
        .record-col-resizer {
            position: absolute;
            top: 0;
            right: -3px;
            width: 8px;
            height: 100%;
            cursor: col-resize;
            touch-action: none;
            z-index: 2;
        }
        .record-col-resizer::after {
            content: "";
            position: absolute;
            top: 20%;
            bottom: 20%;
            left: 50%;
            width: 1px;
            background: rgba(148, 163, 184, 0.7);
            transform: translateX(-50%);
            opacity: 0;
            transition: opacity 0.15s ease;
        }
        .records-header-cell:hover .record-col-resizer::after,
        body.records-col-resizing .record-col-resizer::after {
            opacity: 1;
        }
        body.records-col-resizing {
            cursor: col-resize;
            user-select: none;
        }
        /* ================= 测试页布局 =================
           左栏：产品 → 启动/停止（1366x768 首屏可见） → 人员/二维码；
           右侧：结果横幅（最醒目） → 实时数值 + 程序结果 → 曲线；底部：测试记录。 */
        #page-test {
            width: 100% !important;
            height: auto !important;
            min-height: calc(100vh - 56px);
            overflow: visible !important;
        }
        #canvas {
            display: grid;
            grid-template-columns: 340px minmax(0, 1fr) minmax(0, 1.25fr);
            grid-template-areas:
                "primary hero    hero"
                "primary monitor status"
                "primary charts  charts"
                "records records records";
            grid-template-rows: auto auto auto auto;
            align-items: stretch;
            gap: 14px;
            padding: 14px clamp(12px, 1.5vw, 24px) 20px;
            height: auto !important;
            overflow: visible !important;
        }
        .dashboard-column {
            display: flex;
            flex-direction: column;
            align-self: stretch;
            gap: 14px;
            min-width: 0;
        }
        #dashboard-primary-column { grid-area: primary; }
        #dashboard-hero-column { grid-area: hero; }
        #dashboard-monitor-column { grid-area: monitor; }
        #dashboard-status-column { grid-area: status; }
        #dashboard-chart-column { grid-area: charts; }
        #record-card { grid-area: records; }
        #product-select-card { order: 1; }
        #control-card { order: 2; }
        #operator-scan-card { order: 3; flex: 1 1 auto; }
        /* 测试状态已由结果横幅显示，产品卡片里不再重复 */
        #test-status-container { display: none !important; }
        #monitor-card, #status-lights-card, #chart-card { flex: 1 1 auto; }
        /* Ignore saved drag/resize coordinates on the dashboard; the grid owns the cards. */
        #canvas .control-card {
            position: relative;
            inset: auto !important;
            width: 100% !important;
            height: auto !important;
            min-width: 0;
            min-height: 0 !important;
            transform: none !important;
            touch-action: auto;
            user-select: auto;
            display: flex;
            flex-direction: column;
        }
        #canvas .drag-handle {
            cursor: default;
            background: rgba(30, 41, 59, 0.7);
            border-color: rgba(100, 116, 139, 0.3);
            padding: 10px 16px;
        }
        #canvas .drag-handle h1 { font-size: 0.95rem; letter-spacing: 0.02em; }
        #canvas .resize-handle { display: none; }
        #canvas input, #canvas select { min-width: 0; }

        /* 左栏 */
        #product-selector {
            font-size: 1.05rem;
            font-weight: 600;
            padding: 10px 12px;
            background: #1e293b;
            border-color: #475569;
        }
        #control-card > .p-4.space-y-3 {
            display: flex;
            flex: 1 1 auto;
            flex-direction: column;
            gap: 12px;
        }
        #control-card > .p-4.space-y-3 > * + * { margin-top: 0; }
        #btn-start, #btn-stop {
            min-height: 60px;
            font-size: 1.25rem;
            letter-spacing: 0.1em;
        }
        #operator-scan-card > .p-4.space-y-3 > * + * { margin-top: 12px; }
        #qr-input { font-family: Consolas, 'Courier New', monospace; }
        #scanner-status { overflow-wrap: anywhere; min-height: 1.25rem; }

        /* 结果横幅 */
        .result-hero {
            --hero-bg: rgba(30, 41, 59, 0.85);
            --hero-accent: #64748b;
            --hero-text: #cbd5e1;
            background: var(--hero-bg);
            border-color: var(--hero-accent) !important;
            border-left: 10px solid var(--hero-accent) !important;
            transition: background 0.3s ease, border-color 0.3s ease;
        }
        .result-hero[data-state="running"] { --hero-bg: rgba(120, 83, 9, 0.35); --hero-accent: #f59e0b; --hero-text: #fcd34d; }
        .result-hero[data-state="pass"] { --hero-bg: rgba(6, 95, 70, 0.55); --hero-accent: #10b981; --hero-text: #6ee7b7; }
        .result-hero[data-state="fail"] { --hero-bg: rgba(127, 29, 29, 0.6); --hero-accent: #ef4444; --hero-text: #fca5a5; }
        .result-hero[data-state="abnormal"] { --hero-bg: rgba(124, 45, 18, 0.5); --hero-accent: #f97316; --hero-text: #fdba74; }
        .result-hero-inner {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 24px;
            padding: 18px 28px;
            min-height: 150px;
        }
        .result-hero-main { min-width: 0; }
        .hero-result {
            font-size: clamp(3rem, 5vw, 4.5rem);
            font-weight: 800;
            line-height: 1;
            letter-spacing: 0.08em;
            color: var(--hero-text);
        }
        .result-hero[data-state="running"] .hero-result { animation: hero-pulse 1.4s ease-in-out infinite; }
        @keyframes hero-pulse { 50% { opacity: 0.55; } }
        .hero-detail {
            margin-top: 10px;
            font-size: 1.05rem;
            color: #e2e8f0;
            overflow-wrap: anywhere;
        }
        .hero-meta {
            display: grid;
            grid-template-columns: auto minmax(0, 1fr);
            gap: 8px 16px;
            min-width: 260px;
            font-size: 0.95rem;
        }
        .hero-meta-item { display: contents; }
        .hero-meta-label { color: #94a3b8; white-space: nowrap; }
        .hero-meta-value { color: #f1f5f9; font-weight: 600; overflow-wrap: anywhere; }
        .hero-meta-value.mono { font-family: Consolas, 'Courier New', monospace; }
        .hero-meta-value.comm-bad { color: #fca5a5; }
        .hero-meta-value.comm-ok { color: #86efac; }

        /* 实时数值：大号数字 */
        #monitor-card > .p-4.space-y-4 {
            display: flex;
            flex: 1 1 auto;
            flex-direction: column;
            justify-content: center;
            gap: 12px;
        }
        #monitor-card > .p-4.space-y-4 > * + * { margin-top: 0; }
        #pressure-value, #leak-value {
            font-size: clamp(2.2rem, 3vw, 3rem);
            font-variant-numeric: tabular-nums;
            font-family: Consolas, 'Segoe UI', monospace;
        }
        #pressure-unit, #leak-unit { font-size: 1rem; }

        /* 程序结果 + 状态灯 */
        #status-lights-card > .p-4.space-y-3 {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 8px;
            align-content: start;
        }
        #status-lights-card > .p-4.space-y-3 > * + * { margin-top: 0; }
        #status-lights-card > .p-4.space-y-3 > .flex { display: contents; }
        #status-lights-card > .p-4.space-y-3 > .flex > div { padding: 6px 8px; border-radius: 10px; }
        #status-lights-card > .p-4.space-y-3 > .flex > div [id^="light-"] { width: 14px; height: 14px; flex: 0 0 14px; }
        #status-lights-card > .p-4.space-y-3 > .space-y-2 {
            grid-column: 1 / -1;
            order: -1;
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 4px;
        }
        #status-lights-card > .p-4.space-y-3 > .space-y-2 > * + * { margin-top: 0; }
        #status-lights-card .grid-cols-3 {
            display: grid;
            grid-template-columns: auto minmax(0, 1fr) auto;
            gap: 6px 8px;
            align-items: baseline;
        }
        #status-lights-card .grid-cols-3 > div { display: contents; white-space: nowrap; }
        #program1-pressure, #program1-leak, #program2-pressure, #program2-leak,
        #program1-result, #program2-result {
            font-size: 1.15rem;
            font-variant-numeric: tabular-nums;
        }

        /* 曲线并排 */
        #chart-card > .drag-handle > div { flex-wrap: wrap; gap: 8px; }
        #chart-card > .p-4.space-y-4 {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
        }
        #chart-card > .p-4.space-y-4 > * + * { margin-top: 0; }
        #chart-card > .p-4.space-y-4 > div > div[style*="height"] { height: 200px !important; }

        #records-display-table { min-width: 1000px; }

        @media (max-width: 1279px) {
            #canvas {
                grid-template-columns: 300px minmax(0, 1fr);
                grid-template-areas:
                    "primary hero"
                    "primary monitor"
                    "primary status"
                    "charts  charts"
                    "records records";
            }
        }
        @media (max-width: 899px) {
            #canvas {
                grid-template-columns: minmax(0, 1fr);
                grid-template-areas: "hero" "primary" "monitor" "status" "charts" "records";
                gap: 16px;
                padding: 16px 12px;
            }
            .result-hero-inner { flex-direction: column; align-items: flex-start; }
            .hero-meta { min-width: 0; width: 100%; }
            #chart-card > .p-4.space-y-4 { grid-template-columns: minmax(0, 1fr); }
            #status-lights-card > .p-4.space-y-3 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            #status-lights-card > .p-4.space-y-3 > .space-y-2 { grid-template-columns: minmax(0, 1fr); }
            .tab-navigation .tab-btn,
            #btn-reset-layout {
                padding-left: 12px;
                padding-right: 12px;
                white-space: nowrap;
            }
            #record-card > .drag-handle > div {
                flex-wrap: wrap;
                gap: 8px;
            }
        }
        /* ======== 满屏无滚动（1600x900 工位屏，宽度 >= 1280 时生效） ========
           测试页固定一屏高：左栏贯穿全高；右侧 横幅 → 实时/程序结果 → 曲线（吃掉剩余高度）→ 最新记录。
           记录只显示放得下的最新几条，完整历史在「全量查询」。 */
        @media (min-width: 1280px) {
            body:has(#page-test.active) { overflow: hidden; }
            #page-test {
                height: calc(100vh - 60px) !important;
                min-height: 0;
                overflow: hidden !important;
            }
            #canvas {
                height: 100% !important;
                overflow: hidden !important;
                grid-template-columns: 320px minmax(0, 1fr) minmax(0, 1.3fr);
                grid-template-rows: auto auto minmax(0, 1fr) minmax(150px, 1fr);
                grid-template-areas:
                    "primary hero    hero"
                    "primary monitor status"
                    "primary charts  charts"
                    "primary records records";
                gap: 10px;
                padding: 8px 14px 10px;
            }
            .dashboard-column { gap: 10px; min-height: 0; }
            #dashboard-primary-column { overflow: hidden; }
            #dashboard-chart-column, #record-card { min-height: 0; overflow: hidden; }
            #canvas .drag-handle { padding: 7px 14px; }
            #canvas .control-card > .p-4 { padding: 10px 12px; }

            /* 左栏 */
            #product-select-card > .p-4.space-y-3 > * + * { margin-top: 8px; }
            #product-selector { padding: 7px 10px; }
            #btn-start, #btn-stop { min-height: 56px; }
            #control-card > .p-4.space-y-3 { gap: 10px; }

            /* 横幅 */
            .result-hero-inner { min-height: 104px; padding: 10px 24px; }
            .hero-result { font-size: 3.6rem; }
            .hero-detail { margin-top: 6px; font-size: 1rem; }
            .hero-meta { gap: 4px 16px; }

            /* 实时数据：压力 | 泄漏 并排 */
            #monitor-card > .p-4.space-y-4 {
                display: grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 10px;
                align-content: center;
            }
            #monitor-card > .p-4.space-y-4 > .text-center { grid-column: 1 / -1; }
            #monitor-card > .p-4.space-y-4 > [class~="bg-gray-700/30"] { padding: 10px 12px; }
            #pressure-value, #leak-value { font-size: 2.4rem; }

            /* 程序结果 + 指示灯（一行 6 个，只留灯和名称） */
            #status-lights-card > .p-4.space-y-3 { grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 6px; }
            #status-lights-card > .p-4.space-y-3 > .space-y-2 { gap: 8px; margin-bottom: 2px; }
            #status-lights-card > .p-4.space-y-3 > .space-y-2 > div { padding: 8px 10px; }
            #status-lights-card > .p-4.space-y-3 > .space-y-2 .mb-2 { margin-bottom: 4px; }
            #status-lights-card > .p-4.space-y-3 > .flex > div { padding: 5px 6px; gap: 6px; }
            #status-lights-card > .p-4.space-y-3 > .flex [id^="status-"] { display: none; }
            #status-lights-card > .p-4.space-y-3 > .flex .text-xs.font-medium {
                font-size: 11px;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            /* 曲线：填满剩余高度 */
            #chart-card > .p-4.space-y-4 {
                flex: 1 1 auto;
                min-height: 0;
                padding: 8px 12px;
            }
            #chart-card > .p-4.space-y-4 > div {
                display: flex;
                flex-direction: column;
                min-height: 0;
                padding: 8px 10px;
            }
            #chart-card > .p-4.space-y-4 > div > .mb-2 { margin-bottom: 4px; }
            #chart-card > .p-4.space-y-4 > div > div[style*="height"] {
                flex: 1 1 auto;
                height: auto !important;
                min-height: 0;
                position: relative;
            }

            /* 最新记录：填满本行，不出滚动条 */
            #record-card > .p-4 {
                flex: 1 1 auto;
                min-height: 0;
                padding: 6px 12px 8px;
                display: flex;
                flex-direction: column;
            }
            #record-card > .p-4 > .overflow-auto {
                flex: 1 1 auto;
                min-height: 0;
                max-height: none !important;
                overflow: hidden !important;
            }
            #records-display-table { width: 100% !important; min-width: 0; font-size: 12px !important; }
            #records-table-colgroup col:nth-child(1)  { width: 5%  !important; }
            #records-table-colgroup col:nth-child(2)  { width: 13% !important; }
            #records-table-colgroup col:nth-child(3)  { width: 13% !important; }
            #records-table-colgroup col:nth-child(4)  { width: 15% !important; }
            #records-table-colgroup col:nth-child(5)  { width: 8%  !important; }
            #records-table-colgroup col:nth-child(6)  { width: 8%  !important; }
            #records-table-colgroup col:nth-child(7)  { width: 8%  !important; }
            #records-table-colgroup col:nth-child(8)  { width: 8%  !important; }
            #records-table-colgroup col:nth-child(9)  { width: 7%  !important; }
            #records-table-colgroup col:nth-child(10) { width: 8%  !important; }
            #records-table-colgroup col:nth-child(11) { width: 7%  !important; }
            #record-card .record-col-resizer { display: none; }
        }
        /* 矮屏（Edge 未全屏、有标题栏/任务栏时可视高度约 760）：压缩次要信息，保证不滚动 */
        @media (min-width: 1280px) and (max-height: 820px) {
            #program-config { display: none !important; }
            #canvas { gap: 8px; padding: 6px 12px 8px; }
            .dashboard-column { gap: 8px; }
            #canvas .drag-handle { padding: 5px 12px; }
            #canvas .control-card > .p-4 { padding: 8px 10px; }
            .result-hero-inner { min-height: 84px; padding: 6px 20px; }
            .hero-result { font-size: 2.8rem; }
            .hero-detail { margin-top: 4px; font-size: 0.95rem; }
            #btn-start, #btn-stop { min-height: 48px; }
            #monitor-card > .p-4.space-y-4 > [class~="bg-gray-700/30"] { padding: 6px 10px; }
            #pressure-value, #leak-value { font-size: 2rem; }
            #device-status { display: none; }
            #status-lights-card > .p-4.space-y-3 > .space-y-2 > div { padding: 6px 10px; }
        }
    </style>
</head>
<body>
    <!-- 标签页导航 -->
    <div class="tab-navigation fixed top-0 left-0 right-0 bg-gray-900/90 backdrop-blur z-50 border-b border-gray-700">
        <div class="max-w-full mx-auto px-4">
            <div class="flex space-x-1">
                <button id="tab-test" class="tab-btn active py-3 px-6 text-sm font-medium text-white border-b-2 border-blue-500">测试</button>

                <button id="tab-query" class="tab-btn py-3 px-6 text-sm font-medium text-gray-400 hover:text-white border-b-2 border-transparent">全量查询</button>
                <button id="tab-settings" class="tab-btn py-3 px-6 text-sm font-medium text-gray-400 hover:text-white border-b-2 border-transparent">设置</button>
                <button id="btn-reset-layout" hidden title="恢复所有卡片到默认排列位置" class="ml-auto py-3 px-6 text-sm font-medium text-gray-500 hover:text-white border-b-2 border-transparent transition-colors">重置布局</button>
            </div>
        </div>
    </div>
    
    <!-- 标签页内容 -->
    <div class="tab-content" style="padding-top: 60px; min-height: 100vh;">
        <!-- 测试页面 -->
        <div id="page-test" class="tab-pane active" style="position: relative; width: 100vw; height: calc(100vh - 60px); overflow: hidden;">
        <div id="canvas" style="position: relative; width: 100%; height: 100%; overflow: hidden;">
        <div id="dashboard-primary-column" class="dashboard-column">
        <div id="control-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 24px; top: 16px; width: 320px; height: 185px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <h1 class="text-white font-semibold">ATEQ 控制</h1>
            </div>
            
            <div class="p-4 space-y-3">
                <div class="flex gap-3">
                    <button id="btn-start" class="btn btn-start flex-1 py-3 rounded-xl text-white font-semibold flex items-center justify-center gap-2">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/>
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                        </svg>
                        启动
                    </button>
                    
                    <button id="btn-stop" class="btn btn-stop flex-1 py-3 rounded-xl text-white font-semibold flex items-center justify-center gap-2">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z"/>
                        </svg>
                        停止
                    </button>
                </div>
                
                <!-- 第二行按钮：扫码启动和打印标签 -->
                <div class="flex gap-3">
                    <button id="btn-scan-start" class="btn flex-1 py-2.5 rounded-xl text-white font-semibold flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-500 transition-colors">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v1m6 11h2m-6 0h-2v4m0-11v3m0 0h.01M12 12h4.01M16 20h4M4 12h4m12 0h.01M5 8h2a1 1 0 001-1V5a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1zm12 0h2a1 1 0 001-1V5a1 1 0 00-1-1h-2a1 1 0 00-1 1v2a1 1 0 001 1zM5 20h2a1 1 0 001-1v-2a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1z"/>
                        </svg>
                        扫码启动
                    </button>
                    
                    <button id="btn-print-label" class="btn flex-1 py-2.5 rounded-xl text-white font-semibold flex items-center justify-center gap-2 bg-teal-600 hover:bg-teal-500 transition-colors">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 17h2a2 2 0 002-2v-4a2 2 0 00-2-2H5a2 2 0 00-2 2v4a2 2 0 002 2h2m2 4h6a2 2 0 002-2v-4a2 2 0 00-2-2H9a2 2 0 00-2 2v4a2 2 0 002 2zm8-12V5a2 2 0 00-2-2H9a2 2 0 00-2 2v4h10z"/>
                        </svg>
                        打印标签
                    </button>
                </div>
            </div>
            
            <div class="resize-handle"></div>
        </div>
        
        <!-- 产品选择卡片 -->
        <div id="product-select-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 24px; top: 217px; width: 320px; min-height: 520px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <div class="flex items-center justify-between">
                    <h1 class="text-white font-semibold">产品选择</h1>
                    <div id="product-sync-status" class="text-xs text-gray-400">已同步</div>
                </div>
            </div>
            
            <div class="p-4 space-y-3">
                <!-- 产品型号选择 -->
                <div>
                    <label class="block text-sm text-gray-400 mb-1">产品型号</label>
                    <select id="product-selector" class="w-full px-3 py-2 bg-gray-600 border border-gray-500 rounded-lg text-white text-sm">
                        <option value="">-- 请选择产品 --</option>
                    </select>
                </div>

                <div class="grid grid-cols-2 gap-2 text-xs">
                    <label class="flex items-center justify-between gap-2 bg-gray-700/30 rounded-lg px-3 py-2 text-gray-300">
                        <span>需要扫码</span>
                        <input id="switch-scan-required" type="checkbox" class="h-4 w-4 accent-blue-500">
                    </label>
                    <label class="flex items-center justify-between gap-2 bg-gray-700/30 rounded-lg px-3 py-2 text-gray-300">
                        <span>打印机</span>
                        <input id="switch-printer-enabled" type="checkbox" class="h-4 w-4 accent-blue-500">
                    </label>
                    <label class="flex items-center justify-between gap-2 bg-gray-700/30 rounded-lg px-3 py-2 text-gray-300" title="开启后扫码即自动通过 Modbus 启动测试；关闭时扫码仅记录，由仪器硬件按钮启动">
                        <span>扫码启动</span>
                        <input id="switch-scan-start" type="checkbox" class="h-4 w-4 accent-indigo-500">
                    </label>
                </div>

                <!-- 程序配置显示 -->
                <div id="program-config" class="bg-gray-700/30 rounded-lg p-3 hidden">
                    <div class="grid grid-cols-2 gap-2 text-xs">
                        <div>
                            <span class="text-gray-400">程序号1:</span>
                            <span id="display-program-1" class="text-white ml-1">--</span>
                        </div>
                        <div>
                            <span class="text-gray-400">程序号2:</span>
                            <span id="display-program-2" class="text-white ml-1">--</span>
                        </div>
                        <div>
                            <span class="text-gray-400">切换腔道:</span>
                            <span id="display-switch-chamber" class="text-white ml-1">--</span>
                        </div>
                        <div>
                            <span class="text-gray-400">标签模板路径:</span>
                            <span id="display-label-template" class="text-white ml-1">--</span>
                        </div>
                        <div>
                            <span class="text-gray-400">供应商:</span>
                            <span id="display-supplier-code" class="text-white ml-1">--</span>
                        </div>
                    </div>
                </div>
                
                <!-- 测试状态显示 -->
                <div id="test-status-container" class="bg-gray-700/30 rounded-lg p-2 mt-2 hidden">
                    <div class="flex items-center justify-between">
                        <span class="text-gray-400 text-xs">测试状态:</span>
                        <span id="test-status-display" class="text-xs text-gray-400">待机</span>
                    </div>
                </div>

                <!-- 加载状态 -->
                <div id="product-loading" class="text-center text-gray-500 text-xs hidden">
                    <svg class="animate-spin w-4 h-4 mx-auto mb-1" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    加载中...
                </div>
            </div>
            
            <div class="resize-handle"></div>
        </div>

        <!-- 操作人员与扫码信息（左栏底部） -->
        <div id="operator-scan-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700">
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <h1 class="text-white font-semibold">操作人员与二维码</h1>
            </div>
            <div class="p-4 space-y-3">
                <div>
                    <label class="block text-gray-400 text-xs mb-1">操作人员</label>
                    <select id="operator-input" class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm">
                        <option value="">-- 选择操作人员 --</option>
                    </select>
                </div>
                <div>
                    <label class="block text-gray-400 text-xs mb-1">二维码</label>
                    <div class="flex gap-1">
                        <input type="text" id="qr-input" class="flex-1 px-2 py-1 bg-gray-700 border border-gray-600 rounded text-gray-300 text-sm" placeholder="等待扫码..." readonly>
                        <button id="btn-clear-qr" style="display:none" class="px-2 py-1 bg-gray-500 hover:bg-gray-400 text-white text-xs rounded transition-colors" title="清空二维码">✕</button>
                    </div>
                    <div id="scanner-status" class="mt-1 text-[11px] text-gray-500 leading-snug">串口扫码: 等待连接</div>
                </div>
            </div>
        </div>
        </div>
        
        <!-- 结果横幅：本件结论最醒目 -->
        <div id="dashboard-hero-column" class="dashboard-column">
        <div id="result-hero-card" class="control-card result-hero rounded-2xl overflow-hidden border" data-state="idle">
            <div class="result-hero-inner">
                <div class="result-hero-main">
                    <div id="hero-result" class="hero-result">待机</div>
                    <div id="hero-detail" class="hero-detail">请选择产品后启动测试</div>
                </div>
                <div class="hero-meta">
                    <div class="hero-meta-item"><span class="hero-meta-label">当前产品</span><span id="hero-product" class="hero-meta-value">--</span></div>
                    <div class="hero-meta-item"><span class="hero-meta-label">序列号</span><span id="hero-serial" class="hero-meta-value mono">--</span></div>
                    <div class="hero-meta-item"><span class="hero-meta-label">仪器通讯</span><span id="hero-comm" class="hero-meta-value">--</span></div>
                </div>
            </div>
        </div>
        </div>

        <!-- 实时数据监控卡片 -->
        <div id="dashboard-monitor-column" class="dashboard-column">
        <div id="monitor-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 360px; top: 16px; width: 310px; height: 300px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <div class="flex items-center justify-between">
                    <h1 class="text-white font-semibold">实时数据</h1>
                    <div id="monitor-status" class="w-2 h-2 rounded-full bg-gray-400"></div>
                </div>
            </div>
            
            <div class="p-4 space-y-4">
                <!-- 压力显示 -->
                <div class="bg-gray-700/30 rounded-xl p-3">
                    <div class="flex items-center justify-between mb-1">
                        <span class="text-gray-400 text-sm">压力</span>
                        <svg class="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/>
                        </svg>
                    </div>
                    <div class="flex items-baseline gap-1">
                        <span id="pressure-value" class="text-2xl font-bold text-white">--</span>
                        <span id="pressure-unit" class="text-sm text-gray-400">--</span>
                    </div>
                </div>
                
                <!-- 泄漏量显示 -->
                <div class="bg-gray-700/30 rounded-xl p-3">
                    <div class="flex items-center justify-between mb-1">
                        <span class="text-gray-400 text-sm">泄漏量</span>
                        <svg class="w-4 h-4 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/>
                        </svg>
                    </div>
                    <div class="flex items-baseline gap-1">
                        <span id="leak-value" class="text-2xl font-bold text-white">--</span>
                        <span id="leak-unit" class="text-sm text-gray-400">--</span>
                    </div>
                </div>
                
                <!-- 状态显示 -->
                <div class="text-center">
                    <span id="device-status" class="text-xs text-gray-500">未连接</span>
                </div>
            </div>
            
            <div class="resize-handle"></div>
        </div>
        </div>
        
        <!-- 状态指示灯卡片 -->
        <div id="dashboard-status-column" class="dashboard-column">
        <div id="status-lights-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 1092px; top: 16px; width: 420px; min-height: 520px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <div class="flex items-center justify-between">
                    <h1 class="text-white font-semibold">程序结果与状态</h1>
                    <div class="text-xs text-gray-400">实时状态</div>
                </div>
            </div>
            
            <div class="p-4 space-y-3">
                <!-- 第一行：合格标志/循环结束 -->
                <div class="flex gap-2">
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="测试件合格，通过检测">
                        <div id="light-pass" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">合格标志</div>
                            <div id="status-pass" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="测试循环结束">
                        <div id="light-cycle-end" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">循环结束</div>
                            <div id="status-cycle-end" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                </div>
                
                <!-- 第二行：测试件不合格/参考件不合格 -->
                <div class="flex gap-2">
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="测试件检测不合格">
                        <div id="light-fail-test" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">测试件不合格</div>
                            <div id="status-fail-test" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="参考件检测不合格">
                        <div id="light-fail-ref" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">参考件不合格</div>
                            <div id="status-fail-ref" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                </div>
                
                <!-- 第三行：程序号1合格/程序号2合格 -->
                <div class="flex gap-2">
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="程序号1测试合格">
                        <div id="light-program1-pass" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">程序号1合格</div>
                            <div id="status-program1-pass" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                    <div class="flex-1 flex items-center gap-2 bg-gray-700/30 rounded-xl p-2 group cursor-pointer" 
                         title="程序号2测试合格">
                        <div id="light-program2-pass" class="w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg"></div>
                        <div class="flex-1">
                            <div class="text-white text-xs font-medium">程序号2合格</div>
                            <div id="status-program2-pass" class="text-gray-500 text-xs">未激活</div>
                        </div>
                    </div>
                </div>
                
                <!-- 第四行：程序号1结果/程序号2结果 -->
                <div class="space-y-2">
                    <div class="bg-gray-700/30 rounded-xl p-3">
                        <div class="flex items-center gap-2 mb-2">
                            <span class="text-white text-sm font-medium">程序号1结果</span>
                            <span class="px-1.5 py-0.5 rounded bg-gray-600/70 text-gray-300 text-[10px]">ATEQ程序号 <span id="program1-ateq-number" class="text-cyan-300 font-semibold">--</span></span>
                        </div>
                        <div class="grid grid-cols-3 gap-2 text-xs">
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">压力:</span>
                                <span id="program1-pressure" class="text-green-400 font-medium">--</span>
                                <span id="program1-pressure-unit" class="text-gray-500">--</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">泄漏量:</span>
                                <span id="program1-leak" class="text-blue-400 font-medium">--</span>
                                <span id="program1-leak-unit" class="text-gray-500">--</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">Result:</span>
                                <span id="program1-result" class="text-yellow-400 font-medium">--</span>
                            </div>
                        </div>
                    </div>
                    <div class="bg-gray-700/30 rounded-xl p-3">
                        <div class="flex items-center gap-2 mb-2">
                            <span class="text-white text-sm font-medium">程序号2结果</span>
                            <span class="px-1.5 py-0.5 rounded bg-gray-600/70 text-gray-300 text-[10px]">ATEQ程序号 <span id="program2-ateq-number" class="text-cyan-300 font-semibold">--</span></span>
                        </div>
                        <div class="grid grid-cols-3 gap-2 text-xs">
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">压力:</span>
                                <span id="program2-pressure" class="text-green-400 font-medium">--</span>
                                <span id="program2-pressure-unit" class="text-gray-500">--</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">泄漏量:</span>
                                <span id="program2-leak" class="text-blue-400 font-medium">--</span>
                                <span id="program2-leak-unit" class="text-gray-500">--</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="text-gray-400">Result:</span>
                                <span id="program2-result" class="text-yellow-400 font-medium">--</span>
                            </div>
                        </div>
                    </div>
                </div>

            </div>

            <div class="resize-handle"></div>
        </div>
        </div>
        
        <!-- 实时曲线卡片 -->
        <div id="dashboard-chart-column" class="dashboard-column">
        <div id="chart-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 360px; top: 332px; width: 700px; height: 480px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-3">
                        <h1 class="text-white font-semibold">实时曲线</h1>
                        <span id="chart-status" class="text-xs px-2 py-0.5 rounded bg-gray-600 text-gray-300">待机</span>
                    </div>
                    <div class="flex items-center gap-4 text-xs text-gray-400">
                        <span>Fill: <span id="fill-time">--</span>s</span>
                        <span>Stab: <span id="stab-time">--</span>s</span>
                        <span>Test: <span id="test-time">--</span>s</span>
                    </div>
                </div>
            </div>
            
            <div class="p-4 space-y-4">
                <!-- 压力曲线 -->
                <div class="bg-gray-700/30 rounded-xl p-3">
                    <div class="flex items-center justify-between mb-2">
                        <span class="text-gray-300 text-sm font-medium">压力曲线</span>
                        <span class="text-xs text-gray-500">X轴: Fill+Stab+Test</span>
                    </div>
                    <div style="height: 140px;">
                        <canvas id="pressure-chart"></canvas>
                    </div>
                </div>
                
                <!-- 泄漏量曲线 -->
                <div class="bg-gray-700/30 rounded-xl p-3">
                    <div class="flex items-center justify-between mb-2">
                        <span class="text-gray-300 text-sm font-medium">泄漏量曲线</span>
                        <span class="text-xs text-gray-500">X轴: Test Time</span>
                    </div>
                    <div style="height: 140px;">
                        <canvas id="leak-chart"></canvas>
                    </div>
                </div>
            </div>
            
            <div class="resize-handle"></div>
        </div>
        </div>
        
        <!-- 测试记录卡片 -->
        <div id="record-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700" 
             style="left: 1092px; top: 652px; width: 804px; min-height: 260px;">
            
            <div class="drag-handle bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                <div class="flex items-center justify-between">
                    <h1 class="text-white font-semibold">测试记录</h1>
                    <div class="flex items-center gap-2">
                        <span id="today-count" class="text-xs px-2 py-0.5 rounded bg-blue-600 text-white">今日: 0</span>
                        <span id="pass-rate" class="text-xs px-2 py-0.5 rounded bg-green-600 text-white">合格率: 0%</span>
                        <button id="btn-refresh" class="text-xs bg-gray-600 hover:bg-gray-500 text-white px-2 py-0.5 rounded transition-colors">刷新</button>
                    </div>
                </div>
            </div>
            
            <div class="p-4">
                <div class="overflow-auto" style="max-height: 180px;">
                        <table id="records-display-table" class="w-full border-collapse" style="font-size: 10px;">
                            <colgroup id="records-table-colgroup">
                                <col style="width: 52px;">
                                <col style="width: 165px;">
                                <col style="width: 120px;">
                                <col style="width: 170px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 90px;">
                                <col style="width: 120px;">
                                <col style="width: 96px;">
                            </colgroup>
                            <thead class="bg-gray-600 text-gray-200 sticky top-0">
                                <tr>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">序号<span class="record-col-resizer" data-col-index="0"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">Time<span class="record-col-resizer" data-col-index="1"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">Serial No.<span class="record-col-resizer" data-col-index="2"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">QR Code<span class="record-col-resizer" data-col-index="3"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">#1 Pressure<span class="record-col-resizer" data-col-index="4"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">#1 Leak<span class="record-col-resizer" data-col-index="5"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">#2 Pressure<span class="record-col-resizer" data-col-index="6"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">#2 Leak<span class="record-col-resizer" data-col-index="7"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">Result<span class="record-col-resizer" data-col-index="8"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">Part No.<span class="record-col-resizer" data-col-index="9"></span></th>
                                    <th class="records-header-cell text-center py-1 px-1 border border-gray-500 font-medium">Staff<span class="record-col-resizer" data-col-index="10"></span></th>
                                </tr>
                            </thead>
                            <tbody id="records-table" class="text-gray-300">
                                <!-- 动态填充 -->
                            </tbody>
                        </table>
                    </div>
            </div>
            
            <div class="resize-handle"></div>
        </div>
        </div>
        </div>
        
        <!-- 全量查询页面 -->
        <div id="page-query" class="tab-pane" style="position: relative; width: 100vw; height: calc(100vh - 60px); overflow: hidden; padding: 15px;">
            <div class="h-full flex flex-col">
                <!-- 查询条件 - 第一行 -->
                <div class="bg-gray-800/80 backdrop-blur rounded-xl overflow-hidden border border-gray-700 p-4 mb-4 flex-shrink-0">
                    <div class="flex flex-wrap items-center gap-3">
                        <!-- 开始日期 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">开始:</label>
                            <input type="date" id="query-start-date" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-36">
                        </div>
                        
                        <!-- 结束日期 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">结束:</label>
                            <input type="date" id="query-end-date" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-36">
                        </div>
                        
                        <!-- 产品型号 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">产品:</label>
                            <select id="query-product-model" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-32">
                                <option value="">全部</option>
                            </select>
                        </div>
                        
                        <!-- 测试结果 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">结果:</label>
                            <select id="query-result" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-24">
                                <option value="">全部</option>
                                <option value="PASS">PASS</option>
                                <option value="FAIL">FAIL</option>
                            </select>
                        </div>
                        
                        <!-- QR码 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">QR码:</label>
                            <input type="text" id="query-qr-code" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-32" placeholder="QR码">
                        </div>
                        
                        <!-- 序列号 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">序列号:</label>
                            <input type="text" id="query-serial" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-32" placeholder="序列号">
                        </div>
                        
                        <!-- 每页数量 -->
                        <div class="flex items-center gap-2">
                            <label class="text-gray-300 text-sm whitespace-nowrap">每页:</label>
                            <select id="query-limit" class="px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-white text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-20">
                                <option value="20">20</option>
                                <option value="50">50</option>
                                <option value="100">100</option>
                                <option value="200">200</option>
                            </select>
                        </div>
                        
                        <!-- 操作按钮 -->
                        <div class="flex gap-2 ml-auto">
                            <button id="btn-query-search" class="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded text-sm transition-colors flex items-center gap-1">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
                                </svg>
                                查询
                            </button>
                            <button id="btn-query-reset" class="px-4 py-1.5 bg-gray-600 hover:bg-gray-500 text-white rounded text-sm transition-colors flex items-center gap-1">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
                                </svg>
                                重置
                            </button>
                            <button id="btn-query-export" class="px-4 py-1.5 bg-green-600 hover:bg-green-500 text-white rounded text-sm transition-colors flex items-center gap-1">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                                </svg>
                                导出
                            </button>
                        </div>
                    </div>
                </div>
                
                <!-- 统计信息 - 第二行 -->
                <div class="grid grid-cols-4 gap-3 mb-4 flex-shrink-0">
                    <div class="bg-gray-800/80 backdrop-blur rounded-xl border border-gray-700 p-3 flex items-center justify-between">
                        <span class="text-gray-400 text-sm">总记录数</span>
                        <span id="stat-total" class="text-xl font-bold text-white">0</span>
                    </div>
                    <div class="bg-gray-800/80 backdrop-blur rounded-xl border border-gray-700 p-3 flex items-center justify-between">
                        <span class="text-gray-400 text-sm">PASS</span>
                        <span id="stat-pass" class="text-xl font-bold text-green-400">0</span>
                    </div>
                    <div class="bg-gray-800/80 backdrop-blur rounded-xl border border-gray-700 p-3 flex items-center justify-between">
                        <span class="text-gray-400 text-sm">FAIL</span>
                        <span id="stat-fail" class="text-xl font-bold text-red-400">0</span>
                    </div>
                    <div class="bg-gray-800/80 backdrop-blur rounded-xl border border-gray-700 p-3 flex items-center justify-between">
                        <span class="text-gray-400 text-sm">合格率</span>
                        <span id="stat-rate" class="text-xl font-bold text-blue-400">0%</span>
                    </div>
                </div>
                
                <!-- 查询结果表格 - 占70%高度 -->
                <div class="bg-gray-800/80 backdrop-blur rounded-xl overflow-hidden border border-gray-700 flex-1 flex flex-col" style="min-height: 0;">
                    <div class="p-3 border-b border-gray-700 flex justify-between items-center flex-shrink-0">
                        <span class="text-white font-semibold">查询结果</span>
                        <span id="query-page-info" class="text-gray-400 text-sm">第 1 页 / 共 0 页</span>
                    </div>
                    
                    <div class="flex-1 overflow-auto" style="min-height: 0;">
                        <table id="query-results-table" class="w-full text-xs">
                            <colgroup id="query-table-colgroup">
                                <col style="width: 56px;">
                                <col style="width: 168px;">
                                <col style="width: 180px;">
                                <col style="width: 120px;">
                                <col style="width: 120px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 110px;">
                                <col style="width: 90px;">
                                <col style="width: 96px;">
                            </colgroup>
                            <thead class="bg-gray-700 text-gray-200 sticky top-0">
                                <tr>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">序号<span class="record-col-resizer" data-query-col-index="0"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">测试时间<span class="record-col-resizer" data-query-col-index="1"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">QR码<span class="record-col-resizer" data-query-col-index="2"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">序列号<span class="record-col-resizer" data-query-col-index="3"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">产品型号<span class="record-col-resizer" data-query-col-index="4"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">压力1<span class="record-col-resizer" data-query-col-index="5"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">泄漏1<span class="record-col-resizer" data-query-col-index="6"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">压力2<span class="record-col-resizer" data-query-col-index="7"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">泄漏2<span class="record-col-resizer" data-query-col-index="8"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">结果<span class="record-col-resizer" data-query-col-index="9"></span></th>
                                    <th class="records-header-cell text-center py-1.5 px-1 border border-gray-600 font-medium">操作员<span class="record-col-resizer" data-query-col-index="10"></span></th>
                                </tr>
                            </thead>
                            <tbody id="query-table-body" class="text-gray-300">
                                <tr>
                                    <td colspan="11" class="text-center py-8 text-gray-500">请执行查询</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                    
                    <!-- 分页 -->
                    <div class="p-3 border-t border-gray-700 flex justify-between items-center flex-shrink-0">
                        <div class="flex gap-2">
                            <button id="btn-page-first" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed">首页</button>
                            <button id="btn-page-prev" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed">上一页</button>
                            <button id="btn-page-next" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed">下一页</button>
                            <button id="btn-page-last" class="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed">末页</button>
                        </div>
                        <div class="flex items-center gap-2">
                            <span class="text-gray-400 text-sm">跳转到</span>
                            <input type="number" id="page-input" class="w-16 px-2 py-1 bg-gray-700 border border-gray-600 rounded text-white text-center" min="1" value="1">
                            <button id="btn-page-go" class="px-3 py-1 bg-blue-600 hover:bg-blue-500 text-white rounded transition-colors">GO</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 设置页面 -->
        <div id="page-settings" class="tab-pane" style="position: relative; width: 100vw; height: calc(100vh - 60px); overflow: hidden; padding: 16px 20px;">
            <div id="settings-auth-bar" class="flex flex-wrap items-center justify-between gap-3 bg-gray-800/80 backdrop-blur rounded-2xl border border-gray-700 px-4 py-3">
                <div class="flex items-center gap-3">
                    <span class="text-sm text-gray-300">设置登录</span>
                    <span id="settings-login-status-badge" class="px-2 py-0.5 rounded text-xs bg-red-600 text-white">未登录</span>
                    <span id="settings-login-user-hint" class="text-xs text-gray-400">登录后可修改设置</span>
                </div>
                <div class="flex items-center gap-2">
                    <select id="settings-login-user" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white text-sm min-w-[180px]">
                        <option value="">-- 选择用户名 --</option>
                    </select>
                    <input type="password" id="settings-login-password" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white text-sm min-w-[160px]" placeholder="密码">
                    <button id="btn-settings-login" class="px-3 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded text-sm transition-colors">登录</button>
                    <button id="btn-settings-logout" class="px-3 py-2 bg-gray-600 hover:bg-gray-500 text-white rounded text-sm transition-colors" disabled>退出</button>
                </div>
                <div id="settings-admin-bootstrap" class="w-full mt-1 border-t border-gray-600 pt-3" style="display:none;">
                    <div class="text-xs text-amber-300 mb-2">首次使用：当前浏览器还没有 admin 账号。请设置 admin 密码后登录（账号仅保存在当前浏览器，不会上传服务器）。</div>
                    <div class="flex flex-wrap items-center gap-2">
                        <span class="text-sm text-gray-300">用户名</span>
                        <input type="text" id="bootstrap-admin-user" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white text-sm w-[120px]" value="admin" readonly>
                        <input type="password" id="bootstrap-admin-password" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white text-sm min-w-[160px]" placeholder="设置 admin 密码">
                        <input type="password" id="bootstrap-admin-confirm" class="px-3 py-2 bg-gray-700 border border-gray-600 rounded text-white text-sm min-w-[160px]" placeholder="再次输入密码">
                        <button id="btn-bootstrap-admin" class="px-3 py-2 bg-green-600 hover:bg-green-500 text-white rounded text-sm transition-colors">初始化 admin</button>
                    </div>
                </div>
            </div>
            <div class="flex-1 min-h-0 flex gap-4 items-stretch">
            <!-- 产品设置卡片 -->
            <div id="product-settings-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700 flex flex-col flex-1 min-w-0">
                <div class="bg-gray-700/50 px-4 py-3 border-b border-gray-600 flex justify-between items-center">
                    <h2 class="text-white font-semibold text-xs">产品设置</h2>
                    <button id="btn-add-product" class="px-3 py-2 bg-green-600 hover:bg-green-500 text-white rounded-lg transition-colors flex items-center gap-2 text-xs">
                        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/>
                        </svg>
                        添加产品
                    </button>
                </div>

                <div class="p-4 flex flex-col flex-1 min-h-0">
                    <div class="mb-3 flex flex-wrap items-center gap-3 text-xs bg-gray-700/30 rounded-lg px-3 py-2 flex-shrink-0">
                        <span class="text-gray-300 font-medium">设备通信</span>
                        <label class="flex items-center gap-2 text-gray-300">
                            <span>扫码枪</span>
                            <input id="switch-scanner-enabled" type="checkbox" class="h-4 w-4 accent-blue-500">
                        </label>
                        <label class="flex items-center gap-2 text-gray-300">
                            <span>扫码枪串口</span>
                            <input id="input-scanner-port" list="com-port-options" type="text" class="w-[100px] px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-xs" placeholder="默认">
                        </label>
                        <label class="flex items-center gap-2 text-gray-300">
                            <span>仪器串口</span>
                            <input id="input-instrument-port" list="com-port-options" type="text" class="w-[100px] px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-xs" placeholder="默认">
                        </label>
                        <datalist id="com-port-options"></datalist>
                        <span id="device-port-hint" class="text-[11px] text-gray-400">留空使用默认串口；仪器与扫码枪不能相同</span>
                        <span id="device-restart-hint" class="text-[11px] text-amber-300" style="display:none;">需要重启服务后生效</span>
                    </div>
                    <div class="flex-1 min-h-0 overflow-auto">
                        <table class="w-full text-xs" id="product-table">
                            <thead class="bg-gray-600 text-gray-200">
                                <tr>
                                    <th class="text-center py-1 px-1 border border-gray-500 font-medium w-12">序号</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[120px]">产品型号</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[100px]">ATEQ程序号1</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[100px]">ATEQ程序号2</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[100px]">切换腔道</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[260px]">标签模板路径</th>
                                    <th class="text-left py-1 px-1 border border-gray-500 font-medium min-w-[120px]">供应商代码</th>
                                    <th class="text-center py-1 px-1 border border-gray-500 font-medium w-20">操作</th>
                                </tr>
                            </thead>
                            <tbody class="text-gray-300" id="product-tbody">
                            </tbody>
                        </table>
                    </div>
                    
                    <div class="mt-3 flex justify-end gap-3 flex-shrink-0">
                        <button id="btn-save-settings" class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg transition-colors text-xs">
                            保存设置
                        </button>
                    </div>
                </div>
            </div>

            <!-- 操作人员设置卡片 -->
            <div id="operator-settings-card" class="control-card bg-gray-800/80 backdrop-blur rounded-2xl overflow-hidden border border-gray-700 flex flex-col w-[320px] flex-shrink-0">
                <div class="bg-gray-700/50 px-4 py-3 border-b border-gray-600">
                    <h2 class="text-white font-semibold text-xs">操作人员</h2>
                </div>
                <div class="p-3 space-y-1.5 flex-1 min-h-0 overflow-y-auto" id="operator-list">
                </div>
            </div>
            </div>

            <div id="settings-lock-overlay" class="settings-lock-overlay">
                <div class="settings-lock-panel">
                    <div class="text-amber-300 text-sm font-semibold">设置页已锁定</div>
                    <div class="mt-2 text-xs text-gray-300 leading-6">请在页面中央的登录框选择用户名并输入密码登录，登录后即可编辑设置。</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        function loadState() {
            const saved = localStorage.getItem('ateq_card_v2');
            if (saved) {
                try {
                    const state = JSON.parse(saved);
                    const card = document.getElementById('control-card');
                    if (state.left) card.style.left = state.left + 'px';
                    if (state.top) card.style.top = state.top + 'px';
                    if (state.width) card.style.width = state.width + 'px';
                    if (state.height) card.style.minHeight = state.height + 'px';
                } catch(e) {}
            }
        }
        
        function saveState() {
            const card = document.getElementById('control-card');
            localStorage.setItem('ateq_card_v2', JSON.stringify({
                left: parseInt(card.style.left) || 50,
                top: parseInt(card.style.top) || 50,
                width: parseInt(card.style.width) || 320,
                height: parseInt(card.style.minHeight) || 140
            }));
        }
        
        interact('#control-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { e.target.classList.remove('dragging'); saveState(); }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 280, height: 120 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end() { saveState(); }
                }
            });
        
        document.getElementById('btn-start').addEventListener('click', async function() {
            if (!requireWorkLogin()) return;
            const btn = this;
            const statusEl = document.getElementById('test-status-display');
            
            if (!currentProductConfig) {
                alert('请先选择产品型号');
                return;
            }
            
            const program1 = parseInt(currentProductConfig.program1) || 1;
            const program2 = parseInt(currentProductConfig.program2) || 0;
            const productModel = currentProductConfig.model || '';
            const labelTemplate = currentProductConfig.labelTemplate || '';
            const supplierCode = currentProductConfig.supplierCode || '';
            const operator = getActiveOperatorName();
            const qrCode = document.getElementById('qr-input')?.value || '';
            
            // 再次测试时清除程序号1和程序号2的结果
            clearProgramPassLights();
            
            btn.disabled = true;
            if (statusEl) {
                statusEl.textContent = '正在启动测试...';
                statusEl.className = 'text-xs text-yellow-400';
            }
            
            try {
                const params = new URLSearchParams({
                    program1: program1,
                    program2: program2,
                    product_model: productModel,
                    operator: operator,
                    qr_code: qrCode,
                    label_template: labelTemplate,
                    supplier_code: supplierCode
                });
                
                const response = await fetch(`/api/start_test_sequence?${params}`, { 
                    method: 'POST' 
                });
                const result = await response.json();
                
                if (result.success) {
                    
                    if (statusEl) {
                        statusEl.textContent = '测试已启动';
                        statusEl.className = 'text-xs text-green-400';
                    }
                    startStatusPolling();
                } else {
                    alert('启动失败: ' + result.message);
                    if (statusEl) {
                        statusEl.textContent = '启动失败';
                        statusEl.className = 'text-xs text-red-400';
                    }
                    btn.disabled = false;
                }
            } catch(e) {
                alert('启动异常: ' + e.message);
                if (statusEl) {
                    statusEl.textContent = '启动异常';
                    statusEl.className = 'text-xs text-red-400';
                }
                btn.disabled = false;
            }
        });
        
        document.getElementById('btn-stop').addEventListener('click', async function() {
            const btn = this;
            const statusEl = document.getElementById('test-status-display');
            
            btn.disabled = true;
            try {
                const response = await fetch('/api/stop_test', { method: 'POST' });
                const result = await response.json();
                // 清除程序合格指示灯
                clearProgramPassLights();
                if (statusEl) {
                    if (result.cancel_accepted) {
                        if (result.reset_confirmed) {
                            statusEl.textContent = '已停止，仪器复位已确认';
                            statusEl.className = 'text-xs text-green-400';
                        } else if (result.reset_pending) {
                            statusEl.textContent = '取消已受理，仪器复位待确认';
                            statusEl.className = 'text-xs text-yellow-400';
                        } else {
                            statusEl.textContent = '取消已受理，仪器复位未确认: ' + (result.reset_error || '');
                            statusEl.className = 'text-xs text-red-400';
                        }
                    } else {
                        statusEl.textContent = result.message || '没有正在执行的测试';
                        statusEl.className = 'text-xs text-gray-400';
                    }
                }
                document.getElementById('btn-start').disabled = false;
            } catch(e) {
                console.error('停止失败:', e);
                if (statusEl) {
                    statusEl.textContent = '停止请求失败: ' + e.message;
                    statusEl.className = 'text-xs text-red-400';
                }
            }
            btn.disabled = false;
        });

        // 纯函数：决定打印响应在前端的动作（关闭打印不清码/不谎称已打印）
        function resolvePrintUi(result) {
            if (!result) {
                return { notify: 'error', clearQr: false, message: '无响应' };
            }
            if (result.printed) {
                return { notify: 'success', clearQr: true,
                         message: '补打已提交（不保证纸张已出）: ' + (result.serial_number || '') };
            }
            if (result.state === 'disabled') {
                return { notify: 'warning', clearQr: false, message: '打印功能已关闭，未打印，二维码保留' };
            }
            if (result.state === 'busy') {
                return { notify: 'warning', clearQr: false, message: '该记录打印处理中，请稍后重试' };
            }
            return { notify: 'error', clearQr: false,
                     message: '补打失败: ' + (result.message || result.error_code || '') };
        }

        document.getElementById('btn-print-label').addEventListener('click', async function() {
            if (!requireWorkLogin()) return;
            if (!currentProductConfig) {
                alert('请先选择产品型号');
                return;
            }
            // 手动补打必须使用已保存的PASS记录id，复用原序列号，禁止生成新号
            const recordId = (typeof lastPassRecordId === 'number') ? lastPassRecordId : null;
            if (!recordId) {
                showNotification('没有可补打的已保存PASS记录，请先完成一次合格测试', 'warning');
                return;
            }

            const btn = this;
            btn.disabled = true;
            try {
                const response = await fetch('/api/print_label', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        record_id: recordId,
                        product_model: currentProductConfig.model || '',
                        label_template: currentProductConfig.labelTemplate || '',
                        supplier_code: currentProductConfig.supplierCode || '',
                        qr_code: document.getElementById('qr-input')?.value || ''
                    })
                });
                const result = await response.json();
                const ui = resolvePrintUi(result);
                showNotification(ui.message, ui.notify);
                if (ui.clearQr) {
                    const qrInput = document.getElementById('qr-input');
                    if (qrInput) { qrInput.value = ''; }
                }
            } catch (e) {
                showNotification('打印异常: ' + e.message, 'error');
            } finally {
                btn.disabled = false;
            }
        });

        document.getElementById('btn-scan-start')?.addEventListener('click', function() {
            if (!requireWorkLogin()) return;
            const qrInput = document.getElementById('qr-input');
            if (qrInput) {
                qrInput.value = '';
                qrInput.focus();
            }
            showNotification('请扫码，系统正在监听 ' + (configuredScannerPort || '默认串口'), 'info');
            pollSerialScanner();
        });

        document.getElementById('btn-clear-qr').addEventListener('click', function() {
            const qrInput = document.getElementById('qr-input');
            if (qrInput) {
                qrInput.value = '';
                qrInput.focus();
            }
        });

        // ---------- 结果横幅：只读后台快照，不参与判定 ----------
        const HERO_REASON_TEXT = {
            OK: '判定合格',
            STATUS_FAIL: '仪器判定不合格',
            MODEL_UNKNOWN: '未选择产品型号（硬件按钮启动时无产品）',
            SCAN_REQUIRED: '需要扫码但未扫码',
            PROGRAM_MISMATCH: '仪器程序号与产品配置不一致',
            PROGRAM_UNVERIFIED: '无法确认仪器程序号',
            CHAIN_PROGRAM_MISMATCH: '程序2程序号不一致',
            MISSING_STEP7_LEAK: '未采到泄漏阶段数据',
            MISSING_PRESSURE_SAMPLE: '未采到压力数据',
            MISSING_SAMPLE: '未取得结束数据',
            CYCLE_END_NOT_OBSERVED: '未确认测试循环结束',
            UNKNOWN_STATUS_BITS: '仪器状态位未识别',
            INSTRUMENT_STATE_UNVERIFIED: '仪器状态未确认（启动前未见空闲）',
            ABNORMAL_RESET: '仪器异常复位',
            CANCELLED: '已手动停止',
            PHASE_TIMEOUT: '测试超时',
            CHAIN_TIMEOUT: '程序2未启动（超时）',
            COMMUNICATION_ERROR: '仪器通讯故障',
            BUS_TIMEOUT: '串口总线忙（超时）',
            START_FAILED: '启动命令失败',
            PREPARE_WRITE_FAILED: '写入程序号失败',
            NO_ACTIVITY_OBSERVED: '未观察到测试运行',
        };
        const HERO_PHASE_TEXT = {
            PREPARING: '准备程序中…',
            RUNNING_P1: '程序1 测试中…',
            CHAINING_P2: '切换程序2…',
            RUNNING_P2: '程序2 测试中…',
            FINALIZING: '保存结果中…',
        };

        function updateResultHero(result) {
            const card = document.getElementById('result-hero-card');
            if (!card || !result) return;
            const resultEl = document.getElementById('hero-result');
            const detailEl = document.getElementById('hero-detail');
            const overall = result.overall_result;
            const reasonCode = result.terminal_reason || '';
            const reason = HERO_REASON_TEXT[reasonCode] || reasonCode;
            let state = 'idle';
            let word = '待机';
            let detail = currentProductConfig ? '就绪，等待启动' : '请选择产品后启动测试';
            if (result.busy) {
                state = 'running';
                word = '测试中';
                detail = HERO_PHASE_TEXT[result.phase] || '测试进行中…';
            } else if (overall === 'PASS') {
                state = 'pass';
                word = '合格';
                detail = result.print_message || '判定合格';
            } else if (overall === 'FAIL') {
                state = 'fail';
                word = '不合格';
                detail = reason || '仪器判定不合格';
            } else if (overall) {
                state = 'abnormal';
                word = overall === 'ABORTED' ? '已中止' : '未判定';
                detail = reason ? ('原因：' + reason) : '未取得合格证据';
            }
            card.dataset.state = state;
            resultEl.textContent = word;
            detailEl.textContent = detail;
            document.getElementById('hero-product').textContent =
                result.active_product_model || '未选择';
            document.getElementById('hero-serial').textContent =
                (!result.busy && result.serial_number) ? result.serial_number : '--';
            const commEl = document.getElementById('hero-comm');
            const sample = result.sample || {};
            const commBad = !!sample.error_code || sample.stale;
            commEl.textContent = commBad ? ('异常 ' + (sample.error_code || '数据过期')) : '正常';
            commEl.className = 'hero-meta-value ' + (commBad ? 'comm-bad' : 'comm-ok');
        }

        let statusPollingInterval = null;
        let lastCompletionSequence = 0;
        let lastPassRecordId = null;

        function startStatusPolling() {
            if (statusPollingInterval) return;

            statusPollingInterval = setInterval(async () => {
                try {
                    const response = await fetch('/api/test_status');
                    const result = await response.json();

                    const statusEl = document.getElementById('test-status-display');
                    const btn = document.getElementById('btn-start');

                    if (result.success) {
                        // 其他浏览器切换了产品时，本页跟随服务器显示
                        if (result.active_product_model) {
                            applyServerProductSelection(result.active_product_model);
                        }
                        updateResultHero(result);
                        // 程序合格指示灯与数值均以后台冻结快照为准（不使用实时显示值复制）
                        updateProgramPassLights(result.program1_pass, result.program2_pass);
                        if (result.program1_pressure !== null && result.program1_pressure !== undefined) {
                            document.getElementById('program1-pressure').textContent = result.program1_pressure.toFixed(3);
                        }
                        // 压力/泄漏单位各自独立显示（仪器每个程序的压力单位可单独配置，
                        // 泄漏单位与压力单位无关，绝不能共用一个）
                        if (result.pressure1_unit) {
                            document.getElementById('program1-pressure-unit').textContent = result.pressure1_unit;
                        }
                        if (result.leak1_unit) {
                            document.getElementById('program1-leak-unit').textContent = result.leak1_unit;
                        }
                        if (result.program1_leak !== null && result.program1_leak !== undefined) {
                            document.getElementById('program1-leak').textContent = result.program1_leak.toFixed(3);
                        }
                        if (result.program2_pressure !== null && result.program2_pressure !== undefined) {
                            document.getElementById('program2-pressure').textContent = result.program2_pressure.toFixed(3);
                        }
                        if (result.pressure2_unit) {
                            document.getElementById('program2-pressure-unit').textContent = result.pressure2_unit;
                        }
                        if (result.leak2_unit) {
                            document.getElementById('program2-leak-unit').textContent = result.leak2_unit;
                        }
                        if (result.program2_leak !== null && result.program2_leak !== undefined) {
                            document.getElementById('program2-leak').textContent = result.program2_leak.toFixed(3);
                        }
                        updateProgramResultText('program1-result', result.program1_result, result.program1_pass);
                        updateProgramResultText('program2-result', result.program2_result, result.program2_pass);

                        // 检测新的测试完成（通过完成序列号变化判断）
                        if (result.completion_sequence && result.completion_sequence > lastCompletionSequence) {
                            lastCompletionSequence = result.completion_sequence;
                            if (result.overall_result === 'PASS' && result.record_saved && result.record_id) {
                                lastPassRecordId = result.record_id;
                            }
                            // 仅清空输入框文本，不制造扫码事件、不重置扫码游标
                            const qrInput = document.getElementById('qr-input');
                            if (qrInput) {
                                qrInput.value = '';
                            }
                            setTimeout(() => loadRecords(), RECORD_REFRESH_DELAY_MS);
                            // 结果通知（合格/不合格/未知均如实显示）
                            if (result.overall_result) {
                                const isPass = result.overall_result === 'PASS';
                                if (isPass) {
                                    showNotification('测试合格: ' + (result.serial_number || ''), 'success');
                                } else {
                                    showNotification('测试结束: ' + (result.overall_result || 'UNKNOWN'), 'warning');
                                }
                            }
                            // 打印状态如实通知
                            if (result.print_state === 'failed' || result.print_state === 'timeout') {
                                showNotification('打印未成功: ' + (result.print_message || '未知错误'), 'warning');
                            }
                        }

                        if (result.running || result.busy) {
                            const phaseMessages = {
                                'PREPARING': '准备程序中...',
                                'RUNNING_P1': '执行程序1测试...',
                                'CHAINING_P2': '衔接程序2...',
                                'RUNNING_P2': '执行程序2测试...',
                                'FINALIZING': '保存记录/打印中...'
                            };
                            btn.disabled = true;
                            if (statusEl) {
                                statusEl.textContent = phaseMessages[result.phase] || result.phase || '测试中...';
                                statusEl.className = 'text-xs text-yellow-400';
                            }
                        } else {
                            btn.disabled = false;
                            if (statusEl) {
                                const terminalReason = result.terminal_reason || '';
                                if (result.overall_result === 'PASS') {
                                    statusEl.textContent = '测试完成';
                                    statusEl.className = 'text-xs text-green-400';
                                } else if (result.overall_result) {
                                    statusEl.textContent = '周期结束: ' + result.overall_result + (terminalReason ? (' (' + terminalReason + ')') : '');
                                    statusEl.className = 'text-xs text-yellow-400';
                                }
                            }
                        }
                    }
                } catch(e) {
                    console.error('状态查询失败:', e);
                }
            }, 500);
        }
        
        function stopStatusPolling() {
            if (statusPollingInterval) {
                clearInterval(statusPollingInterval);
                statusPollingInterval = null;
            }
        }
        
        // 监控卡片拖拽
        interact('#monitor-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('monitor-card');
                        localStorage.setItem('monitor_card_v2', JSON.stringify({
                            left: parseInt(card.style.left) || 400,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 300,
                            height: parseInt(card.style.minHeight) || 180
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 250, height: 150 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end(e) {
                        const card = document.getElementById('monitor-card');
                        localStorage.setItem('monitor_card_v2', JSON.stringify({
                            left: parseInt(card.style.left) || 400,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 300,
                            height: parseInt(card.style.minHeight) || 180
                        }));
                    }
                }
            });
        
        // 加载监控卡片位置
        const monitorSaved = localStorage.getItem('monitor_card_v2');
        if (monitorSaved) {
            try {
                const state = JSON.parse(monitorSaved);
                const card = document.getElementById('monitor-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = state.height + 'px';
            } catch(e) {}
        }
        
        // 产品选择卡片拖拽
        interact('#product-select-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('product-select-card');
                        localStorage.setItem('product_select_card_v2', JSON.stringify({
                            left: parseInt(card.style.left) || 50,
                            top: parseInt(card.style.top) || 210,
                            width: parseInt(card.style.width) || 320,
                            height: Math.max(parseInt(card.style.minHeight) || 500, 500)
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 280, height: 500 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end(e) {
                        const card = document.getElementById('product-select-card');
                        localStorage.setItem('product_select_card_v2', JSON.stringify({
                            left: parseInt(card.style.left) || 50,
                            top: parseInt(card.style.top) || 210,
                            width: parseInt(card.style.width) || 320,
                            height: Math.max(parseInt(card.style.minHeight) || 500, 500)
                        }));
                    }
                }
            });
        
        // 加载产品选择卡片位置
        const productSelectSaved = localStorage.getItem('product_select_card_v2');
        if (productSelectSaved) {
            try {
                const state = JSON.parse(productSelectSaved);
                const card = document.getElementById('product-select-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = Math.max(parseInt(state.height) || 500, 500) + 'px';
            } catch(e) {}
        }
        
        // 当前选中的产品配置
        let currentProductConfig = null;
        const LOGIN_USER_STORAGE_KEY = 'ateq_logged_in_user';
        const OPERATOR_ACCOUNTS_STORAGE_KEY = 'ateq_operator_accounts';
        let loggedInUser = '';
        updateAteqProgramNumberDisplay();

        function readConfiguredAccounts() {
            try {
                const saved = localStorage.getItem(OPERATOR_ACCOUNTS_STORAGE_KEY);
                if (saved) {
                    const accounts = JSON.parse(saved);
                    if (Array.isArray(accounts)) {
                        return accounts
                            .map(item => ({
                                username: String(item?.username || '').trim(),
                                password: String(item?.password || '')
                            }))
                            .filter(item => item.username);
                    }
                }
            } catch (e) {}

            try {
                const legacy = localStorage.getItem('ateq_operators');
                const names = legacy ? JSON.parse(legacy) : [];
                if (Array.isArray(names)) {
                    return names
                        .map(name => ({ username: String(name || '').trim(), password: '' }))
                        .filter(item => item.username);
                }
            } catch (e) {}

            return [];
        }

        function readConfiguredOperators() {
            return readConfiguredAccounts().map(item => item.username).filter(Boolean);
        }

        function isSettingsLoginRequired() {
            return readConfiguredOperators().length > 0;
        }

        function isAdminUser(userName = loggedInUser) {
            return String(userName || '').trim().toLowerCase() === 'admin';
        }

        function canEditSettings() {
            return isAdminUser();
        }

        function hasConfiguredAdminIn(accounts) {
            return (Array.isArray(accounts) ? accounts : []).some(function(item) {
                return String((item && item.username) || '').trim().toLowerCase() === 'admin';
            });
        }

        function hasConfiguredAdmin() {
            return hasConfiguredAdminIn(readConfiguredAccounts());
        }

        function buildFirstAdminAccounts(existingAccounts, password, confirmPassword) {
            const existing = (Array.isArray(existingAccounts) ? existingAccounts : [])
                .map(function(item) {
                    return {
                        username: String((item && item.username) || '').trim(),
                        password: String((item && item.password) || '')
                    };
                })
                .filter(function(item) { return item.username; });
            if (hasConfiguredAdminIn(existing)) {
                return { ok: false, accounts: existing, message: '已存在 admin 账号，无需初始化' };
            }
            const pwd = password == null ? '' : String(password);
            const confirm = confirmPassword == null ? '' : String(confirmPassword);
            if (!pwd.length || !pwd.trim().length) {
                return { ok: false, accounts: existing, message: '密码不能为空' };
            }
            if (pwd !== confirm) {
                return { ok: false, accounts: existing, message: '两次输入的密码不一致' };
            }
            const accounts = existing.filter(function(item) {
                return item.username.toLowerCase() !== 'admin';
            });
            accounts.push({ username: 'admin', password: pwd });
            return { ok: true, accounts: accounts, message: 'admin 账号已创建，请使用该密码登录' };
        }

        function getActiveOperatorName() {
            return String(document.getElementById('operator-input')?.value || '').trim();
        }

        function applyOperatorLoginState() {
            const select = document.getElementById('operator-input');
            if (!select) return;
            if (loggedInUser && !isAdminUser()) {
                const ownOption = Array.from(select.options).find(option => option.value === loggedInUser);
                if (ownOption) select.value = loggedInUser;
                select.disabled = true;
            } else {
                if (!loggedInUser) select.value = '';
                select.disabled = false;
            }
        }

        function requireWorkLogin() {
            const configuredUsers = readConfiguredOperators();
            if (loggedInUser && configuredUsers.includes(loggedInUser)) return true;
            showNotification('请先登录操作人员账号，再进行测试或查询', 'warning');
            switchTab('settings');
            return false;
        }

        function resetSettingsAuthBarPosition() {
            const authBar = document.getElementById('settings-auth-bar');
            if (!authBar) return;
            authBar.style.left = '';
            authBar.style.top = '';
            authBar.style.transform = '';
            authBar.removeAttribute('data-drag-free');
            authBar.classList.remove('dragging');
        }

        function applySettingsAccessState() {
            const locked = !canEditSettings();
            const settingsPage = document.getElementById('page-settings');

            if (settingsPage) {
                settingsPage.classList.toggle('settings-locked', locked);
                if (!locked) {
                    resetSettingsAuthBarPosition();
                }
            }

            document.querySelectorAll('#page-settings .control-card input, #page-settings .control-card select, #page-settings .control-card button, #page-settings .control-card textarea').forEach(control => {
                control.disabled = locked;
            });
            applyOperatorLoginState();

            const lockTitle = document.querySelector('#settings-lock-overlay .settings-lock-panel > div:first-child');
            const lockMessage = document.querySelector('#settings-lock-overlay .settings-lock-panel > div:last-child');
            if (lockTitle && lockMessage) {
                if (loggedInUser && !isAdminUser()) {
                    lockTitle.textContent = '设置页仅限 admin';
                    lockMessage.textContent = '当前用户 ' + loggedInUser + ' 仅可操作测试和查询。请退出后使用 admin 登录以修改产品和操作人员。';
                } else {
                    lockTitle.textContent = '设置页已锁定';
                    lockMessage.textContent = '请使用用户名 admin 登录，才能修改产品和操作人员设置。其他账号仅可操作测试和查询。';
                }
            }
        }

        function applyLoginUiState() {
            const select = document.getElementById('settings-login-user');
            const passwordInput = document.getElementById('settings-login-password');
            const loginButton = document.getElementById('btn-settings-login');
            const logoutButton = document.getElementById('btn-settings-logout');
            const badge = document.getElementById('settings-login-status-badge');
            const hint = document.getElementById('settings-login-user-hint');
            const hasUsers = isSettingsLoginRequired();

            if (badge) {
                if (loggedInUser) {
                    badge.textContent = isAdminUser() ? '管理员' : '操作员';
                    badge.className = 'px-2 py-0.5 rounded text-[11px] ' +
                        (isAdminUser() ? 'bg-green-600' : 'bg-blue-600') + ' text-white';
                } else if (hasUsers) {
                    badge.textContent = '未登录';
                    badge.className = 'px-2 py-0.5 rounded text-[11px] bg-red-600 text-white';
                } else {
                    badge.textContent = '未配置';
                    badge.className = 'px-2 py-0.5 rounded text-[11px] bg-yellow-600 text-white';
                }
            }

            if (hint) {
                if (isAdminUser()) {
                    hint.textContent = '当前用户: admin（可修改产品及人员设置）';
                } else if (loggedInUser) {
                    hint.textContent = '当前用户: ' + loggedInUser + '（仅测试和查询）';
                } else if (hasUsers) {
                    hint.textContent = '登录后可测试和查询；仅 admin 可修改设置';
                } else {
                    hint.textContent = '尚未配置 admin；请在上方设置 admin 密码后登录';
                }
            }

            if (loginButton) {
                loginButton.disabled = !!loggedInUser;
            }
            if (logoutButton) {
                logoutButton.disabled = !loggedInUser;
            }
            if (select) {
                select.disabled = !!loggedInUser;
            }
            if (passwordInput) {
                passwordInput.disabled = !!loggedInUser || !hasUsers;
                if (loggedInUser) {
                    passwordInput.value = '';
                }
            }
            const bootstrapPanel = document.getElementById('settings-admin-bootstrap');
            if (bootstrapPanel) {
                bootstrapPanel.style.display = hasConfiguredAdmin() ? 'none' : '';
            }
        }

        function setLoggedInUser(userName) {
            loggedInUser = String(userName || '').trim();
            // Shared shop-floor workstation: do not carry an authenticated role across page reloads.
            localStorage.removeItem(LOGIN_USER_STORAGE_KEY);
            const passwordInput = document.getElementById('settings-login-password');
            if (passwordInput) {
                passwordInput.value = '';
            }

            applyLoginUiState();
            applySettingsAccessState();
            if (typeof syncTestContext === 'function') syncTestContext();
        }

        function initializeLoginState() {
            loggedInUser = '';
            localStorage.removeItem(LOGIN_USER_STORAGE_KEY);

            applyLoginUiState();
            applySettingsAccessState();
        }

        function requireSettingsLogin() {
            if (canEditSettings()) {
                return true;
            }
            showNotification(loggedInUser
                ? '仅 admin 可以修改产品和操作人员设置'
                : '请在设置页面使用 admin 账号登录', 'warning');
            return false;
        }

        function updateAteqProgramNumberDisplay() {
            const number1 = Number.parseInt(currentProductConfig?.program1, 10);
            const number2 = Number.parseInt(currentProductConfig?.program2, 10);
            const display1 = document.getElementById('program1-ateq-number');
            const display2 = document.getElementById('program2-ateq-number');
            if (display1) display1.textContent = Number.isInteger(number1) && number1 > 0 ? String(number1) : '--';
            if (display2) display2.textContent = Number.isInteger(number2) && number2 > 0 ? String(number2) : '未配置';
        }

        let configuredScannerPort = '';

        async function loadAvailableComPorts() {
            try {
                const response = await fetch('/api/com_ports');
                const data = await response.json();
                const list = document.getElementById('com-port-options');
                if (!list || !data || !data.success) return;
                list.innerHTML = '';
                (data.ports || []).forEach(function(port) {
                    const option = document.createElement('option');
                    option.value = port;
                    list.appendChild(option);
                });
            } catch (e) {
                console.error('加载串口列表失败:', e);
            }
        }

        async function loadLineSettings() {
            try {
                const response = await fetch('/api/line_settings');
                const data = await response.json();
                if (!data.success || !data.settings) return;
                const scanSwitch = document.getElementById('switch-scan-required');
                const printerSwitch = document.getElementById('switch-printer-enabled');
                const scanStartSwitch = document.getElementById('switch-scan-start');
                const scannerSwitch = document.getElementById('switch-scanner-enabled');
                if (scanSwitch) scanSwitch.checked = !!data.settings.scan_required;
                if (printerSwitch) printerSwitch.checked = !!data.settings.printer_enabled;
                if (scanStartSwitch) scanStartSwitch.checked = !!data.settings.scan_start;
                if (scannerSwitch) scannerSwitch.checked = data.settings.scanner_enabled !== false;
                const scannerPortInput = document.getElementById('input-scanner-port');
                if (scannerPortInput) scannerPortInput.value = data.settings.scanner_port || '';
                const instrumentPortInput = document.getElementById('input-instrument-port');
                if (instrumentPortInput) instrumentPortInput.value = data.settings.instrument_port || '';
                configuredScannerPort = data.settings.scanner_port || '';
            } catch (e) {
                console.error('加载产线设置失败:', e);
            }
        }

        async function updateLineSettings(partial) {
            if (!requireSettingsLogin()) {
                await loadLineSettings();
                return;
            }
            try {
                const response = await fetch('/api/line_settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(partial)
                });
                const data = await response.json();
                if (!data.success) {
                    showNotification('设置切换失败: ' + (data.message || ''), 'error');
                    await loadLineSettings();
                    return;
                }
                if (data.settings && data.settings.scan_required_auto_enabled) {
                    showNotification('已开启「扫码启动」：「需要扫码」已自动勾选', 'info');
                }
                const restartHint = document.getElementById('device-restart-hint');
                if (restartHint) {
                    restartHint.style.display = data.restart_required ? '' : 'none';
                }
                if (data.message) {
                    showNotification(data.message, data.restart_required ? 'warning' : 'info');
                }
                await loadLineSettings();
            } catch (e) {
                showNotification('设置切换异常: ' + e.message, 'error');
                await loadLineSettings();
            }
        }

        document.getElementById('switch-scan-required')?.addEventListener('change', function() {
            updateLineSettings({ scan_required: this.checked });
        });

        document.getElementById('switch-scan-start')?.addEventListener('change', function() {
            const scannerSwitch = document.getElementById('switch-scanner-enabled');
            if (this.checked && scannerSwitch && !scannerSwitch.checked) {
                this.checked = false;
                showNotification('扫码枪已禁用，不能开启「扫码启动」；请先启用扫码枪', 'warning');
                return;
            }
            updateLineSettings({ scan_start: this.checked });
            if (typeof syncTestContext === 'function') {
                syncTestContext();
            }
        });

        document.getElementById('switch-printer-enabled')?.addEventListener('change', function() {
            updateLineSettings({ printer_enabled: this.checked });
        });

        document.getElementById('switch-scanner-enabled')?.addEventListener('change', function() {
            if (!this.checked) {
                const scanStartSwitch = document.getElementById('switch-scan-start');
                if (scanStartSwitch && scanStartSwitch.checked) {
                    scanStartSwitch.checked = false;
                }
            }
            updateLineSettings({ scanner_enabled: this.checked });
        });

        function commitPortChanges() {
            const scannerPort = String(document.getElementById('input-scanner-port')?.value || '').trim();
            const instrumentPort = String(document.getElementById('input-instrument-port')?.value || '').trim();
            updateLineSettings({
                scanner_port: scannerPort,
                instrument_port: instrumentPort
            });
        }

        document.getElementById('input-scanner-port')?.addEventListener('change', commitPortChanges);
        document.getElementById('input-instrument-port')?.addEventListener('change', commitPortChanges);

        document.getElementById('operator-input')?.addEventListener('change', function() {
            if (typeof syncTestContext === 'function') {
                syncTestContext();
            }
        });

        document.getElementById('settings-login-user')?.addEventListener('change', function() {
            applyLoginUiState();
        });

        document.getElementById('settings-login-password')?.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                document.getElementById('btn-settings-login')?.click();
            }
        });

        document.getElementById('btn-settings-login')?.addEventListener('click', function() {
            const userName = String(document.getElementById('settings-login-user')?.value || '').trim();
            const password = String(document.getElementById('settings-login-password')?.value || '');
            if (!userName) {
                showNotification('请先选择用户名', 'warning');
                return;
            }
            const account = readConfiguredAccounts().find(item => item.username === userName);
            if (!account) {
                showNotification('用户名不存在', 'error');
                return;
            }
            if (String(account.password || '') !== password) {
                showNotification('密码错误', 'error');
                return;
            }
            setLoggedInUser(userName);
            showNotification('已登录: ' + userName, 'success');
            if (!isAdminUser()) switchTab('test');
        });

        document.getElementById('btn-settings-logout')?.addEventListener('click', function() {
            const previousUser = loggedInUser;
            setLoggedInUser('');
            showNotification(previousUser ? ('已退出: ' + previousUser) : '已退出登录', 'info');
            if (previousUser && !isAdminUser(previousUser)) switchTab('settings');
        });

        function bootstrapFirstAdmin() {
            const password = String(document.getElementById('bootstrap-admin-password')?.value || '');
            const confirmPassword = String(document.getElementById('bootstrap-admin-confirm')?.value || '');
            const result = buildFirstAdminAccounts(readConfiguredAccounts(), password, confirmPassword);
            if (!result.ok) {
                // Re-read eligibility: another tab may have created the admin meanwhile.
                loadOperators();
                showNotification(result.message, 'error');
                return;
            }
            localStorage.setItem(OPERATOR_ACCOUNTS_STORAGE_KEY, JSON.stringify(result.accounts));
            localStorage.setItem('ateq_operators', JSON.stringify(result.accounts.map(function(item) { return item.username; })));
            const pwdEl = document.getElementById('bootstrap-admin-password');
            const confirmEl = document.getElementById('bootstrap-admin-confirm');
            if (pwdEl) pwdEl.value = '';
            if (confirmEl) confirmEl.value = '';
            loadOperators();
            showNotification(result.message, 'success');
        }

        document.getElementById('btn-bootstrap-admin')?.addEventListener('click', bootstrapFirstAdmin);
        document.getElementById('bootstrap-admin-confirm')?.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                bootstrapFirstAdmin();
            }
        });

        let lastScannerSession = String(localStorage.getItem('last_scanner_session') || '');
        let lastScannerSeq = Number(localStorage.getItem('last_scanner_seq') || '0');
        let scannerMarking = false;
        let scannerPolling = false;
        let scannerPollFailures = 0;
        let scannerHasResponded = false;

        // 纯函数：扫码游标推进。新会话从任意 seq 开始都接受；完成后不重置为0。
        function scannerCursorAdvance(session, seq, lastSession, lastSeq) {
            const currentSession = String(session || '');
            const currentSeq = Number(seq || 0);
            if (!currentSession || !currentSeq) return { isNew: false, session: lastSession, seq: lastSeq };
            if (currentSession !== String(lastSession || '')) {
                return { isNew: true, session: currentSession, seq: currentSeq };
            }
            return { isNew: currentSeq > Number(lastSeq || 0), session: currentSession, seq: Math.max(currentSeq, Number(lastSeq || 0)) };
        }

        function updateScannerStatus(data, extraMessage = '') {
            const el = document.getElementById('scanner-status');
            if (!el) return;
            if (!data || !data.success) {
                el.textContent = '串口扫码: 异常 - ' + ((data && data.message) || '无法读取扫码状态');
                el.className = 'mt-1 text-[11px] text-red-300 leading-snug';
                return;
            }
            if (!data.connected) {
                el.textContent = '串口扫码: 未连接 ' + (data.port || configuredScannerPort || '默认串口') + (data.error ? '，' + data.error : '，请关闭串口助手后重启Web UI');
                el.className = 'mt-1 text-[11px] text-red-300 leading-snug';
                return;
            }
            const codeText = data.code ? ('，最后: ' + data.code) : '，等待扫码';
            const timeText = data.timestamp ? ('，' + data.timestamp) : '';
            el.textContent = '串口扫码: 已连接 ' + data.port + ' ' + data.baudrate + codeText + timeText + extraMessage;
            el.className = 'mt-1 text-[11px] text-green-300 leading-snug';
        }

        async function markScanQualified(code, eventInfo) {
            if (scannerMarking || !code) return;
            scannerMarking = true;
            try {
                const body = { qr_code: code };
                if (eventInfo && eventInfo.event_id) {
                    body.event_id = eventInfo.event_id;
                    body.session_id = eventInfo.session_id;
                    body.seq = eventInfo.seq;
                }
                const response = await fetch('/api/scan_qualified', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                const result = await response.json();
                if (result.started) {
                    updateScannerStatus({
                        success: true,
                        connected: true,
                        port: result.scanner_port || configuredScannerPort || '默认串口',
                        baudrate: 9600,
                        code,
                        timestamp: ''
                    }, '，已自动启动测试');
                    showNotification('扫码成功，已自动启动测试: ' + code, 'success');
                } else if (result.duplicate) {
                    updateScannerStatus({
                        success: true,
                        connected: true,
                        port: result.scanner_port || configuredScannerPort || '默认串口',
                        baudrate: 9600,
                        code,
                        timestamp: ''
                    }, '，事件已处理（未重复启动）');
                } else if (result.confirmed) {
                    updateScannerStatus({
                        success: true,
                        connected: true,
                        port: result.scanner_port || configuredScannerPort || '默认串口',
                        baudrate: 9600,
                        code,
                        timestamp: ''
                    }, '，扫码已确认');
                } else {
                    showNotification('扫码已收到，未启动: ' + (result.message || result.error_code || ''), 'warning');
                }
            } catch (e) {
                showNotification('扫码处理异常: ' + e.message, 'error');
            } finally {
                scannerMarking = false;
            }
        }

        async function pollSerialScanner() {
            if (scannerPolling) return;
            scannerPolling = true;
            let timeoutId;
            try {
                const controller = new AbortController();
                timeoutId = setTimeout(() => controller.abort(), 3500);
                const response = await fetch('/api/scanner/status', {
                    cache: 'no-store',
                    headers: { 'Accept': 'application/json' },
                    signal: controller.signal
                });
                if (!response.ok) throw new Error('HTTP ' + response.status);
                const data = await response.json();
                scannerPollFailures = 0;
                scannerHasResponded = true;
                updateScannerStatus(data);
                if (!data.success || !data.connected || !data.code) return;
                // Leave the scanner cursor untouched until an operator logs in so the
                // pending code can be handled by the next authenticated polling cycle.
                if (!loggedInUser || !readConfiguredOperators().includes(loggedInUser)) return;

                const qrInput = document.getElementById("qr-input");
                const cursor = scannerCursorAdvance(data.session_id, data.seq,
                                                    lastScannerSession, lastScannerSeq);
                if (qrInput && data.code && cursor.isNew) {
                    qrInput.value = '';
                    qrInput.value = data.code;
                    lastScannerSession = cursor.session;
                    lastScannerSeq = cursor.seq;
                    localStorage.setItem('last_scanner_session', String(lastScannerSession));
                    localStorage.setItem('last_scanner_seq', String(lastScannerSeq));
                    syncTestContext();
                    qrInput.focus();
                    showNotification("扫码成功: " + data.code, "success");
                    await markScanQualified(data.code, {
                        event_id: data.event_id,
                        session_id: data.session_id,
                        seq: data.seq
                    });
                }
            } catch (e) {
                scannerPollFailures += 1;
                const el = document.getElementById('scanner-status');
                if (el) {
                    if (!scannerHasResponded && scannerPollFailures < 3) {
                        el.textContent = '串口扫码: 正在连接服务…';
                        el.className = 'mt-1 text-[11px] text-amber-300 leading-snug';
                    } else {
                        el.textContent = scannerPollFailures >= 3
                            ? '串口扫码: 状态服务暂不可用，正在自动重试'
                            : '串口扫码: 状态查询短暂中断，正在重试';
                        el.className = 'mt-1 text-[11px] text-amber-300 leading-snug';
                    }
                }
                console.warn('读取串口扫码状态失败，稍后自动重试:', e);
            } finally {
                if (timeoutId) clearTimeout(timeoutId);
                scannerPolling = false;
            }
        }

        pollSerialScanner();
        setInterval(pollSerialScanner, 1000);
        
        // 加载产品列表到下拉菜单（服务器为唯一权威来源）
        let serverProducts = [];
        let productConfigRevision = null;

        function renderProductOptions(products, staleNote) {
            const selector = document.getElementById('product-selector');
            const syncStatus = document.getElementById('product-sync-status');
            selector.innerHTML = '<option value="">-- 请选择产品 --</option>';
            products.forEach((product) => {
                if (!product || !product.model) return;
                const option = document.createElement('option');
                option.value = String(products.indexOf(product));
                option.textContent = product.model;
                option.dataset.program1 = product.program1 || '';
                option.dataset.program2 = product.program2 || '';
                option.dataset.switchChamber = product.switchChamber ? 'true' : 'false';
                option.dataset.labelTemplate = product.labelTemplate || '';
                option.dataset.supplierCode = product.supplierCode || '';
                selector.appendChild(option);
            });
            if (products.length > 0) {
                syncStatus.textContent = (staleNote || ('已同步 ' + products.length + ' 个产品'));
                syncStatus.className = staleNote ? 'text-xs text-amber-400' : 'text-xs text-green-400';
            } else {
                syncStatus.textContent = '未配置产品（请在设置页保存到服务器）';
                syncStatus.className = 'text-xs text-orange-400';
            }
        }

        async function fetchServerProducts() {
            try {
                const response = await fetch('/api/product_config', { cache: 'no-store' });
                const data = await response.json();
                if (data.success) {
                    serverProducts = data.products || [];
                    productConfigRevision = data.revision;
                    return true;
                }
            } catch (e) {
                console.error('服务器产品配置读取失败:', e);
            }
            return false;
        }

        function legacyLocalProducts() {
            try {
                const saved = localStorage.getItem('ateq_products');
                const parsed = saved ? JSON.parse(saved) : null;
                return Array.isArray(parsed) ? parsed : null;
            } catch (e) {
                return null;
            }
        }

        async function importLocalProductsOnce(products) {
            if (!products || !products.length) return false;
            if (localStorage.getItem('ateq_products_imported') === '1') return false;
            const agreed = window.confirm('检测到浏览器旧产品配置。是否一次性导入到服务器？（不会静默覆盖，仅此确认一次）');
            localStorage.setItem('ateq_products_imported', '1');
            if (!agreed) return false;
            try {
                const response = await fetch('/api/product_config', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ revision: productConfigRevision, products })
                });
                const result = await response.json();
                if (!result.success) {
                    showNotification('导入失败: ' + (result.message || result.error_code || ''), 'error');
                    return false;
                }
                showNotification('旧产品配置已导入服务器（program映射到program1，program2需人工确认）', 'success');
                return true;
            } catch (e) {
                showNotification('导入异常: ' + e.message, 'error');
                return false;
            }
        }

        async function loadProductSelector() {
            const selector = document.getElementById('product-selector');
            const loading = document.getElementById('product-loading');
            const syncStatus = document.getElementById('product-sync-status');

            loading.classList.remove('hidden');
            syncStatus.textContent = '同步中...';
            syncStatus.className = 'text-xs text-yellow-400';

            const ok = await fetchServerProducts();
            if (ok && serverProducts.length === 0) {
                const local = legacyLocalProducts();
                if (local && local.length) {
                    const imported = await importLocalProductsOnce(local);
                    if (imported) await fetchServerProducts();
                }
            }

            if (ok) {
                renderProductOptions(serverProducts);
            } else {
                const local = legacyLocalProducts() || [];
                renderProductOptions(local, '服务器不可用：显示浏览器缓存（只读，未同步）');
            }

            loading.classList.add('hidden');

            // 当前产品以服务器为准（服务端持久化，重启不丢）；浏览器不再用本地记忆覆盖服务器
            localStorage.removeItem('selected_product_index');
            try {
                const response = await fetch('/api/test_context', { cache: 'no-store' });
                const data = await response.json();
                if (data.success) applyServerProductSelection(data.product_model);
            } catch (e) {
                console.log('读取服务器当前产品失败:', e);
            }
        }

        // 把下拉框切到服务器当前产品，只更新显示，不回写服务器
        let applyingServerProduct = false;
        function applyServerProductSelection(model) {
            const selector = document.getElementById('product-selector');
            if (!selector || !model) return;
            if (currentProductConfig && currentProductConfig.model === model) return;
            const option = Array.from(selector.options).find(o => o.value !== '' && o.textContent === model);
            if (!option) return;
            selector.value = option.value;
            applyingServerProduct = true;
            try {
                selector.dispatchEvent(new Event('change'));
            } finally {
                applyingServerProduct = false;
            }
        }

        // 产品选择变化事件
        document.getElementById('product-selector').addEventListener('change', function() {
            const selectedIndex = this.value;
            const configDiv = document.getElementById('program-config');
            const statusContainer = document.getElementById('test-status-container');

            if (selectedIndex === '') {
                configDiv.classList.add('hidden');
                if (statusContainer) statusContainer.classList.add('hidden');
                currentProductConfig = null;
                updateAteqProgramNumberDisplay();
                // 只清本页显示；服务器当前产品不会被清空，下一次状态轮询会恢复显示
                return;
            }
            
            const selectedOption = this.options[this.selectedIndex];
            currentProductConfig = {
                model: selectedOption.textContent,
                program1: selectedOption.dataset.program1,
                program2: selectedOption.dataset.program2,
                switchChamber: selectedOption.dataset.switchChamber === 'true',
                labelTemplate: selectedOption.dataset.labelTemplate,
                supplierCode: selectedOption.dataset.supplierCode
            };
            updateAteqProgramNumberDisplay();
            
            // 显示配置信息
            document.getElementById('display-program-1').textContent = currentProductConfig.program1 || '--';
            document.getElementById('display-program-2').textContent = currentProductConfig.program2 || '--';
            document.getElementById('display-switch-chamber').textContent = currentProductConfig.switchChamber ? '是' : '否';
            document.getElementById('display-label-template').textContent = currentProductConfig.labelTemplate || '--';
            document.getElementById('display-supplier-code').textContent = currentProductConfig.supplierCode || '--';
            
            configDiv.classList.remove('hidden');
            if (statusContainer) statusContainer.classList.remove('hidden');

            // 程序写入只走后端统一入口（sync_test_context -> 串行命令通道），
            // 浏览器不再单独调用 /api/select_program 形成第二竞争路径。
            const prog1 = parseInt(currentProductConfig.program1);
            if (prog1 > 0) {
                loadTestParams(prog1);
            } else {
                loadTestParams();
            }

            // 同步测试上下文到后台；空闲时由后台准备程序号，忙时只更新下一周期配置。
            // 从服务器恢复的选择不回写。
            if (!applyingServerProduct) syncTestContext();

            // 时间参数在程序号写入完成后刷新，避免读取到上一个型号的参数

            // 清空曲线数据（新产品的测试参数可能不同）
            pressureData = [];
            leakData = [];
            testStartTime = null;
            testPhaseStartTime = null;
            lastStepCode = 0;
            if (pressureChart) { pressureChart.data.datasets[0].data = []; pressureChart.update('none'); }
            if (leakChart) { leakChart.data.datasets[0].data = []; leakChart.update('none'); }

            // 刷新测试记录列表
            loadRecords();

            // 清除之前的测试结果指示灯
            clearStatusLights();
        });
        
        // 页面加载时初始化产品选择器；状态轮询常开（无需手动点开始也能看到硬件周期）
        loadProductSelector();
        loadLineSettings();
        loadAvailableComPorts();
        loadOperators();
        initializeLoginState();
        startStatusPolling();
        
        // 实时面板在测试阶段显示泄漏变化，结束后可显示最终值；
        // 曲线只在 StepCode 6 采集，最终记录仍由后台 7／65535 证据决定。
        let lastValidPressure = null;
        let lastValidPressureUnit = null;
        let lastValidLeak = null;
        let lastValidLeakUnit = null;
        let lastStepcode = 0;
        const FINAL_IDLE_CAPTURE_MS = 1200;
        const RECORD_REFRESH_DELAY_MS = 1400;
        let monitorDataPolling = false;

        // 实时数据更新（仅展示监控面板；测试结果/合格判别以后台快照为准）
        async function updateMonitorData() {
            if (monitorDataPolling) return;
            monitorDataPolling = true;
            try {
                const response = await fetch('/api/data');
                const data = await response.json();
                if (data.busy) return;
                
                if (data.success) {
                    const capturePressureRealtime = data.step_code >= 4 && data.step_code <= 6;
                    const captureLeakRealtime = data.step_code === 6 || data.step_code === 7 || data.step_code === 65535;

                    if (capturePressureRealtime && data.pressure !== null && data.pressure !== undefined) {
                        lastValidPressure = data.pressure;
                        lastValidPressureUnit = data.pressure_unit || lastValidPressureUnit;
                        document.getElementById('pressure-value').textContent = data.pressure.toFixed(3);
                        document.getElementById('pressure-unit').textContent = data.pressure_unit || '';
                    } else if (lastValidPressure !== null) {
                        document.getElementById('pressure-value').textContent = lastValidPressure.toFixed(3);
                        document.getElementById('pressure-unit').textContent = lastValidPressureUnit;
                    }

                    if (captureLeakRealtime && data.leak !== null && data.leak !== undefined) {
                        lastValidLeak = data.leak;
                        lastValidLeakUnit = data.leak_unit || lastValidLeakUnit;
                        document.getElementById('leak-value').textContent = data.leak.toFixed(3);
                        document.getElementById('leak-unit').textContent = data.leak_unit || '';
                    } else if (lastValidLeak !== null) {
                        document.getElementById('leak-value').textContent = lastValidLeak.toFixed(3);
                        document.getElementById('leak-unit').textContent = lastValidLeakUnit;
                    }

                    document.getElementById('device-status').textContent = data.status_text;
                    document.getElementById('monitor-status').className = 'w-2 h-2 rounded-full bg-green-400';

                    // stepcode跳变到4-100范围时，新测试周期开始，清除所有指示灯
                    if (data.step_code >= 4 && data.step_code <= 100 && lastStepcode !== data.step_code && (lastStepcode === 0 || lastStepcode === 65535 || lastStepcode < 4)) {
                        clearStatusLights();
                    }

                    lastStepcode = data.step_code;

                    // 更新状态指示灯
                    updateStatusLights(data.status);
                } else {
                    document.getElementById('monitor-status').className = 'w-2 h-2 rounded-full bg-red-400';
                    document.getElementById('device-status').textContent = '连接失败';
                    // 清除所有指示灯
                    clearStatusLights();
                }
            } catch(e) {
                document.getElementById('monitor-status').className = 'w-2 h-2 rounded-full bg-gray-400';
                document.getElementById('device-status').textContent = '未连接';
                // 清除所有指示灯
                clearStatusLights();
            } finally {
                monitorDataPolling = false;
            }
        }
        
        // 更新状态指示灯
        function updateStatusLights(status) {
            // 状态字位定义（基于F5协议）
            // 位0: 合格标志
            // 位1: 测试件不合格
            // 位2: 参考件不合格
            // 位5: 循环结束
            
            const bitPass = (status & 0x0001) !== 0;
            const bitFailTest = (status & 0x0002) !== 0;
            const bitFailRef = (status & 0x0004) !== 0;
            const bitCycleEnd = (status & 0x0020) !== 0;
            
            // 更新合格标志指示灯（绿色）
            const lightPass = document.getElementById('light-pass');
            const statusPass = document.getElementById('status-pass');
            if (bitPass && bitCycleEnd) {
                lightPass.className = 'w-6 h-6 rounded-full bg-green-500 shadow-lg shadow-green-500/50 transition-all duration-300';
                statusPass.textContent = '已激活';
                statusPass.className = 'text-xs text-green-400';
            } else {
                lightPass.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusPass.textContent = '未激活';
                statusPass.className = 'text-gray-500 text-xs';
            }
            
            // 更新测试件不合格指示灯（红色）
            const lightFailTest = document.getElementById('light-fail-test');
            const statusFailTest = document.getElementById('status-fail-test');
            if (bitFailTest && bitCycleEnd) {
                lightFailTest.className = 'w-6 h-6 rounded-full bg-red-500 shadow-lg shadow-red-500/50 transition-all duration-300';
                statusFailTest.textContent = '已激活';
                statusFailTest.className = 'text-xs text-red-400';
            } else {
                lightFailTest.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusFailTest.textContent = '未激活';
                statusFailTest.className = 'text-gray-500 text-xs';
            }
            
            // 更新参考件不合格指示灯（橙色）
            const lightFailRef = document.getElementById('light-fail-ref');
            const statusFailRef = document.getElementById('status-fail-ref');
            if (bitFailRef && bitCycleEnd) {
                lightFailRef.className = 'w-6 h-6 rounded-full bg-orange-500 shadow-lg shadow-orange-500/50 transition-all duration-300';
                statusFailRef.textContent = '已激活';
                statusFailRef.className = 'text-xs text-orange-400';
            } else {
                lightFailRef.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusFailRef.textContent = '未激活';
                statusFailRef.className = 'text-gray-500 text-xs';
            }
            
            // 更新循环结束标志指示灯（蓝色）
            const lightCycleEnd = document.getElementById('light-cycle-end');
            const statusCycleEnd = document.getElementById('status-cycle-end');
            if (bitCycleEnd) {
                lightCycleEnd.className = 'w-6 h-6 rounded-full bg-blue-500 shadow-lg shadow-blue-500/50 transition-all duration-300';
                statusCycleEnd.textContent = '已激活';
                statusCycleEnd.className = 'text-xs text-blue-400';
            } else {
                lightCycleEnd.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusCycleEnd.textContent = '未激活';
                statusCycleEnd.className = 'text-gray-500 text-xs';
            }
        }
        
        // 清除所有指示灯
        function clearStatusLights() {
            const lights = ['light-pass', 'light-fail-test', 'light-fail-ref', 'light-cycle-end'];
            const statuses = ['status-pass', 'status-fail-test', 'status-fail-ref', 'status-cycle-end'];
            
            lights.forEach((id, index) => {
                const light = document.getElementById(id);
                const status = document.getElementById(statuses[index]);
                if (light) {
                    light.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                }
                if (status) {
                    status.textContent = '未激活';
                    status.className = 'text-gray-500 text-xs';
                }
            });
            
            // 同时清除程序合格指示灯
            clearProgramPassLights();
        }
        
        // 更新程序合格指示灯
        function updateProgramPassLights(program1Pass, program2Pass) {
            // 更新程序号1合格指示灯（绿色）
            const lightProgram1 = document.getElementById('light-program1-pass');
            const statusProgram1 = document.getElementById('status-program1-pass');
            if (program1Pass) {
                lightProgram1.className = 'w-6 h-6 rounded-full bg-green-500 shadow-lg shadow-green-500/50 transition-all duration-300';
                statusProgram1.textContent = '合格';
                statusProgram1.className = 'text-xs text-green-400';
            } else {
                lightProgram1.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusProgram1.textContent = '未激活';
                statusProgram1.className = 'text-gray-500 text-xs';
            }
            
            // 更新程序号2合格指示灯（绿色）
            const lightProgram2 = document.getElementById('light-program2-pass');
            const statusProgram2 = document.getElementById('status-program2-pass');
            if (program2Pass) {
                lightProgram2.className = 'w-6 h-6 rounded-full bg-green-500 shadow-lg shadow-green-500/50 transition-all duration-300';
                statusProgram2.textContent = '合格';
                statusProgram2.className = 'text-xs text-green-400';
            } else {
                lightProgram2.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                statusProgram2.textContent = '未激活';
                statusProgram2.className = 'text-gray-500 text-xs';
            }
        }

        function updateProgramResultText(elementId, resultValue, passValue) {
            const el = document.getElementById(elementId);
            if (!el) {
                return;
            }

            if (resultValue === null || resultValue === undefined || resultValue === '') {
                el.textContent = '--';
                el.className = 'text-yellow-400 font-medium';
                return;
            }

            const normalized = String(resultValue).toUpperCase();
            el.textContent = normalized;

            if (normalized === 'PASS' || passValue === true) {
                el.className = 'text-green-400 font-medium';
            } else if (normalized === 'FAIL' || passValue === false) {
                el.className = 'text-red-400 font-medium';
            } else {
                el.className = 'text-yellow-400 font-medium';
            }
        }
        
        // 清除程序合格指示灯
        function clearProgramPassLights() {
            const lights = ['light-program1-pass', 'light-program2-pass'];
            const statuses = ['status-program1-pass', 'status-program2-pass'];
            
            lights.forEach((id, index) => {
                const light = document.getElementById(id);
                const status = document.getElementById(statuses[index]);
                if (light) {
                    light.className = 'w-6 h-6 rounded-full bg-gray-600 transition-all duration-300 shadow-lg';
                }
                if (status) {
                    status.textContent = '未激活';
                    status.className = 'text-gray-500 text-xs';
                }
            });
            
            // 清除程序结果数据
            document.getElementById('program1-pressure').textContent = '--';
            document.getElementById('program1-pressure-unit').textContent = '--';
            document.getElementById('program1-leak').textContent = '--';
            document.getElementById('program1-leak-unit').textContent = '--';
            document.getElementById('program1-result').textContent = '--';
            document.getElementById('program1-result').className = 'text-yellow-400 font-medium';
            document.getElementById('program2-pressure').textContent = '--';
            document.getElementById('program2-pressure-unit').textContent = '--';
            document.getElementById('program2-leak').textContent = '--';
            document.getElementById('program2-leak-unit').textContent = '--';
            document.getElementById('program2-result').textContent = '--';
            document.getElementById('program2-result').className = 'text-yellow-400 font-medium';
        }
        
        // 每秒更新一次数据
        setInterval(updateMonitorData, 1000);
        updateMonitorData();
        
        // 页面加载即启动状态轮询，供仪器硬件按钮启动的测试刷新状态
        startStatusPolling();
        
        // 曲线卡片拖拽
        interact('#chart-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('chart-card');
                        localStorage.setItem('chart_card_v2', JSON.stringify({
                            left: parseInt(card.style.left) || 50,
                            top: parseInt(card.style.top) || 250,
                            width: parseInt(card.style.width) || 700,
                            height: parseInt(card.style.minHeight) || 400
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 500, height: 300 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                        // 调整图表大小
                        if (pressureChart) pressureChart.resize();
                        if (leakChart) leakChart.resize();
                    },
                    end(e) {
                        const card = document.getElementById('chart-card');
                        localStorage.setItem('chart_card_v2', JSON.stringify({
                            left: parseInt(card.style.left) || 50,
                            top: parseInt(card.style.top) || 250,
                            width: parseInt(card.style.width) || 700,
                            height: parseInt(card.style.minHeight) || 400
                        }));
                    }
                }
            });
        
        // 加载曲线卡片位置
        const chartSaved = localStorage.getItem('chart_card_v2');
        if (chartSaved) {
            try {
                const state = JSON.parse(chartSaved);
                const card = document.getElementById('chart-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = state.height + 'px';
            } catch(e) {}
        }
        
        // 记录卡片拖拽
        interact('#record-card')
            .draggable({
                inertia: true,
                allowFrom: '.drag-handle',
                ignoreFrom: '.record-col-resizer, table, thead, tbody, th, td, .overflow-auto, button, input, select, textarea',
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('record-card');
                        localStorage.setItem('record_card_v2', JSON.stringify({
                            left: parseInt(card.style.left) || 800,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 450,
                            height: parseInt(card.style.minHeight) || 500
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 350, height: 20 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end(e) {
                        const card = document.getElementById('record-card');
                        localStorage.setItem('record_card_v2', JSON.stringify({
                            left: parseInt(card.style.left) || 800,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 450,
                            height: parseInt(card.style.minHeight) || 500
                        }));
                    }
                }
            });
        
        // 加载记录卡片位置
        const recordSaved = localStorage.getItem('record_card_v2');
        if (recordSaved) {
            try {
                const state = JSON.parse(recordSaved);
                const card = document.getElementById('record-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = state.height + 'px';
            } catch(e) {}
        }
        
        // 状态指示灯卡片拖拽
        interact('#status-lights-card')
            .draggable({
                inertia: true,
                modifiers: [interact.modifiers.restrictRect({ restriction: '#canvas', endOnly: true })],
                listeners: {
                    start(e) { e.target.classList.add('dragging'); },
                    move(e) {
                        const target = e.target;
                        target.style.left = (parseFloat(target.style.left) || 0) + e.dx + 'px';
                        target.style.top = (parseFloat(target.style.top) || 0) + e.dy + 'px';
                    },
                    end(e) { 
                        e.target.classList.remove('dragging'); 
                        const card = document.getElementById('status-lights-card');
                        localStorage.setItem('status_lights_card_v2', JSON.stringify({
                            left: parseInt(card.style.left) || 720,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 320,
                            height: parseInt(card.style.minHeight) || 200
                        }));
                    }
                }
            })
            .resizable({
                edges: { left: false, right: true, bottom: true, top: false },
                modifiers: [interact.modifiers.restrictSize({ min: { width: 280, height: 180 } })],
                listeners: {
                    move(e) {
                        e.target.style.width = e.rect.width + 'px';
                        e.target.style.minHeight = e.rect.height + 'px';
                    },
                    end(e) {
                        const card = document.getElementById('status-lights-card');
                        localStorage.setItem('status_lights_card_v2', JSON.stringify({
                            left: parseInt(card.style.left) || 720,
                            top: parseInt(card.style.top) || 50,
                            width: parseInt(card.style.width) || 320,
                            height: parseInt(card.style.minHeight) || 200
                        }));
                    }
                }
            });
        
        // 设置页两张卡片为流式布局，固定排列不重叠，无需拖拽/缩放

        interact('#settings-auth-bar')
            .draggable({
                ignoreFrom: 'input, button, select, option, textarea',
                listeners: {
                    start(event) {
                        const settingsPage = document.getElementById('page-settings');
                        const authBar = event.target;
                        if (!settingsPage || !settingsPage.classList.contains('settings-locked')) {
                            return;
                        }

                        if (!authBar.dataset.dragFree) {
                            const pageRect = settingsPage.getBoundingClientRect();
                            const barRect = authBar.getBoundingClientRect();
                            authBar.style.left = `${barRect.left - pageRect.left}px`;
                            authBar.style.top = `${barRect.top - pageRect.top}px`;
                            authBar.style.transform = 'none';
                            authBar.dataset.dragFree = '1';
                        }

                        authBar.classList.add('dragging');
                    },
                    move(event) {
                        const settingsPage = document.getElementById('page-settings');
                        const authBar = event.target;
                        if (!settingsPage || !settingsPage.classList.contains('settings-locked')) {
                            return;
                        }

                        const maxLeft = Math.max(0, settingsPage.clientWidth - authBar.offsetWidth);
                        const maxTop = Math.max(0, settingsPage.clientHeight - authBar.offsetHeight);
                        const nextLeft = Math.min(maxLeft, Math.max(0, (parseFloat(authBar.style.left) || 0) + event.dx));
                        const nextTop = Math.min(maxTop, Math.max(0, (parseFloat(authBar.style.top) || 0) + event.dy));
                        authBar.style.left = `${nextLeft}px`;
                        authBar.style.top = `${nextTop}px`;
                    },
                    end(event) {
                        event.target.classList.remove('dragging');
                    }
                }
            });

        // 加载状态指示灯卡片位置
        const statusLightsSaved = localStorage.getItem('status_lights_card_v2');
        if (statusLightsSaved) {
            try {
                const state = JSON.parse(statusLightsSaved);
                const card = document.getElementById('status-lights-card');
                if (state.left) card.style.left = state.left + 'px';
                if (state.top) card.style.top = state.top + 'px';
                if (state.width) card.style.width = state.width + 'px';
                if (state.height) card.style.minHeight = state.height + 'px';
            } catch(e) {}
        }
        
        // 初始化图表
        let pressureChart, leakChart;
        let testParams = { fill_time: 3, stab_time: 2, test_time: 5 }; // 默认参数
        
        // 图表配置
        const chartOptions = {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: {
                legend: { display: false }
            },
            scales: {
                x: {
                    type: 'linear',
                    grid: { color: 'rgba(255,255,255,0.1)' },
                    ticks: { color: '#9ca3af', font: { size: 10 } }
                },
                y: {
                    grid: { color: 'rgba(255,255,255,0.1)' },
                    ticks: { color: '#9ca3af', font: { size: 10 } }
                }
            },
            elements: {
                point: { radius: 0, hitRadius: 5, hoverRadius: 3 },
                line: { tension: 0.3, borderWidth: 2 }
            }
        };
        
        function initCharts() {
            const pressureCtx = document.getElementById('pressure-chart').getContext('2d');
            const leakCtx = document.getElementById('leak-chart').getContext('2d');
            
            // 压力曲线 - X轴范围: 0 到 fill+stab+test
            const totalTime = testParams.fill_time + testParams.stab_time + testParams.test_time;
            pressureChart = new Chart(pressureCtx, {
                type: 'line',
                data: {
                    datasets: [{
                        label: '压力',
                        data: [],
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        fill: true
                    }]
                },
                options: {
                    ...chartOptions,
                    scales: {
                        ...chartOptions.scales,
                        x: {
                            ...chartOptions.scales.x,
                            min: 0,
                            max: totalTime,
                            title: { display: true, text: '时间 (s)', color: '#6b7280', font: { size: 10 } }
                        },
                        y: {
                            ...chartOptions.scales.y,
                            title: { display: true, text: '压力', color: '#6b7280', font: { size: 10 } }
                        }
                    }
                }
            });
            
            // 泄漏量曲线 - X轴范围: 0 到 test_time
            leakChart = new Chart(leakCtx, {
                type: 'line',
                data: {
                    datasets: [{
                        label: '泄漏量',
                        data: [],
                        borderColor: '#06b6d4',
                        backgroundColor: 'rgba(6, 182, 212, 0.1)',
                        fill: true
                    }]
                },
                options: {
                    ...chartOptions,
                    scales: {
                        ...chartOptions.scales,
                        x: {
                            ...chartOptions.scales.x,
                            min: 0,
                            max: testParams.test_time,
                            title: { display: true, text: 'Test Time (s)', color: '#6b7280', font: { size: 10 } }
                        },
                        y: {
                            ...chartOptions.scales.y,
                            title: { display: true, text: '泄漏量', color: '#6b7280', font: { size: 10 } }
                        }
                    }
                }
            });
        }
        
        // 获取测试参数
        async function loadTestParams(programNumber = null) {
            try {
                const selectedProgram = programNumber || (currentProductConfig ? parseInt(currentProductConfig.program1) : null);
                const paramsUrl = selectedProgram > 0 ? `/api/params?program=${selectedProgram}` : '/api/params';
                const response = await fetch(paramsUrl);
                const data = await response.json();
                if (data.success) {
                    testParams = data;
                    document.getElementById('fill-time').textContent = data.fill_time;
                    document.getElementById('stab-time').textContent = data.stab_time;
                    document.getElementById('test-time').textContent = data.test_time;
                    // 更新图表X轴范围
                    if (pressureChart) {
                        const totalTime = data.fill_time + data.stab_time + data.test_time;
                        pressureChart.options.scales.x.max = totalTime;
                        pressureChart.update('none');
                    }
                    if (leakChart) {
                        leakChart.options.scales.x.max = data.test_time;
                        leakChart.update('none');
                    }
                }
            } catch(e) {}
        }
        
        // 更新曲线数据
        let pressureData = [];
        let leakData = [];
        let testStartTime = null;
        let testPhaseStartTime = null;
        let lastStepCode = 0;
        let postTestIdleCaptureStartedAt = null;
        
        async function updateCharts() {
            try {
                const response = await fetch('/api/data');
                const data = await response.json();
                
                if (data.success) {
                    const now = Date.now() / 1000;
                    let stepCode = data.step_code != null ? data.step_code : 65535;

                    // 曲线轴标题跟随仪器当前单位（避免轴/数值/记录单位不一致）
                    try {
                        if (pressureChart && data.pressure_unit &&
                                pressureChart.options.scales.y.title.text !== '压力 (' + data.pressure_unit + ')') {
                            pressureChart.options.scales.y.title.text = '压力 (' + data.pressure_unit + ')';
                            pressureChart.update('none');
                        }
                        if (leakChart && data.leak_unit &&
                                leakChart.options.scales.y.title.text !== '泄漏量 (' + data.leak_unit + ')') {
                            leakChart.options.scales.y.title.text = '泄漏量 (' + data.leak_unit + ')';
                            leakChart.update('none');
                        }
                    } catch (axisErr) { /* 轴标题仅展示用，失败不影响数据 */ }

                    // 调试: 显示实际 step_code
                    console.log('API数据:', {step_code: stepCode, pressure: data.pressure, leak: data.leak});
                    
                    // 正确的step code定义
                    const stepNames = {
                        0: 'Pre-fill', 1: 'Pre-dump', 2: 'Sealed fill', 3: 'Sealed stab',
                        4: 'Fill', 5: 'Stab', 6: 'Test', 7: 'Dump', 65535: '空闲'
                    };
                    const statusEl = document.getElementById('chart-status');
                    statusEl.textContent = `${stepNames[stepCode] || stepCode} (${stepCode})`;
                    
                    // Fill开始 (step_code = 4)
                    const isFillStart = stepCode === 4 && lastStepCode !== 4;
                    const isTestStart = stepCode === 6 && lastStepCode !== 6;
                    const isTestEnd = lastStepCode === 6 && stepCode !== 6;
                    const isPostTestIdleCaptureStart = stepCode === 65535 && lastStepCode !== 65535;
                    const postTestIdleCaptureActive =
                        isPostTestIdleCaptureStart ||
                        (
                            stepCode === 65535 &&
                            postTestIdleCaptureStartedAt !== null &&
                            (now - postTestIdleCaptureStartedAt) <= (FINAL_IDLE_CAPTURE_MS / 1000)
                        );
                    
                    // Fill开始时清空并开始记录
                    if (isFillStart) {
                        testStartTime = now;
                        testPhaseStartTime = null;
                        postTestIdleCaptureStartedAt = null;
                        pressureData = [];
                        leakData = [];
                        // 清空泄漏量曲线
                        leakChart.data.datasets[0].data = [];
                        leakChart.update('none');
                        console.log('Fill开始(stepcode=4), 清空压力和泄漏量数据');
                    }
                    
                    // 过程曲线与最终取值分离：压力曲线覆盖 4..7，泄漏曲线只覆盖 Test=6。
                    if (((stepCode >= 4 && stepCode <= 7) || postTestIdleCaptureActive) && testStartTime) {
                        const elapsed = now - testStartTime;
                        const totalTime = testParams.fill_time + testParams.stab_time + testParams.test_time;
                        
                        // 压力曲线只使用活动阶段，最终压力数值仍由后台 StepCode 6 证据决定。
                        if (stepCode >= 4 && stepCode <= 7 && elapsed <= totalTime) {
                            pressureData.push({ x: parseFloat(elapsed.toFixed(2)), y: data.pressure });
                            pressureChart.data.datasets[0].data = pressureData;
                            pressureChart.update('none');
                        }
                        
                        // 泄漏过程曲线只在 Test=6 采集；最终泄漏数值由后台 7/65535 判定。
                        if (stepCode === 6) {
                            if (isTestStart || testPhaseStartTime === null) {
                                testPhaseStartTime = now;
                                leakData = [];
                                leakChart.data.datasets[0].data = [];
                            }
                            const testElapsed = now - testPhaseStartTime;
                            if (testElapsed >= 0 && testElapsed <= (testParams.test_time + FINAL_IDLE_CAPTURE_MS / 1000)) {
                                leakData.push({ x: parseFloat(testElapsed.toFixed(2)), y: data.leak });
                                leakChart.data.datasets[0].data = leakData;
                                leakChart.update('none');
                            }
                        }
                        
                        statusEl.className = 'text-xs px-2 py-0.5 rounded bg-green-600 text-white';
                    } else {
                        statusEl.className = 'text-xs px-2 py-0.5 rounded bg-gray-600 text-gray-300';
                    }
                    
                    // 离开 Test=6 后停止泄漏曲线采集，但保留已有曲线供查看。
                    if (isTestEnd) {
                        console.log('Test结束, 保持曲线');
                    }
                    if (isPostTestIdleCaptureStart) {
                        postTestIdleCaptureStartedAt = now;
                    }

                    if (
                        postTestIdleCaptureStartedAt !== null &&
                        stepCode === 65535 &&
                        !postTestIdleCaptureActive
                    ) {
                        testStartTime = null;
                        testPhaseStartTime = null;
                        postTestIdleCaptureStartedAt = null;
                    }
                    
                    lastStepCode = stepCode;
                }
            } catch(e) {
                console.error('更新曲线错误:', e);
            }
        }
        
        // 初始化
        initCharts();
        loadTestParams();
        setInterval(updateCharts, 200); // 每200ms更新一次曲线
        setInterval(loadTestParams, 5000); // 每5秒刷新一次参数
        
        loadState();
        
        // ========== 测试记录卡片功能 ==========
        let lastTestData = null;
        let currentSerialNumber = null;
        let recordsFilterKey = null;
        let renderedRecordKeys = new Set();
        const frozenProgram1Results = new Map();
        const RECORD_COLUMN_WIDTHS_KEY = 'test-record-column-widths-v1';
        const DEFAULT_RECORD_COLUMN_WIDTHS = [52, 165, 120, 170, 110, 110, 110, 110, 90, 120, 96];
        let recordColumnWidths = [...DEFAULT_RECORD_COLUMN_WIDTHS];
        let activeRecordColumnResize = null;
        let recordsColumnResizeInitialized = false;

        function clampRecordColumnWidth(index, width) {
            const nextWidth = Number(width);
            if (!Number.isFinite(nextWidth)) {
                return DEFAULT_RECORD_COLUMN_WIDTHS[index] || 1;
            }
            return Math.max(1, Math.round(nextWidth));
        }

        function loadRecordColumnWidths() {
            try {
                // 列宽语义升级（Serial/QR 列按容器弹性扩展）后，旧的手动列宽整体作废一次
                const widthsVersion = localStorage.getItem(RECORD_COLUMN_WIDTHS_KEY + ':v');
                if (widthsVersion !== '2') {
                    localStorage.removeItem(RECORD_COLUMN_WIDTHS_KEY);
                    localStorage.setItem(RECORD_COLUMN_WIDTHS_KEY + ':v', '2');
                    return;
                }
                const saved = localStorage.getItem(RECORD_COLUMN_WIDTHS_KEY);
                if (!saved) return;

                const parsed = JSON.parse(saved);
                if (!Array.isArray(parsed) || parsed.length !== DEFAULT_RECORD_COLUMN_WIDTHS.length) {
                    return;
                }

                recordColumnWidths = DEFAULT_RECORD_COLUMN_WIDTHS.map((defaultWidth, index) => {
                    const parsedWidth = Number(parsed[index]);
                    return Number.isFinite(parsedWidth)
                        ? clampRecordColumnWidth(index, parsedWidth)
                        : defaultWidth;
                });
            } catch (error) {
                console.warn('加载测试记录列宽失败:', error);
            }
        }

        function persistRecordColumnWidths() {
            try {
                localStorage.setItem(RECORD_COLUMN_WIDTHS_KEY, JSON.stringify(recordColumnWidths));
            } catch (error) {
                console.warn('保存测试记录列宽失败:', error);
            }
        }

        function applyRecordColumnWidths() {
            const table = document.getElementById('records-display-table');
            const cols = document.querySelectorAll('#records-table-colgroup col');
            if (!table || !cols.length) return;

            const widths = cols.map ? [] : null;
            const baseWidths = [];
            let totalWidth = 0;
            cols.forEach((col, index) => {
                const width = clampRecordColumnWidth(index, recordColumnWidths[index] ?? DEFAULT_RECORD_COLUMN_WIDTHS[index]);
                recordColumnWidths[index] = width;
                baseWidths.push(width);
                totalWidth += width;
            });

            const containerWidth = table.parentElement?.clientWidth || 0;
            // 容器比列宽总和多出富余时，按比例分摊给 Serial No.(2) 和 QR Code(3) 两列，
            // 让长序列号/二维码尽量完整显示且表格填满整行；容器不足时维持基准宽度
            if (containerWidth > totalWidth && totalWidth > 0) {
                const flexIndexes = [2, 3];
                const flexBase = flexIndexes.reduce((s, i) => s + baseWidths[i], 0);
                let surplus = containerWidth - totalWidth;
                flexIndexes.forEach((i, n) => {
                    const share = (n === flexIndexes.length - 1)
                        ? surplus
                        : Math.round(surplus * (baseWidths[i] / flexBase));
                    surplus -= share;
                    baseWidths[i] += share;
                });
            }
            baseWidths.forEach((width, index) => {
                cols[index].style.width = `${width}px`;
            });
            const appliedWidth = baseWidths.reduce((s, w) => s + w, 0);
            table.style.width = `${appliedWidth}px`;
        }

        function handleRecordColumnResizeMove(event) {
            if (!activeRecordColumnResize) return;

            const nextWidth = clampRecordColumnWidth(
                activeRecordColumnResize.index,
                activeRecordColumnResize.startWidth + (event.clientX - activeRecordColumnResize.startX)
            );

            if (nextWidth === recordColumnWidths[activeRecordColumnResize.index]) {
                return;
            }

            recordColumnWidths[activeRecordColumnResize.index] = nextWidth;
            applyRecordColumnWidths();
        }

        function stopRecordColumnResize() {
            if (!activeRecordColumnResize) return;

            activeRecordColumnResize = null;
            document.body.classList.remove('records-col-resizing');
            document.removeEventListener('pointermove', handleRecordColumnResizeMove);
            document.removeEventListener('pointerup', stopRecordColumnResize);
            document.removeEventListener('pointercancel', stopRecordColumnResize);
            persistRecordColumnWidths();
        }

        function startRecordColumnResize(event) {
            const handle = event.target.closest('.record-col-resizer');
            if (!handle) return;

            event.preventDefault();
            event.stopPropagation();

            const index = Number(handle.dataset.colIndex);
            if (!Number.isFinite(index)) return;

            activeRecordColumnResize = {
                index,
                startX: event.clientX,
                startWidth: recordColumnWidths[index] ?? DEFAULT_RECORD_COLUMN_WIDTHS[index]
            };

            document.body.classList.add('records-col-resizing');
            document.addEventListener('pointermove', handleRecordColumnResizeMove);
            document.addEventListener('pointerup', stopRecordColumnResize);
            document.addEventListener('pointercancel', stopRecordColumnResize);
        }

        function initResizableRecordsTable() {
            loadRecordColumnWidths();
            applyRecordColumnWidths();

            if (recordsColumnResizeInitialized) {
                return;
            }

            document.querySelectorAll('#records-display-table .record-col-resizer').forEach(handle => {
                handle.addEventListener('pointerdown', startRecordColumnResize);
            });

            recordsColumnResizeInitialized = true;
        }
        
        function getRecordFilterState() {
            const selector = document.getElementById('product-selector');
            const selectedProduct = selector?.value || '';
            const selectedProductText = selector?.selectedOptions[0]?.text || '';
            const filterKey = selectedProduct ? selectedProductText : '__ALL__';
            return { selectedProduct, selectedProductText, filterKey };
        }

        function buildRecordKey(record) {
            if (record && record.id !== null && record.id !== undefined) {
                return `id:${record.id}`;
            }
            return [
                record?.test_time || '',
                record?.daily_serial || '',
                record?.serial_number || '',
                record?.qr_code || ''
            ].join('|');
        }

        function getProgram1ResultSnapshot() {
            const pressure = document.getElementById('program1-pressure')?.textContent?.trim() || '--';
            const leak = document.getElementById('program1-leak')?.textContent?.trim() || '--';
            const pressureUnit = document.getElementById('program1-pressure-unit')?.textContent?.trim() || '';
            const leakUnit = document.getElementById('program1-leak-unit')?.textContent?.trim() || '';

            if (pressure === '--' && leak === '--') {
                return null;
            }

            return { pressure, leak, pressureUnit, leakUnit };
        }

        function formatRecordTimestamp(testTime) {
            const date = new Date(testTime);
            return date.getFullYear() + '-' +
                String(date.getMonth() + 1).padStart(2, '0') + '-' +
                String(date.getDate()).padStart(2, '0') + ' ' +
                String(date.getHours()).padStart(2, '0') + ':' +
                String(date.getMinutes()).padStart(2, '0') + ':' +
                String(date.getSeconds()).padStart(2, '0');
        }

        function formatRecordValue(value, unit) {
            if (value === null || value === undefined) {
                return '-';
            }
            return `${value.toFixed(3)} ${unit}`;
        }

        function renderRecordRow(record) {
            const recordKey = buildRecordKey(record);
            const snapshot = frozenProgram1Results.get(recordKey);
            // 单位一律以记录自身的 *_unit 列为准（保存当次仪器实际单位）；
            // 冻结快照只提供数值，不提供单位（快照取自 DOM，曾把压力单位串到泄漏上）
            const p1PressureUnit = record.pressure1_unit || '';
            const p1LeakUnit = record.leak1_unit || '';
            const p2PressureUnit = record.pressure2_unit || '';
            const p2LeakUnit = record.leak2_unit || '';
            const pressure1Text = snapshot?.pressure && snapshot.pressure !== '--'
                ? `${snapshot.pressure} ${p1PressureUnit}`
                : formatRecordValue(record.pressure1, p1PressureUnit);
            const leak1Text = snapshot?.leak && snapshot.leak !== '--'
                ? `${snapshot.leak} ${p1LeakUnit}`
                : formatRecordValue(record.leak1, p1LeakUnit);
            const pressure2Text = formatRecordValue(record.pressure2, p2PressureUnit);
            const leak2Text = formatRecordValue(record.leak2, p2LeakUnit);
            const formattedTime = formatRecordTimestamp(record.test_time);

            return `
                            <tr class="hover:bg-gray-600/30" data-record-key="${recordKey}">
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${record.model_serial ?? record.daily_serial ?? '-'}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${formattedTime}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${record.serial_number || '-'}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center text-xs">${record.qr_code || '-'}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${pressure1Text}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${leak1Text}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${pressure2Text}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center font-mono text-xs">${leak2Text}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center">
                                    <span class="px-1 py-0.5 rounded font-medium text-xs ${record.test_result === 'PASS' ? 'bg-green-600 text-white' : 'bg-red-600 text-white'}">
                                        ${record.test_result}
                                    </span>
                                </td>
                                <td class="py-1 px-1 border border-gray-600 text-center text-xs">${record.product_model || '-'}</td>
                                <td class="py-1 px-1 border border-gray-600 text-center text-xs">${record.operator || '-'}</td>
                            </tr>
                        `;
        }

        function updateRecordStats(records) {
            const total = records.length;
            const passCount = records.filter(r => r.test_result === 'PASS').length;
            const passRate = total > 0 ? Math.round((passCount / total) * 100) : 0;

            document.getElementById('today-count').textContent = `今日: ${total}`;
            document.getElementById('pass-rate').textContent = `合格率: ${passRate}%`;
        }

        function renderRecordsTable(records) {
            const tbody = document.getElementById('records-table');
            if (!records || records.length === 0) {
                tbody.innerHTML = '<tr><td colspan="11" class="py-8 text-center text-gray-500">暂无今日记录</td></tr>';
                renderedRecordKeys = new Set();
                return;
            }

            tbody.innerHTML = records.map(renderRecordRow).join('');
            renderedRecordKeys = new Set(records.map(buildRecordKey));
        }

        function prependNewRecordRows(records) {
            if (!records || records.length === 0) {
                return;
            }

            const tbody = document.getElementById('records-table');
            const rowsHtml = records.map(renderRecordRow).join('');
            const hasPlaceholder = tbody.querySelector('td[colspan="11"]');

            if (hasPlaceholder) {
                tbody.innerHTML = rowsHtml;
            } else {
                tbody.insertAdjacentHTML('afterbegin', rowsHtml);
            }

            records.forEach(record => renderedRecordKeys.add(buildRecordKey(record)));
        }
        
        // 监听测试结束，保存测试数据
        let testEndDetected = false;
        const originalUpdateCharts = updateCharts;
        updateCharts = async function() {
            await originalUpdateCharts();
            
            // 检测测试结束 (从Test=6变为其他)
            if (lastStepCode === 6 && stepCode !== 6) {
                // 测试刚结束，获取最终数据
                try {
                    const response = await fetch('/api/data');
                    const data = await response.json();
                    if (data.success) {
                        lastTestData = {
                            pressure1: data.pressure,
                            leak1: data.leak,
                            pressure2: null,
                            leak2: null,
                            result: 'PASS' // 根据泄漏量判断
                        };
                    }
                } catch(e) {}
            }
        };
        
        // 加载当日记录列表
        async function loadRecords(options = {}) {
            try {
                const { forceReload = false } = options;
                const response = await fetch('/api/records/today');
                const data = await response.json();
                
                if (data.success) {
                    // 获取当前选中的产品型号
                    const { selectedProduct, selectedProductText, filterKey } = getRecordFilterState();
                    let visibleRecords = data.records;
                    if (selectedProduct && selectedProduct !== '') {
                        visibleRecords = data.records.filter(r => r.product_model === selectedProductText);
                    }

                    const sortedVisibleRecords = visibleRecords
                        .slice()
                        .sort((a, b) => new Date(b.test_time) - new Date(a.test_time));

                    updateRecordStats(sortedVisibleRecords);

                    const currentKeys = new Set(sortedVisibleRecords.map(buildRecordKey));
                    const filterChanged = recordsFilterKey !== filterKey;
                    const hasRemovedRecords = Array.from(renderedRecordKeys).some(key => !currentKeys.has(key));

                    if (forceReload || filterChanged || renderedRecordKeys.size === 0 || hasRemovedRecords) {
                        renderRecordsTable(sortedVisibleRecords);
                        recordsFilterKey = filterKey;
                        return;
                    }

                    const incomingRecords = sortedVisibleRecords.filter(record => !renderedRecordKeys.has(buildRecordKey(record)));
                    if (incomingRecords.length > 0) {
                        const program1Snapshot = getProgram1ResultSnapshot();
                        if (program1Snapshot) {
                            frozenProgram1Results.set(buildRecordKey(incomingRecords[0]), program1Snapshot);
                        }
                        prependNewRecordRows(incomingRecords);
                    }

                    recordsFilterKey = filterKey;
                    renderedRecordKeys = currentKeys;
                    return;
                }
            } catch(e) {
                console.error('加载记录失败:', e);
            }
        }
        
        // 刷新记录
        document.getElementById('btn-refresh').addEventListener('click', () => loadRecords({ forceReload: true }));
        
        // 初始加载
        initResizableRecordsTable();
        loadRecords({ forceReload: true });
        setInterval(loadRecords, 10000); // 每10秒刷新
        // 标签页切换
        document.getElementById('tab-test').addEventListener('click', function() {
            if (requireWorkLogin()) switchTab('test');
        });
        
        document.getElementById('tab-settings').addEventListener('click', function() {
            switchTab('settings');
        });
        
        document.getElementById('tab-query').addEventListener('click', function() {
            if (requireWorkLogin()) switchTab('query');
        });
        
        // 显示通知
        function showNotification(message, type = 'info') {
            const notification = document.createElement('div');
            const colors = {
                info: 'bg-blue-600',
                success: 'bg-green-600',
                error: 'bg-red-600',
                warning: 'bg-yellow-600'
            };
            
            notification.className = `fixed top-20 right-4 ${colors[type]} text-white px-4 py-2 rounded-lg shadow-lg z-[90] transition-opacity duration-300`;
            notification.textContent = message;
            document.body.appendChild(notification);
            
            setTimeout(() => {
                notification.style.opacity = '0';
                setTimeout(() => notification.remove(), 300);
            }, 3000);
        }
        
        function switchTab(tabName) {
            // 隐藏所有标签页
            document.querySelectorAll('.tab-pane').forEach(pane => {
                pane.classList.remove('active');
            });
            
            // 移除所有标签按钮的活动状态
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.classList.remove('active');
                btn.classList.add('text-gray-400');
                btn.classList.remove('text-white');
                btn.classList.remove('border-blue-500');
                btn.classList.add('border-transparent');
            });
            
            // 显示选中的标签页
            document.getElementById(`page-${tabName}`).classList.add('active');
            if (tabName === 'query') {
                applyQueryColumnWidths();
            }
            
            // 激活选中的标签按钮
            const activeBtn = document.getElementById(`tab-${tabName}`);
            activeBtn.classList.add('active');
            activeBtn.classList.remove('text-gray-400');
            activeBtn.classList.add('text-white');
            activeBtn.classList.add('border-blue-500');
            activeBtn.classList.remove('border-transparent');
            
            // 切换到设置页面时加载设置
            if (tabName === 'settings') {
                loadSettings();
            }
            
            // 切换到测试页面时刷新产品选择器
            if (tabName === 'test') {
                loadProductSelector();
            }
            
            // 切换到全量查询页面时加载产品型号
            if (tabName === 'query') {
                loadQueryProductModels();
                // 默认查询当日数据
                const today = new Date().toISOString().split('T')[0];
                document.getElementById('query-start-date').value = today;
                document.getElementById('query-end-date').value = today;
            }
        }
        
        // ==================== 全量查询功能 ====================
        let queryCurrentPage = 1;
        let queryTotalPages = 0;
        let queryTotalRecords = 0;
        const QUERY_COLUMN_WIDTHS_KEY = 'query-record-column-widths-v1';
        const DEFAULT_QUERY_COLUMN_WIDTHS = [56, 168, 180, 120, 120, 110, 110, 110, 110, 90, 96];
        let queryColumnWidths = [...DEFAULT_QUERY_COLUMN_WIDTHS];
        let activeQueryColumnResize = null;
        let queryColumnResizeInitialized = false;

        function clampQueryColumnWidth(index, width) {
            const nextWidth = Number(width);
            if (!Number.isFinite(nextWidth)) {
                return DEFAULT_QUERY_COLUMN_WIDTHS[index] || 1;
            }
            return Math.max(1, Math.round(nextWidth));
        }

        function loadQueryColumnWidths() {
            try {
                const saved = localStorage.getItem(QUERY_COLUMN_WIDTHS_KEY);
                if (!saved) return;

                const parsed = JSON.parse(saved);
                if (!Array.isArray(parsed) || parsed.length !== DEFAULT_QUERY_COLUMN_WIDTHS.length) {
                    return;
                }

                queryColumnWidths = DEFAULT_QUERY_COLUMN_WIDTHS.map((defaultWidth, index) => {
                    const parsedWidth = Number(parsed[index]);
                    return Number.isFinite(parsedWidth)
                        ? clampQueryColumnWidth(index, parsedWidth)
                        : defaultWidth;
                });
            } catch (error) {
                console.warn('加载查询列宽失败:', error);
            }
        }

        function persistQueryColumnWidths() {
            try {
                localStorage.setItem(QUERY_COLUMN_WIDTHS_KEY, JSON.stringify(queryColumnWidths));
            } catch (error) {
                console.warn('保存查询列宽失败:', error);
            }
        }

        function applyQueryColumnWidths() {
            const table = document.getElementById('query-results-table');
            const cols = document.querySelectorAll('#query-table-colgroup col');
            if (!table || !cols.length) return;

            let totalWidth = 0;
            cols.forEach((col, index) => {
                const width = clampQueryColumnWidth(index, queryColumnWidths[index] ?? DEFAULT_QUERY_COLUMN_WIDTHS[index]);
                queryColumnWidths[index] = width;
                col.style.width = `${width}px`;
                totalWidth += width;
            });

            const containerWidth = table.parentElement?.clientWidth || 0;
            const appliedWidth = Math.max(totalWidth, containerWidth);
            table.style.width = `${appliedWidth}px`;
        }

        function handleQueryColumnResizeMove(event) {
            if (!activeQueryColumnResize) return;

            const nextWidth = clampQueryColumnWidth(
                activeQueryColumnResize.index,
                activeQueryColumnResize.startWidth + (event.clientX - activeQueryColumnResize.startX)
            );

            if (nextWidth === queryColumnWidths[activeQueryColumnResize.index]) {
                return;
            }

            queryColumnWidths[activeQueryColumnResize.index] = nextWidth;
            applyQueryColumnWidths();
        }

        function stopQueryColumnResize() {
            if (!activeQueryColumnResize) return;

            activeQueryColumnResize = null;
            document.body.classList.remove('records-col-resizing');
            document.removeEventListener('pointermove', handleQueryColumnResizeMove);
            document.removeEventListener('pointerup', stopQueryColumnResize);
            document.removeEventListener('pointercancel', stopQueryColumnResize);
            persistQueryColumnWidths();
        }

        function startQueryColumnResize(event) {
            const handle = event.target.closest('.record-col-resizer');
            if (!handle) return;

            event.preventDefault();
            event.stopPropagation();

            const index = Number(handle.dataset.queryColIndex);
            if (!Number.isFinite(index)) return;

            activeQueryColumnResize = {
                index,
                startX: event.clientX,
                startWidth: queryColumnWidths[index] ?? DEFAULT_QUERY_COLUMN_WIDTHS[index]
            };

            document.body.classList.add('records-col-resizing');
            document.addEventListener('pointermove', handleQueryColumnResizeMove);
            document.addEventListener('pointerup', stopQueryColumnResize);
            document.addEventListener('pointercancel', stopQueryColumnResize);
        }

        function initResizableQueryTable() {
            loadQueryColumnWidths();
            applyQueryColumnWidths();

            if (queryColumnResizeInitialized) {
                return;
            }

            document.querySelectorAll('#query-results-table .record-col-resizer').forEach(handle => {
                handle.addEventListener('pointerdown', startQueryColumnResize);
            });

            queryColumnResizeInitialized = true;
        }
         
        // 加载产品型号到查询页面
        async function loadQueryProductModels() {
            try {
                const response = await fetch('/api/products');
                const data = await response.json();
                
                const select = document.getElementById('query-product-model');
                select.innerHTML = '<option value="">全部</option>';
                
                if (data.success && data.products) {
                    data.products.forEach(product => {
                        const option = document.createElement('option');
                        option.value = product.model;
                        option.textContent = product.model;
                        select.appendChild(option);
                    });
                }
            } catch(e) {
                console.error('加载产品型号失败:', e);
            }
        }
        
        // 构建查询参数
        function buildQueryParams(page = 1) {
            const params = new URLSearchParams();
            params.append('page', page);
            params.append('limit', document.getElementById('query-limit').value);
            
            const startDate = document.getElementById('query-start-date').value;
            const endDate = document.getElementById('query-end-date').value;
            const productModel = document.getElementById('query-product-model').value;
            const result = document.getElementById('query-result').value;
            const qrCode = document.getElementById('query-qr-code').value;
            const serial = document.getElementById('query-serial').value;
            
            if (startDate) params.append('start_date', startDate);
            if (endDate) params.append('end_date', endDate);
            if (productModel) params.append('product_model', productModel);
            if (result) params.append('result', result);
            if (qrCode) params.append('qr_code', qrCode);
            if (serial) params.append('serial', serial);
            
            return params;
        }
        
        // 执行查询
        async function executeQuery(page = 1) {
            if (!requireWorkLogin()) return;
            try {
                const params = buildQueryParams(page);
                const response = await fetch(`/api/query/records?${params.toString()}`);
                const data = await response.json();
                
                if (data.success) {
                    queryCurrentPage = data.page;
                    queryTotalPages = data.total_pages;
                    queryTotalRecords = data.total;
                    
                    // 更新统计信息
                    document.getElementById('stat-total').textContent = data.total;
                    document.getElementById('stat-pass').textContent = data.pass_count;
                    document.getElementById('stat-fail').textContent = data.fail_count;
                    document.getElementById('stat-rate').textContent = data.pass_rate + '%';
                    
                    // 更新分页信息
                    document.getElementById('query-page-info').textContent = `第 ${data.page} 页 / 共 ${data.total_pages} 页`;
                    document.getElementById('page-input').value = data.page;
                    
                    // 更新表格
                    updateQueryTable(data.records);
                    
                    // 更新分页按钮状态
                    updatePaginationButtons();
                    
                    showNotification(`查询完成，共 ${data.total} 条记录`, 'success');
                } else {
                    showNotification('查询失败: ' + data.message, 'error');
                }
            } catch(e) {
                console.error('查询失败:', e);
                showNotification('查询失败: ' + e.message, 'error');
            }
        }
        
        // 更新查询表格
        function updateQueryTable(records) {
            const tbody = document.getElementById('query-table-body');

            if (!records || records.length === 0) {
                tbody.innerHTML = '<tr><td colspan="11" class="text-center py-8 text-gray-500">暂无数据</td></tr>';
                return;
            }

            // 单位来自每条记录自身的 *_unit 列（保存当次仪器实际单位）
            tbody.innerHTML = records.map((r, index) => {
                const p1Unit = r.pressure1_unit || r.leak1_unit || '';
                const p2Unit = r.pressure2_unit || r.leak2_unit || p1Unit;
                return `
                <tr class="hover:bg-gray-700/50">
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.model_serial ?? r.daily_serial ?? '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.test_time || '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.qr_code || '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.serial_number || '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.product_model || '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.pressure1 !== null ? r.pressure1.toFixed(3) + ' ' + (r.pressure1_unit || p1Unit) : '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.leak1 !== null ? r.leak1.toFixed(3) + ' ' + (r.leak1_unit || p1Unit) : '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.pressure2 !== null ? r.pressure2.toFixed(3) + ' ' + (r.pressure2_unit || p2Unit) : '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.leak2 !== null ? r.leak2.toFixed(3) + ' ' + (r.leak2_unit || p2Unit) : '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center ${r.test_result === 'PASS' ? 'text-green-400' : 'text-red-400'}">${r.test_result || '-'}</td>
                    <td class="py-1 px-2 border border-gray-600 text-center">${r.operator || '-'}</td>
                </tr>
            `;}).join('');
        }
        
        // 更新分页按钮状态
        function updatePaginationButtons() {
            const btnFirst = document.getElementById('btn-page-first');
            const btnPrev = document.getElementById('btn-page-prev');
            const btnNext = document.getElementById('btn-page-next');
            const btnLast = document.getElementById('btn-page-last');
            
            btnFirst.disabled = queryCurrentPage <= 1;
            btnPrev.disabled = queryCurrentPage <= 1;
            btnNext.disabled = queryCurrentPage >= queryTotalPages;
            btnLast.disabled = queryCurrentPage >= queryTotalPages;
        }
        
        // 重置查询条件
        function resetQuery() {
            const today = new Date().toISOString().split('T')[0];
            document.getElementById('query-start-date').value = today;
            document.getElementById('query-end-date').value = today;
            document.getElementById('query-product-model').value = '';
            document.getElementById('query-result').value = '';
            document.getElementById('query-qr-code').value = '';
            document.getElementById('query-serial').value = '';
            document.getElementById('query-limit').value = '20';
            
            // 清空统计和表格
            document.getElementById('stat-total').textContent = '0';
            document.getElementById('stat-pass').textContent = '0';
            document.getElementById('stat-fail').textContent = '0';
            document.getElementById('stat-rate').textContent = '0%';
            document.getElementById('query-table-body').innerHTML = '<tr><td colspan="11" class="text-center py-8 text-gray-500">请执行查询</td></tr>';
            document.getElementById('query-page-info').textContent = '第 1 页 / 共 0 页';
        }
        
        // 导出CSV
        async function exportCSV() {
            if (!requireWorkLogin()) return;
            try {
                const params = buildQueryParams(1);
                params.set('limit', '10000'); // 导出最多10000条
                params.set('export', '1');
                
                const response = await fetch(`/api/query/export?${params.toString()}`);
                const blob = await response.blob();
                
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `test_records_${new Date().toISOString().split('T')[0]}.csv`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                
                showNotification('导出成功', 'success');
            } catch(e) {
                console.error('导出失败:', e);
                showNotification('导出失败: ' + e.message, 'error');
            }
        }
        
        // 绑定查询页面事件
        // ===== 布局工具：重置布局 + 卡片视口钳制 =====
        const LAYOUT_STORAGE_KEYS = [
            'ateq_card_v2', 'monitor_card_v2', 'product_select_card_v2',
            'chart_card_v2', 'record_card_v2', 'status_lights_card_v2',
            'test-record-column-widths-v1', 'query-record-column-widths-v1'
        ];

        document.getElementById('btn-reset-layout').addEventListener('click', function() {
            if (!confirm('确定恢复默认布局吗？所有卡片的位置、大小和表格列宽都会重置。')) return;
            LAYOUT_STORAGE_KEYS.forEach(function(key) { localStorage.removeItem(key); });
            location.reload();
        });

        document.querySelectorAll('#canvas .control-card').forEach(function(card) {
            interact('#' + card.id).draggable(false).resizable(false);
        });

        // 窗口缩放/小屏时把卡片拉回可视区，避免恢复到屏幕外
        function clampCardsToViewport() {
            document.querySelectorAll('.control-card').forEach(function(card) {
                if (card.closest('#canvas')) return;
                if (card.closest('#page-settings')) return;
                if (!card.offsetParent) return;
                const parent = card.offsetParent;
                const maxX = Math.max(0, parent.clientWidth - card.offsetWidth);
                const maxY = Math.max(0, parent.clientHeight - card.offsetHeight);
                const left = parseFloat(card.style.left) || 0;
                const top = parseFloat(card.style.top) || 0;
                const clampedLeft = Math.min(Math.max(0, left), maxX);
                const clampedTop = Math.min(Math.max(0, top), maxY);
                if (clampedLeft !== left) card.style.left = clampedLeft + 'px';
                if (clampedTop !== top) card.style.top = clampedTop + 'px';
            });
        }
        clampCardsToViewport();
        window.addEventListener('resize', clampCardsToViewport);

        document.addEventListener('DOMContentLoaded', function() {
            initResizableQueryTable();
            // 查询按钮
            document.getElementById('btn-query-search').addEventListener('click', function() {
                executeQuery(1);
            });
            
            // 重置按钮
            document.getElementById('btn-query-reset').addEventListener('click', resetQuery);
            
            // 导出按钮
            document.getElementById('btn-query-export').addEventListener('click', exportCSV);
            
            // 分页按钮
            document.getElementById('btn-page-first').addEventListener('click', function() {
                executeQuery(1);
            });
            
            document.getElementById('btn-page-prev').addEventListener('click', function() {
                if (queryCurrentPage > 1) {
                    executeQuery(queryCurrentPage - 1);
                }
            });
            
            document.getElementById('btn-page-next').addEventListener('click', function() {
                if (queryCurrentPage < queryTotalPages) {
                    executeQuery(queryCurrentPage + 1);
                }
            });
            
            document.getElementById('btn-page-last').addEventListener('click', function() {
                executeQuery(queryTotalPages);
            });
            
            document.getElementById('btn-page-go').addEventListener('click', function() {
                const page = parseInt(document.getElementById('page-input').value);
                if (page >= 1 && page <= queryTotalPages) {
                    executeQuery(page);
                } else {
                    showNotification('请输入有效的页码', 'warning');
                }
            });
            
            // 回车跳转
            document.getElementById('page-input').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') {
                    document.getElementById('btn-page-go').click();
                }
            });
        });
        
        // 产品列表数据
        let products = [];
        let productCounter = 0;

        function escapeAttr(value) {
            return String(value ?? '')
                .replace(/&/g, '&amp;')
                .replace(/"/g, '&quot;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');
        }

        function renderOperatorList() {
            const container = document.getElementById('operator-list');
            if (!container) return;
            const accounts = readConfiguredAccounts();
            let html = '';
            for (let i = 0; i < 10; i++) {
                const account = accounts[i] || {};
                const name = account.username || '';
                const password = account.password || '';
                html += '<div class="grid grid-cols-[24px_1fr_1fr] gap-1 text-xs items-center">' +
                    '<span class="text-gray-500 w-5">' + (i + 1) + '</span>' +
                    '<input type="text" class="operator-name-input w-full px-1.5 py-0.5 bg-gray-600 border border-gray-500 rounded text-white text-xs" ' +
                    'placeholder="用户名' + (i + 1) + '" value="' + escapeAttr(name) + '" data-index="' + i + '">' +
                    '<input type="password" class="operator-password-input w-full px-1.5 py-0.5 bg-gray-600 border border-gray-500 rounded text-white text-xs" ' +
                    'placeholder="密码" value="' + escapeAttr(password) + '" data-index="' + i + '">' +
                    '</div>';
            }
            container.innerHTML = html;
        }

        function loadOperators() {
            const accounts = readConfiguredAccounts();
            const operators = accounts.map(item => item.username).filter(Boolean);
            // 填充设置页面
            const inputs = document.querySelectorAll('.operator-name-input');
            inputs.forEach(inp => {
                const idx = parseInt(inp.dataset.index);
                if (idx >= 0 && idx < 10) {
                    inp.value = operators[idx] || '';
                }
            });
            const passwordInputs = document.querySelectorAll('.operator-password-input');
            passwordInputs.forEach(inp => {
                const idx = parseInt(inp.dataset.index);
                if (idx >= 0 && idx < 10) {
                    inp.value = accounts[idx]?.password || '';
                }
            });
            // 填充测试页面下拉框
            const select = document.getElementById('operator-input');
            if (select) {
                const currentVal = select.value;
                select.innerHTML = '<option value="">-- 选择操作人员 --</option>';
                operators.forEach(name => {
                    if (name && name.trim()) {
                        select.innerHTML += '<option value="' + escapeAttr(name.trim()) + '">' + escapeAttr(name.trim()) + '</option>';
                    }
                });
                if (currentVal) select.value = currentVal;
            }
            const settingsLoginSelect = document.getElementById('settings-login-user');
            if (settingsLoginSelect) {
                const currentLoginValue = settingsLoginSelect.value;
                settingsLoginSelect.innerHTML = operators.length
                    ? '<option value="">-- 选择用户名 --</option>'
                    : '<option value="">-- 请先在操作人员卡片区填写用户名密码并保存 --</option>';
                operators.forEach(name => {
                    settingsLoginSelect.innerHTML += '<option value="' + escapeAttr(name) + '">' + escapeAttr(name) + '</option>';
                });
                if (loggedInUser && operators.includes(loggedInUser)) {
                    settingsLoginSelect.value = loggedInUser;
                } else if (currentLoginValue) {
                    settingsLoginSelect.value = currentLoginValue;
                }
            }
            if (loggedInUser && !operators.includes(loggedInUser)) {
                loggedInUser = '';
                localStorage.removeItem(LOGIN_USER_STORAGE_KEY);
            }
            applyOperatorLoginState();
            applyLoginUiState();
            applySettingsAccessState();
            return operators;
        }

        function saveOperators() {
            const nameInputs = document.querySelectorAll('.operator-name-input');
            const passwordInputs = document.querySelectorAll('.operator-password-input');
            const accounts = [];
            nameInputs.forEach((inp, idx) => {
                const userName = inp.value.trim();
                const password = passwordInputs[idx] ? passwordInputs[idx].value : '';
                if (userName) {
                    accounts.push({ username: userName, password: password });
                }
            });
            localStorage.setItem(OPERATOR_ACCOUNTS_STORAGE_KEY, JSON.stringify(accounts));
            localStorage.setItem('ateq_operators', JSON.stringify(accounts.map(item => item.username)));
            loadOperators();
            return accounts;
        }
        
        // 创建产品行
        function createProductRow(product = null, index = null) {
            const idx = index !== null ? index : productCounter++;
            const model = product ? product.model : '';
            const program1 = product ? product.program1 : '';
            const program2 = product ? product.program2 : '';
            const switchChamber = product ? (product.switchChamber ? 'true' : 'false') : 'false';
            const labelTemplate = product ? product.labelTemplate : '';
            const supplierCode = product ? (product.supplierCode || '') : '';
            
            const tr = document.createElement('tr');
            tr.className = 'hover:bg-gray-700/30 product-row';
            tr.dataset.index = idx;
            tr.innerHTML = `
                <td class="py-2 px-3 border border-gray-600 text-center">${idx + 1}</td>
                <td class="py-2 px-3 border border-gray-600">
                    <input type="text" class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" value="${escapeAttr(model)}" placeholder="产品型号">
                </td>
                <td class="py-2 px-3 border border-gray-600">
                    <input type="number" class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" value="${escapeAttr(program1)}" placeholder="程序号">
                </td>
                <td class="py-2 px-3 border border-gray-600">
                    <input type="number" class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" value="${escapeAttr(program2)}" placeholder="程序号">
                </td>
                <td class="py-2 px-3 border border-gray-600">
                    <select class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm">
                        <option value="true" ${switchChamber === 'true' ? 'selected' : ''}>是</option>
                        <option value="false" ${switchChamber === 'false' ? 'selected' : ''}>否</option>
                    </select>
                </td>
                <td class="py-2 px-3 border border-gray-600">
                    <div class="flex gap-1">
                        <input type="text" class="label-template-input flex-1 min-w-[180px] px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" value="${escapeAttr(labelTemplate)}" placeholder="可选，选择*.btw文件">
                        <input type="file" class="label-template-file" accept=".btw" style="display:none">
                        <button type="button" class="btn-select-template px-2 py-1 bg-blue-600 hover:bg-blue-500 disabled:bg-gray-600 text-white rounded text-xs whitespace-nowrap">选择</button>
                    </div>
                </td>
                <td class="py-2 px-3 border border-gray-600">
                    <input type="text" class="w-full px-2 py-1 bg-gray-600 border border-gray-500 rounded text-white text-sm" value="${escapeAttr(supplierCode)}" placeholder="供应商代码">
                </td>
                <td class="py-2 px-3 border border-gray-600 text-center">
                    <button class="px-2 py-1 bg-red-600 hover:bg-red-500 text-white rounded text-xs delete-product" title="删除">
                        <svg class="w-4 h-4 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                        </svg>
                    </button>
                </td>
            `;

            const templateButton = tr.querySelector('.btn-select-template');
            const templateFileInput = tr.querySelector('.label-template-file');
            templateButton.addEventListener('click', function() {
                templateFileInput.value = '';
                templateFileInput.click();
            });

            templateFileInput.addEventListener('change', async function() {
                const file = this.files && this.files[0];
                if (!file) return;
                if (!file.name.toLowerCase().endsWith('.btw')) {
                    showNotification('请选择 .btw 标签模板文件', 'error');
                    return;
                }

                const btn = templateButton;
                const input = tr.querySelector('.label-template-input');
                const oldText = btn.textContent;
                btn.disabled = true;
                btn.textContent = '上传中...';
                try {
                    const response = await fetch('/api/upload_label_template?filename=' + encodeURIComponent(file.name), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/octet-stream' },
                        body: file
                    });
                    const result = await response.json();
                    if (result.success && result.path) {
                        input.value = result.path;
                        showNotification('已选择标签模板', 'success');
                    } else {
                        showNotification('选择标签模板失败: ' + (result.message || ''), 'error');
                    }
                } catch (e) {
                    showNotification('选择标签模板异常: ' + e.message, 'error');
                } finally {
                    btn.textContent = oldText;
                    btn.disabled = false;
                }
            });

            tr.querySelector('.label-template-input').addEventListener('dblclick', async function() {
                const input = this;
                input.disabled = true;
                try {
                    const response = await fetch('/api/select_label_template', { method: 'POST' });
                    const result = await response.json();
                    if (result.success && result.path) {
                        input.value = result.path;
                        showNotification('已选择标签模板', 'success');
                    } else if (!result.cancelled) {
                        showNotification('选择标签模板失败: ' + (result.message || ''), 'error');
                    } else {
                        showNotification('已取消选择标签模板', 'warning');
                    }
                } catch (e) {
                    showNotification('选择标签模板异常: ' + e.message, 'error');
                } finally {
                    input.disabled = false;
                }
            });
            
            // 绑定删除按钮事件
            tr.querySelector('.delete-product').addEventListener('click', function() {
                if (document.querySelectorAll('.product-row').length > 1) {
                    tr.remove();
                    updateRowNumbers();
                } else {
                    alert('至少保留一个产品设置');
                }
            });
            
            return tr;
        }
        
        // 更新行号
        function updateRowNumbers() {
            const rows = document.querySelectorAll('.product-row');
            rows.forEach((row, index) => {
                row.querySelector('td:first-child').textContent = index + 1;
                row.dataset.index = index;
            });
            productCounter = rows.length;
        }
        
        // 添加产品
        document.getElementById('btn-add-product').addEventListener('click', function() {
            if (!requireSettingsLogin()) {
                return;
            }
            const tbody = document.getElementById('product-tbody');
            const newRow = createProductRow();
            tbody.appendChild(newRow);
            updateRowNumbers();
            
            // 滚动到新添加的行
            newRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
        });
        
        // 保存设置（服务器为权威来源；浏览器只保留布局等偏好）
        document.getElementById('btn-save-settings').addEventListener('click', async function() {
            if (!requireSettingsLogin()) {
                return;
            }
            const rows = document.querySelectorAll('.product-row');
            const products = [];
            saveOperators();

            rows.forEach((row, index) => {
                const inputs = row.querySelectorAll('input[type="text"], input[type="number"], select');
                products.push({
                    id: index + 1,
                    model: inputs[0].value.trim(),
                    program1: inputs[1].value,
                    program2: inputs[2].value,
                    switchChamber: inputs[3].value === 'true',
                    labelTemplate: inputs[4].value.trim(),
                    supplierCode: inputs[5].value.trim()
                });
            });

            try {
                const response = await fetch('/api/product_config', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ revision: productConfigRevision, products })
                });
                const result = await response.json();
                if (!result.success) {
                    const suffix = result.error_code === 'REVISION_CONFLICT'
                        ? '（其他客户端已修改，请刷新后重试）' : '';
                    showNotification('保存失败: ' + (result.message || result.error_code || '') + suffix, 'error');
                    return;
                }
                productConfigRevision = result.revision;
                if (Array.isArray(result.warnings) && result.warnings.length) {
                    showNotification('已保存到服务器；提示: ' + result.warnings.join('；'), 'warning');
                } else {
                    showNotification('设置已保存到服务器！共 ' + products.length + ' 个产品', 'success');
                }
                await loadProductSelector();
            } catch (e) {
                showNotification('保存异常: ' + e.message, 'error');
            }
        });
        
        // 加载设置（来自服务器配置）
        async function loadSettings() {
            renderOperatorList();
            loadOperators();
            const tbody = document.getElementById('product-tbody');
            tbody.innerHTML = '';
            productCounter = 0;

            let products = serverProducts;
            if (!products || !products.length) {
                const ok = await fetchServerProducts();
                if (ok) products = serverProducts;
            }

            if (products && products.length > 0) {
                products.forEach((product, index) => {
                    const row = createProductRow(product, index);
                    tbody.appendChild(row);
                });
                productCounter = products.length;
            } else {
                // 首次使用时添加一个默认产品
                const defaultProduct = {
                    model: 'MODEL001',
                    program1: '1',
                    program2: '0',
                    switchChamber: false,
                    labelTemplate: '',
                    supplierCode: ''
                };
                tbody.appendChild(createProductRow(defaultProduct, 0));
            }
            applySettingsAccessState();
        }
        

        // --- Auto-sync context for hardware-triggered tests ---
        function syncTestContext() {
            var ctx = {
                product_model: (currentProductConfig && currentProductConfig.model) || "",
                operator: getActiveOperatorName(),
                qr_code: document.getElementById('qr-input')?.value || "",
                label_template: (currentProductConfig && currentProductConfig.labelTemplate) || "",
                supplier_code: (currentProductConfig && currentProductConfig.supplierCode) || "",
                program1: (currentProductConfig && currentProductConfig.program1) || 0,
                program2: (currentProductConfig && currentProductConfig.program2) || 0,
                scan_required: !!document.getElementById('switch-scan-required')?.checked,
                scan_start: !!document.getElementById('switch-scan-start')?.checked,
            };
            fetch("/api/sync_test_context", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(ctx),
            }).catch(function(e){console.log("syncTestContext error:",e);});
        }
        var qrEl = document.getElementById("qr-input");
        if (qrEl) qrEl.addEventListener("input", syncTestContext);
        var opEl = document.getElementById("operator-input");
        if (opEl) opEl.addEventListener("change", syncTestContext);
        setTimeout(syncTestContext, 1000);

    </script>
    <div style="position:fixed;bottom:2px;right:10px;z-index:9999;font-size:11px;color:#64748b;pointer-events:none;font-family:monospace;">UI-20261001-1</div>
</body>
</html>
'''

@app.get("/", response_class=HTMLResponse)
def get_ui():
    # 禁止缓存：改代码重启后浏览器必须拿到新页面
    return HTMLResponse(content=HTML_TEMPLATE, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


def _executor():
    from program_selector import get_test_executor
    return get_test_executor()


def _scanner_module():
    global SERIAL_SCANNER_IMPORT_ERROR
    try:
        import serial_scanner
        return serial_scanner
    except Exception as exc:
        SERIAL_SCANNER_IMPORT_ERROR = exc
        return None


def _effective_scanner_port():
    """Effective (override or default) scanner port; "" when unavailable."""
    try:
        scanner = _scanner_module()
        if scanner is not None:
            getter = getattr(scanner, "get_scan_port", None)
            if getter is not None:
                return str(getter() or "")
    except Exception:
        pass
    return ""


def _json_error(error_code: str, message: str, **extra):
    payload = {"success": False, "error_code": error_code, "message": message}
    payload.update(extra)
    return payload


_LINE_SETTINGS_FALLBACK = {
    "scan_required": True, "printer_enabled": True, "scan_start": False,
    "scanner_enabled": True, "scanner_port": "", "instrument_port": "",
}


def _line_settings():
    if get_line_settings is None:
        return dict(_LINE_SETTINGS_FALLBACK)
    try:
        return get_line_settings()
    except Exception:
        return dict(_LINE_SETTINGS_FALLBACK)


def _find_product(model):
    """Server product configuration is the authority (PLAN_FINAL amendment 5)."""
    name = str(model or "").strip()
    if not name:
        return None
    try:
        import product_config
        return product_config.find_product(name)
    except Exception:
        return None


def _server_programs_for(model):
    state, product = _product_for_admission(model)
    if state != "OK" or product is None:
        return None
    return product.get("program1"), product.get("program2") or 0


def _product_for_admission(model):
    """Server product store is the single authority for admitted configuration."""
    name = str(model or "").strip()
    if not name:
        return "UNKNOWN", None
    try:
        import product_config
    except Exception as exc:
        return "UNAVAILABLE", None
    health = product_config.store_health()
    if not health.get("ok"):
        return "UNAVAILABLE", None
    product = product_config.find_product(name)
    if product is None:
        return "UNKNOWN", None
    return "OK", product


def _config_health():
    try:
        import product_config
        return product_config.store_health()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": ""}


@app.post("/api/start")
def start_instrument():
    return _json_error(
        "PROGRAM_REQUIRED",
        "旧独立脚本启动已停用：请使用 /api/start_test_sequence、扫码或仪器按钮启动",
    )


@app.post("/api/stop")
def stop_instrument():
    return api_stop_test()


@app.post("/api/select_program/{program_number}")
def api_select_program(program_number: int):
    executor = _executor()
    if executor._manager.is_busy():
        return _json_error("BUSY", "测试执行中，禁止切换程序", busy=True)
    if executor._manager.reset_pending():
        return _json_error("RESET_PENDING", "仪器复位处理中，暂不切换程序")
    if isinstance(program_number, bool) or not 1 <= int(program_number) <= 255:
        return _json_error("PROGRAM_INVALID", "程序号必须为 1..255")
    try:
        ok, msg = write_program(program_number)
        return {"success": ok, "message": msg, "program": int(program_number)}
    except Exception as e:
        return _json_error("PROGRAM_WRITE_FAILED", str(e))


@app.get("/api/current_program")
def api_get_current_program():
    executor = _executor()
    try:
        program = read_current_program()
        return {
            "success": program is not None,
            "program": program,
            "busy": executor._manager.is_busy(),
            "message": "" if program is not None else "程序号读取失败（不可用）",
        }
    except Exception as e:
        return {"success": False, "program": None, "message": str(e)}


@app.get("/api/line_settings")
def api_get_line_settings():
    """Get scan/printer switches."""
    try:
        if get_line_settings is None:
            return {"success": False, "message": str(LINE_RUNTIME_IMPORT_ERROR)}
        return {"success": True, "settings": get_line_settings()}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/com_ports")
def api_com_ports():
    """GET-only list of serial port names detected on this host (no writes)."""
    ports = []
    try:
        from serial.tools import list_ports
        for info in list_ports.comports():
            name = str(getattr(info, "device", "") or "").strip().upper()
            if name:
                ports.append(name)
    except Exception as exc:
        return {"success": False, "message": str(exc), "ports": []}
    return {"success": True, "ports": sorted(set(ports))}


_COMM_APPLY_LOCK = threading.Lock()


def _is_comm_change(previous, merged):
    """Does the merged request change live device communication behavior?

    Ports are compared by EFFECTIVE value (persisted value or env/default
    fallback), so explicit COM7 vs empty is not a change. A scanner port change
    while the scanner is disabled (or stays disabled) is not a runtime change.
    """
    from line_runtime import effective_ports

    prev_enabled = bool(previous.get("scanner_enabled", True))
    new_enabled = bool(merged.get("scanner_enabled", True))
    if prev_enabled != new_enabled:
        return True
    prev_scanner, prev_instrument = effective_ports(previous)
    new_scanner, new_instrument = effective_ports(merged)
    if new_enabled and prev_scanner != new_scanner:
        return True
    if prev_instrument != new_instrument:
        return True
    return False


def _acquire_comm_reservation():
    """Reserve the cycle admission slot for a communication change.

    Returns (context_manager, None) or (None, error_response). Fail closed: if the
    executor or its reservation state is unavailable the change is refused, never
    treated as idle.
    """
    try:
        executor = _executor()
    except Exception as exc:
        return None, _json_error(
            "TEST_ACTIVE", f"无法确认测试状态，已拒绝设备通信设置修改: {exc}")
    reservation = getattr(executor, "params_reservation", None)
    if reservation is None:
        return None, _json_error(
            "TEST_ACTIVE", "测试状态不可用（缺少设备预约接口），已拒绝设备通信设置修改")
    return reservation, None


def _apply_communication_settings(previous, settings):
    """Apply persisted communication settings to live modules.

    The CALLER holds the cycle admission reservation and the request lock, so no
    cycle can be admitted while the live reconfiguration happens. Returns
    (restart_required, message). Never raises: any failure keeps the persisted
    setting and asks for an explicit service restart instead of leaving a
    silently mixed old/new communication state.
    """
    restart_required = False
    notes = []
    from line_runtime import effective_ports
    prev_enabled = bool(previous.get("scanner_enabled", True))
    new_enabled = bool(settings.get("scanner_enabled", True))
    prev_scanner, prev_instrument = effective_ports(previous)
    new_scanner, new_instrument = effective_ports(settings)
    scanner_changed = (prev_enabled != new_enabled) or (
        new_enabled and prev_scanner != new_scanner)
    instrument_changed = prev_instrument != new_instrument

    if scanner_changed:
        try:
            scanner = _scanner_module()
            if scanner is None:
                restart_required = True
                notes.append("扫码枪模块不可用，重启服务后生效")
            else:
                stopper = getattr(scanner, "stop_serial_scanner", None)
                stopped = bool(stopper(2.0)) if stopper is not None else True
                if not stopped:
                    restart_required = True
                    notes.append("扫码枪线程未能安全停止，已阻止启动新线程；重启服务后生效")
                else:
                    clear_events = getattr(scanner, "clear_pending_events", None)
                    if clear_events is not None:
                        clear_events("scanner reconfigured")
                    else:
                        scanner.clear_binding("scanner reconfigured")
                    if new_enabled:
                        scanner.set_scan_port(settings.get("scanner_port") or None)
                        if _hardware_enabled():
                            try:
                                executor = _executor()
                                scanner.set_event_callback(executor.handle_scan_event)
                                starter = getattr(scanner, "start_serial_scanner", None)
                                started = bool(starter()) if starter is not None else True
                                if not started:
                                    restart_required = True
                                    notes.append(
                                        "扫码枪启动被拒绝（可能仍在停止旧线程），重启服务后生效")
                            except Exception as exc:
                                restart_required = True
                                notes.append(f"扫码枪启动失败，重启服务后生效: {exc}")
                    else:
                        scanner.set_scan_port(None)
        except Exception as exc:
            restart_required = True
            notes.append(f"扫码枪重配置失败，重启服务后生效: {exc}")

    if instrument_changed:
        try:
            import modbus_utils
            modbus_utils.set_instrument_port(settings.get("instrument_port") or None)
        except Exception as exc:
            restart_required = True
            notes.append(f"仪器串口应用失败，重启服务后生效: {exc}")

    return restart_required, "；".join(notes)


@app.post("/api/line_settings")
def api_update_line_settings(data: dict):
    """Update scan/printer switches and device communication settings.

    Additive: legacy payloads (three booleans) behave exactly as before. Port
    changes are validated first and persisted all-or-nothing. Communication changes
    (ports / scanner enable) hold the cycle admission reservation for the whole
    read-merge-write-apply window, so a cycle cannot be admitted mid-save; a refused
    reservation returns TEST_ACTIVE without persisting anything.
    """
    try:
        if update_line_settings is None:
            return {"success": False, "message": str(LINE_RUNTIME_IMPORT_ERROR)}

        def _as_bool(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "1", "yes", "on"):
                    return True
                if lowered in ("false", "0", "no", "off"):
                    return False
            if isinstance(value, (int, float)):
                return bool(value)
            return None

        def _bool_field(name):
            if name not in data:
                return None, None
            parsed = _as_bool(data.get(name))
            if parsed is None:
                return None, _json_error(
                    "SETTINGS_INVALID",
                    f"{name} 取值无效: {data.get(name)!r}（应为 true/false）")
            return parsed, None

        scan_required, error = _bool_field("scan_required")
        if error is not None:
            return error
        printer_enabled, error = _bool_field("printer_enabled")
        if error is not None:
            return error
        scan_start, error = _bool_field("scan_start")
        if error is not None:
            return error
        scanner_enabled, error = _bool_field("scanner_enabled")
        if error is not None:
            return error

        scanner_port_given = "scanner_port" in data
        instrument_port_given = "instrument_port" in data
        try:
            from line_runtime import normalize_port
            scanner_port = normalize_port(data.get("scanner_port")) if scanner_port_given else None
            instrument_port = normalize_port(data.get("instrument_port")) if instrument_port_given else None
        except Exception as exc:
            return _json_error("SETTINGS_INVALID", str(exc))

        def _merge(current):
            merged = dict(current)
            if scanner_enabled is not None:
                merged["scanner_enabled"] = scanner_enabled
            if scanner_port_given:
                merged["scanner_port"] = scanner_port or ""
            if instrument_port_given:
                merged["instrument_port"] = instrument_port or ""
            return merged

        def _perform():
            # Authoritative read happens inside the request lock AND the cycle
            # reservation (for communication changes) to avoid stale merges.
            current = get_line_settings() if get_line_settings is not None \
                else dict(_LINE_SETTINGS_FALLBACK)
            # "扫码启动"依赖扫码准入：取消"需要扫码"时若"扫码启动"仍开着则拒绝
            if scan_required is False and current.get('scan_start'):
                return {"success": False,
                        "message": '「扫码启动」已开启，请先关闭「扫码启动」再取消「需要扫码」'}
            # 开"扫码启动"时自动联动勾选"需要扫码"
            auto_enabled = bool(scan_start) and not (
                scan_required if scan_required is not None else current.get('scan_required'))
            scan_required_final = True if scan_start else scan_required

            scanner_enabled_effective = (
                scanner_enabled if scanner_enabled is not None
                else bool(current.get("scanner_enabled", True)))
            if scan_start and not scanner_enabled_effective:
                return _json_error("SCANNER_DISABLED",
                                   "扫码枪已禁用，不能开启「扫码启动」；请先启用扫码枪")

            comm_change = _is_comm_change(current, _merge(current))

            settings = update_line_settings(
                scan_required=scan_required_final,
                printer_enabled=printer_enabled,
                scan_start=scan_start,
                scanner_enabled=scanner_enabled,
                scanner_port=scanner_port if scanner_port_given else None,
                instrument_port=instrument_port if instrument_port_given else None,
            )

            restart_required = False
            message = ""
            if comm_change:
                restart_required, message = _apply_communication_settings(
                    current, settings)

            if auto_enabled:
                settings = dict(settings)
                settings['scan_required_auto_enabled'] = True
            response = {"success": True, "settings": settings}
            if restart_required:
                response["restart_required"] = True
            if message:
                response["message"] = message
            return response

        with _COMM_APPLY_LOCK:
            preliminary = get_line_settings() if get_line_settings is not None \
                else dict(_LINE_SETTINGS_FALLBACK)
            if _is_comm_change(preliminary, _merge(preliminary)):
                reservation, error = _acquire_comm_reservation()
                if error is not None:
                    return error
                with reservation() as acquired:
                    if not acquired:
                        return _json_error(
                            "TEST_ACTIVE",
                            "测试运行中或状态未就绪，不能修改设备通信设置（串口或扫码枪开关）")
                    return _perform()
            return _perform()
    except ValueError as e:
        return _json_error("SETTINGS_INVALID", str(e))
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/scanner/status")
def api_scanner_status():
    """Return latest COM scanner state."""
    scanner = _scanner_module()
    if scanner is None:
        return {"success": False, "message": f"serial_scanner import failed: {SERIAL_SCANNER_IMPORT_ERROR}"}
    try:
        state = scanner.get_scanner_state()
        return {"success": True, **state}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/scan_qualified")
def api_scan_qualified(data: dict):
    """Idempotent scan-event entry point.

    Device events are processed by the backend consumer first. This endpoint never
    invents trusted device events: unknown event ids are rejected, a repeated event id
    returns its stored result, and a legacy call without an id may only create a single
    manual binding (frozen compatibility rule, device_verified=false).
    """
    try:
        executor = _executor()
        scanner = _scanner_module()
        qr_code = str(data.get("qr_code") or "").strip()
        event_id = str(data.get("event_id") or "").strip()
        session_id = str(data.get("session_id") or "").strip()
        seq = data.get("seq")
        if not event_id and session_id and seq is not None:
            event_id = f"{session_id}:{int(seq)}"

        settings = _line_settings()
        scanner_port_label = _effective_scanner_port() or "COM7"

        if event_id:
            event = scanner.get_event(event_id) if scanner is not None else None
            if event is None:
                return _json_error("UNKNOWN_EVENT", "未知或已过期扫码事件，已忽略",
                                   duplicate=False, started=False, confirmed=False)
            if event.get("processed"):
                return {
                    "success": True,
                    "qr_code": event.get("code"),
                    "scanner_port": scanner_port_label,
                    "duplicate": True,
                    "started": bool(event.get("started")),
                    "confirmed": True,
                    "busy": executor._manager.is_busy(),
                    "event_id": event_id,
                    "message": event.get("message", "事件已处理"),
                }
            if executor._manager.is_busy():
                scanner.mark_event_result(event_id, "BUSY", message="测试中，扫码未启动")
                return {"success": False, "qr_code": event.get("code"),
                        "scanner_port": scanner_port_label, "duplicate": False, "started": False,
                        "confirmed": False, "busy": True, "event_id": event_id,
                        "error_code": "BUSY", "message": "测试中，扫码已消费未启动"}
            return _json_error("EVENT_NOT_CONSUMED", "事件等待后台处理", event_id=event_id,
                               duplicate=False, started=False, confirmed=False)

        # Legacy call without an event id: manual (typed) QR compatibility path.
        if not qr_code:
            return _json_error("QR_REQUIRED", "缺少二维码")
        if executor._manager.is_busy():
            return {"success": False, "qr_code": qr_code, "scanner_port": scanner_port_label,
                    "duplicate": False, "started": False, "confirmed": False,
                    "busy": True, "error_code": "BUSY", "message": "测试中，未创建扫码事件"}
        existing_binding = scanner.get_binding() if scanner is not None else None
        if existing_binding is not None:
            if existing_binding.get("code") == qr_code:
                return {"success": True, "qr_code": qr_code, "scanner_port": scanner_port_label,
                        "duplicate": True, "started": False, "confirmed": True,
                        "event_id": existing_binding.get("event_id"),
                        "message": "同一二维码已绑定，未重复启动"}
            return _json_error("QR_MISMATCH", "已有未消费二维码，请先清码或完成",
                               duplicate=False, started=False, confirmed=False)
        manual = scanner.create_manual_binding(qr_code) if scanner is not None else None
        if manual is None:
            return _json_error("SCANNER_UNAVAILABLE", "扫码服务不可用")
        context = executor.get_pending_context()
        context.update({"qr_code": qr_code, "scan_required": settings.get("scan_required", True),
                        "scan_start": settings.get("scan_start", False),
                        "scan_source": "manual", "device_verified": False})
        if settings.get("scan_start") and context.get("product_model") and context.get("program1"):
            binding = scanner.consume_binding()
            ok, error, cycle_id = executor.start_cycle(context, trigger="scan",
                                                       scan_event=binding or manual)
            if ok:
                scanner.mark_event_result(manual["event_id"], "AUTO_STARTED", started=True,
                                          message="手动二维码启动")
                return {"success": True, "qr_code": qr_code, "scanner_port": scanner_port_label,
                        "duplicate": False, "started": True, "confirmed": True,
                        "event_id": manual["event_id"], "cycle_id": cycle_id,
                        "message": "扫码已确认并启动"}
            if error != "BUSY":
                scanner.set_binding(manual["event_id"], source="manual")
            return {"success": False, "qr_code": qr_code, "scanner_port": scanner_port_label,
                    "duplicate": False, "started": False, "confirmed": error == "BUSY",
                    "busy": error == "BUSY", "event_id": manual["event_id"],
                    "error_code": error, "message": f"启动被拒绝: {error}"}
        return {"success": True, "qr_code": qr_code, "scanner_port": scanner_port_label,
                "duplicate": False, "started": False, "confirmed": True,
                "event_id": manual["event_id"],
                "message": "二维码已暂存，等待手动/硬件启动（手动录入，未确认条码规则）"}
    except Exception as e:
        return _json_error("SCAN_API_ERROR", str(e))


@app.post("/api/start_test_sequence")
def api_start_test_sequence(
    program1: int,
    program2: int = 0,
    product_model: str = "",
    operator: str = "",
    qr_code: str = "",
    label_template: str = "",
    supplier_code: str = "",
):
    try:
        if STARTUP_ERROR:
            return _json_error("STARTUP_UNAVAILABLE", STARTUP_ERROR)
        executor = _executor()
        if isinstance(program1, bool) or not 1 <= int(program1) <= 255:
            return _json_error("PROGRAM1_INVALID", "程序1必须为 1..255 的整数")
        if isinstance(program2, bool) or not 0 <= int(program2) <= 255:
            return _json_error("PROGRAM2_INVALID", "程序2必须为 0..255 的整数")
        model = str(product_model or "").strip()
        if not model:
            return _json_error("MODEL_REQUIRED", "请先选择产品型号")
        state, product = _product_for_admission(model)
        if state == "UNAVAILABLE":
            return _json_error("CONFIG_UNAVAILABLE", "服务器产品配置不可用，已拒绝启动")
        if state == "UNKNOWN" or product is None:
            return _json_error("MODEL_UNKNOWN", "产品型号不在服务器配置中")
        # Server store is the authority for programs/template/supplier; request values
        # are only cross-checked, never adopted.
        server_p1 = int(product.get("program1"))
        server_p2 = int(product.get("program2") or 0)
        if int(program1) != server_p1 or int(program2 or 0) != server_p2:
            return _json_error("MODEL_PROGRAM_MISMATCH",
                               "程序号与服务器产品配置不一致，已拒绝")
        if executor._manager.reset_inhibits_admission():
            return _json_error("RESET_PENDING", "仪器复位处理中，暂不能启动")

        settings = _line_settings()
        scanner = _scanner_module()
        binding = scanner.get_binding() if scanner is not None else None
        qr = str(qr_code or "").strip()
        scan_source = None
        device_verified = False
        if settings.get("scan_required", True):
            if binding is None:
                if not qr:
                    return _json_error("SCAN_REQUIRED", "扫码开关已开启：请先扫码或手动输入二维码")
                binding = scanner.create_manual_binding(qr) if scanner is not None else None
                if binding is None:
                    return _json_error("SCANNER_UNAVAILABLE", "扫码服务不可用")
                scan_source = "manual"
            else:
                if qr and qr != binding.get("code"):
                    return _json_error("QR_MISMATCH", "二维码与已扫码不一致，请先清码")
                qr = binding.get("code") or ""
                scan_source = binding.get("source")
                device_verified = bool(binding.get("device_verified"))
            binding = scanner.consume_binding() if scanner is not None else None
        else:
            # Scan optional: an existing device binding still identifies the part.
            if binding is not None:
                if qr and qr != binding.get("code"):
                    return _json_error("QR_MISMATCH", "二维码与已扫码不一致，请先清码")
                qr = binding.get("code") or qr
                scan_source = binding.get("source")
                device_verified = bool(binding.get("device_verified"))
                binding = scanner.consume_binding()
            else:
                scan_source = "manual" if qr else None

        context = {
            "product_model": model,
            "operator": str(operator or ""),
            "qr_code": qr,
            "label_template": str(product.get("labelTemplate") or ""),
            "supplier_code": str(product.get("supplierCode") or ""),
            "program1": server_p1,
            "program2": server_p2,
            "switch_chamber": bool(product.get("switchChamber")),
            "scan_required": bool(settings.get("scan_required", True)),
            "scan_start": bool(settings.get("scan_start", False)),
            "scan_source": scan_source,
            "device_verified": device_verified,
        }
        ok, error, cycle_id = executor.start_cycle(context, trigger="software",
                                                   scan_event=binding)
        if not ok:
            reason = {
                'BUSY': '已有测试正在进行，请等当前测试结束再启动',
                'DEVICE_BUSY': '仪器正被硬件触发流程占用',
                'RESET_PENDING': '仪器复位处理中，请稍候',
                'RESET_FAILED': '仪器复位失败，请检查仪器状态',
                'PERSISTENCE_UNAVAILABLE': '执行器状态持久化不可用，请重启服务',
            }.get(error or 'BUSY')
            text = f'测试启动被拒绝：{reason or (error or "未知原因")}'
            return _json_error(error or "BUSY", text, busy=error == "BUSY")
        return {"success": True, "message": "测试已启动", "cycle_id": cycle_id}
    except Exception as e:
        return _json_error("START_API_ERROR", str(e))


def _active_product():
    """Persisted active product resolved against the server store (None if unset)."""
    try:
        import product_config
        return product_config.get_active_product()
    except Exception:
        return None


def _next_context_for(product, operator=None, qr_code=None):
    settings = _line_settings()
    return {
        "product_model": product.get("model") if product else None,
        "operator": operator,
        "qr_code": qr_code,
        "label_template": product.get("labelTemplate") if product else None,
        "supplier_code": product.get("supplierCode") if product else "",
        "program1": int(product.get("program1")) if product else None,
        "program2": int(product.get("program2") or 0) if product else 0,
        "switch_chamber": bool(product.get("switchChamber")) if product else None,
        "scan_required": bool(settings.get("scan_required", True)),
        "scan_start": bool(settings.get("scan_start", False)),
    }


def _rearm_active_product(write_program_now):
    """Rebuild the NEXT context from the persisted active product (restart/config edit)."""
    executor = _executor()
    pending = executor.get_pending_context()
    context = _next_context_for(_active_product(),
                                operator=pending.get("operator"),
                                qr_code=pending.get("qr_code"))
    return executor.arm_next_context(context, write_program_now=write_program_now)


@app.get("/api/test_context")
def api_get_test_context():
    """Server-side active product: the authority every browser displays."""
    product = _active_product()
    pending = _executor().get_pending_context()
    return {
        "success": True,
        "product_model": product.get("model") if product else None,
        "program1": product.get("program1") if product else None,
        "program2": (product.get("program2") or 0) if product else 0,
        "operator": pending.get("operator"),
    }


@app.post("/api/sync_test_context")
def sync_test_context(data: dict):
    """Prepare the NEXT cycle context only; never blocks the event loop or an active cycle.

    The active product is persisted server state: an empty product_model keeps it
    (a browser without a selection cannot clear it), an unknown model is rejected
    without touching it, and only a configured model replaces it.
    """
    try:
        if STARTUP_ERROR:
            return _json_error("STARTUP_UNAVAILABLE", STARTUP_ERROR)
        executor = _executor()
        model = str(data.get("product_model") or "").strip()
        if model:
            state, product = _product_for_admission(model)
            if state == "UNAVAILABLE":
                return _json_error("CONFIG_UNAVAILABLE",
                                   "服务器产品配置不可用，当前产品保持不变")
            if state != "OK" or product is None:
                return _json_error("MODEL_UNKNOWN",
                                   "产品型号不在服务器配置中，当前产品保持不变")
            import product_config
            product_config.set_active_model(model)
        else:
            product = _active_product()
        pending = executor.get_pending_context()
        operator = str(data.get("operator") or "").strip() or pending.get("operator")
        context = _next_context_for(product, operator=operator,
                                    qr_code=data.get("qr_code"))
        arm = executor.arm_next_context(context, write_program_now=True)
        return {"success": True, "armed": arm, "busy": executor._manager.is_busy(),
                "product_model": product.get("model") if product else None}
    except Exception as e:
        return _json_error("SYNC_API_ERROR", str(e))


@app.get("/api/test_status")
def api_get_test_status():
    try:
        executor = _executor()
        snap = executor.runtime_snapshot()
        cp = snap.get("completion_payload") or {}
        p1 = snap.get("p1") or {}
        p2 = snap.get("p2") or {}
        sample = snap.get("sample") or {}
        busy = bool(snap.get("busy"))
        same_cycle_payload = bool(cp) and cp.get("cycle_id") == snap.get("cycle_id")
        visible = cp if same_cycle_payload else {}

        program1_pressure = p1.get("pressure")
        program1_leak = p1.get("leak")
        program1_result = p1.get("result")
        program2_pressure = p2.get("pressure")
        program2_leak = p2.get("leak")
        program2_result = p2.get("result")
        pressure1_unit = p1.get("pressure_unit")
        leak1_unit = p1.get("leak_unit")
        pressure2_unit = p2.get("pressure_unit")
        leak2_unit = p2.get("leak_unit")
        actual_program1 = p1.get("actual_program")
        actual_program2 = p2.get("actual_program")
        # After the cycle finished, expose the last cycle's frozen snapshot (legacy fields);
        # a new active cycle never inherits the previous payload.
        if same_cycle_payload:
            program1_pressure = program1_pressure if program1_pressure is not None else visible.get("pressure1")
            program1_leak = program1_leak if program1_leak is not None else visible.get("leak1")
            program1_result = program1_result if program1_result is not None else visible.get("result1")
            program2_pressure = program2_pressure if program2_pressure is not None else visible.get("pressure2")
            program2_leak = program2_leak if program2_leak is not None else visible.get("leak2")
            program2_result = program2_result if program2_result is not None else visible.get("result2")
            pressure1_unit = pressure1_unit or visible.get("pressure1_unit")
            leak1_unit = leak1_unit or visible.get("leak1_unit")
            pressure2_unit = pressure2_unit or visible.get("pressure2_unit")
            leak2_unit = leak2_unit or visible.get("leak2_unit")
            actual_program1 = actual_program1 if actual_program1 is not None else visible.get("actual_program1")
            actual_program2 = actual_program2 if actual_program2 is not None else visible.get("actual_program2")
        program1_pass = program1_result == "PASS"
        program2_pass = program2_result == "PASS"
        return {
            "success": True,
            "running": busy,
            "busy": busy,
            "phase": snap.get("phase"),
            "trigger": snap.get("trigger"),
            "cycle_id": snap.get("cycle_id"),
            "required_programs": snap.get("required_programs", []),
            "terminal_reason": snap.get("terminal_reason") if same_cycle_payload else None,
            "admission_violation": snap.get("admission_violation"),
            "program1_pass": program1_pass,
            "program2_pass": program2_pass,
            "program1_pressure": program1_pressure,
            "program1_leak": program1_leak,
            "program1_result": program1_result,
            "program2_pressure": program2_pressure,
            "program2_leak": program2_leak,
            "program2_result": program2_result,
            "pressure1_unit": pressure1_unit,
            "leak1_unit": leak1_unit,
            "pressure2_unit": pressure2_unit,
            "leak2_unit": leak2_unit,
            "actual_program1": actual_program1,
            "actual_program2": actual_program2,
            "program_identity_verified1": p1.get("program_identity_verified"),
            "program_identity_verified2": p2.get("program_identity_verified"),
            "phase1_status": p1.get("status"),
            "phase2_status": p2.get("status"),
            "skip_reason2": p2.get("skip_reason"),
            "completion_sequence": snap.get("completion_sequence", 0),
            "overall_result": visible.get("overall_result"),
            "completion_phase": visible.get("phase"),
            "serial_number": visible.get("serial_number"),
            "record_id": visible.get("record_id"),
            "record_saved": visible.get("record_saved"),
            "record_error": visible.get("record_error"),
            "print_attempted": visible.get("print_attempted"),
            "print_success": visible.get("print_success"),
            "print_state": visible.get("print_state"),
            "print_message": visible.get("print_message"),
            "sample": sample,
            "prepare": snap.get("prepare"),
            "reset": snap.get("reset"),
            "supervisor": snap.get("supervisor"),
            "state_source": "memory",
            "server_pid": os.getpid(),
            "active_product_model": executor.get_pending_context().get("product_model"),
        }
    except Exception as e:
        return _json_error("STATUS_API_ERROR", str(e))


@app.post("/api/stop_test")
def api_stop_test():
    """Stop: admission is immediate; reset confirmation is reported separately."""
    try:
        executor = _executor()
        response = executor.request_stop("user")
        if not response.get("cancel_accepted"):
            return {"success": False, "error_code": "NO_ACTIVE_CYCLE",
                    "message": response.get("message", "没有正在执行的周期"),
                    **response}
        return {"success": True, "message": "取消已受理", **response}
    except Exception as e:
        return _json_error("STOP_API_ERROR", str(e))


def parse_realtime_data(response_hex):
    """Pure formatter for a validated 13-register frame (no padding, no fabrication)."""
    if not response_hex:
        return None
    try:
        data = bytes.fromhex(response_hex)
    except ValueError:
        return None
    if len(data) != 31 or data[1] != 0x03 or data[2] != 26:
        return None
    registers = [int.from_bytes(data[3 + i * 2:5 + i * 2], "big") for i in range(13)]
    from program_selector import _decode_pressure_leak, _parse_status, unit_name
    pressure, pressure_unit_code, leak, leak_unit_code = _decode_pressure_leak(registers)
    status = (registers[3] >> 8) | ((registers[3] & 0xFF) << 8)
    step_code = (registers[4] >> 8) | ((registers[4] & 0xFF) << 8)
    parsed = _parse_status(status)
    return {
        "pressure": pressure,
        "pressure_unit": unit_name(pressure_unit_code),
        "leak": leak,
        "leak_unit": unit_name(leak_unit_code),
        "status": status,
        "status_text": _status_text_for(parsed),
        "status_bits": parsed,
        "step_code": step_code,
        "actual_program": ((registers[0] & 0xFF) << 8 | (registers[0] >> 8)) + 1,
    }


def _status_text_for(parsed):
    if parsed is None:
        return "无状态"
    bits = []
    if parsed["keyboard_lock"]:
        bits.append("键盘锁定")
    if parsed["cycle_end"]:
        bits.append("循环结束")
    if parsed["fail_flags"]:
        bits.append("失败标志")
    if parsed["pass_bit"]:
        bits.append("测试通过")
    if parsed["unknown_bits"]:
        bits.append(f"未确认标志位(0x{parsed['unknown_bits']:04X})")
    return " | ".join(bits) if bits else "待机中"


@app.get("/api/data")
def get_data():
    """Serve the sampler cache; never performs serial IO (F08)."""
    try:
        executor = _executor()
        snap = executor.runtime_snapshot()
        sample = snap.get("sample") or {}
        error_code = sample.get("error_code")
        if error_code:
            # Current sample failed: never report success; expose last-good as stale.
            payload = {"success": False, "busy": bool(snap.get("busy")),
                       "error_code": error_code,
                       "message": sample.get("message", "采样失败"),
                       "sampled_at": sample.get("sampled_at"),
                       "stale": True, "last_good": sample.get("last_good")}
            return payload
        return {
            "success": True,
            **sample,
            "busy": bool(snap.get("busy")),
        }
    except Exception as e:
        return _json_error("DATA_API_ERROR", str(e), busy=False, stale=True)


from pydantic import BaseModel
from datetime import datetime


class TestRecordRequest(BaseModel):
    serial_number: str = None
    qr_code: str = None
    pressure1: float
    leak1: float
    pressure2: float = None
    leak2: float = None
    test_result: str
    product_model: str = None
    operator: str = None
    cycle_id: str = None


class PrintLabelRequest(BaseModel):
    record_id: int = None
    product_model: str = ""
    label_template: str = ""
    supplier_code: str = ""
    qr_code: str = ""


@app.post("/api/upload_label_template")
async def api_upload_label_template(request: Request, filename: str = ""):
    """Save a browser-selected .btw file on the target computer and return its path."""
    safe_name = Path(filename or "").name.strip()
    if not safe_name:
        return {"success": False, "message": "文件名为空"}
    if not safe_name.lower().endswith(".btw"):
        return {"success": False, "message": "请选择 .btw 标签模板文件"}

    data = await request.body()
    if not data:
        return {"success": False, "message": "标签模板文件为空"}

    target_dir = Path(os.environ.get("ATEQ_LABEL_TEMPLATE_DIR", r"D:\data\label_templates"))
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_name
        target_path.write_bytes(data)
    except Exception as exc:
        return {"success": False, "message": f"保存标签模板失败: {exc}"}

    return {"success": True, "path": str(target_path)}


@app.post("/api/select_label_template")
def api_select_label_template():
    """Open a native file picker on the target computer and return a .btw path."""
    script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$owner = New-Object System.Windows.Forms.Form
$owner.Text = "选择BarTender标签模板"
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.StartPosition = "CenterScreen"
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.WindowState = "Minimized"
$owner.Add_Shown({
    $owner.WindowState = "Normal"
    $owner.Activate()
})
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = "选择BarTender标签模板"
$dialog.Filter = "BarTender标签 (*.btw)|*.btw"
$dialog.CheckFileExists = $true
$dialog.Multiselect = $false
$dialog.RestoreDirectory = $true
if (Test-Path "D:\data") {
    $dialog.InitialDirectory = "D:\data"
}
$owner.Show()
$owner.TopMost = $true
$owner.Activate()
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.FileName
    $owner.Close()
    exit 0
}
$owner.Close()
exit 2
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "cancelled": True, "message": "选择标签模板超时"}
    except Exception as exc:
        return {"success": False, "cancelled": False, "message": str(exc)}

    selected_path = (result.stdout or "").strip().splitlines()
    selected_path = selected_path[-1].strip().lstrip("\ufeff") if selected_path else ""
    if result.returncode == 2:
        return {"success": False, "cancelled": True, "message": "已取消选择"}
    if result.returncode != 0:
        return {"success": False, "cancelled": False, "message": (result.stderr or result.stdout or "").strip()}
    if not selected_path:
        return {"success": False, "cancelled": True, "message": "未选择标签模板"}
    if not selected_path.lower().endswith(".btw"):
        return {"success": False, "cancelled": False, "message": "请选择 .btw 标签模板文件"}

    return {"success": True, "path": selected_path}


@app.post("/api/serial")
def generate_serial(data: dict):
    """Legacy admin number generation (no record linkage, never prints)."""
    try:
        product_model = str(data.get('product_model', '')).strip()
        qr_code = str(data.get('qr_code', '')).strip()

        if not product_model:
            return {"success": False, "error": "产品型号不能为空"}

        serial = get_generator().get_next_serial_number(product_model, qr_code)

        return {
            "success": True,
            "serial_number": serial.serial_number,
            "product_model": serial.product_model,
            "sequence": serial.sequence,
            "date": serial.date_str,
            "note": "仅生成号；打印必须使用已保存的PASS记录补打接口",
        }
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"生成序列号失败: {e}")
        return {"success": False, "error": f"生成序列号失败: {str(e)}"}


@app.post("/api/print_label")
def api_print_label(data: PrintLabelRequest):
    """Reprint by saved PASS record id; never allocates a new serial number."""
    try:
        if data.record_id is None:
            return _json_error("RECORD_REQUIRED",
                               "手动打印必须提供已保存的PASS记录id")
        try:
            import database
            record = database.get_record(int(data.record_id))
        except Exception as exc:
            return _json_error("RECORD_LOOKUP_FAILED", str(exc))
        if record is None:
            return _json_error("RECORD_NOT_FOUND", "记录不存在")
        if str(record.get("test_result") or "") != "PASS":
            return _json_error("RECORD_NOT_PASS", "只有已保存的PASS记录可以补打")
        serial_number = record.get("serial_number")
        if not serial_number:
            return _json_error("SERIAL_MISSING", "该记录没有有效序列号，不能打印")
        from line_runtime import print_label_if_enabled
        # Durable record-level admission: a visible auto-pending record cannot be
        # manually submitted, and manual jobs are claimed atomically in SQLite.
        claim = database.claim_manual_print(int(data.record_id))
        if not claim.get("claimed"):
            state = claim.get("state")
            if state in ("auto_pending", "manual_pending"):
                return {"success": False, "printed": False, "state": "busy",
                        "serial_number": serial_number,
                        "record_id": int(data.record_id),
                        "message": "该记录打印处理中，请稍后重试"}
            return _json_error("PRINT_STATE", f"当前打印状态不可补打: {state}",
                               state=state, printed=False,
                               serial_number=serial_number,
                               record_id=int(data.record_id))
        # Reprint identity: use the saved record's template/supplier; request values
        # are only a fallback for legacy rows created before those columns existed.
        result = print_label_if_enabled(
            template_name=str(record.get("label_template") or data.label_template or ""),
            serial_number=serial_number,
            product_model=str(record.get("product_model") or data.product_model or ""),
            qr_code=str(record.get("qr_code") or data.qr_code or serial_number),
            supplier_code=str(record.get("supplier_code") or data.supplier_code or ""),
            record_id=int(data.record_id),
        )
        try:
            database.update_print_status(int(data.record_id),
                                         result.get("state", "unknown"),
                                         result.get("message", ""))
        except Exception:
            pass
        return {
            "success": bool(result.get("success")),
            "printed": bool(result.get("printed")),
            "state": result.get("state", "unknown"),
            "serial_number": serial_number,
            "record_id": int(data.record_id),
            "duration_ms": result.get("duration_ms"),
            "message": result.get("message", ""),
        }
    except Exception as e:
        logger.error(f"打印标签失败: {e}")
        return _json_error("PRINT_API_ERROR", str(e))


@app.post("/api/records")
def create_record(record: TestRecordRequest):
    try:
        record_id = save_test_record(
            qr_code=record.qr_code,
            pressure1=record.pressure1,
            leak1=record.leak1,
            pressure2=record.pressure2,
            leak2=record.leak2,
            test_result=record.test_result,
            product_model=record.product_model,
            operator=record.operator,
            serial_number=record.serial_number,
            cycle_id=record.cycle_id,
        )
        return {"success": True, "id": record_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/records")
def get_records():
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        records = get_latest_records(50)
        stats = get_statistics_by_date(today)
        return {
            "success": True,
            "records": records,
            "stats": stats
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/records/today")
def get_today_records():
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        records = get_records_by_date(today)
        stats = get_statistics_by_date(today)
        return {
            "success": True,
            "records": records,
            "stats": stats
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/query/records")
def api_query_records(
    page: int = 1,
    limit: int = 20,
    start_date: str = None,
    end_date: str = None,
    product_model: str = None,
    result: str = None,
    qr_code: str = None,
    serial: str = None
):
    try:
        offset = (page - 1) * limit
        result = query_records(
            start_date=start_date,
            end_date=end_date,
            product_model=product_model,
            result=result,
            qr_code=qr_code,
            serial=serial,
            limit=limit,
            offset=offset
        )

        total_pages = (result['total'] + limit - 1) // limit

        return {
            "success": True,
            "page": page,
            "limit": limit,
            "total": result['total'],
            "total_pages": total_pages,
            "pass_count": result['pass_count'],
            "fail_count": result['fail_count'],
            "pass_rate": result['pass_rate'],
            "records": result['records']
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/query/export")
def api_export_records(
    start_date: str = None,
    end_date: str = None,
    product_model: str = None,
    result: str = None,
    qr_code: str = None,
    serial: str = None,
    limit: int = 10000
):
    try:
        result_data = query_records(
            start_date=start_date,
            end_date=end_date,
            product_model=product_model,
            result=result,
            qr_code=qr_code,
            serial=serial,
            limit=limit,
            offset=0
        )

        # Units are per-record (each row carries its own *_unit columns).
        import csv
        from io import StringIO

        output = StringIO()
        writer = csv.writer(output)

        # 单位不写死在表头：每行数据自带当次记录的 *_unit 列（单位跟随仪器实际配置，
        # 历史记录之间单位不同也不会错），表头保持纯名称
        writer.writerow([
            '序号', '测试时间', 'QR码', '序列号', '产品型号',
            '压力1', '压力1单位', '泄漏1', '泄漏1单位',
            '压力2', '压力2单位', '泄漏2', '泄漏2单位',
            '结果', '操作员', '终止原因', '打印状态'
        ])

        for idx, r in enumerate(result_data['records'], 1):
            writer.writerow([
                idx,
                r.get('test_time', ''),
                r.get('qr_code', ''),
                r.get('serial_number', ''),
                r.get('product_model', ''),
                r.get('pressure1', ''),
                r.get('pressure1_unit', ''),
                r.get('leak1', ''),
                r.get('leak1_unit', ''),
                r.get('pressure2', ''),
                r.get('pressure2_unit', ''),
                r.get('leak2', ''),
                r.get('leak2_unit', ''),
                r.get('test_result', ''),
                r.get('operator', ''),
                r.get('terminal_reason', ''),
                r.get('print_state', '')
            ])

        output.seek(0)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=test_records_{datetime.now().strftime('%Y%m%d')}.csv"}
        )
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/products")
def api_get_products():
    """Legacy shape is preserved; data comes from the server store when available."""
    try:
        models = []
        try:
            import product_config
            models = [p.get("model") for p in product_config.list_products() if p.get("model")]
        except Exception:
            models = []
        if not models:
            try:
                models = get_all_product_models()
            except Exception:
                models = []
        seen = []
        for m in models:
            if m and m not in seen:
                seen.append(m)
        return {
            "success": True,
            "products": [{"model": m} for m in seen]
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/product_config")
def api_get_product_config():
    try:
        import product_config
        health = product_config.store_health()
        if not health.get("ok"):
            return _json_error("CONFIG_UNAVAILABLE", health.get("error") or "配置不可用",
                               path=health.get("path"))
        return {
            "success": True,
            "revision": product_config.get_revision(),
            "switch_chamber_implemented": False,
            "products": product_config.list_products(),
        }
    except Exception as e:
        return _json_error("CONFIG_UNAVAILABLE", str(e))


@app.put("/api/product_config")
def api_put_product_config(data: dict):
    try:
        import product_config
        revision = data.get("revision")
        products = data.get("products")
        if not isinstance(products, list):
            return _json_error("VALIDATION", "products 必须为数组")
        health = product_config.store_health()
        if not health.get("ok"):
            return _json_error("CONFIG_UNAVAILABLE", health.get("error") or "配置不可用")
        result = product_config.replace_products(products, revision=revision)
        if not result.get("success"):
            return result
        try:
            # Programs/template of the active product may have changed.
            _rearm_active_product(write_program_now=_hardware_enabled())
        except Exception as exc:
            logger.error("active product re-arm after config save failed: %s", exc)
        return {"success": True, "revision": result.get("revision"),
                "switch_chamber_implemented": False,
                "products": product_config.list_products(),
                "warnings": result.get("warnings", [])}
    except Exception as e:
        return _json_error("CONFIG_WRITE_FAILED", str(e))


_PARAMS_CACHE = {"data": None, "cached_at": None, "program": None}


@app.get("/api/params")
def get_params(program: int = 1):
    """Read program times; the whole multi-frame sequence holds an IDLE reservation."""
    try:
        executor = _executor()
        if isinstance(program, bool) or not 1 <= int(program) <= 255:
            return _json_error("PROGRAM_INVALID", "程序号必须为 1..255")
        program_num = int(program)
        if executor._manager.is_busy() or executor._manager.reset_pending():
            return _json_error("BUSY", "测试执行中，参数读取已拒绝（未发送任何帧）",
                               program=program_num, cached=False)
        with executor.params_reservation() as reserved:
            if not reserved:
                return _json_error("BUSY", "总线忙，参数读取已拒绝（未发送任何帧）",
                                   program=program_num, cached=False)
            try:
                with executor.run_bus_transaction(timeout=10.0):
                    values = _read_program_times(program_num)
            except Exception as exc:
                if type(exc).__name__ == 'BusUnavailable':
                    return _json_error("BUSY", "总线忙，参数读取已拒绝（未发送任何帧）",
                                       program=program_num, cached=False)
                raise
        if values is None:
            cached = _PARAMS_CACHE["data"]
            response = _json_error("PARAMS_READ_FAILED", "参数读取失败，数值不可用",
                                   program=program_num, cached=False)
            if cached is not None:
                response["cached_params"] = {
                    "program": _PARAMS_CACHE["program"],
                    "cached_at": _PARAMS_CACHE["cached_at"],
                    "stale": True,
                    "fill_time": cached.get("fill_time"),
                    "stab_time": cached.get("stab_time"),
                    "test_time": cached.get("test_time"),
                }
            return response
        _PARAMS_CACHE.update({"data": values, "cached_at": time.time(),
                              "program": program_num})
        return {
            "success": True,
            "program": program_num,
            "fill_time": values["fill_time"],
            "stab_time": values["stab_time"],
            "test_time": values["test_time"],
            "cached": False,
            "stale": False,
            "sampled_at": time.time(),
        }
    except Exception as e:
        return _json_error("PARAMS_API_ERROR", str(e), program=int(program or 1),
                           cached=False)


def _read_program_times(program_num: int):
    """Return {fill_time, stab_time, test_time} or None; never fabricates defaults."""
    param_ids = [0x0001, 0x0002, 0x0003]
    prog_val = program_num - 1
    # Original reference frame uses selector address 0x3004 (verified against the
    # supplied webui_server implementation); do not invent a different register.
    select_cmd = f"{STATION_ID:02X}10{0x3004:04X}000102{prog_val & 0xFF:02X}{(prog_val >> 8) & 0xFF:02X}"
    with modbus_transaction():
        resp = send_raw_ex(select_cmd + modbus_crc(select_cmd))
        if resp["error_code"] != "OK":
            return None
        values = [len(param_ids)] + param_ids
        count = len(values)
        byte_count = count * 2
        data_hex = ''.join([f"{v & 0xFF:02X}{(v >> 8) & 0xFF:02X}" for v in values])
        prep_cmd = f"{STATION_ID:02X}10{0x0000:04X}{count:04X}{byte_count:02X}{data_hex}"
        resp = send_raw_ex(prep_cmd + modbus_crc(prep_cmd))
        if resp["error_code"] != "OK":
            return None
        total_regs = 3 * len(param_ids)
        read_cmd = f"{STATION_ID:02X}03{0x0000:04X}{total_regs:04X}"
        resp = send_raw_ex(read_cmd + modbus_crc(read_cmd))
        if resp["error_code"] != "OK" or not resp["response"]:
            return None
        try:
            frame = bytes.fromhex(resp["response"])
        except ValueError:
            return None
        if len(frame) != 5 + total_regs * 2 or frame[2] != total_regs * 2:
            return None
        data = frame[3:3 + total_regs * 2]
        results = {}
        for i, expected_id in enumerate(param_ids):
            base = i * 6
            word_id = int.from_bytes(data[base:base + 2], "big")
            param_id = ((word_id & 0xFF) << 8) | ((word_id >> 8) & 0xFF)
            word1 = int.from_bytes(data[base + 2:base + 4], "big")
            word2 = int.from_bytes(data[base + 4:base + 6], "big")
            word1_le = ((word1 & 0xFF) << 8) | ((word1 >> 8) & 0xFF)
            word2_le = ((word2 & 0xFF) << 8) | ((word2 >> 8) & 0xFF)
            value_ms = (word2_le << 16) | word1_le
            name = {0x0001: 'fill_time', 0x0002: 'stab_time', 0x0003: 'test_time'}.get(param_id)
            if name:
                results[name] = round(value_ms / 1000.0, 2)
        if len(results) != len(param_ids):
            return None
        return results


if __name__ == '__main__':
    import uvicorn
    stop_previous_server(PORT)
    stop_stale_project_python_processes()
    time.sleep(0.5)
    print("ATEQ 控制界面: http://localhost:8001")
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
