#!/usr/bin/env python3
"""
最终版本：读取程序1的所有时间参数（Fill, Stab, Test, Dump）
修复了字节序问题，使用小端序写入参数列表
"""

import socket
import time

# 配置参数
WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502
STATION_ID = 1

# 参数标识符定义
PARAMS = {
    0x0001: {'name': '填充时间 (Fill Time)', 'expected': 7.0},
    0x0002: {'name': '稳定时间 (Stab Time)', 'expected': 10.7},
    0x0003: {'name': '测试时间 (Test Time)', 'expected': 5.0},
    0x0009: {'name': '排放时间 (Dump Time)', 'expected': 2.2},
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

def send_raw(data_hex, timeout=8, debug=False):
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
        time.sleep(0.3)

def write_multiple_registers_le(address, values, debug=False):
    """
    写入多个寄存器，使用小端序（低字节在前）
    这是关键修复！
    """
    count = len(values)
    byte_count = count * 2
    # 小端序：每个值的低字节在前，高字节在后
    data_hex = ''.join([f"{v & 0xFF:02X}{(v >> 8) & 0xFF:02X}" for v in values])
    cmd = f"{STATION_ID:02X}10{address:04X}{count:04X}{byte_count:02X}{data_hex}"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd, debug=debug)
    return resp is not None

def read_program_times(program_num=1, debug=False):
    """
    读取指定程序的所有时间参数
    """
    results = {}
    param_ids = list(PARAMS.keys())
    
    print("=" * 70)
    print(f"读取程序 {program_num} 的时间参数")
    print("=" * 70)
    
    # 步骤1: 选择要编辑的程序 (使用用户提供的精确命令)
    print(f"\n📌 步骤1: 选择程序 {program_num}")
    # 用户提供的命令格式: 01 10 30 04 00 01 02 00 00 97 D7
    prog_val = program_num - 1
    cmd = f"01103004000102{prog_val & 0xFF:02X}{(prog_val >> 8) & 0xFF:02X}"
    full_cmd = cmd + modbus_crc(cmd)
    if debug:
        print(f"  命令: {full_cmd}")
    resp = send_raw(full_cmd, debug=debug)
    if not resp:
        print("❌ 选择程序失败")
        print("  提示: 请检查仪器是否开机，或尝试重启仪器")
        return None
    print("✅ 程序选择成功")
    time.sleep(0.5)
    
    # 步骤2: 准备参数标识符列表 (小端序!)
    print(f"\n📌 步骤2: 准备参数列表 ({len(param_ids)}个参数)")
    print(f"  参数IDs: {[f'0x{x:04X}' for x in param_ids]}")
    values = [len(param_ids)] + param_ids  # 数量 + 参数ID列表
    if not write_multiple_registers_le(0x0000, values, debug=debug):
        print("❌ 准备参数列表失败")
        return None
    print("✅ 参数列表准备成功")
    time.sleep(0.5)
    
    # 步骤3: 读取参数值 (每个参数占3个字)
    print(f"\n📌 步骤3: 读取参数值")
    total_regs = 3 * len(param_ids)
    cmd = f"{STATION_ID:02X}03{0x0000:04X}{total_regs:04X}"
    full_cmd = cmd + modbus_crc(cmd)
    if debug:
        print(f"  命令: {full_cmd}")
    
    # 尝试读取多次
    resp = None
    for attempt in range(3):
        if attempt > 0:
            print(f"  重试第 {attempt+1} 次...")
            time.sleep(1)
        resp = send_raw(full_cmd, debug=debug)
        if resp and len(resp) > 10:
            break
    
    if not resp:
        print("❌ 读取参数值失败")
        return None
    
    # 解析响应
    print("\n" + "=" * 70)
    print("📊 解析结果")
    print("=" * 70)
    print(f"{'参数名称':<20} {'读取值(ms)':<10} {'读取值(s)':<10} {'期望值(s)':<10} {'状态'}")
    print("-" * 70)
    
    # 清理响应
    while len(resp) >= 2 and resp.startswith('00'):
        resp = resp[2:]
    
    # 解析数据
    if len(resp) >= 10 and resp[2:4] == '03':
        byte_count = int(resp[4:6], 16)
        data = resp[6:6 + byte_count * 2]
        
        total_ms = 0
        all_correct = True
        
        for i in range(len(param_ids)):
            base = i * 12  # 每个参数占6字节=12个十六进制字符
            if base + 12 > len(data):
                print(f"  ⚠️  参数 {i+1} 数据不完整")
                continue
            
            # 解析: 参数ID(2字节) + 值(4字节)
            param_id_word = int(data[base:base+4], 16)
            param_id = swap_bytes(param_id_word)  # 小端序
            
            # 值: 4字节小端序
            word1 = int(data[base+4:base+8], 16)
            word2 = int(data[base+8:base+12], 16)
            word1_le = swap_bytes(word1)
            word2_le = swap_bytes(word2)
            
            # 组合成32位值: 字2是高16位，字1是低16位
            value_ms = (word2_le << 16) | word1_le
            value_s = value_ms / 1000.0
            
            param_info = PARAMS.get(param_id, {})
            name = param_info.get('name', f'未知参数(0x{param_id:04X})')
            expected = param_info.get('expected', 0)
            
            status = "✅" if abs(value_s - expected) < 0.5 else "⚠️"
            if abs(value_s - expected) >= 0.5:
                all_correct = False
            
            print(f"{name:<20} {value_ms:<10} {value_s:<10.3f} {expected:<10.1f} {status}")
            
            results[param_id] = {
                'name': name,
                'value_ms': value_ms,
                'value_s': value_s,
                'expected': expected
            }
            total_ms += value_ms
        
        print("-" * 70)
        total_s = total_ms / 1000.0
        expected_total = sum(p['expected'] for p in PARAMS.values())
        total_status = "✅" if abs(total_s - expected_total) < 1.0 else "⚠️"
        print(f"{'时间总和':<20} {total_ms:<10} {total_s:<10.3f} {expected_total:<10.1f} {total_status}")
        print("=" * 70)
        
        results['total_ms'] = total_ms
        results['total_s'] = total_s
        results['success'] = all_correct
        
        return results
    else:
        print(f"❌ 响应格式错误: {resp}")
        return None

if __name__ == "__main__":
    import sys
    debug = len(sys.argv) > 1 and sys.argv[1] == 'debug'
    
    print("\n提示: 如果仪器无响应，请尝试以下操作:")
    print("1. 重启仪器电源")
    print("2. 检查Windows上的com2tcp是否运行")
    print("3. 检查串口连接是否正常\n")
    
    result = read_program_times(program_num=1, debug=debug)
    
    if result and result.get('success'):
        print("\n🎉 所有时间参数读取成功!")
    elif result:
        print("\n⚠️  部分参数可能不正确")
    else:
        print("\n❌ 读取失败，请检查仪器状态后重试")
