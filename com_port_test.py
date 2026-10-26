#!/usr/bin/env python3
"""COM 口独立连接测试（不依赖 webui_server 运行状态）。

用法:
    python com_port_test.py            # 测 COM1 + COM7
    python com_port_test.py COM8       # 测任意 ATEQ 串口 (9600/8/E/1, 从站 255/1)
    python com_port_test.py COM8SWEEP  # 对该口做波特率/校验/从站全扫描
    python com_port_test.py COM1PROBE  # COM1 DTR/RTS 精细探测
    python com_port_test.py COM7       # 扫码枪监听 5 秒
    python com_port_test.py ALL        # 扫全部板载串口找 ATEQ

COM1 = ATEQ Modbus RTU（生产配置），COM7 = 扫码枪
"""
import sys
import time

import serial
from serial.tools import list_ports

TARGETS = [a.upper() for a in sys.argv[1:]] or ['COM1', 'COM7']

MODBUS_EXCEPTION_TEXT = {
    1: '非法功能（仪器不支持该功能码）',
    2: '非法数据地址（寄存器不存在或不可写）',
    3: '非法数据值',
    4: '从站设备故障',
    6: '从站忙',
}


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def swap16(v: int) -> int:
    return ((v & 0xFF) << 8) | ((v >> 8) & 0xFF)


def decode_registers(resp: bytes):
    regs = [int.from_bytes(resp[3 + i * 2:5 + i * 2], 'big') for i in range(13)]
    return {
        '程序号': swap16(regs[0]) + 1,
        'FIFO': swap16(regs[1]),
        '状态位': f'0x{swap16(regs[3]):04X}',
        'StepCode': swap16(regs[4]),
    }


def open_port(port, baud=9600, parity=serial.PARITY_EVEN, timeout=0.8):
    return serial.Serial(port, baud, bytesize=8, parity=parity,
                         stopbits=1, timeout=timeout)


def probe_ateq_station(ser: serial.Serial, station: int, attempts: int = 3):
    """读实时块 0x30 共 13 个寄存器，返回 (是否成功, 说明)。"""
    payload = bytes([station, 0x03]) + (0x0030).to_bytes(2, 'big') + (13).to_bytes(2, 'big')
    req = payload + crc16(payload)
    for attempt in range(1, attempts + 1):
        ser.reset_input_buffer()
        ser.write(req)
        time.sleep(0.2)
        resp = ser.read(64)
        if not resp:
            continue
        if len(resp) == 5 and resp[1] == (0x03 | 0x80):
            text = MODBUS_EXCEPTION_TEXT.get(resp[2], '未知异常')
            return False, f'仪器应答了 Modbus 异常帧 code={resp[2]}: {text}（仪器在线！但拒绝该读取）'
        expected = 5 + 13 * 2
        if len(resp) != expected:
            return False, f'应答长度异常 {len(resp)} 字节: {resp.hex(" ").upper()}'
        if crc16(resp[:-2]) != resp[-2:]:
            return False, f'CRC 校验失败: {resp.hex(" ").upper()}'
        info = ', '.join(f'{k}={v}' for k, v in decode_registers(resp).items())
        return True, f'通信正常（第 {attempt} 次尝试）| {info}'
    return False, f'{attempts} 次请求均无应答'


def test_ateq_port(port='COM1'):
    print(f'== {port} ATEQ Modbus 测试 (9600/8/E/1) ==')
    try:
        ser = open_port(port)
    except Exception as e:
        print(f'  {port} 打开失败: {e}')
        if 'Permission' in type(e).__name__ or '拒绝访问' in str(e):
            print('  -> 端口被占用：先停掉占用它的程序再测')
        return
    print('  打开成功（端口可用，驱动正常）')
    try:
        for station in (255, 1):
            ok, msg = probe_ateq_station(ser, station)
            tag = 'OK' if ok else 'NG'
            print(f'  [{tag}] 从站 {station}: {msg}')
            if ok:
                break
    finally:
        ser.close()


def test_com7():
    print('== COM7 扫码枪测试 (9600/8/N/1) ==')
    try:
        ser = open_port('COM7', parity=serial.PARITY_NONE, timeout=0.2)
    except Exception as e:
        print(f'  COM7 打开失败: {e}')
        return
    print('  打开成功（端口可用，驱动正常）')
    try:
        ser.reset_input_buffer()
        print('  监听 5 秒（此刻可手动扫一个码）...')
        buf = b''
        end = time.time() + 5
        while time.time() < end:
            chunk = ser.read(64)
            if chunk:
                buf += chunk
                end = max(end, time.time() + 0.4)
    finally:
        ser.close()
    if buf:
        print(f'  收到数据: {buf.hex(" ").upper()}')
        print(f'  解码: {buf.decode("gbk", "replace").strip()!r} -> 扫码枪通信正常')
    else:
        print('  5 秒内无数据（未扫码属正常；端口本身可用）')


def scan_all_for_ateq():
    """逐个打开板载串口，探测 ATEQ（从站 255/1，读实时块 0x30）。"""
    print('== 全串口 ATEQ 探测 (9600/8/E/1, 读 0x30) ==')
    ports = [p.device for p in list_ports.comports()
             if p.device.upper() != 'COM7']
    for port in ports:
        try:
            ser = open_port(port, timeout=0.6)
        except Exception as e:
            print(f'  {port}: 打开失败 ({e})')
            continue
        try:
            for station in (255, 1):
                ok, msg = probe_ateq_station(ser, station, attempts=2)
                mark = 'OK' if ok else '--'
                print(f'  {port} [从站 {station}] {mark}: {msg}')
                if ok:
                    print(f'  >>> ATEQ 找到了：在 {port}，从站 {station}！')
                    return
        finally:
            ser.close()


def sweep_port(port='COM1'):
    """指定口参数扫描：被动监听 + 波特率/校验/从站组合逐一探测。"""
    print(f'== {port} 参数扫描 ==')
    parity_map = {'E': serial.PARITY_EVEN, 'N': serial.PARITY_NONE, 'O': serial.PARITY_ODD}

    print('-- 被动监听 3 秒 @9600/8E1（看仪器是否主动发流）...')
    try:
        ser = open_port(port, timeout=0.3)
        buf = b''
        end = time.time() + 3
        while time.time() < end:
            buf += ser.read(256)
        ser.close()
        if buf:
            print(f'   收到被动数据 {len(buf)} 字节: {buf[:64].hex(" ").upper()}')
        else:
            print('   无被动数据')
    except Exception as e:
        print(f'   被动监听失败: {e}')
        return

    print('-- 主动探测：波特率 x 校验 x 从站组合...')
    payload_base = (0x0030).to_bytes(2, 'big') + (13).to_bytes(2, 'big')
    found = False
    for baud in (9600, 19200, 4800, 38400, 115200):
        for par_name, par in parity_map.items():
            try:
                ser = serial.Serial(port, baud, bytesize=8,
                                    parity=par, stopbits=1, timeout=0.5)
            except Exception as e:
                print(f'   {baud}/8{par_name}/1: 打开失败 {e}')
                continue
            try:
                for station in (255, 1):
                    payload = bytes([station, 0x03]) + payload_base
                    req = payload + crc16(payload)
                    for _ in range(2):
                        ser.reset_input_buffer()
                        ser.write(req)
                        time.sleep(0.15)
                        resp = ser.read(64)
                        if not resp:
                            continue
                        if len(resp) == 5 and resp[1] == 0x83:
                            print(f'   {baud}/8{par_name}/1 从站{station}: 异常帧 code={resp[2]} '
                                  f'({MODBUS_EXCEPTION_TEXT.get(resp[2])}) <- 参数对了！')
                            found = True
                            continue
                        expected = 5 + 13 * 2
                        if len(resp) == expected and crc16(resp[:-2]) == resp[-2:]:
                            info = ', '.join(f'{k}={v}' for k, v in decode_registers(resp).items())
                            print(f'   >>> 命中！{baud}/8{par_name}/1 从站{station} CRC 正确 | {info}')
                            found = True
                            continue
                        print(f'   {baud}/8{par_name}/1 从站{station}: '
                              f'收到 {len(resp)} 字节 {resp[:16].hex(" ").upper()}...（参数不对但线路有信号）')
            finally:
                ser.close()
            if found:
                return
    if not found:
        print('   所有组合均未获得有效应答。')


def probe_com1_live(dtr, rts, station, attempts, label):
    """模拟串口助手行为：显式 DTR/RTS、慢节奏、逐段打印收到的原始字节。"""
    try:
        ser = open_port('COM1', timeout=0.3)
    except Exception as e:
        print(f'  [{label}] 打开失败: {e}')
        return False
    ser.dtr = dtr
    ser.rts = rts
    print(f'  [{label}] 已打开 (DTR={dtr}, RTS={rts})')
    payload = bytes([station, 0x03]) + (0x0030).to_bytes(2, 'big') + (13).to_bytes(2, 'big')
    req = payload + crc16(payload)
    try:
        buf = b''
        end = time.time() + 1.5
        while time.time() < end:
            buf += ser.read(256)
        if buf:
            print(f'    静听收到 {len(buf)} 字节: {buf.hex(" ").upper()}')
        for attempt in range(1, attempts + 1):
            ser.reset_input_buffer()
            ser.write(req)
            print(f'    第 {attempt} 帧 -> {req.hex(" ").upper()}')
            buf = b''
            end = time.time() + 2.5
            while time.time() < end:
                chunk = ser.read(256)
                if chunk:
                    buf += chunk
                    print(f'      收到 {len(chunk)} 字节: {chunk.hex(" ").upper()}')
            if not buf:
                print('      (2.5 秒无任何字节)')
                continue
            if len(buf) >= 31 and crc16(buf[:31]) == buf[31:33]:
                info = ', '.join(f'{k}={v}' for k, v in decode_registers(buf).items())
                print(f'    >>> 有效应答！{info}')
                return True
            if len(buf) == 5 and buf[1] == 0x83:
                print(f'    >>> Modbus 异常帧 code={buf[2]}: {MODBUS_EXCEPTION_TEXT.get(buf[2])}')
                return True
            print(f'    共收到 {len(buf)} 字节（非标准帧）')
        return False
    finally:
        ser.close()


def probe_com1_deep():
    """分四种握手组合 + 从站 255/1，逐一探测。"""
    print('== COM1 精细探测 (9600/8/E/1，模拟串口助手节奏) ==')
    combos = [
        ('DTR+RTS', True, True),
        ('仅DTR', True, False),
        ('仅RTS', False, True),
        ('都不拉', False, False),
    ]
    for label, dtr, rts in combos:
        for station in (255, 1):
            print(f'-- 组合 {label}, 从站 {station} --')
            if probe_com1_live(dtr, rts, station, attempts=2, label=label):
                print(f'>>> 通了！配置: {label}, 从站 {station}')
                return
    print('== 全部组合仍无应答。请对照串口助手确认：发的是哪串 hex？收到的是哪串 hex？在哪个 COM 口测的？==')


def main():
    print('== 系统串口清单 ==')
    for p in list_ports.comports():
        print(f'  {p.device}: {p.description} [{p.hwid}]')
    print()
    for target in TARGETS:
        if target == 'ALL':
            scan_all_for_ateq()
        elif target.endswith('SWEEP'):
            sweep_port(target[:-5] or 'COM1')
        elif target == 'COM1PROBE':
            probe_com1_deep()
        elif target == 'COM7':
            test_com7()
        elif target.startswith('COM'):
            test_ateq_port(target)


if __name__ == '__main__':
    main()
