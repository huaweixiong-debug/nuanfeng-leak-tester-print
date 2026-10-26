#!/usr/bin/env python3
"""
独立的程序号验证脚本
不依赖webui，直接通过Modbus协议写入和读取程序号进行验证
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

def send_raw(data_hex, timeout=2):
    """发送原始十六进制数据并返回响应"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((WINDOWS_HOST_IP, TCP_PORT))
        data = bytes.fromhex(data_hex)
        sock.sendall(data)
        response = sock.recv(1024)
        return response.hex().upper()
    except Exception as e:
        print(f"  连接错误: {e}")
        return None
    finally:
        sock.close()

def select_program(program_number):
    """选择程序号
    
    根据用户提供的协议:
    - 功能码: 0x10 (写多个寄存器)
    - 地址: 0x0200
    - 数据格式: 高字节=0，低字节=程序号
    """
    if program_number < 1 or program_number > 255:
        print(f"  错误: 程序号 {program_number} 超出范围 (1-255)")
        return False
    
    # 构造命令
    data_hex = f"00{program_number:02X}"  # 高字节=00，低字节=程序号
    cmd = f"{STATION_ID:02X}10{0x0200:04X}000102{data_hex}"
    full_cmd = cmd + modbus_crc(cmd)
    
    print(f"  写入命令: {full_cmd}")
    print(f"  数据部分: 00 {program_number:02X} (高字节=00, 低字节={program_number:02X})")
    
    response = send_raw(full_cmd)
    
    if response:
        print(f"  写入响应: {response}")
        return True
    else:
        print(f"  写入失败: 无响应")
        return False

def read_current_program():
    """读取当前程序号
    
    根据用户提供的规则: 程序号 = 读取值 + 1
    """
    # 读取地址 0x202
    cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    
    print(f"  读取命令: {full_cmd}")
    
    response = send_raw(full_cmd)
    
    if not response or len(response) < 10:
        print(f"  读取失败: 响应太短或无响应 ({response})")
        return None
    
    try:
        if not response.startswith(f"{STATION_ID:02X}03"):
            print(f"  读取失败: 响应格式错误 ({response})")
            return None
        
        data_hex = response[6:10]
        register_value = int(data_hex, 16)
        program_number = register_value + 1  # 根据规则: 程序号 = 读取值 + 1
        
        print(f"  读取结果: 寄存器值=0x{register_value:04X}={register_value}, 程序号={program_number}")
        return program_number
        
    except Exception as e:
        print(f"  解析错误: {e}")
        return None

def verify_program(program_number):
    """验证单个程序号的写入和读取"""
    print(f"\n{'='*60}")
    print(f"验证程序号: {program_number}")
    print(f"{'='*60}")
    
    # 写入程序号
    print("1. 写入程序号...")
    if not select_program(program_number):
        print("   写入失败!")
        return False
    
    # 等待一下
    time.sleep(0.5)
    
    # 读取程序号
    print("2. 读取程序号...")
    read_back = read_current_program()
    
    if read_back is None:
        print("   读取失败!")
        return False
    
    # 验证
    if read_back == program_number:
        print(f"3. 验证结果: PASS (写入={program_number}, 读取={read_back})")
        return True
    else:
        print(f"3. 验证结果: FAIL (写入={program_number}, 读取={read_back})")
        print(f"   不匹配! 请检查协议是否正确!")
        return False

def main():
    print("ATEQ 程序号写入读取验证工具")
    print("=" * 60)
    
    # 要测试的程序号列表
    test_programs = [1, 2, 3, 4, 5, 10, 20, 50, 100]
    
    results = []
    for prog in test_programs:
        results.append(verify_program(prog))
    
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

if __name__ == "__main__":
    main()
