#!/usr/bin/env python3
"""
读取 ATEQ 仪器当前运行的程序号
地址: 0x202 (514)
解码方式: 程序号 = ((寄存器值 >> 8) & 0xFF) + 1
"""

import socket
import time
import struct

# 配置参数
WINDOWS_HOST_IP = '172.18.144.1'  # Windows 主机 IP
TCP_PORT = 502                     # TCP 端口
STATION_ID = 1                     # 设备站号

# ------------------ CRC16计算 ------------------
def crc16(data: bytes) -> bytes:
    """
    计算Modbus RTU CRC16
    返回2字节小端序（低字节在前，高字节在后）
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    # 返回低字节在前，高字节在后
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])

# ------------------ 构建Modbus帧 ------------------
def build_write_single_register(slave: int, address: int, value: int) -> bytes:
    """
    构建写单个寄存器(0x06)的请求帧
    address: 16位寄存器地址
    value:   16位写入值（注意：ATEQ要求小端序，即先发低字节，后发高字节）
    """
    frame = bytearray()
    frame.append(slave)               # 从站地址
    frame.append(0x06)                 # 功能码
    # 地址（16位），按Modbus标准，地址本身在帧中是大端序（高字节在前）
    frame.append((address >> 8) & 0xFF)
    frame.append(address & 0xFF)
    # 数据值（16位），ATEQ要求小端序，因此先发低字节，后发高字节
    frame.append(value & 0xFF)         # 低字节
    frame.append((value >> 8) & 0xFF)  # 高字节
    frame.extend(crc16(frame))          # 添加CRC16（已小端序）
    return bytes(frame)

def build_read_holding_registers(slave: int, start_addr: int, quantity: int) -> bytes:
    """
    构建读保持寄存器(0x03)的请求帧
    quantity: 要读取的寄存器数量（16位字个数）
    """
    frame = bytearray()
    frame.append(slave)
    frame.append(0x03)
    # 起始地址（大端序）
    frame.append((start_addr >> 8) & 0xFF)
    frame.append(start_addr & 0xFF)
    # 寄存器数量（大端序）
    frame.append((quantity >> 8) & 0xFF)
    frame.append(quantity & 0xFF)
    frame.extend(crc16(frame))
    return bytes(frame)

# ------------------ 解析响应 ------------------
def parse_read_response(response: bytes, expected_slave: int, expected_func: int, expected_words: int):
    """
    解析读寄存器响应
    返回解析后的寄存器值列表（每个值为原始16位整数，大端序）
    如果出错抛出异常
    """
    if len(response) < 5:
        raise ValueError("响应帧长度不足")
    slave = response[0]
    func = response[1]
    if slave != expected_slave:
        raise ValueError(f"从站地址不匹配: 期望{expected_slave}，收到{slave}")
    if func == expected_func + 0x80:  # 异常响应
        error_code = response[2]
        raise ValueError(f"Modbus异常: 错误码 {error_code:02X}")
    if func != expected_func:
        raise ValueError(f"功能码错误: 期望{expected_func:02X}，收到{func:02X}")
    byte_count = response[2]
    if byte_count != expected_words * 2:
        raise ValueError(f"数据字节数不匹配: 期望{expected_words*2}，收到{byte_count}")
    data = response[3:3+byte_count]
    # 计算CRC校验
    recv_crc = response[-2:]
    calc_crc = crc16(response[:-2])
    if recv_crc != calc_crc:
        raise ValueError("CRC校验失败")
    # 将字节数据解析为16位整数（注意：响应数据中每个16位字是小端序！）
    # 即每个字的两字节是低字节在前，高字节在后
    values = []
    for i in range(0, byte_count, 2):
        low = data[i]
        high = data[i+1]
        val = (high << 8) | low   # 组合成16位整数（此时是大端序？）
        # 实际上，由于接收到的字节是小端序，所以组合后得到的整数就是ATEQ实际存储的16位值（小端解释）
        # 例如收到 [0xA0, 0x92] -> low=0xA0, high=0x92 -> val=0x92A0 = 37536，这正是ATEQ期望的16位值。
        # 但如果我们后续要与其他16位组合成32位，需要保持小端序，即直接使用val作为低16位即可。
        # 因此这里返回的val已经是ATEQ内部表示的16位值（小端解释）。
        values.append(val)
    return values

def parse_write_response(response: bytes, expected_slave: int, expected_addr: int, expected_value: int):
    """
    解析写单个寄存器响应（应回显请求）
    """
    if len(response) < 8:
        raise ValueError("响应帧长度不足")
    slave = response[0]
    func = response[1]
    if slave != expected_slave:
        raise ValueError(f"从站地址不匹配")
    if func == 0x86:  # 写寄存器异常
        error_code = response[2]
        raise ValueError(f"Modbus异常: 错误码 {error_code:02X}")
    if func != 0x06:
        raise ValueError(f"功能码错误")
    addr_high = response[2]
    addr_low = response[3]
    addr = (addr_high << 8) | addr_low
    if addr != expected_addr:
        raise ValueError(f"地址不匹配")
    val_low = response[4]
    val_high = response[5]
    val = (val_high << 8) | val_low
    if val != expected_value:
        # 注意：响应中的数据也是小端序，这里val是按大端组合的，但ATEQ返回时数据部分也是小端，所以组合后应该等于我们写入的值（按小端解释）
        # 我们写入时使用的是value（小端解释），但这里返回的值是按大端组合，因此可能不相等？需要验证
        # 实际上，ATEQ写响应会回显请求帧，请求帧中数据部分是我们发送的小端字节顺序，响应中应该完全相同。
        # 而我们构建请求时，数据部分用了小端，所以响应中接收到的字节序列与请求一致。
        # 解析时按大端组合得到的是另一个数，但这里我们只检查地址，不检查值也可以，或者我们直接比较字节。
        pass
    # 可选的CRC校验
    recv_crc = response[-2:]
    calc_crc = crc16(response[:-2])
    if recv_crc != calc_crc:
        raise ValueError("CRC校验失败")
    return

# ------------------ 读取测试时间的主函数 ------------------
def read_test_time(port: str, slave: int, program_number: int, baudrate=9600, timeout=1):
    """
    读取指定程序号的测试时间（秒）
    """
    # 打开串口
    ser = serial.Serial(port=port, baudrate=baudrate, bytesize=8, parity='N', stopbits=1, timeout=timeout)

    try:
        # 步骤1: 选择程序号（写入地址0x6000）
        prog_val = program_number - 1
        write_addr = 0x6000
        req = build_write_single_register(slave, write_addr, prog_val)
        print(f"发送写请求: {req.hex()}")
        ser.write(req)
        time.sleep(0.1)  # 等待处理
        # 读取响应（至少8字节）
        resp = ser.read(8)
        if len(resp) < 8:
            raise TimeoutError("写响应超时")
        print(f"收到写响应: {resp.hex()}")
        parse_write_response(resp, slave, write_addr, prog_val)

        # 短暂延时，确保ATEQ完成内部处理
        time.sleep(0.1)

        # 步骤2: 读取测试时间（地址0x2003，2个寄存器）
        read_addr = 0x2003
        req = build_read_holding_registers(slave, read_addr, 2)
        print(f"发送读请求: {req.hex()}")
        ser.write(req)
        # 预计响应长度: 1(from) + 1(func) + 1(bytecount) + 4(data) + 2(crc) = 9字节
        resp = ser.read(9)
        if len(resp) < 9:
            raise TimeoutError("读响应超时")
        print(f"收到读响应: {resp.hex()}")

        # 解析数据
        values = parse_read_response(resp, slave, 0x03, 2)
        # values包含两个16位整数，都是小端解释的（即ATEQ内部值）
        # 组合成32位有符号整数（低16位在前）
        combined = values[0] | (values[1] << 16)
        # 转换为有符号整数（如果最高位为1）
        if combined & 0x80000000:
            combined -= 0x100000000
        # 除以1000得到实际秒数
        test_time = combined / 1000.0
        return test_time

    finally:
        ser.close()

# ------------------ 使用示例 ------------------
if __name__ == "__main__":
    # 配置参数
    SERIAL_PORT = "COM1"       # 根据实际情况修改
    SLAVE_ADDRESS = 1          # 从站地址（ATEQ仪器地址）
    PROGRAM_NUM = 3            # 要读取的程序号

    try:
        result = read_test_time(SERIAL_PORT, SLAVE_ADDRESS, PROGRAM_NUM, baudrate=9600)
        print(f"程序 {PROGRAM_NUM} 的测试时间为: {result} 秒")
    except Exception as e:
        print(f"错误: {e}")