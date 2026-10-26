#!/usr/bin/env python3
"""
测试程序选择功能 - 使用功能码 0x10 写地址 0x3004
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

def select_program_0x10(program_num):
    """
    使用功能码 0x10 写地址 0x3004 选择程序
    格式: 01 10 30 04 00 01 02 XX XX CRC CRC
    其中 XX XX 是程序号-1 的小端序
    """
    print(f"\n选择程序 {program_num} (功能码 0x10, 地址 0x3004)")
    print("-" * 50)
    
    prog_val = program_num - 1
    # 小端序: 低字节在前
    data_hex = f"{prog_val & 0xFF:02X}{(prog_val >> 8) & 0xFF:02X}"
    cmd = f"{STATION_ID:02X}103004000102{data_hex}"
    full_cmd = cmd + modbus_crc(cmd)
    
    print(f"  程序值: {prog_val} (程序号-1)")
    print(f"  数据(小端): {data_hex}")
    print(f"  完整命令: {full_cmd}")
    
    response = send_raw(full_cmd)
    
    if response and response.startswith(f"{STATION_ID:02X}10"):
        print("  ✅ 写入成功")
        return True
    else:
        print("  ❌ 写入失败")
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
    """读取实时状态中的程序号"""
    cmd = f"{STATION_ID:02X}03{0x30:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    response = send_raw(full_cmd)
    if response and len(response) >= 10 and response.startswith(f"{STATION_ID:02X}03"):
        data_hex = response[6:10]
        register_val = int(data_hex, 16)
        return register_val
    return None

def main():
    print("=" * 60)
    print("ATEQ 程序号选择测试 - 功能码 0x10")
    print("=" * 60)
    
    # 测试程序选择
    test_programs = [1, 2, 3, 5]
    results = []
    
    for prog in test_programs:
        print("\n" + "=" * 60)
        success = select_program_0x10(prog)
        time.sleep(0.5)
        
        prog_202 = read_program_0x202()
        prog_30 = read_program_0x30()
        
        print(f"\n  读取 0x202: 程序号 {prog_202}")
        print(f"  读取 0x30: 程序号 {prog_30}")
        
        if prog_202 == prog or prog_30 == prog:
            print(f"  ✅ 程序 {prog} 验证通过!")
            results.append(True)
        else:
            print(f"  ❌ 程序 {prog} 验证失败!")
            results.append(False)
    
    print("\n" + "=" * 60)
    print("汇总结果")
    print("=" * 60)
    passed = sum(results)
    print(f"结果: {passed}/{len(test_programs)} 通过")
    
    for i, prog in enumerate(test_programs):
        status = "✅" if results[i] else "❌"
        print(f"  {status} 程序 {prog}")

if __name__ == "__main__":
    main()
