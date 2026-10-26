#!/usr/bin/env python3
"""
按照ATEQ手册正确流程读取程序参数
参考流程:
1. 选择要编辑的程序：向地址 3004h 写入目标程序号（程序号需减1）
2. 准备要读取的参数标识符列表：向地址 00h 写入参数数量及标识符
3. 读取参数值：从地址 00h 读取数据（每个参数占3个字：标识符+长整型值）
"""

import socket
import time

# 配置参数
WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502
STATION_ID = 1

# 参数标识符定义
PARAM_IDS = {
    0x0001: "填充时间 (Fill time)",
    0x0002: "稳定时间 (Stabilization time)",
    0x0003: "测试时间 (Test time)",
    0x0006: "预填充时间 (Pre-fill time)",
    0x0009: "排放时间 (Dump time)",
    0x0015: "测试类型 (Test type)",
    0x0032: "最小压力值 (Min pressure)",
    0x0033: "最大压力值 (Max pressure)",
    0x0035: "压力单位 (Pressure unit)",
    0x007F: "泄漏单位 (Reject unit)",
}

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

def send_raw(data_hex, timeout=5, debug=False):
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

def write_single_register(address, value, debug=False):
    """使用功能码06写单个寄存器"""
    cmd = f"{STATION_ID:02X}06{address:04X}{value:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd, debug=debug)
    return resp is not None and resp.startswith(f"{STATION_ID:02X}06")

def write_multiple_registers(address, values, debug=False):
    """使用功能码10写多个寄存器"""
    count = len(values)
    byte_count = count * 2
    values_hex = ''.join([f"{v:04X}" for v in values])
    cmd = f"{STATION_ID:02X}10{address:04X}{count:04X}{byte_count:02X}{values_hex}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd, debug=debug)
    return resp is not None and resp.startswith(f"{STATION_ID:02X}10")

def read_holding_registers(start_address, count, debug=False):
    """使用功能码03读取保持寄存器"""
    cmd = f"{STATION_ID:02X}03{start_address:04X}{count:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd, debug=debug)
    
    if resp and len(resp) >= 5 + count * 4 and resp.startswith(f"{STATION_ID:02X}03"):
        data_bytes = int(resp[4:6], 16)
        data_hex = resp[6:6 + data_bytes * 2]
        registers = []
        for i in range(0, len(data_hex), 4):
            if i + 4 <= len(data_hex):
                reg_val = int(data_hex[i:i+4], 16)
                registers.append(reg_val)
        return registers
    return None

def select_program_for_editing(program_num, debug=False):
    """
    步骤1: 选择要编辑的程序
    向地址 3004h 写入目标程序号（程序号需减1）
    """
    program_val = program_num - 1
    if debug:
        print(f"选择程序 {program_num}: 向地址0x3004写入 {program_val} (程序号-1)")
    return write_single_register(0x3004, program_val, debug=debug)

def prepare_parameter_list(param_ids, debug=False):
    """
    步骤2: 准备要读取的参数标识符列表
    向地址 00h 写入: [参数数量][参数ID1][参数ID2]...
    """
    # 构造数据: 数量 + 参数ID列表
    data = [len(param_ids)] + param_ids
    if debug:
        print(f"准备参数列表: 数量={len(param_ids)}, IDs={[hex(x) for x in param_ids]}")
        print(f"写入数据: {[hex(x) for x in data]}")
    return write_multiple_registers(0x0000, data, debug=debug)

def read_parameter_values(count, debug=False):
    """
    步骤3: 读取参数值
    从地址 00h 读取数据（每个参数占3个字：标识符+长整型值 = 2+4=6字节）
    返回: [(param_id, value), ...]
    """
    # 每个参数占3个字，共需要 3 * count 个字
    total_regs = 3 * count
    regs = read_holding_registers(0x0000, total_regs, debug=debug)
    
    if not regs or len(regs) < total_regs:
        return None
    
    results = []
    for i in range(count):
        base = i * 3
        # 每个参数: [标识符(1字)][值(2字=长整型)]
        param_id = regs[base]
        # 值是长整型（2字，4字节），需要正确解析
        value_high = regs[base + 1]
        value_low = regs[base + 2]
        
        # 解析为32位有符号整数（注意字节序）
        # 先交换每个字的字节，然后组合
        value_high_swapped = swap_bytes(value_high)
        value_low_swapped = swap_bytes(value_low)
        value = (value_high_swapped << 16) | value_low_swapped
        
        results.append((param_id, value, value_high, value_low, value_high_swapped, value_low_swapped))
    
    return results

def read_program_parameters(program_num, param_id_list, debug=False):
    """完整的读取流程"""
    print(f"\n{'='*60}")
    print(f"读取程序 {program_num} 的参数")
    print(f"{'='*60}")
    
    # 步骤1: 选择程序
    print(f"\n📌 步骤1: 选择程序 {program_num}")
    if not select_program_for_editing(program_num, debug=debug):
        print("❌ 选择程序失败")
        return None
    print("✅ 程序选择成功")
    time.sleep(0.1)
    
    # 步骤2: 准备参数列表
    print(f"\n📌 步骤2: 准备参数标识符列表")
    if not prepare_parameter_list(param_id_list, debug=debug):
        print("❌ 准备参数列表失败")
        return None
    print("✅ 参数列表准备成功")
    time.sleep(0.1)
    
    # 步骤3: 读取参数值
    print(f"\n📌 步骤3: 读取参数值")
    results = read_parameter_values(len(param_id_list), debug=debug)
    if not results:
        print("❌ 读取参数值失败")
        return None
    print("✅ 参数读取成功")
    
    # 解析和显示结果
    print(f"\n📊 读取结果:")
    print(f"{'-'*60}")
    print(f"{'ID(hex)':<10} {'ID(dec)':<8} {'参数名称':<30} {'原始值':<15} {'解析值'}")
    print(f"{'-'*60}")
    
    time_params = []
    total_ms = 0
    
    for param_id, value, vh, vl, vhs, vls in results:
        name = PARAM_IDS.get(param_id, f"未知参数({param_id})")
        
        # 时间参数通常以毫秒为单位
        display_value = f"{value} ms"
        if value >= 1000:
            display_value += f" = {value/1000:.2f} s"
        
        # 收集时间参数
        if param_id in [0x0001, 0x0002, 0x0003, 0x0006, 0x0009]:
            time_params.append((name, value))
            total_ms += value
        
        print(f"0x{param_id:04X}    {param_id:<8} {name:<30} {value:<15} {display_value}")
        if debug:
            print(f"  调试: 高字=0x{vh:04X}->0x{vhs:04X}, 低字=0x{vl:04X}->0x{vls:04X}")
    
    print(f"{'-'*60}")
    
    # 计算时间总和
    if time_params:
        print(f"\n⏱️  时间参数统计:")
        for name, value in time_params:
            print(f"  {name}: {value} ms = {value/1000:.2f} s")
        print(f"\n📈 时间参数总和:")
        print(f"  总时间: {total_ms} ms = {total_ms/1000:.2f} s = {total_ms/60000:.2f} min")
    
    return results

if __name__ == "__main__":
    import sys
    
    # 要读取的参数ID列表（时间参数）
    TIME_PARAM_IDS = [0x0001, 0x0002, 0x0003, 0x0006, 0x0009]
    
    # 常用参数完整列表
    ALL_PARAM_IDS = [0x0001, 0x0002, 0x0003, 0x0006, 0x0009, 0x0015, 0x0032, 0x0033, 0x0035, 0x007F]
    
    debug = False
    if len(sys.argv) > 1 and sys.argv[1] == 'debug':
        debug = True
    
    # 读取程序1的时间参数
    read_program_parameters(1, TIME_PARAM_IDS, debug=debug)
    
    # 可选: 读取所有参数
    # read_program_parameters(1, ALL_PARAM_IDS, debug=debug)
