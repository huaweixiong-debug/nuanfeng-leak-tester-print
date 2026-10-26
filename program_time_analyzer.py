#!/usr/bin/env python3
"""
程序时间参数分析器
专门读取程序1中的时间参数并计算总和
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

def read_holding_registers(start_address, count):
    """读取保持寄存器"""
    cmd = f"{STATION_ID:02X}03{start_address:04X}{count:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd)
    
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

def write_program(program_number):
    """写入程序号"""
    write_val = program_number - 1
    write_val_swapped = swap_bytes(write_val)
    cmd = f"{STATION_ID:02X}06{0x0200:04X}{write_val_swapped:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd)
    return resp is not None and resp.startswith(f"{STATION_ID:02X}06")

def read_time_parameters(program_num=1):
    """读取指定程序的时间参数"""
    print("=" * 70)
    print(f"读取程序 {program_num} 的时间参数")
    print("=" * 70)
    
    # 切换到目标程序
    if not write_program(program_num):
        print(f"❌ 切换到程序 {program_num} 失败")
        return None
    
    time.sleep(0.3)
    print(f"✅ 已切换到程序 {program_num}")
    
    # ATEQ F620 的时间参数通常在这些地址区域
    # 根据扫描结果，重点检查 0x0420 - 0x04B0 区域
    base_addrs = [0x0420, 0x0480, 0x0400, 0x0460, 0x04A0]
    all_params = {}
    
    for base in base_addrs:
        regs = read_holding_registers(base, 20)
        if regs:
            swapped = [swap_bytes(r) for r in regs]
            all_params[base] = swapped
    
    if not all_params:
        print("❌ 无法读取任何参数")
        return None
    
    # 分析并找出合理的时间参数
    print("\n📊 时间参数分析结果:")
    print("-" * 70)
    
    # 时间参数名称映射（根据ATEQ常见参数）
    param_names = [
        "填充时间 (Fill)",
        "稳定时间 (Stab)",
        "测试时间 (Test)",
        "排气时间 (Vent)",
        "延迟1 (Delay1)",
        "延迟2 (Delay2)",
        "超时时间 (Timeout)",
        "吹扫时间 (Purge)",
        "等待时间 (Wait)",
        "校准时间 (Cal)",
    ]
    
    # 找出最可能的时间参数区域
    best_base = None
    best_values = None
    max_valid = 0
    
    for base, values in all_params.items():
        # 统计合理的时间值 (0 < 值 < 60000 ms)
        valid_count = sum(1 for v in values if 0 <= v <= 60000 and v != 65535)
        print(f"地址 0x{base:04X}: 有效参数数量 = {valid_count}")
        
        if valid_count > max_valid:
            max_valid = valid_count
            best_base = base
            best_values = values
    
    if best_values is None:
        print("❌ 未找到有效的时间参数区域")
        return None
    
    print(f"\n✅ 最佳参数区域: 地址 0x{best_base:04X}")
    print("-" * 70)
    
    # 提取并显示时间参数
    time_params = []
    total_ms = 0
    
    print(f"{'序号':<4} {'参数名称':<20} {'原始值(ms)':<10} {'转换值':<15} {'备注'}")
    print(f"{'-'*4} {'-'*20} {'-'*10} {'-'*15} {'-'*20}")
    
    for i, val in enumerate(best_values[:12]):  # 显示前12个参数
        if val == 0xFFFF or val == 65535:  # 无效值
            display_val = "无效"
            ms_val = 0
        else:
            ms_val = val
            total_ms += ms_val
            if ms_val >= 1000:
                display_val = f"{ms_val/1000:.2f} s"
            else:
                display_val = f"{ms_val} ms"
        
        param_name = param_names[i] if i < len(param_names) else f"参数{i+1}"
        
        # 标记可能的主要时间参数
        remark = ""
        if 1000 <= ms_val <= 30000:  # 1-30秒可能是主要时间
            remark = "⭐ 主要时间"
        elif 100 <= ms_val < 1000:  # 0.1-1秒可能是延迟
            remark = "⚡ 短延迟"
        
        time_params.append({
            "index": i + 1,
            "name": param_name,
            "ms": ms_val,
            "display": display_val,
            "remark": remark
        })
        
        print(f"{i+1:<4} {param_name:<20} {ms_val:<10} {display_val:<15} {remark}")
    
    # 计算总和
    print("-" * 70)
    print(f"\n📈 时间参数统计:")
    print(f"  参数总数: {len(time_params)}")
    print(f"  非零参数: {sum(1 for p in time_params if p['ms'] > 0)}")
    print(f"  总时间: {total_ms} ms = {total_ms/1000:.2f} 秒 = {total_ms/60000:.2f} 分钟")
    
    # 主要时间总和（排除0值和特别小的值）
    main_times = [p['ms'] for p in time_params if p['ms'] >= 100]  # >= 100ms
    main_total = sum(main_times)
    if main_times:
        print(f"  主要时间总和(>=100ms): {main_total} ms = {main_total/1000:.2f} 秒")
    
    # 显示详细的参数列表供参考
    print(f"\n📋 所有参数值列表:")
    print(f"  {[p['ms'] for p in time_params]}")
    
    return time_params, total_ms

def compare_programs(prog_list):
    """比较多个程序的时间参数"""
    print("\n" + "=" * 70)
    print(f"比较多个程序的时间参数")
    print("=" * 70)
    
    results = {}
    for prog in prog_list:
        params, total = read_time_parameters(prog)
        if params:
            results[prog] = {
                "params": params,
                "total": total
            }
        time.sleep(0.5)
    
    if results:
        print("\n" + "=" * 70)
        print("📊 多程序时间参数比较汇总")
        print("=" * 70)
        print(f"{'程序号':<8} {'总时间(ms)':<12} {'总时间(秒)':<12} {'总时间(分钟)':<15}")
        print("-" * 50)
        for prog, data in results.items():
            total = data['total']
            print(f"程序{prog:<4} {total:<12} {total/1000:<12.2f} {total/60000:<15.2f}")

if __name__ == "__main__":
    # 读取程序1的时间参数
    read_time_parameters(1)
    
    # 如果需要比较多个程序，取消下面的注释
    # compare_programs([1, 2, 3])
