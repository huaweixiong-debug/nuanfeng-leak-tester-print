#!/usr/bin/env python3
"""
程序号写入读取验证工具
完整的独立验证脚本
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

def send_raw(data_hex, timeout=3, debug=False):
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

def test_connection():
    """测试基本连接"""
    print("测试1: 基本连接测试")
    print("-" * 50)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect((WINDOWS_HOST_IP, TCP_PORT))
        print("  ✅ 网络连接成功!")
        sock.close()
        return True
    except Exception as e:
        print(f"  ❌ 网络连接失败: {e}")
        print("  请确保:")
        print("  1. Windows 端正在运行 serial_to_tcp.py 或 windows_proxy.py")
        print("  2. COM 端口正确配置并且仪器已连接")
        print("  3. Windows 防火墙允许端口 502 的连接")
        return False

def read_program_number(debug=True):
    """读取当前程序号
    
    根据用户提供的规则: 
    - 读取地址: 0x202 (514)
    - 程序号 = 寄存器值 + 1
    """
    if debug:
        print("\n读取当前程序号...")
    
    # 功能码 03: 读取保持寄存器
    cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    
    if debug:
        print(f"  命令: {full_cmd}")
    
    response = send_raw(full_cmd, debug=debug)
    
    if not response or len(response) < 10:
        if debug:
            print(f"  读取失败: {response}")
        return None
    
    try:
        if not response.startswith(f"{STATION_ID:02X}03"):
            if debug:
                print(f"  响应格式错误")
            return None
        
        data_hex = response[6:10]
        register_value = int(data_hex, 16)
        program_number = register_value + 1  # 根据用户规则
        
        if debug:
            print(f"  寄存器值: 0x{register_value:04X} = {register_value}")
            print(f"  当前程序号: {program_number}")
        
        return program_number
        
    except Exception as e:
        if debug:
            print(f"  解析错误: {e}")
        return None

def write_program_number(program_number, debug=True):
    """写入程序号
    
    根据用户提供的协议:
    - 功能码: 0x10 (写多个寄存器)
    - 地址: 0x0200
    - 数据格式: 高字节=0，低字节=程序号
    """
    if debug:
        print(f"\n写入程序号 {program_number}...")
    
    if program_number < 1 or program_number > 255:
        if debug:
            print(f"  程序号超出范围 (1-255)")
        return False
    
    # 构造命令: 高字节=0，低字节=程序号
    data_hex = f"00{program_number:02X}"
    cmd = f"{STATION_ID:02X}10{0x0200:04X}000102{data_hex}"
    full_cmd = cmd + modbus_crc(cmd)
    
    if debug:
        print(f"  数据: 00 {program_number:02X}")
        print(f"  命令: {full_cmd}")
    
    response = send_raw(full_cmd, debug=debug)
    
    if response and response.startswith(f"{STATION_ID:02X}10"):
        if debug:
            print("  写入成功!")
        return True
    else:
        if debug:
            print(f"  写入失败: {response}")
        return False

def verify_single_program(program_number):
    """验证单个程序号的写入和读取"""
    print(f"\n{'='*60}")
    print(f"验证程序号: {program_number}")
    print('='*60)
    
    # 写入程序号
    write_success = write_program_number(program_number)
    if not write_success:
        print("❌ 写入失败!")
        return False
    
    # 等待仪器处理
    time.sleep(0.3)
    
    # 读取程序号
    read_back = read_program_number()
    
    # 验证
    if read_back == program_number:
        print(f"✅ 验证通过! 写入={program_number}, 读取={read_back}")
        return True
    else:
        print(f"❌ 验证失败! 写入={program_number}, 读取={read_back}")
        return False

def main():
    print("=" * 60)
    print("ATEQ 程序号写入读取验证工具")
    print("=" * 60)
    
    # 测试连接
    if not test_connection():
        print("\n请先确保 Windows 端运行 serial_to_tcp.py:")
        print("  python serial_to_tcp.py")
        return
    
    # 读取初始程序号
    initial_program = read_program_number()
    if initial_program:
        print(f"\n当前仪器程序号: {initial_program}")
    
    # 验证测试
    test_programs = [1, 2, 3, 5, 10]
    results = []
    
    print("\n开始程序号验证测试...")
    for prog in test_programs:
        results.append(verify_single_program(prog))
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("测试汇总")
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"测试结果: {passed}/{total} 个通过")
    
    for i, prog in enumerate(test_programs):
        status = "PASS" if results[i] else "FAIL"
        print(f"  程序 {prog}: {status}")
    
    # 恢复初始程序号
    if initial_program:
        print(f"\n恢复初始程序号: {initial_program}")
        write_program_number(initial_program, debug=False)

if __name__ == "__main__":
    main()
