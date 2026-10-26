#!/usr/bin/env python3
"""
简化版程序号测试脚本
快速测试单个程序号的写入和读取
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

def send_raw(data_hex, timeout=2):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((WINDOWS_HOST_IP, TCP_PORT))
        sock.sendall(bytes.fromhex(data_hex))
        return sock.recv(1024).hex().upper()
    except:
        return None
    finally:
        sock.close()

def write_and_read(program_num):
    print(f"测试程序号: {program_num}")
    
    # 写入
    data_part = f"00{program_num:02X}"
    cmd = f"{STATION_ID:02X}10{0x0200:04X}000102{data_part}"
    full_cmd = cmd + modbus_crc(cmd)
    print(f"  写入: {full_cmd}")
    
    resp = send_raw(full_cmd)
    print(f"  响应: {resp}")
    
    time.sleep(0.2)
    
    # 读取
    cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    print(f"  读取: {full_cmd}")
    
    resp = send_raw(full_cmd)
    print(f"  响应: {resp}")
    
    if resp and len(resp) >= 10:
        reg_val = int(resp[6:10], 16)
        result = reg_val + 1
        print(f"  结果: 寄存器={reg_val}, 程序号={result}")
        return result == program_num
    return False

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        num = int(sys.argv[1])
        write_and_read(num)
    else:
        # 默认测试程序号 1-5
        for i in [1, 2, 3, 4, 5]:
            ok = write_and_read(i)
            print(f"  {'PASS' if ok else 'FAIL'}\n")
            time.sleep(0.5)
