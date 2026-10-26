#!/usr/bin/env python3
"""
测试stepcode=6时的数据读取
"""

import time
from modbus_utils import send_raw, modbus_crc, STATION_ID

def read_step_code():
    """读取当前步骤码"""
    from modbus_utils import swap_bytes
    cmd = f"{STATION_ID:02X}03{0x20:04X}0001"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd)
    
    if resp and len(resp) >= 10 and resp.startswith(f"{STATION_ID:02X}03"):
        data_hex = resp[6:10]
        step_code = int(data_hex, 16)
        step_code_swapped = swap_bytes(step_code)
        return step_code_swapped
    return None

def read_test_data():
    """读取测试数据"""
    cmd = f"{STATION_ID:02X}03{0x30:04X}000D"
    full_cmd = cmd + modbus_crc(cmd)
    resp = send_raw(full_cmd)
    
    if not resp or len(resp) < 32:
        return None
    
    data = bytes.fromhex(resp)
    registers = []
    for i in range(3, 31, 2):
        registers.append((data[i] << 8) | data[i+1])
    
    while len(registers) < 13:
        registers.append(0)
    
    # 解析压力值
    pressure_raw = ((registers[6] & 0xFF) << 24) | ((registers[6] >> 8) << 16) | ((registers[5] & 0xFF) << 8) | (registers[5] >> 8)
    if pressure_raw & 0x80000000:
        pressure_raw -= 0x100000000
    pressure = pressure_raw / 1000.0
    
    # 解析泄漏量
    leak_raw = ((registers[10] & 0xFF) << 24) | ((registers[10] >> 8) << 16) | ((registers[9] & 0xFF) << 8) | (registers[9] >> 8)
    if leak_raw & 0x80000000:
        leak_raw -= 0x100000000
    leak = leak_raw / 1000.0
    
    # 读取状态
    status = registers[3]
    test_result = 'PASS' if (status & 0x0001) else 'FAIL'
    
    return {
        'pressure': pressure,
        'leak': leak,
        'test_result': test_result
    }

print("监控stepcode，当stepcode=6时读取数据...")
print("=" * 60)

stepcode_6_data = None

for i in range(100):
    stepcode = read_step_code()
    
    if stepcode is not None:
        if stepcode == 6:
            data = read_test_data()
            if data:
                stepcode_6_data = data
                print(f"[stepcode={stepcode}] 压力: {data['pressure']:.3f} kPa, 泄漏: {data['leak']:.3f} mL/min, 结果: {data['test_result']}")
        elif stepcode == 65535 and stepcode_6_data:
            print(f"\n测试完成！最后记录的stepcode=6数据:")
            print(f"  压力: {stepcode_6_data['pressure']:.3f} kPa")
            print(f"  泄漏: {stepcode_6_data['leak']:.3f} mL/min")
            print(f"  结果: {stepcode_6_data['test_result']}")
            break
        else:
            print(f"[stepcode={stepcode}]")
    
    time.sleep(0.2)
