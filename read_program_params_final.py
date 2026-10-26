#!/usr/bin/env python3
import socket
import time

WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502
STATION_ID = 1

# 参数定义 - 按标识符
PARAMS = [
    (0x0001, "填充时间", "Fill time", "ms"),
    (0x0002, "稳定时间", "Stabilization time", "ms"),
    (0x0003, "测试时间", "Test time", "ms"),
    (0x0006, "预填充时间", "Pre-fill time", "ms"),
    (0x0009, "排放时间", "Dump time", "ms"),
    (0x0015, "测试类型", "Test type", ""),
    (0x0032, "最小压力值", "Min pressure", ""),
    (0x0033, "最大压力值", "Max pressure", ""),
    (0x0035, "压力单位", "Pressure unit", ""),
    (0x007F, "泄漏单位", "Reject unit", ""),
]

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

def swap_bytes(value):
    return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)

def send_raw(data_hex, timeout=3, debug=False):
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
        print(f"  错误: {e}")
        return None
    finally:
        sock.close()

def reset_instrument():
    """发送复位命令"""
    print("发送复位命令...")
    reset_cmd = "01050000FF008C0A"
    send_raw(reset_cmd, timeout=1, debug=False)
    time.sleep(2)
    print("复位完成")

def read_current_program():
    """读取当前程序号"""
    cmd = "010302020001"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd, timeout=2, debug=False)
    if resp and len(resp) >= 10:
        reg_val = int(resp[6:10], 16)
        return swap_bytes(reg_val) + 1
    return None

def select_program(program_num):
    """选择程序"""
    value = program_num - 1
    cmd = f"{STATION_ID:02X}06{0x3004:04X}{value:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    print(f"选择程序 {program_num}: {full_cmd}")
    resp = send_raw(full_cmd, timeout=2, debug=True)
    if resp and resp.startswith(f"{STATION_ID:02X}06"):
        time.sleep(0.5)
        return True
    return False

def read_holding_registers(start_address, count):
    """读取保持寄存器"""
    cmd = f"{STATION_ID:02X}03{start_address:04X}{count:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd, timeout=3, debug=True)
    if resp and len(resp) >= 10 and resp.startswith(f"{STATION_ID:02X}03"):
        data_bytes = int(resp[4:6], 16)
        data_hex = resp[6:6 + data_bytes * 2]
        registers = []
        for i in range(0, len(data_hex), 4):
            if i + 4 <= len(data_hex):
                registers.append(int(data_hex[i:i+4], 16))
        return registers
    return None

def wait_for_connection(max_attempts=10):
    """等待连接恢复"""
    print("等待仪器连接...")
    for i in range(max_attempts):
        prog = read_current_program()
        if prog:
            print(f"✅ 连接成功! 当前程序号: {prog}")
            return prog
        print(f"  尝试 {i+1}/{max_attempts} 失败，等待...")
        time.sleep(1)
    print("❌ 无法连接仪器，请手动复位仪器")
    return None

def read_program_params(program_num=1):
    """读取指定程序的所有参数"""
    print("\n" + "=" * 70)
    print(f"📊 读取程序 {program_num} 的参数")
    print("=" * 70)

    # 选择程序
    if not select_program(program_num):
        print("❌ 选择程序失败")
        return None

    # 方法1: 直接读取0x0400开始的区域（之前发现的数据区域）
    print("\n📌 方法1: 读取0x0400开始的参数区域 (选择程序后自动加载)")
    print("-" * 50)
    
    all_params = []
    time_values = []
    
    for block in range(4):
        start = 0x0400 + (block * 8)
        regs = read_holding_registers(start, 8)
        if regs:
            for i, val in enumerate(regs):
                addr = start + i
                swapped = swap_bytes(val)
                # 检查是否为合理的时间值
                if 10 <= swapped <= 600000:
                    time_values.append(swapped)
                    display = f"{swapped} ms = {swapped/1000:.3f} s"
                else:
                    display = f"{swapped}"
                
                # 尝试匹配参数名称
                param_idx = (addr - 0x0400)
                param_info = None
                for param_id, name_cn, name_en, unit in PARAMS:
                    if param_id - 1 == param_idx:
                        param_info = (name_cn, unit)
                        break
                if param_info:
                    name, unit = param_info
                else:
                    name = f"参数{param_idx+1}"
                    unit = ""
                
                all_params.append({
                    'index': param_idx,
                    'name': name,
                    'raw_value': val,
                    'value': swapped,
                    'display': display
                })
                
                print(f"  [{param_idx:2d}] {name:<15} {display}")
        time.sleep(0.2)

    # 计算时间总和
    if time_values:
        print("\n" + "-" * 50)
        print(f"⏱️  时间参数列表(ms): {time_values}")
        total_ms = sum(time_values)
        print(f"📈 时间参数总和: {total_ms} ms = {total_ms/1000:.2f} s = {total_ms/60000:.2f} min")

    # 方法2: 尝试按标识符读取参数 (0x0001, 0x0002等标识符对应的地址)
    print("\n" + "-" * 50)
    print("📌 方法2: 按标识符直接读取参数")
    print("-" * 50)
    
    for param_id, name_cn, name_en, unit in PARAMS:
        # 尝试读取标识符对应的地址
        regs = read_holding_registers(param_id * 2, 2)  # 每个参数占2个寄存器
        if regs and len(regs) >= 2:
            val = (swap_bytes(regs[0]) << 16) | swap_bytes(regs[1])
            if 0 < val < 600000:
                print(f"  {name_cn}: {val} ms = {val/1000:.3f} s")
        time.sleep(0.1)

    return all_params

if __name__ == "__main__":
    import sys
    
    # 先尝试复位和等待连接
    reset_instrument()
    current_prog = wait_for_connection(15)
    
    if current_prog:
        # 读取程序1的参数
        params = read_program_params(1)
        
        # 也可以尝试读取其他程序
        print("\n" + "=" * 70)
        print("📋 也可以读取其他程序的参数，例如:")
        print("  python read_program_params_final.py 2   # 读取程序2")
        print("=" * 70)
