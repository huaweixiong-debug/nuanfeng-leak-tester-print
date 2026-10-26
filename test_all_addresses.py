#!/usr/bin/env python3
"""
测试程序选择功能 - 测试多个可能的地址
"""

import socket
import time

WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502
STATION_ID = 1

def modbus_crc(data_hex):
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

def send_raw(data_hex, timeout=3):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((WINDOWS_HOST_IP, TCP_PORT))
        data = bytes.fromhex(data_hex)
        print(f"  发送 -> {data.hex().upper()}")
        sock.sendall(data)
        response = sock.recv(1024)
        response_hex = response.hex().upper()
        print(f"  接收 <- {response_hex}")
        return response_hex
    except Exception as e:
        print(f"  错误: {e}")
        return None
    finally:
        sock.close()

def write_single_register(address, value):
    """功能码 0x06 写单个寄存器"""
    cmd = f"{STATION_ID:02X}06{address:04X}{value & 0xFFFF:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    response = send_raw(full_cmd)
    if response and response.startswith(f"{STATION_ID:02X}06"):
        return True
    return False

def read_program_0x202():
    """读取当前程序号从地址 0x202"""
    cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    response = send_raw(full_cmd)
    if response and len(response) >= 10 and response.startswith(f"{STATION_ID:02X}03"):
        data_hex = response[6:10]
        register_val = int(data_hex, 16)
        program_num = register_val + 1
        return program_num
    return None

def read_program_0x30():
    """读取当前程序号从地址 0x30"""
    cmd = f"{STATION_ID:02X}03{0x30:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    response = send_raw(full_cmd)
    if response and len(response) >= 10 and response.startswith(f"{STATION_ID:02X}03"):
        data_hex = response[6:10]
        register_val = int(data_hex, 16)
        return register_val
    return None

def test_address(addr, addr_name, program_num):
    """测试指定地址"""
    print(f"\n测试地址 {addr_name} (0x{addr:04X}) - 程序号 {program_num}")
    print("-" * 50)
    
    value = program_num - 1  # 程序号-1
    success = write_single_register(addr, value)
    
    if success:
        print("  ✅ 写入成功")
    else:
        print("  ❌ 写入失败")
        return False
    
    time.sleep(0.3)
    
    # 从两个地址读取验证
    prog_202 = read_program_0x202()
    prog_30 = read_program_0x30()
    
    print(f"  读取 0x202: 程序号 {prog_202}")
    print(f"  读取 0x30: 程序号 {prog_30}")
    
    return prog_202 == program_num or prog_30 == program_num

def main():
    print("=" * 60)
    print("ATEQ 程序号选择测试 - 测试多个地址")
    print("=" * 60)
    
    # 测试的地址列表
    addresses = [
        (0x3004, "0x3004 (read_program_params_final.py)"),
        (0x0200, "0x0200 (verify_program.py)"),
        (0x6000, "0x6000 (文档地址)"),
    ]
    
    results = {}
    
    for addr, addr_name in addresses:
        print("\n" + "=" * 60)
        print(f"测试地址: {addr_name}")
        print("=" * 60)
        
        passed = 0
        for prog in [1, 2, 3]:
            if test_address(addr, addr_name, prog):
                passed += 1
        
        results[addr_name] = passed
        print(f"\n  结果: {passed}/3 通过")
    
    print("\n" + "=" * 60)
    print("汇总结果")
    print("=" * 60)
    for addr_name, passed in results.items():
        status = "✅" if passed == 3 else "❌"
        print(f"  {status} {addr_name}: {passed}/3")

if __name__ == "__main__":
    main()
