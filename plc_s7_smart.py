#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""西门子 S7-200 SMART PLC 握手通讯模块（snap7 / S7comm over Ethernet）。

信号定义（M 区，握手字节默认 0）：

    M0.0   PLC -> PC    启动        start   （上升沿触发）
    M0.1   PLC -> PC    复位        reset   （上升沿触发）
    M0.2   PC  -> PLC   测试合格    pass
    M0.3   PC  -> PLC   测试不合格  fail

所有信号脉宽 2 秒。

环境变量：

    ATEQ_PLC_IP               PLC 地址            默认 192.168.2.1
    ATEQ_PLC_RACK             机架号              默认 0
    ATEQ_PLC_SLOT             槽号                默认 1
    ATEQ_PLC_PORT             TCP 端口            默认 102
    ATEQ_PLC_PULSE_SECONDS    输出脉宽（秒）      默认 2.0
    ATEQ_PLC_POLL_MS          输入轮询周期（毫秒）默认 100
    ATEQ_PLC_BYTE             握手字节            默认 0
    ATEQ_PLC_TIMEOUT_MS       连接超时（毫秒）    默认 3000

命令行：

    python plc_s7_smart.py read                 读取当前握手位
    python plc_s7_smart.py status               连接状态 + 当前握手位
    python plc_s7_smart.py pulse-pass           输出 2 秒"测试合格"
    python plc_s7_smart.py pulse-fail           输出 2 秒"测试不合格"
    python plc_s7_smart.py clear                两个输出位都置 0（安全清理）
    python plc_s7_smart.py monitor              实时监视 M0.0/M0.1 上升沿
    python plc_s7_smart.py selftest             用内置模拟 PLC 自检（不需要真实 PLC）

写输出位采用"读-改-写"字节 0：只改 bit2/bit3，保留 bit0/bit1。
M0.0/M0.1 由 PLC 程序每个扫描周期刷新，因此该写法不会影响 PLC 的输入位。
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent

try:
    import snap7
    from snap7.type import Area, SrvArea
except Exception as _exc:  # pragma: no cover - only when snap7 missing
    snap7 = None
    Area = None
    SrvArea = None
    _SNAP7_IMPORT_ERROR: Optional[BaseException] = _exc
else:
    _SNAP7_IMPORT_ERROR = None


# ---------------------------------------------------------------- 位定义 ---
BIT_START, BIT_RESET, BIT_PASS, BIT_FAIL = 0, 1, 2, 3
MASK_START = 1 << BIT_START
MASK_RESET = 1 << BIT_RESET
MASK_PASS = 1 << BIT_PASS
MASK_FAIL = 1 << BIT_FAIL
MASK_OUTPUTS = MASK_PASS | MASK_FAIL

DEFAULT_PULSE_SECONDS = 2.0


class PlcError(RuntimeError):
    """PLC 通讯错误。"""


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip())
    except Exception:
        return default


def _build_logger() -> logging.Logger:
    log = logging.getLogger("S7SmartPlc")
    if log.handlers:
        return log
    if os.environ.get("ATEQ_DISABLE_HARDWARE") == "1":
        log.addHandler(logging.NullHandler())
    else:
        try:
            log.addHandler(logging.FileHandler(ROOT / "plc_s7_smart.log", encoding="utf-8"))
        except Exception:
            log.addHandler(logging.NullHandler())
    log.setLevel(logging.INFO)
    return log


logger = _build_logger()


def bits_of(byte_value: int) -> dict:
    """把握手字节拆成 4 个命名位。"""
    return {
        "start": bool(byte_value & MASK_START),
        "reset": bool(byte_value & MASK_RESET),
        "pass": bool(byte_value & MASK_PASS),
        "fail": bool(byte_value & MASK_FAIL),
    }


def format_bits(byte_value: Optional[int]) -> str:
    if byte_value is None:
        return "start=? reset=? pass=? fail=?"
    b = bits_of(byte_value)
    return "start={} reset={} pass={} fail={}".format(
        int(b["start"]), int(b["reset"]), int(b["pass"]), int(b["fail"])
    )


# --------------------------------------------------------------- 客户端 ---
class S7SmartPlc:
    """S7-200 SMART 握手客户端。

    读：M 区一个字节（默认 M0）。
    写：读-改-写同一字节，只翻转 M0.2 / M0.3。
    """

    def __init__(
        self,
        ip: Optional[str] = None,
        rack: Optional[int] = None,
        slot: Optional[int] = None,
        port: Optional[int] = None,
        pulse_seconds: Optional[float] = None,
        poll_ms: Optional[int] = None,
        byte_addr: Optional[int] = None,
        timeout_ms: Optional[int] = None,
    ) -> None:
        if snap7 is None:
            raise PlcError("python-snap7 未安装: {}".format(_SNAP7_IMPORT_ERROR))

        self.ip = ip or os.environ.get("ATEQ_PLC_IP", "192.168.2.1")
        self.rack = _env_int("ATEQ_PLC_RACK", 0) if rack is None else int(rack)
        self.slot = _env_int("ATEQ_PLC_SLOT", 1) if slot is None else int(slot)
        self.port = _env_int("ATEQ_PLC_PORT", 102) if port is None else int(port)
        self.pulse_seconds = (
            _env_float("ATEQ_PLC_PULSE_SECONDS", DEFAULT_PULSE_SECONDS)
            if pulse_seconds is None
            else float(pulse_seconds)
        )
        self.poll_ms = _env_int("ATEQ_PLC_POLL_MS", 100) if poll_ms is None else int(poll_ms)
        self.byte_addr = _env_int("ATEQ_PLC_BYTE", 0) if byte_addr is None else int(byte_addr)
        self.timeout_ms = _env_int("ATEQ_PLC_TIMEOUT_MS", 3000) if timeout_ms is None else int(timeout_ms)

        self._client = None
        self._io_lock = threading.RLock()
        self._pulse_lock = threading.Lock()
        self.last_error: Optional[str] = None

    # -- 连接 ---------------------------------------------------------
    def _tcp_precheck(self) -> None:
        """先做一次短超时的 TCP 探测。

        snap7 的 connect() 没有超时参数，网段不通时会长时间阻塞；
        先探测端口可以让"PLC 不在线上"这种情况立刻失败并给出明确原因。
        """
        timeout_s = max(self.timeout_ms, 100) / 1000.0
        try:
            with socket.create_connection((self.ip, self.port), timeout=timeout_s):
                return
        except Exception as exc:
            raise PlcError(
                "TCP {}:{} 不可达（{}）；请检查网线、PC 网段与 PLC 电源".format(self.ip, self.port, exc)
            )

    def connect(self) -> bool:
        with self._io_lock:
            if self._client is not None and self._client.get_connected():
                return True
            try:
                self._tcp_precheck()
                client = snap7.Client()
                client.connect(self.ip, self.rack, self.slot, tcp_port=self.port)
                if not client.get_connected():
                    raise PlcError("connect() 未建立连接")
                self._client = client
                self.last_error = None
                logger.info("PLC 已连接: %s rack=%s slot=%s", self.ip, self.rack, self.slot)
                return True
            except Exception as exc:
                self._client = None
                self.last_error = str(exc)
                logger.error("PLC 连接失败 %s: %s", self.ip, exc)
                return False

    def disconnect(self) -> None:
        with self._io_lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
            logger.info("PLC 已断开: %s", self.ip)

    def is_connected(self) -> bool:
        with self._io_lock:
            return bool(self._client is not None and self._client.get_connected())

    def ensure_connected(self) -> None:
        if not self.is_connected() and not self.connect():
            raise PlcError("无法连接 PLC {}".format(self.ip))

    # -- 读 -----------------------------------------------------------
    def read_byte(self) -> int:
        with self._io_lock:
            if self._client is None:
                raise PlcError("PLC 未连接")
            data = self._client.mb_read(self.byte_addr, 1)
        if not data:
            raise PlcError("读取 M{}.0 失败".format(self.byte_addr))
        return data[0]

    def read_flags(self) -> dict:
        return bits_of(self.read_byte())

    # -- 写 -----------------------------------------------------------
    def write_outputs(self, pass_on: bool, fail_on: bool) -> int:
        """读-改-写：只修改 M0.2 / M0.3，保留 M0.0 / M0.1。"""
        with self._io_lock:
            if self._client is None:
                raise PlcError("PLC 未连接")
            current = self._client.mb_read(self.byte_addr, 1)[0]
            new_value = (current & ~MASK_OUTPUTS) | (MASK_PASS if pass_on else 0) | (MASK_FAIL if fail_on else 0)
            self._client.mb_write(self.byte_addr, 1, bytearray([new_value]))
        return new_value

    def clear_outputs(self) -> None:
        """两个输出位都置 0。失败不抛出（用于安全清理）。"""
        try:
            self.write_outputs(False, False)
            logger.info("输出已清零 (M%s.2 / M%s.3 = 0)", self.byte_addr, self.byte_addr)
        except Exception as exc:
            logger.error("输出清零失败: %s", exc)

    def pulse_result(self, ok: bool) -> dict:
        """输出"测试合格"或"测试不合格"脉冲，脉宽 pulse_seconds（默认 2 秒）。

        同一时刻只会有一位为 1。
        """
        seconds = float(self.pulse_seconds)
        with self._pulse_lock:
            self.ensure_connected()
            started = time.monotonic()
            value = self.write_outputs(pass_on=ok, fail_on=not ok)
            logger.info("输出 %s = 1 (M%s.%s)", "PASS" if ok else "FAIL",
                        self.byte_addr, BIT_PASS if ok else BIT_FAIL)
            try:
                time.sleep(seconds)
            finally:
                self.write_outputs(False, False)
                logger.info("输出 %s = 0", "PASS" if ok else "FAIL")
            return {
                "success": True,
                "result": "pass" if ok else "fail",
                "held_seconds": seconds,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "byte": value,
            }

    # -- 上下文管理 ---------------------------------------------------
    def __enter__(self) -> "S7SmartPlc":
        self.ensure_connected()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.clear_outputs()
        self.disconnect()


# --------------------------------------------------------------- 监视器 ---
class HandshakeMonitor(threading.Thread):
    """轮询 M0.0 / M0.1，做上升沿检测并回调。

    首次采样只作为基准，不触发回调（避免启动时误触发）。
    """

    def __init__(
        self,
        plc: S7SmartPlc,
        on_start: Optional[Callable[[], None]] = None,
        on_reset: Optional[Callable[[], None]] = None,
        poll_ms: Optional[int] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        super().__init__(name="PlcHandshakeMonitor", daemon=True)
        self.plc = plc
        self.on_start = on_start
        self.on_reset = on_reset
        self.on_error = on_error
        self.poll_ms = int(poll_ms if poll_ms is not None else plc.poll_ms)
        self._stop_evt = threading.Event()
        self._prev: Optional[tuple] = None
        self._lock = threading.Lock()
        self._stats = {"polls": 0, "errors": 0, "start_edges": 0, "reset_edges": 0, "last_byte": None}

    # -- 回调派发 -----------------------------------------------------
    def _fire(self, callback: Optional[Callable[[], None]], name: str) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:  # 回调异常不能终止轮询
            logger.error("%s 回调异常: %s", name, exc)

    def poll_once(self) -> Optional[dict]:
        """采样一次并派发边沿。返回当前位状态。"""
        try:
            value = self.plc.read_byte()
        except Exception as exc:
            with self._lock:
                self._stats["errors"] += 1
            if self.on_error is not None:
                try:
                    self.on_error(exc)
                except Exception:
                    pass
            return None

        state = bits_of(value)
        with self._lock:
            self._stats["polls"] += 1
            self._stats["last_byte"] = value
            prev = self._prev
            self._prev = (state["start"], state["reset"])

        if prev is not None:
            prev_start, prev_reset = prev
            if state["start"] and not prev_start:
                with self._lock:
                    self._stats["start_edges"] += 1
                logger.info("检测到 M%s.0 启动上升沿", self.plc.byte_addr)
                self._fire(self.on_start, "启动")
            if state["reset"] and not prev_reset:
                with self._lock:
                    self._stats["reset_edges"] += 1
                logger.info("检测到 M%s.1 复位上升沿", self.plc.byte_addr)
                self._fire(self.on_reset, "复位")
        return state

    def run(self) -> None:
        """Poll forever with bounded reconnects; never reuse a stale edge baseline."""
        period = max(self.poll_ms, 1) / 1000.0
        reconnect_period = max(_env_int("ATEQ_PLC_RECONNECT_MS", 2000), 100) / 1000.0
        while not self._stop_evt.is_set():
            if not self.plc.is_connected():
                # The first sample after every reconnect is only a baseline.  A bit
                # which stayed ON while disconnected must not look like a new edge.
                with self._lock:
                    self._prev = None
                try:
                    self.plc.ensure_connected()
                except Exception as exc:
                    if self.on_error is not None:
                        try:
                            self.on_error(exc)
                        except Exception:
                            pass
                    self._stop_evt.wait(reconnect_period)
                    continue
            state = self.poll_once()
            if state is None:
                self.plc.disconnect()
                with self._lock:
                    self._prev = None
                self._stop_evt.wait(reconnect_period)
            else:
                self._stop_evt.wait(period)

    def stop(self) -> None:
        self._stop_evt.set()

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)


# --------------------------------------------------------------- 自检 -----
def _wait_for(predicate, timeout, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def selftest(verbose: bool = True) -> int:
    """用进程内 snap7 Server 模拟 PLC 的 M 区，验证全部握手行为。

    不接触真实 PLC 与现场网络。
    """
    if snap7 is None:
        print("FAIL: python-snap7 missing:", _SNAP7_IMPORT_ERROR)
        return 2

    port = _env_int("ATEQ_PLC_SELFTEST_PORT", 10102)
    failures = []

    def check(name, ok, detail=""):
        print("{:<46} {}".format(name, "PASS" if ok else "FAIL"))
        if detail:
            print("      " + detail)
        if not ok:
            failures.append(name)

    mem = bytearray(64)
    server = snap7.Server(log=False)
    server.register_area(SrvArea.MK, 0, mem)
    server.start(tcp_port=port)
    time.sleep(0.3)

    plc = S7SmartPlc(ip="127.0.0.1", rack=0, slot=1, port=port, pulse_seconds=0.4)

    try:
        check("connect to emulated PLC", plc.connect() and plc.is_connected(),
              "ip=127.0.0.1 port={}".format(port))

        # 1) 读取 PLC 输入位
        mem[0] = MASK_START | MASK_RESET
        flags = plc.read_flags()
        check("read M0.0/M0.1 from PLC", flags["start"] and flags["reset"], format_bits(plc.read_byte()))

        # 2) 写输出位不改动输入位
        plc.write_outputs(pass_on=True, fail_on=False)
        check("write M0.2 leaves M0.0/M0.1 intact",
              (mem[0] & MASK_START) and (mem[0] & MASK_RESET) and (mem[0] & MASK_PASS) and not (mem[0] & MASK_FAIL),
              "byte=0b{:04b}".format(mem[0]))

        # 3) 互斥：置 fail 必须清掉 pass
        plc.write_outputs(pass_on=False, fail_on=True)
        check("pass/fail are mutually exclusive",
              bool(mem[0] & MASK_FAIL) and not (mem[0] & MASK_PASS),
              "byte=0b{:04b}".format(mem[0]))

        plc.clear_outputs()
        check("clear_outputs() zeroes both outputs", not (mem[0] & MASK_OUTPUTS),
              "byte=0b{:04b}".format(mem[0]))

        # 4) 脉宽测量：用真实 2 秒默认值
        plc.pulse_seconds = DEFAULT_PULSE_SECONDS
        samples = []
        stop_sampler = threading.Event()

        def sampler():
            while not stop_sampler.is_set():
                samples.append((time.monotonic(), bool(mem[0] & MASK_PASS)))
                time.sleep(0.01)

        t = threading.Thread(target=sampler, daemon=True)
        t.start()
        started = time.monotonic()
        plc.pulse_result(ok=True)
        wall = time.monotonic() - started
        stop_sampler.set()
        t.join(timeout=1)

        on_times = [ts for ts, on in samples if on]
        measured = (max(on_times) - min(on_times)) if on_times else 0.0
        check("M0.2 pulse width ~= 2.0 s",
              on_times and abs(measured - DEFAULT_PULSE_SECONDS) <= 0.25,
              "measured {:.2f}s (wall {:.2f}s, {} samples)".format(measured, wall, len(samples)))
        check("M0.2 returns to 0 after pulse", not (mem[0] & MASK_OUTPUTS),
              "byte=0b{:04b}".format(mem[0]))

        # 5) fail 脉冲
        plc.pulse_seconds = 0.3
        stop2 = threading.Event()
        seen_fail = []

        def sampler2():
            while not stop2.is_set():
                seen_fail.append(bool(mem[0] & MASK_FAIL))
                time.sleep(0.01)

        t2 = threading.Thread(target=sampler2, daemon=True)
        t2.start()
        plc.pulse_result(ok=False)
        stop2.set()
        t2.join(timeout=1)
        check("M0.3 fail pulse asserted", any(seen_fail), "samples={}".format(len(seen_fail)))

        # 6) 边沿检测
        mem[0] = 0
        events = {"start": 0, "reset": 0}
        mon = HandshakeMonitor(plc, on_start=lambda: events.__setitem__("start", events["start"] + 1),
                               on_reset=lambda: events.__setitem__("reset", events["reset"] + 1),
                               poll_ms=20)
        mon.start()
        time.sleep(0.3)                      # 首次采样建立基准

        mem[0] = MASK_START                   # PLC 拉高 M0.0，保持 2 秒
        time.sleep(0.3)
        mem[0] = MASK_START                   # 仍在 2 秒窗口内保持
        time.sleep(0.3)
        check("start edge fires exactly once while held", events["start"] == 1,
              "start_edges={}".format(events["start"]))

        mem[0] = 0                            # 释放
        time.sleep(0.3)
        mem[0] = MASK_START                   # 第二次启动
        time.sleep(0.3)
        check("second start edge fires again", events["start"] == 2,
              "start_edges={}".format(events["start"]))

        mem[0] = MASK_START | MASK_RESET
        time.sleep(0.3)
        check("reset edge fires", events["reset"] == 1,
              "reset_edges={}".format(events["reset"]))

        mon.stop()
        mon.join(timeout=2)
        check("monitor thread stops cleanly", not mon.is_alive(),
              "polls={} errors={}".format(mon.stats()["polls"], mon.stats()["errors"]))

        plc.clear_outputs()
    finally:
        try:
            plc.disconnect()
        except Exception:
            pass
        try:
            server.stop()
        except Exception:
            pass

    print()
    if failures:
        print("SELFTEST FAILED ({}): {}".format(len(failures), ", ".join(failures)))
        return 1
    print("SELFTEST PASSED")
    return 0


# --------------------------------------------------------------- CLI ------
def _connect_or_report(plc) -> bool:
    """连接 PLC，失败时打印明确原因。"""
    if plc.connect():
        return True
    print("PLC connect FAILED: {}:{} rack={} slot={}".format(plc.ip, plc.port, plc.rack, plc.slot))
    if plc.last_error:
        print("  reason: " + plc.last_error)
    return False


def _cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description="S7-200 SMART PLC 握手通讯 (snap7)")
    parser.add_argument("command", choices=["read", "status", "pulse-pass", "pulse-fail", "clear", "monitor", "selftest"])
    parser.add_argument("--seconds", type=float, default=None, help="输出脉宽（秒），默认 2")
    parser.add_argument("--ip", default=None, help="覆盖 ATEQ_PLC_IP")
    args = parser.parse_args(argv)

    if args.command == "selftest":
        return selftest()

    plc = S7SmartPlc(ip=args.ip, pulse_seconds=args.seconds)

    if args.command in ("read", "status"):
        if not _connect_or_report(plc):
            return 1
        try:
            value = plc.read_byte()
            if args.command == "status":
                print("ip={} rack={} slot={} port={}".format(plc.ip, plc.rack, plc.slot, plc.port))
                print("connected={}".format(plc.is_connected()))
            print("M{} byte=0b{:04b}  {}".format(plc.byte_addr, value, format_bits(value)))
        finally:
            plc.disconnect()
        return 0

    if args.command == "clear":
        if not _connect_or_report(plc):
            return 1
        try:
            plc.clear_outputs()
            print("cleared:", format_bits(plc.read_byte()))
        finally:
            plc.disconnect()
        return 0

    if args.command in ("pulse-pass", "pulse-fail"):
        ok = args.command == "pulse-pass"
        if not _connect_or_report(plc):
            return 1
        try:
            result = plc.pulse_result(ok=ok)
            print("pulse {} done: held={}s elapsed={}ms".format(
                result["result"], result["held_seconds"], result["elapsed_ms"]))
        finally:
            plc.disconnect()
        return 0

    if args.command == "monitor":
        if not _connect_or_report(plc):
            return 1
        print("monitoring M{}.0 (start) / M{}.1 (reset) ... Ctrl+C to stop".format(
            plc.byte_addr, plc.byte_addr))
        mon = HandshakeMonitor(
            plc,
            on_start=lambda: print("[{}] START edge".format(time.strftime("%H:%M:%S"))),
            on_reset=lambda: print("[{}] RESET edge".format(time.strftime("%H:%M:%S"))),
        )
        mon.start()
        try:
            while True:
                time.sleep(1)
                print("[{}] {}".format(time.strftime("%H:%M:%S"), format_bits(mon.stats()["last_byte"])))
        except KeyboardInterrupt:
            print("\nstopping ...")
        finally:
            mon.stop()
            mon.join(timeout=2)
            plc.clear_outputs()
            plc.disconnect()
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
