#!/usr/bin/env python3
"""
最终版程序号写入读取验证工具
经过实际测试验证的正确版本
"""

import socket
import time

# 配置参数
WINDOWS_HOST_IP = '172.18.144.1'  # Windows 主机 IP
TCP_PORT = 502                     # TCP 端口
STATION_ID = 1                     # 设备站号

def modbus_crc(data_hex):
    """计算 Modbus RTU CRC16 校验码 (已验证正确)"""
    data = bytes.fromhex(data_hex)
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return f"{crc & 0xFF:02X}{(crc >> 8) & 0xFF:02X}"

def swap_bytes(value):
    """交换16位值的高低字节 (关键修正!)"""
    return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)

def send_raw(data_hex, timeout=2, debug=False):
    """发送原始十六进制数据并返回响应"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((WINDOWS_HOST_IP, TCP_PORT))
        data = bytes.fromhex(data_hex)
        if debug:
            print(f"  发送: {data.hex().upper()}")
        sock.sendall(data)
        response = sock.recv(1024)
        response_hex = response.hex().upper()
        if debug:
            print(f"  接收: {response_hex}")
        return response_hex
    except Exception as e:
        if debug:
            print(f"  错误: {e}")
        return None
    finally:
        sock.close()

def read_current_program(debug=False):
    """读取当前程序号
    
    经过验证的正确公式:
    1. 读取寄存器值 (地址 0x202)
    2. 交换高低字节 (关键修正!)
    3. 程序号 = 交换后的值 + 1
    """
    cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd, debug=debug)
    
    if resp and len(resp) >= 10 and resp.startswith(f"{STATION_ID:02X}03"):
        reg_val = int(resp[6:10], 16)
        reg_val_swapped = swap_bytes(reg_val)  # 关键修正!
        program_number = reg_val_swapped + 1
        return program_number, reg_val, reg_val_swapped
    return None, None, None

def write_program(program_number, debug=False):
    """写入程序号
    
    经过验证的正确公式:
    1. 计算写入值 = 程序号 - 1
    2. 交换高低字节 (关键修正!)
    3. 使用功能码 0x06 写入单个寄存器到地址 0x0200
    """
    if program_number < 1 or program_number > 255:
        return False, "程序号必须在 1-255 之间"
    
    # 关键修正! 交换字节序
    write_val = program_number - 1
    write_val_swapped = swap_bytes(write_val)
    
    # 使用功能码06 (写单个寄存器)
    cmd = f"{STATION_ID:02X}06{0x0200:04X}{write_val_swapped:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    
    if debug:
        print(f"  程序号={program_number} → 原值={write_val} → 交换后=0x{write_val_swapped:04X}={write_val_swapped}")
        print(f"  命令: {full_cmd}")
    
    resp = send_raw(full_cmd, debug=debug)
    
    if resp and resp.startswith(f"{STATION_ID:02X}06"):
        return True, "写入成功"
    return False, f"写入失败: {resp}"

def verify_program(program_number, delay=0.3):
    """验证单个程序号"""
    print(f"\n验证程序号: {program_number}")
    print("-" * 40)
    
    # 写入
    ok, msg = write_program(program_number, debug=True)
    if not ok:
        print(f"  ❌ {msg}")
        return False
    
    time.sleep(delay)
    
    # 读取
    p, reg_raw, reg_swap = read_current_program()
    if p is None:
        print("  ❌ 读取失败")
        return False
    
    print(f"  读取结果: 程序号={p}, 原始寄存器=0x{reg_raw:04X}={reg_raw}, 交换后=0x{reg_swap:04X}={reg_swap}")
    
    if p == program_number:
        print("  ✅ 验证通过!")
        return True
    else:
        print(f"  ❌ 验证失败! 期望={program_number}, 实际={p}")
        return False

def main():
    print("=" * 60)
    print("ATEQ 程序号写入读取验证工具 (最终版)")
    print("=" * 60)
    print("核心算法:")
    print("  写入: 程序号 N → (N-1) → 交换字节 → 写入地址 0x0200")
    print("  读取: 读取地址 0x202 → 交换字节 → +1 → 程序号")
    print("=" * 60)
    
    # 测试连接
    print("\n测试连接...")
    p, _, _ = read_current_program()
    if p is None:
        print("❌ 连接失败!")
        print("请确保:")
        print("  1. Windows 上运行 serial_to_tcp.py")
        print("  2. 仪器已连接到 COM1")
        print("  3. 防火墙允许端口 502")
        return
    print(f"✅ 连接成功! 当前程序号: {p}")
    
    # 批量测试
    test_programs = [1, 2, 3, 5, 10, 20, 50, 100]
    results = []
    
    print(f"\n开始批量测试 {len(test_programs)} 个程序号...")
    for prog in test_programs:
        results.append(verify_program(prog))
    
    # 汇总
    print("\n" + "=" * 60)
    print("测试汇总")
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"结果: {passed}/{total} 个通过")
    
    for i, prog in enumerate(test_programs):
        status = "PASS" if results[i] else "FAIL"
        print(f"  程序 {prog}: {status}")
    
    # 恢复原程序号
    print(f"\n恢复原程序号: {p}")
    write_program(p)
    print("完成!")

if __name__ == "__main__":
    main()
