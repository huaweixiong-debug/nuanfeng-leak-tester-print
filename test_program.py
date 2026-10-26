#!/usr/bin/env python3
"""
测试程序选择功能
"""

import socket

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
        print(f"连接错误: {e}")
        return None
    finally:
        sock.close()

def read_holding_registers(address, count):
    """读取保持寄存器"""
    cmd = f"{STATION_ID:02X}03{address:04X}{count:04X}"
    return send_raw(cmd + modbus_crc(cmd))

def write_single_register(address, value):
    """写入单个保持寄存器 (功能码 0x06)"""
    cmd = f"{STATION_ID:02X}06{address:04X}{value & 0xFFFF:04X}"
    return send_raw(cmd + modbus_crc(cmd))

def read_current_program():
    """读取当前程序号"""
    response = read_holding_registers(0x202, 1)
    if response and len(response) >= 10:
        data_hex = response[6:10]
        return int(data_hex, 16)
    return None

def test_program_selection():
    """测试程序选择"""
    
    # 先读取当前程序
    current = read_current_program()
    print(f"当前程序号: {current}")
    
    # 测试选择程序 1
    print("\n--- 测试选择程序 1 ---")
    response = write_single_register(0x200, 1)
    print(f"写入响应: {response}")
    
    # 读取验证
    import time
    time.sleep(0.5)
    current = read_current_program()
    print(f"选择后程序号: {current}")
    
    # 测试选择程序 2
    print("\n--- 测试选择程序 2 ---")
    response = write_single_register(0x200, 2)
    print(f"写入响应: {response}")
    
    time.sleep(0.5)
    current = read_current_program()
    print(f"选择后程序号: {current}")
    
    # 测试选择程序 3
    print("\n--- 测试选择程序 3 ---")
    response = write_single_register(0x200, 3)
    print(f"写入响应: {response}")
    
    time.sleep(0.5)
    current = read_current_program()
    print(f"选择后程序号: {current}")

if __name__ == "__main__":
    test_program_selection()
