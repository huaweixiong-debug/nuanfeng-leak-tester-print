#!/usr/bin/env python3
"""
串口转 TCP 转发脚本
运行在 Windows 上，将 COM 端口的 Modbus RTU 数据转发到 TCP 端口
"""

import socket
import serial
import threading
import time
import sys

# 配置
SERIAL_PORT = 'COM1'       # 串口
BAUD_RATE = 9600           # 波特率
PARITY = 'E'               # 偶校验 (ATEQ 仪器使用偶校验)
DATA_BITS = 8              # 数据位
STOP_BITS = 1              # 停止位
TCP_PORT = 502             # TCP 监听端口
LISTEN_ALL = True          # 是否监听所有网络接口

is_running = True

def serial_to_socket(ser, client_socket):
    """将串口数据转发到 socket"""
    while is_running:
        try:
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting)
                if data:
                    print(f"串口 -> Socket: {data.hex().upper()}")
                    client_socket.sendall(data)
            time.sleep(0.01)
        except Exception as e:
            print(f"串口读取错误: {e}")
            break

def socket_to_serial(ser, client_socket):
    """将 socket 数据转发到串口"""
    client_socket.settimeout(0.1)
    while is_running:
        try:
            data = client_socket.recv(1024)
            if not data:
                break
            print(f"Socket -> 串口: {data.hex().upper()}")
            ser.write(data)
        except socket.timeout:
            continue
        except Exception as e:
            print(f"Socket 读取错误: {e}")
            break

def handle_client(client_socket, ser):
    """处理客户端连接"""
    print(f"新客户端连接: {client_socket.getpeername()}")
    
    # 创建双向转发线程
    t1 = threading.Thread(target=serial_to_socket, args=(ser, client_socket))
    t2 = threading.Thread(target=socket_to_serial, args=(ser, client_socket))
    t1.start()
    t2.start()
    
    t1.join()
    t2.join()
    print(f"客户端断开连接")
    client_socket.close()

def main():
    global is_running
    
    print("=" * 60)
    print("串口转 TCP 转发服务")
    print("=" * 60)
    print(f"串口: {SERIAL_PORT} {BAUD_RATE} {PARITY} {DATA_BITS} {STOP_BITS}")
    print(f"TCP 端口: {TCP_PORT}")
    print("=" * 60)
    
    # 打开串口
    try:
        ser = serial.Serial(
            port=SERIAL_PORT,
            baudrate=BAUD_RATE,
            parity=PARITY,
            bytesize=DATA_BITS,
            stopbits=STOP_BITS,
            timeout=0.1
        )
        print(f"✅ 串口 {SERIAL_PORT} 已打开")
    except Exception as e:
        print(f"❌ 无法打开串口 {SERIAL_PORT}: {e}")
        print("请检查:")
        print("  1. 仪器是否连接")
        print("  2. 串口号是否正确")
        print("  3. 串口是否被其他程序占用")
        return
    
    # 创建 TCP 服务器
    try:
        host = '0.0.0.0' if LISTEN_ALL else '127.0.0.1'
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, TCP_PORT))
        server.listen(5)
        print(f"✅ TCP 服务已启动，监听端口 {TCP_PORT}")
        if host == '0.0.0.0':
            print(f"   可以通过 localhost:{TCP_PORT} 或 本机IP:{TCP_PORT} 连接")
    except Exception as e:
        print(f"❌ 无法启动 TCP 服务: {e}")
        ser.close()
        return
    
    print("\n等待客户端连接... (按 Ctrl+C 停止)")
    
    try:
        while True:
            client_socket, addr = server.accept()
            client_handler = threading.Thread(
                target=handle_client,
                args=(client_socket, ser)
            )
            client_handler.start()
    except KeyboardInterrupt:
        print("\n停止服务...")
        is_running = False
        server.close()
        ser.close()
        print("服务已停止")

if __name__ == "__main__":
    main()
