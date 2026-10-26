#!/usr/bin/env python3
"""
程序时间参数读取测试代码
功能: 读取指定程序中的时间参数并计算总和
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

def send_raw(data_hex, timeout=2):
    """发送原始十六进制数据并返回响应"""
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

def read_current_program():
    """读取当前程序号"""
    cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd)
    if resp and len(resp) >= 10 and resp.startswith(f"{STATION_ID:02X}03"):
        reg_val = int(resp[6:10], 16)
        reg_val_swapped = swap_bytes(reg_val)
        return reg_val_swapped + 1
    return None

def write_program(program_number, retries=3):
    """切换到指定程序"""
    for i in range(retries):
        write_val = program_number - 1
        write_val_swapped = swap_bytes(write_val)
        cmd = f"{STATION_ID:02X}06{0x0200:04X}{write_val_swapped:04X}"
        full_cmd = cmd + modbus_crc(cmd)
        resp = send_raw(full_cmd)
        if resp and resp.startswith(f"{STATION_ID:02X}06"):
            time.sleep(0.2)
            if read_current_program() == program_number:
                return True
        time.sleep(0.1)
    return False

def read_holding_registers(start_address, count):
    """读取保持寄存器（支持分批读取）"""
    max_per_read = 32
    all_regs = []
    for offset in range(0, count, max_per_read):
        read_count = min(max_per_read, count - offset)
        cmd = f"{STATION_ID:02X}03{(start_address + offset):04X}{read_count:04X}"
        full_cmd = cmd + modbus_crc(cmd)
        resp = send_raw(full_cmd)
        if resp and len(resp) >= 5 + read_count * 4 and resp.startswith(f"{STATION_ID:02X}03"):
            data_bytes = int(resp[4:6], 16)
            data_hex = resp[6:6 + data_bytes * 2]
            for i in range(0, len(data_hex), 4):
                if i + 4 <= len(data_hex):
                    reg_val = int(data_hex[i:i+4], 16)
                    all_regs.append(reg_val)
        else:
            return None
        time.sleep(0.05)
    return all_regs

def get_time_parameters(program_num=1):
    """
    获取指定程序的时间参数
    
    返回:
    {
        'program_num': 程序号,
        'time_params': [时间参数列表(ms)],
        'total_ms': 总时间(ms),
        'total_seconds': 总时间(秒),
        'total_minutes': 总时间(分钟),
        'high_confidence': 高置信度时间参数,
        'all_detected': 所有检测到的可能时间值
    }
    """
    result = {
        'program_num': program_num,
        'time_params': [],
        'total_ms': 0,
        'total_seconds': 0,
        'total_minutes': 0,
        'high_confidence': [],
        'all_detected': []
    }
    
    # 切换到目标程序
    current_prog = read_current_program()
    if current_prog != program_num:
        if not write_program(program_num):
            print(f"❌ 切换到程序 {program_num} 失败")
            return result
    
    print(f"✅ 已切换到程序 {program_num}")
    
    # ATEQ 时间参数存储区域
    # 根据测试，主要时间参数存储在 0x0400-0x0480 区域
    regions_to_check = [
        (0x0400, 64, "主要参数区"),
    ]
    
    all_values = []
    high_confidence = []
    
    for start, count, name in regions_to_check:
        regs = read_holding_registers(start, count)
        if regs:
            for i, val in enumerate(regs):
                swapped = swap_bytes(val)
                # 筛选合理的时间值 (1ms - 10分钟)
                if 1 <= swapped <= 600000 and swapped != 65535:
                    all_values.append(swapped)
                    # 高置信度: 100ms倍数且在1-60秒之间
                    if swapped % 100 == 0 and swapped <= 60000:
                        high_confidence.append(swapped)
    
    # 去重并排序
    all_values = sorted(list(set(all_values)))
    high_confidence = sorted(list(set(high_confidence)))
    
    # 计算总和（使用高置信度值）
    if high_confidence:
        total_ms = sum(high_confidence)
        result['time_params'] = high_confidence
        result['total_ms'] = total_ms
        result['total_seconds'] = total_ms / 1000
        result['total_minutes'] = total_ms / 60000
        result['high_confidence'] = high_confidence
        result['all_detected'] = all_values
    else:
        # 如果没有高置信度值，使用所有检测到的值
        total_ms = sum(all_values) if all_values else 0
        result['time_params'] = all_values
        result['total_ms'] = total_ms
        result['total_seconds'] = total_ms / 1000
        result['total_minutes'] = total_ms / 60000
        result['high_confidence'] = []
        result['all_detected'] = all_values
    
    return result

def print_time_report(result, show_all=False):
    """打印时间参数报告"""
    print("\n" + "=" * 70)
    print(f"📊 程序 {result['program_num']} 时间参数分析报告")
    print("=" * 70)
    
    if result['high_confidence']:
        print(f"\n🌟 高置信度时间参数 ({len(result['high_confidence'])} 个):")
        for i, t in enumerate(result['high_confidence'], 1):
            if t >= 1000:
                print(f"   {i}. {t} ms = {t/1000:.2f} 秒")
            else:
                print(f"   {i}. {t} ms")
        
        print(f"\n⏱️  时间总和 (高置信度):")
        print(f"   总时间: {result['total_ms']} ms")
        print(f"   总时间: {result['total_seconds']:.2f} 秒")
        print(f"   总时间: {result['total_minutes']:.2f} 分钟")
        print(f"   平均: {result['total_ms']/len(result['high_confidence']):.0f} ms/参数")
    else:
        print("\n⚠️  未检测到高置信度时间参数")
        if result['all_detected']:
            print(f"\n📋 所有检测到的可能值:")
            print(f"   {result['all_detected']} ms")
            print(f"\n⏱️  粗略总和: {sum(result['all_detected'])} ms = {sum(result['all_detected'])/1000:.2f} 秒")
    
    if show_all and result['all_detected']:
        print(f"\n📋 所有检测到的可能时间值 ({len(result['all_detected'])} 个):")
        for t in result['all_detected']:
            if t >= 1000:
                print(f"   - {t} ms = {t/1000:.2f} 秒")
            else:
                print(f"   - {t} ms")
    
    print("\n" + "=" * 70)

def compare_multiple_programs(prog_list):
    """比较多个程序的时间参数"""
    results = []
    for prog in prog_list:
        print(f"\n处理程序 {prog}...")
        result = get_time_parameters(prog)
        results.append(result)
        time.sleep(0.5)
    
    print("\n" + "=" * 70)
    print("📊 多程序时间参数比较汇总")
    print("=" * 70)
    print(f"{'程序号':<8} {'高置信度参数':<15} {'总时间(秒)':<12} {'总时间(分钟)':<15}")
    print("-" * 55)
    for r in results:
        count = len(r['high_confidence']) if r['high_confidence'] else "-"
        seconds = f"{r['total_seconds']:.2f}" if r['total_seconds'] > 0 else "-"
        minutes = f"{r['total_minutes']:.2f}" if r['total_minutes'] > 0 else "-"
        print(f"程序{r['program_num']:<4} {str(count):<15} {seconds:<12} {minutes:<15}")
    print("=" * 70)

if __name__ == "__main__":
    import sys
    
    # 读取命令行参数
    if len(sys.argv) > 1:
        if sys.argv[1] == 'compare' and len(sys.argv) > 2:
            # 比较多个程序: python test_program_time.py compare 1 2 3
            progs = [int(p) for p in sys.argv[2:]]
            compare_multiple_programs(progs)
        else:
            # 读取单个程序: python test_program_time.py 5
            prog_num = int(sys.argv[1])
            result = get_time_parameters(prog_num)
            print_time_report(result, show_all=True)
    else:
        # 默认读取程序1
        result = get_time_parameters(1)
        print_time_report(result, show_all=True)
