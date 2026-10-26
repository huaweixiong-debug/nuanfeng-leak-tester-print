#!/usr/bin/env python3
"""
读取程序参数的测试代码
读取程序1中的时间参数并计算总和
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

def swap_bytes(value):
    """交换16位值的高低字节"""
    return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)

def send_raw(data_hex, timeout=2, debug=False):
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

def read_holding_registers(start_address, count, debug=False):
    """读取保持寄存器"""
    cmd = f"{STATION_ID:02X}03{start_address:04X}{count:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd, debug=debug)
    
    if resp and len(resp) >= 5 + count * 4 and resp.startswith(f"{STATION_ID:02X}03"):
        # 解析响应: 站号(2) + 功能码(2) + 字节数(2) + 数据...
        data_bytes = int(resp[4:6], 16)
        data_hex = resp[6:6 + data_bytes * 2]
        registers = []
        for i in range(0, len(data_hex), 4):
            if i + 4 <= len(data_hex):
                reg_val = int(data_hex[i:i+4], 16)
                registers.append(reg_val)
        return registers
    return None

def write_program(program_number):
    """写入程序号"""
    write_val = program_number - 1
    write_val_swapped = swap_bytes(write_val)
    cmd = f"{STATION_ID:02X}06{0x0200:04X}{write_val_swapped:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd)
    return resp is not None and resp.startswith(f"{STATION_ID:02X}06")

def read_current_program():
    """读取当前程序号"""
    regs = read_holding_registers(0x202, 1)
    if regs and len(regs) >= 1:
        reg_val_swapped = swap_bytes(regs[0])
        return reg_val_swapped + 1
    return None

def scan_program_parameters(program_num, start_addr, num_registers=32):
    """扫描程序参数"""
    print(f"\n=== 扫描程序 {program_num} 的参数 (地址 {start_addr:04X} 开始) ===")
    
    # 先切换到目标程序
    if write_program(program_num):
        print(f"已切换到程序 {program_num}")
        time.sleep(0.2)
    else:
        print(f"切换到程序 {program_num} 失败")
        return
    
    # 读取多个寄存器
    regs = read_holding_registers(start_addr, num_registers)
    if regs:
        print(f"地址 {start_addr:04X} 开始的 {num_registers} 个寄存器值:")
        for i, val in enumerate(regs):
            swapped = swap_bytes(val)
            print(f"  [{start_addr + i:04X}] = 0x{val:04X} = {val:5d} (交换后: 0x{swapped:04X} = {swapped:5d})")
        return regs
    else:
        print("读取失败")
        return None

def detect_time_parameters():
    """探测时间参数"""
    print("=" * 60)
    print("探测程序1的时间参数")
    print("=" * 60)
    
    # 先读取当前程序号
    current_prog = read_current_program()
    print(f"当前程序号: {current_prog}")
    
    # ATEQ 仪器的程序参数通常在 0x400-0x600 地址范围
    # 尝试几个可能的起始地址
    possible_starts = [0x400, 0x420, 0x440, 0x460, 0x480, 0x500, 0x600]
    
    for start in possible_starts:
        regs = scan_program_parameters(1, start, 20)
        if regs:
            # 检查是否有看起来像时间参数的值 (通常是0-60000之间的数)
            plausible_times = [swap_bytes(r) for r in regs if 0 < swap_bytes(r) < 60000]
            if len(plausible_times) >= 3:  # 至少有3个合理的时间值
                print(f"  *** 在地址 {start:04X} 发现可能的时间参数 ***")
                return start, regs
    
    return None, None

def read_and_calculate_time_sum():
    """读取时间参数并计算总和"""
    print("\n" + "=" * 60)
    print("读取程序1的时间参数并计算总和")
    print("=" * 60)
    
    # 先切换到程序1
    if not write_program(1):
        print("切换到程序1失败")
        return
    
    time.sleep(0.3)
    
    # ATEQ F620 通常的时间参数地址:
    # 根据文档，程序参数通常在 0x400 + (program-1)* 参数数量
    # 尝试读取 0x400 开始的区域
    
    # 常见的时间参数:
    # - 填充时间 (Fill time)
    # - 稳定时间 (Stabilization time)
    # - 测试时间 (Test time)
    # - 排气时间 (Vent time)
    
    print("\n尝试读取程序1的时间参数区域 (0x400-0x430)...")
    regs = read_holding_registers(0x400, 24)  # 读取更多寄存器
    
    if regs:
        print("\n寄存器值 (地址: 原始值 -> 交换后值):")
        time_values = []
        for i, val in enumerate(regs):
            swapped = swap_bytes(val)
            addr = 0x400 + i
            print(f"  0x{addr:04X}: 0x{val:04X}={val:5d} -> 0x{swapped:04X}={swapped:5d}")
            
            # 收集合理的时间值 (0-60000 ms)
            if 0 <= swapped <= 60000:
                time_values.append(swapped)
        
        print("\n可能的时间参数列表:", time_values)
        
        # 计算总和
        if time_values:
            total = sum(time_values)
            print(f"\n时间参数总和: {total} ms = {total/1000:.2f} 秒")
            
            # 尝试识别常见的时间参数
            if len(time_values) >= 4:
                print("\n参数识别尝试:")
                print(f"  填充时间: {time_values[0]} ms")
                print(f"  稳定时间: {time_values[1]} ms")
                print(f"  测试时间: {time_values[2]} ms")
                print(f"  排气时间: {time_values[3]} ms")
                if len(time_values) > 4:
                    print(f"  其他参数: {time_values[4:]} ms")
        else:
            print("\n未找到有效的时间参数")
    else:
        print("读取寄存器失败")
    
    # 也尝试其他可能的地址
    print("\n" + "-" * 60)
    print("尝试其他可能的地址区域...")
    for base_addr in [0x420, 0x440, 0x460, 0x480, 0x500]:
        regs = read_holding_registers(base_addr, 16)
        if regs:
            swapped_vals = [swap_bytes(r) for r in regs]
            plausible = [v for v in swapped_vals if 0 < v < 60000]
            if len(plausible) >= 3:
                print(f"\n地址 0x{base_addr:04X} 发现可能的时间参数:")
                print(f"  值: {plausible}")
                print(f"  总和: {sum(plausible)} ms = {sum(plausible)/1000:.2f} 秒")

if __name__ == "__main__":
    # 先探测时间参数位置
    detect_time_parameters()
    
    # 然后读取并计算总和
    read_and_calculate_time_sum()
