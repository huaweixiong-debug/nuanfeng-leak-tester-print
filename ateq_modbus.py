import socket

WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502
STATION_ID = 1

def send_raw(data_hex):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect((WINDOWS_HOST_IP, TCP_PORT))
        data = bytes.fromhex(data_hex)
        print(f"Sending: {data.hex().upper()}")
        sock.sendall(data)
        response = sock.recv(1024)
        return response.hex().upper()
    except Exception as e:
        print(f"Error: {e}")
        return None
    finally:
        sock.close()

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

def read_holding_registers(address, count):
    cmd = f"{STATION_ID:02X}03{address:04X}{count:04X}"
    return send_raw(cmd + modbus_crc(cmd))

def write_single_coil(address, value):
    """写入单个线圈 (功能码 05)
    
    Args:
        address: 线圈地址 (0x00 = RESET, 0x01 = START)
        value: True = ON (FF00), False = OFF (0000)
    """
    value_hex = "FF00" if value else "0000"
    cmd = f"{STATION_ID:02X}05{address:04X}{value_hex}"
    return send_raw(cmd + modbus_crc(cmd))

def start_device():
    """启动仪器测试 (线圈地址 0x01)"""
    print("启动仪器测试...")
    response = write_single_coil(0x01, True)
    if response:
        print(f"响应: {response}")
        return True
    return False

def stop_device():
    """停止/重置仪器 (线圈地址 0x00)"""
    print("停止/重置仪器...")
    response = write_single_coil(0x00, True)
    if response:
        print(f"响应: {response}")
        return True
    return False

def get_unit_name(unit_code):
    unit_table = {
        12000: "kPa",
        13000: "MPa",
        11000: "Bar",
        10000: "mBar",
        30000: "L/hour",
        46000: "inch³/s",
        47000: "inch³/min",
        48000: "inch³/hour",
        50000: "mL/s",
        51000: "mL/min",
        52000: "mL/hour",
        3000: "mm³/h",
        6000: "Pa",
        4000: "calibrated Pa",
        9000: "high-res Pa/s",
        8000: "PSI",
        14000: "kg/cm²",
    }
    return unit_table.get(unit_code, f"unknown({unit_code})")

def translate_status_bits(bits):
    translations = []
    if bits.get('bit_15_key'):
        translations.append("键盘锁定")
    if bits.get('bit_5_cycle_end'):
        translations.append("测试周期结束")
    if bits.get('bit_4_alarm'):
        translations.append("报警")
    if bits.get('bit_1_fail_ref'):
        translations.append("参考失败")
    if bits.get('bit_0_fail_test'):
        translations.append("测试失败")
    if bits.get('bit_0_pass'):
        translations.append("测试通过")
    return " | ".join(translations) if translations else "无特殊状态"

def parse_realtime_status(response_hex):
    if not response_hex or len(response_hex) < 30:
        return None

    data = bytes.fromhex(response_hex)
    registers = []
    for i in range(3, 29, 2):
        registers.append((data[i] << 8) | data[i+1])

    while len(registers) < 13:
        registers.append(0)

    program_no = registers[0]
    fifo_count = registers[1]
    test_type = registers[2]
    status = (registers[3] >> 8) | ((registers[3] & 0xFF) << 8)
    step_code = (registers[4] >> 8) | ((registers[4] & 0xFF) << 8)

    pressure_raw = ((registers[6] & 0xFF) << 24) | ((registers[6] >> 8) << 16) | ((registers[5] & 0xFF) << 8) | (registers[5] >> 8)
    if pressure_raw & 0x80000000:
        pressure_raw -= 0x100000000
    pressure = pressure_raw / 1000.0
    pressure_unit_code = ((registers[8] & 0xFF) << 16) | ((registers[8] >> 8) << 8) | ((registers[7] & 0xFF) << 8) | (registers[7] >> 8)

    leak_raw = ((registers[10] & 0xFF) << 24) | ((registers[10] >> 8) << 16) | ((registers[9] & 0xFF) << 8) | (registers[9] >> 8)
    if leak_raw & 0x80000000:
        leak_raw -= 0x100000000
    leak = leak_raw / 1000.0
    leak_unit_code = ((registers[12] & 0xFF) << 16) | ((registers[12] >> 8) << 8) | ((registers[11] & 0xFF) << 8) | (registers[11] >> 8)

    bits = {
        'bit_15_key': bool(status & 0x8000),
        'bit_5_cycle_end': bool(status & 0x0020),
        'bit_4_alarm': bool(status & 0x0010),
        'bit_1_fail_ref': bool(status & 0x0004),
        'bit_0_fail_test': bool(status & 0x0002),
        'bit_0_pass': bool(status & 0x0001),
    }

    result = {
        'program_number': program_no,
        'fifo_count': fifo_count,
        'test_type': 'Leak' if test_type == 1 else 'Other',
        'status': status,
        'step_code': step_code,
        'pressure': pressure,
        'pressure_unit': get_unit_name(pressure_unit_code),
        'pressure_unit_code': pressure_unit_code,
        'leak': leak,
        'leak_unit': get_unit_name(leak_unit_code),
        'leak_unit_code': leak_unit_code,
        'bits': bits,
        'bits_translated': translate_status_bits(bits),
    }
    return result

def parse_stepcode(response_hex):
    if not response_hex or len(response_hex) < 10:
        return None
    data = bytes.fromhex(response_hex)
    if len(data) >= 4:
        return (data[3] << 8) | data[4]
    return None

def run_test_cycle():
    print("--- Test Cycle ---\n")

    print("0. 测试启动仪器命令 (线圈地址 0x01):")
    print("   发送命令: 01 05 00 01 FF 00 DD FA")
    start_success = start_device()
    if start_success:
        print("   启动命令发送成功!")
    else:
        print("   启动命令发送失败!")

    print("\n1. Read 13 holding registers from 0x30 (Realtime Status):")
    response = read_holding_registers(0x30, 13)
    if response:
        print(f"   Raw: {response}")
        status = parse_realtime_status(response)
        if status:
            print(f"   Program Number: {status['program_number']}")
            print(f"   FIFO Count: {status['fifo_count']}")
            print(f"   Test Type: {status['test_type']}")
            print(f"   Status: 0x{status['status']:04X}")
            print(f"   Step Code: {status['step_code']}")
            print(f"   Pressure: {status['pressure']} {status['pressure_unit']} (unit code: {status['pressure_unit_code']})")
            print(f"   Leak: {status['leak']} {status['leak_unit']} (unit code: {status['leak_unit_code']})")
            print(f"   Status Bits (原始): {status['bits']}")
            print(f"   Status (中文): {status['bits_translated']}")

    print("\n2. Read stepcode from 0x20:")
    response = read_holding_registers(0x20, 1)
    if response:
        print(f"   Raw: {response}")
        stepcode = parse_stepcode(response)
        print(f"   Stepcode: {stepcode}")

    print("\nDone.")

if __name__ == '__main__':
    run_test_cycle()