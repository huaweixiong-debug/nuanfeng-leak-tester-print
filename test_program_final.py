#!/usr/bin/env python3
"""
最终的程序号测试脚本
使用正确的 Modbus 协议格式写入和读取程序号

使用方法:
1. 在 Windows 上运行 serial_to_tcp.py
2. 在 WSL 上运行此脚本: python test_program_final.py
"""

import socket
import time

# 配置参数 - 请确认这些与您的设置匹配
WINDOWS_HOST_IP = '172.18.144.1'  # Windows 主机 IP (从 WSL 能访问的 IP)
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

def send_raw(data_hex, timeout=3, debug=True):
    """发送原始十六进制数据并返回响应"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((WINDOWS_HOST_IP, TCP_PORT))
        data = bytes.fromhex(data_hex)
        if debug:
            print(f"  发送 -> {data.hex().upper()}")
        sock.sendall(data)
        response = sock.recv(1024)
        response_hex = response.hex().upper()
        if debug:
            print(f"  接收 <- {response_hex}")
        return response_hex
    except Exception as e:
        if debug:
            print(f"  错误: {e}")
        return None
    finally:
        sock.close()

def write_program(program_num):
    """
    写入程序号 (根据用户提供的协议)
    
    帧格式:
    - 从站地址: 01
    - 功能码: 10 (写多个寄存器)
    - 地址: 02 00 (0x0200)
    - 寄存器数: 00 01 (1个寄存器)
    - 字节数: 02 (2个字节)
    - 数据: 00 XX (高字节=0, 低字节=程序号)
    - CRC: 计算得出
    """
    print(f"\n写入程序号: {program_num}")
    print("-" * 40)
    
    data_part = f"00{program_num:02X}"  # 高字节00，低字节程序号
    cmd = f"{STATION_ID:02X}10{0x0200:04X}000102{data_part}"
    full_cmd = cmd + modbus_crc(cmd)
    
    print(f"  数据部分: {data_part}")
    print(f"  完整命令: {full_cmd}")
    
    # 格式化显示
    if len(full_cmd) >= 22:
        print(f"  格式化: {full_cmd[0:2]} {full_cmd[2:4]} {full_cmd[4:8]} "
              f"{full_cmd[8:12]} {full_cmd[12:14]} {full_cmd[14:18]} {full_cmd[18:]}")
    
    response = send_raw(full_cmd)
    
    if response and response.startswith(f"{STATION_ID:02X}10"):
        print("  ✅ 写入成功")
        return True
    else:
        print("  ❌ 写入失败")
        return False

def read_program():
    """
    读取当前程序号
    
    帧格式:
    - 从站地址: 01
    - 功能码: 03 (读保持寄存器)
    - 地址: 02 02 (0x0202 = 514)
    - 寄存器数: 00 01 (1个寄存器)
    - CRC: 计算得出
    
    返回: 程序号 = 寄存器值 + 1
    """
    print(f"\n读取当前程序号")
    print("-" * 40)
    
    cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    
    print(f"  读取命令: {full_cmd}")
    
    response = send_raw(full_cmd)
    
    if response and len(response) >= 10 and response.startswith(f"{STATION_ID:02X}03"):
        data_hex = response[6:10]
        register_val = int(data_hex, 16)
        program_num = register_val + 1  # 根据用户规则
        print(f"  寄存器值: 0x{register_val:04X} = {register_val}")
        print(f"  程序号: {program_num}")
        return program_num
    else:
        print("  ❌ 读取失败")
        return None

def test_connection():
    """测试与仪器的连接"""
    print("测试连接状态")
    print("-" * 40)
    
    # 尝试读取程序号来测试连接
    cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    
    response = send_raw(full_cmd, timeout=2, debug=False)
    
    if response:
        print("  ✅ 连接正常")
        return True
    else:
        print("  ❌ 连接失败")
        print("\n请检查以下事项:")
        print("  1. 在 Windows 上运行 serial_to_tcp.py")
        print("  2. 确认仪器已连接到 COM1")
        print("  3. 确认 Windows 防火墙允许端口 502")
        print("  4. 确认 IP 地址正确: 172.18.144.1")
        return False

def main():
    print("=" * 60)
    print("ATEQ 程序号测试工具 (最终版)")
    print("=" * 60)
    print(f"目标地址: {WINDOWS_HOST_IP}:{TCP_PORT}")
    print()
    
    # 测试连接
    if not test_connection():
        return
    
    # 读取初始程序号
    initial = read_program()
    
    # 测试写入和读取
    test_numbers = [1, 2, 3, 5, 10]
    results = []
    
    print("\n" + "=" * 60)
    print("开始测试程序号写入和读取")
    print("=" * 60)
    
    for num in test_numbers:
        print(f"\n测试程序号: {num}")
        print("=" * 40)
        
        # 写入
        write_ok = write_program(num)
        time.sleep(0.3)
        
        # 读取
        read_back = read_program()
        
        # 验证
        if write_ok and read_back == num:
            print(f"\n  ✅ 程序号 {num} 测试通过!")
            results.append(True)
        else:
            print(f"\n  ❌ 程序号 {num} 测试失败!")
            results.append(False)
    
    # 恢复初始程序号
    if initial:
        print("\n" + "=" * 60)
        print(f"恢复初始程序号: {initial}")
        write_program(initial)
    
    # 汇总
    print("\n" + "=" * 60)
    print("测试汇总")
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"结果: {passed}/{total} 个测试通过")
    
    for i, num in enumerate(test_numbers):
        status = "通过" if results[i] else "失败"
        print(f"  程序 {num}: {status}")

if __name__ == "__main__":
    main()
