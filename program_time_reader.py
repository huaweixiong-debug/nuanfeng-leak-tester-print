#!/usr/bin/env python3
"""
程序时间参数读取工具（最终版）
按照正确的ATEQ协议流程读取参数:
1. 向地址 0x3004 写入 程序号-1 (选择程序)
2. 从地址 0x0000 读取参数（参数已自动加载）
3. 每个寄存器值交换字节得到实际值
"""

import socket
import time

# 配置参数
WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502
STATION_ID = 1

# 参数定义（根据寄存器索引）
PARAM_DEF = [
    ("填充时间", "Fill Time"),
    ("稳定时间", "Stabilization Time"),
    ("测试时间", "Test Time"),
    ("排气时间", "Vent Time"),
    ("预填充时间", "Pre-fill Time"),
    ("延迟时间1", "Delay Time 1"),
    ("延迟时间2", "Delay Time 2"),
    ("超时时间", "Timeout"),
    ("吹扫时间", "Purge Time"),
    ("等待时间", "Wait Time"),
]

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

def select_program(program_num):
    """
    选择要读取参数的程序
    向地址 0x3004 写入: 程序号 - 1
    """
    value = program_num - 1  # 程序号需减1
    cmd = f"{STATION_ID:02X}06{0x3004:04X}{value:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd)
    
    if resp and resp.startswith(f"{STATION_ID:02X}06"):
        time.sleep(0.1)  # 等待参数加载
        return True
    return False

def read_program_parameters(count=16):
    """
    从地址0x0000读取程序参数
    返回: 原始寄存器值列表
    """
    cmd = f"{STATION_ID:02X}03{0x0000:04X}{count:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd)
    
    if resp and len(resp) >= 10 and resp.startswith(f"{STATION_ID:02X}03"):
        data_bytes = int(resp[4:6], 16)
        data_hex = resp[6:6 + data_bytes * 2]
        registers = []
        for i in range(0, len(data_hex), 4):
            if i + 4 <= len(data_hex):
                registers.append(int(data_hex[i:i+4], 16))
        return registers
    return None

def get_program_time_params(program_num, count=10):
    """
    获取指定程序的时间参数
    返回: [(参数名, 原始值, 交换后值, 毫秒值, 格式化显示), ...]
    """
    # 步骤1: 选择程序
    if not select_program(program_num):
        return None, f"选择程序 {program_num} 失败"
    
    # 步骤2: 读取参数
    regs = read_program_parameters(count)
    if not regs:
        return None, "读取参数失败"
    
    # 步骤3: 解析和转换
    results = []
    time_values = []
    
    for i in range(count):
        if i >= len(regs):
            break
            
        raw_val = regs[i]
        swapped_val = swap_bytes(raw_val)
        
        # 判断是否为有效的时间值（0.1ms ~ 10分钟）
        if 0 <= swapped_val <= 600000:
            ms_val = swapped_val
            time_values.append(ms_val)
            
            if ms_val >= 1000:
                display = f"{ms_val} ms = {ms_val/1000:.2f} s"
            elif ms_val > 0:
                display = f"{ms_val} ms"
            else:
                display = "0 ms (未使用)"
        else:
            ms_val = None
            display = "无效值"
        
        name_cn, name_en = PARAM_DEF[i] if i < len(PARAM_DEF) else (f"参数{i+1}", f"Param{i+1}")
        
        results.append({
            'index': i,
            'name_cn': name_cn,
            'name_en': name_en,
            'raw_value': raw_val,
            'swapped_value': swapped_val,
            'ms_value': ms_val,
            'display': display
        })
    
    summary = {
        'program_num': program_num,
        'time_values': [v for v in time_values if v > 0],  # 排除0值
        'total_ms': sum(time_values),
        'param_count': len([v for v in time_values if v > 0])
    }
    
    return results, summary

def print_time_report(program_num=1):
    """打印时间参数报告"""
    print("\n" + "=" * 70)
    print(f"📊 ATEQ 程序 {program_num} 时间参数报告")
    print("=" * 70)
    
    results, summary = get_program_time_params(program_num)
    if not results:
        print(f"❌ {summary}")
        return
    
    print(f"\n📋 时间参数详情:")
    print(f"{'-'*70}")
    print(f"{'序号':<4} {'参数名称':<12} {'原始值':<10} {'交换后值':<10} {'毫秒值':<12} {'显示'}")
    print(f"{'-'*70}")
    
    for r in results:
        if r['ms_value'] is not None:
            print(f"{r['index']+1:<4} {r['name_cn']:<12} 0x{r['raw_value']:04X}    0x{r['swapped_value']:04X}    {str(r['ms_value']):<12} {r['display']}")
    
    print(f"{'-'*70}")
    print(f"\n📈 统计汇总:")
    print(f"  程序号: {summary['program_num']}")
    print(f"  有效时间参数: {summary['param_count']} 个")
    print(f"  时间参数列表(ms): {summary['time_values']}")
    print(f"  总时间: {summary['total_ms']} ms")
    print(f"  总时间: {summary['total_ms']/1000:.2f} 秒")
    print(f"  总时间: {summary['total_ms']/60000:.2f} 分钟")
    
    if summary['time_values']:
        print(f"\n⏱️  各阶段时间:")
        for i, ms in enumerate(summary['time_values'][:4]):  # 显示前4个主要时间
            phase = ["填充", "稳定", "测试", "排气"][i] if i < 4 else f"阶段{i+1}"
            print(f"  {phase}: {ms} ms = {ms/1000:.2f} s")
    
    print(f"\n" + "=" * 70)
    return summary

def compare_programs(prog_list):
    """比较多个程序的时间参数"""
    summaries = []
    for prog in prog_list:
        print(f"\n处理程序 {prog}...")
        _, summary = get_program_time_params(prog)
        if summary:
            summaries.append(summary)
        time.sleep(0.3)
    
    if not summaries:
        print("没有可比较的数据")
        return
    
    print("\n" + "=" * 60)
    print("📊 多程序时间参数比较")
    print("=" * 60)
    print(f"{'程序号':<8} {'参数数量':<10} {'总时间(ms)':<15} {'总时间(秒)':<15} {'总时间(分钟)':<15}")
    print("-" * 60)
    
    for s in summaries:
        print(f"程序{s['program_num']:<4} {s['param_count']:<10} {s['total_ms']:<15} {s['total_ms']/1000:<15.2f} {s['total_ms']/60000:<15.2f}")
    
    print("=" * 60)

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        if sys.argv[1] == 'compare' and len(sys.argv) > 2:
            # 比较多个程序: python program_time_reader.py compare 1 2 3
            progs = [int(p) for p in sys.argv[2:]]
            compare_programs(progs)
        else:
            # 读取单个程序: python program_time_reader.py 5
            prog_num = int(sys.argv[1])
            print_time_report(prog_num)
    else:
        # 默认读取程序1
        print_time_report(1)
