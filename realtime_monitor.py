#!/usr/bin/env python3
"""
实时读取 ATEQ 仪器的压力和泄漏量
读取地址: 0x30 (实时状态寄存器)
"""

import socket
import time

# 配置参数
WINDOWS_HOST_IP = '172.18.144.1'  # Windows 主机 IP
TCP_PORT = 502                     # TCP 端口
STATION_ID = 1                     # 设备站号
READ_INTERVAL = 1                  # 读取间隔(秒)

def get_unit_name(unit_code):
    """获取单位名称 - 根据 ATEQ 官方文档更新"""
    unit_table = {
        0: "cm³/s",
        1000: "cm³/min",
        2000: "cm³/h",
        3000: "mm³/h",
        6000: "Pa",
        11000: "Bar",
        12000: "kPa",
        13000: "PSI",
        14000: "mBar",
        15000: "MPa",
        30000: "L/h",
        46000: "in³/s",
        47000: "in³/min",
        48000: "in³/h",
        49000: "ft³/h",
        50000: "mL/s",
        51000: "mL/min",
        52000: "mL/h",
    }
    return unit_table.get(unit_code, f"Unknown({unit_code})")

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

def send_raw(data_hex):
    """发送原始十六进制数据并返回响应"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
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

def parse_realtime_data(response_hex):
    """解析实时数据响应"""
    if not response_hex or len(response_hex) < 30:
        return None
    
    data = bytes.fromhex(response_hex)
    registers = []
    for i in range(3, 29, 2):
        registers.append((data[i] << 8) | data[i+1])
    
    while len(registers) < 13:
        registers.append(0)
    
    # 解析压力值 (32位有符号整数)
    pressure_raw = ((registers[6] & 0xFF) << 24) | ((registers[6] >> 8) << 16) | ((registers[5] & 0xFF) << 8) | (registers[5] >> 8)
    if pressure_raw & 0x80000000:
        pressure_raw -= 0x100000000
    pressure = pressure_raw / 1000.0
    
    # 压力单位
    pressure_unit_code = ((registers[8] & 0xFF) << 16) | ((registers[8] >> 8) << 8) | ((registers[7] & 0xFF) << 8) | (registers[7] >> 8)
    
    # 解析泄漏量值 (32位有符号整数)
    leak_raw = ((registers[10] & 0xFF) << 24) | ((registers[10] >> 8) << 16) | ((registers[9] & 0xFF) << 8) | (registers[9] >> 8)
    if leak_raw & 0x80000000:
        leak_raw -= 0x100000000
    leak = leak_raw / 1000.0
    
    # 泄漏量单位
    leak_unit_code = ((registers[12] & 0xFF) << 16) | ((registers[12] >> 8) << 8) | ((registers[11] & 0xFF) << 8) | (registers[11] >> 8)
    
    # 状态位
    status = (registers[3] >> 8) | ((registers[3] & 0xFF) << 8)
    
    return {
        'pressure': pressure,
        'pressure_unit': get_unit_name(pressure_unit_code),
        'pressure_unit_code': pressure_unit_code,
        'leak': leak,
        'leak_unit': get_unit_name(leak_unit_code),
        'leak_unit_code': leak_unit_code,
        'status': status,
        'step_code': (registers[4] >> 8) | ((registers[4] & 0xFF) << 8),
    }

def translate_status(status):
    """翻译状态位"""
    bits = []
    if status & 0x8000: bits.append("键盘锁定")
    if status & 0x0020: bits.append("循环结束")
    if status & 0x0010: bits.append("报警")
    if status & 0x0008: bits.append("参考端失败")
    if status & 0x0004: bits.append("测试端失败")
    if status & 0x0002: bits.append("测试失败")
    if status & 0x0001: bits.append("测试通过")
    return " | ".join(bits) if bits else "待机中"

def main():
    """主函数 - 实时读取压力和泄漏量"""
    print("=" * 70)
    print("ATEQ 仪器实时数据监测")
    print("=" * 70)
    print(f"连接地址: {WINDOWS_HOST_IP}:{TCP_PORT}")
    print(f"读取间隔: {READ_INTERVAL} 秒")
    print("=" * 70)
    print()
    
    try:
        while True:
            response = read_holding_registers(0x30, 13)
            if response:
                data = parse_realtime_data(response)
                if data:
                    # 获取当前时间
                    current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    
                    # 清屏并显示数据
                    print(f"\033[H\033[J", end="")  # ANSI 清屏码
                    print("=" * 70)
                    print(f"ATEQ 仪器实时数据监测 | 更新时间: {current_time}")
                    print("=" * 70)
                    print()
                    print(f"📊 状态: {translate_status(data['status'])}")
                    print(f"🔢 步骤代码: {data['step_code']}")
                    print()
                    print(f"🌡️  压力: {data['pressure']:.3f} {data['pressure_unit']}")
                    print(f"💧 泄漏: {data['leak']:.6f} {data['leak_unit']}")
                    print()
                    print(f"原始数据 - 压力单位代码: {data['pressure_unit_code']}, 泄漏单位代码: {data['leak_unit_code']}")
                    print("=" * 70)
                    print(f"按 Ctrl+C 停止监测")
                else:
                    print("数据解析失败")
            else:
                print(f"\r连接失败，正在重试... (按 Ctrl+C 停止)", end="")
            
            time.sleep(READ_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n\n监测结束")
    except Exception as e:
        print(f"\n发生错误: {e}")

if __name__ == '__main__':
    main()