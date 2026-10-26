#!/usr/bin/env python3
"""
测试程序选择功能 - 使用地址 0x6000
"""

import socket
import time

WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502
STATION_ID = 1

def modbus_crc(data_hex):
    """计算 Modbus RTU CRC16 校验码"""
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
    """发送原始十六进制数据并返回响应"""
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

def write_program_0x6000(program_num):
    """
    写入程序号到地址 0x6000
    程序号 = program_num - 1 (程序1写入0，程序2写入1，以此类推)
    """
    print(f"\n写入程序号 {program_num} 到地址 0x6000")
    print("-" * 50)
    
    prog_val = program_num - 1
    # 功能码 0x06 写单个寄存器
    # 地址 0x6000 = 24576
    cmd = f"{STATION_ID:02X}06{0x6000:04X}{prog_val:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    
    print(f"  程序值: {prog_val} (程序号-1)")
    print(f"  完整命令: {full_cmd}")
    
    response = send_raw(full_cmd)
    
    if response and response.startswith(f"{STATION_ID:02X}06"):
        print("  ✅ 写入成功")
        return True
    else:
        print("  ❌ 写入失败")
        return False

def write_program_0x0200(program_num):
    """
    写入程序号到地址 0x0200 (使用功能码 0x10)
    """
    print(f"\n写入程序号 {program_num} 到地址 0x0200")
    print("-" * 50)
    
    # 功能码 0x10 写多个寄存器
    data_part = f"00{program_num:02X}"
    cmd = f"{STATION_ID:02X}10{0x0200:04X}000102{data_part}"
    full_cmd = cmd + modbus_crc(cmd)
    
    print(f"  数据部分: {data_part}")
    print(f"  完整命令: {full_cmd}")
    
    response = send_raw(full_cmd)
    
    if response and response.startswith(f"{STATION_ID:02X}10"):
        print("  ✅ 写入成功")
        return True
    else:
        print("  ❌ 写入失败")
        return False

def read_program_0x202():
    """
    读取当前程序号从地址 0x202
    程序号 = 读取值 + 1
    """
    print(f"\n读取程序号从地址 0x202")
    print("-" * 50)
    
    cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    
    print(f"  读取命令: {full_cmd}")
    
    response = send_raw(full_cmd)
    
    if response and len(response) >= 10 and response.startswith(f"{STATION_ID:02X}03"):
        data_hex = response[6:10]
        register_val = int(data_hex, 16)
        program_num = register_val + 1
        print(f"  寄存器值: {register_val}")
        print(f"  程序号: {program_num}")
        return program_num
    else:
        print("  ❌ 读取失败")
        return None

def main():
    print("=" * 60)
    print("ATEQ 程序号选择测试")
    print("=" * 60)
    
    # 测试地址 0x6000
    print("\n" + "=" * 60)
    print("测试 1: 使用地址 0x6000 写入程序号")
    print("=" * 60)
    
    for prog in [1, 2, 3]:
        write_program_0x6000(prog)
        time.sleep(0.3)
        read_back = read_program_0x202()
        if read_back == prog:
            print(f"  ✅ 程序 {prog} 验证通过!")
        else:
            print(f"  ❌ 程序 {prog} 验证失败! (读取到 {read_back})")
    
    # 测试地址 0x0200
    print("\n" + "=" * 60)
    print("测试 2: 使用地址 0x0200 写入程序号")
    print("=" * 60)
    
    for prog in [1, 2, 3]:
        write_program_0x0200(prog)
        time.sleep(0.3)
        read_back = read_program_0x202()
        if read_back == prog:
            print(f"  ✅ 程序 {prog} 验证通过!")
        else:
            print(f"  ❌ 程序 {prog} 验证失败! (读取到 {read_back})")

if __name__ == "__main__":
    main()
