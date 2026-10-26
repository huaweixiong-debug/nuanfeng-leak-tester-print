#!/usr/bin/env python3
"""
基础通信测试脚本
先验证与仪器的基本通信是否正常
"""

import socket
import time

# 配置参数
WINDOWS_HOST_IP = '172.18.144.1'  # Windows 主机 IP
TCP_PORT = 502                     # TCP 端口
STATION_ID = 1                     # 设备站号

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
        print(f"  发送: {data.hex().upper()}")
        sock.sendall(data)
        response = sock.recv(1024)
        response_hex = response.hex().upper()
        print(f"  接收: {response_hex}")
        return response_hex
    except Exception as e:
        print(f"  错误: {e}")
        return None
    finally:
        sock.close()

def test_start_command():
    """测试启动命令 - 已知正确的命令"""
    print("\n测试1: 启动命令 (已知正确)")
    print("-" * 40)
    # 启动命令: 01 05 00 01 FF 00 DD FA
    cmd = "01050001FF00"
    full_cmd = cmd + modbus_crc(cmd)
    print(f"  预期CRC后的命令: 01050001FF00DDFA")
    print(f"  计算CRC后的命令: {full_cmd}")
    print(f"  CRC匹配: {full_cmd == '01050001FF00DDFA'}")
    return full_cmd == '01050001FF00DDFA'

def test_read_program():
    """测试读取当前程序号 - 使用功能码03"""
    print("\n测试2: 读取当前程序号")
    print("-" * 40)
    # 读取地址 0x202 (514)，1个寄存器
    cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    print(f"  读取命令: {full_cmd}")
    
    response = send_raw(full_cmd)
    if response and len(response) >= 10:
        if response.startswith(f"{STATION_ID:02X}03"):
            data_hex = response[6:10]
            register_value = int(data_hex, 16)
            program_number = register_value + 1  # 根据用户规则
            print(f"  寄存器值: 0x{register_value:04X} = {register_value}")
            print(f"  当前程序号: {program_number}")
            return program_number
        else:
            print(f"  响应格式错误")
    return None

def test_write_program(program_number):
    """测试写入程序号 - 尝试不同的功能码"""
    print(f"\n测试3: 写入程序号 {program_number}")
    print("-" * 40)
    
    # 尝试功能码06 (写单个寄存器)
    print("  尝试功能码06 (写单个寄存器)...")
    write_value = program_number  # 直接写入程序号值
    cmd = f"{STATION_ID:02X}06{0x0200:04X}{write_value:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    response = send_raw(full_cmd)
    if response:
        print(f"  响应: {response}")
    
    time.sleep(0.5)
    
    # 尝试功能码10 (写多个寄存器)
    print("\n  尝试功能码10 (写多个寄存器)...")
    data_hex = f"{program_number:04X}"  # 16位值
    cmd = f"{STATION_ID:02X}10{0x0200:04X}000102{data_hex}"
    full_cmd = cmd + modbus_crc(cmd)
    print(f"  命令(数据={data_hex}): {full_cmd}")
    response = send_raw(full_cmd)
    if response:
        print(f"  响应: {response}")
    
    time.sleep(0.5)
    
    # 尝试高字节=0，低字节=程序号的格式
    print("\n  尝试高字节=0，低字节=程序号格式...")
    data_hex = f"00{program_number:02X}"  # 高字节=00，低字节=程序号
    cmd = f"{STATION_ID:02X}10{0x0200:04X}000102{data_hex}"
    full_cmd = cmd + modbus_crc(cmd)
    print(f"  命令(数据={data_hex}): {full_cmd}")
    response = send_raw(full_cmd)
    if response:
        print(f"  响应: {response}")
    
    time.sleep(0.5)
    
    # 尝试地址0x0201
    print("\n  尝试地址0x0201...")
    data_hex = f"00{program_number:02X}"
    cmd = f"{STATION_ID:02X}10{0x0201:04X}000102{data_hex}"
    full_cmd = cmd + modbus_crc(cmd)
    print(f"  命令: {full_cmd}")
    response = send_raw(full_cmd)
    if response:
        print(f"  响应: {response}")

def main():
    print("=" * 60)
    print("ATEQ 通信测试工具")
    print("=" * 60)
    
    # 测试1: 验证CRC计算
    if not test_start_command():
        print("CRC计算错误!")
        return
    
    # 测试2: 读取当前程序号
    current = test_read_program()
    if current:
        print(f"  当前程序号读取成功: {current}")
    
    # 测试3: 尝试写入程序号
    test_program = 5  # 测试写入程序5
    test_write_program(test_program)
    
    # 测试4: 再次读取验证
    time.sleep(1)
    print("\n测试4: 写入后再次读取")
    print("-" * 40)
    new_current = test_read_program()
    if new_current:
        print(f"  读取结果: {new_current}")
        if new_current == test_program:
            print("  写入成功!")
        else:
            print(f"  写入可能失败，期望={test_program}, 实际={new_current}")

if __name__ == "__main__":
    main()
