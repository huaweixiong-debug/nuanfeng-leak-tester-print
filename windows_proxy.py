
"""
Windows 端 HTTP 代理服务
运行在 Windows 上，提供 HTTP API 来控制 COM 端口转发
可以设置为 Windows 服务开机自启动
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import socket
import threading
import serial
import time
import sys

# 配置
SERIAL_PORT = 'COM1'
BAUD_RATE = 9600
PARITY = 'E'  # Even parity
DATA_BITS = 8
STOP_BITS = 1
TCP_PORT = 502
HTTP_PORT = 8001  # HTTP 服务端口

serial_conn = None
client_sockets = []
is_running = True
bridge_thread = None

def setup_serial():
    """建立串口连接"""
    global serial_conn
    try:
        if serial_conn and serial_conn.is_open:
            serial_conn.close()
        serial_conn = serial.Serial(
            port=SERIAL_PORT,
            baudrate=BAUD_RATE,
            parity=PARITY,
            bytesize=DATA_BITS,
            stopbits=STOP_BITS,
            timeout=0.1
        )
        return True
    except Exception as e:
        print(f"串口连接失败: {e}")
        return False

def handle_client(client_socket):
    """处理单个客户端连接"""
    global serial_conn
    client_socket.settimeout(0.1)
    while is_running:
        try:
            # 从 TCP 客户端读取数据 (Modbus 请求)
            data = client_socket.recv(1024)
            if not data:
                break
                
            # 转发到串口
            if serial_conn and serial_conn.is_open:
                serial_conn.write(data)
                # 从串口读取响应
                response = b''
                start_time = time.time()
                while time.time() - start_time < 0.5:
                    chunk = serial_conn.read(256)
                    if chunk:
                        response += chunk
                    if len(response) >= 5:
                        break
                if response:
                    client_socket.sendall(response)
        except socket.timeout:
            continue
        except Exception as e:
            break
    client_socket.close()

def bridge_loop(tcp_server):
    """串口-TCP 桥接主循环"""
    global is_running, client_sockets
    while is_running:
        try:
            client_socket, addr = tcp_server.accept()
            client_handler = threading.Thread(target=handle_client, args=(client_socket,))
            client_handler.daemon = True
            client_handler.start()
        except Exception as e:
            time.sleep(0.1)

class RequestHandler(BaseHTTPRequestHandler):
    def _send_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def do_OPTIONS(self):
        self._send_response({'status': 'ok'})

    def do_GET(self):
        if self.path == '/api/status':
            serial_status = serial_conn.is_open if serial_conn else False
            self._send_response({
                'success': True,
                'serial_connected': serial_status,
                'status': 'running' if serial_status else 'serial_disconnected',
                'serial_port': SERIAL_PORT
            })
        elif self.path == '/api/start':
            if setup_serial():
                self._send_response({'success': True, 'message': '串口连接成功'})
            else:
                self._send_response({'success': False, 'message': '串口连接失败'})
        elif self.path == '/api/stop':
            global serial_conn
            if serial_conn:
                serial_conn.close()
            self._send_response({'success': True, 'message': '串口已断开'})
        elif self.path == '/api/printers':
            # 获取 Windows 系统打印机列表
            try:
                import subprocess
                result = subprocess.run(
                    ["powershell", "-Command", "Get-Printer | Select-Object -ExpandProperty Name"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    printer_list = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
                    self._send_response({'success': True, 'printers': printer_list})
                else:
                    self._send_response({'success': False, 'message': '获取打印机列表失败', 'printers': []})
            except Exception as e:
                self._send_response({'success': False, 'message': str(e), 'printers': []})
        else:
            self._send_response({'success': False, 'message': 'Not found'}, 404)

    def do_POST(self):
        self.do_GET()

def main():
    global bridge_thread
    print("=" * 60)
    print("ATEQ 代理服务")
    print("=" * 60)
    print(f"HTTP 服务端口: {HTTP_PORT}")
    print(f"TCP 桥接端口: {TCP_PORT}")
    print(f"串口: {SERIAL_PORT}")
    print("=" * 60)
    
    # 初始化串口
    setup_serial()
    
    # 启动 TCP 桥接服务
    tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        tcp_server.bind(('0.0.0.0', TCP_PORT))
    except:
        print(f"TCP 端口 {TCP_PORT} 被占用，可能已有服务在运行")
    tcp_server.listen(5)
    tcp_server.settimeout(0.1)
    
    bridge_thread = threading.Thread(target=bridge_loop, args=(tcp_server,))
    bridge_thread.daemon = True
    bridge_thread.start()
    
    # 启动 HTTP 服务
    http_server = HTTPServer(('0.0.0.0', HTTP_PORT), RequestHandler)
    print(f"服务已启动!")
    print(f"API 地址: http://localhost:{HTTP_PORT}")
    print("=" * 60)
    
    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        tcp_server.close()
        if serial_conn:
            serial_conn.close()

if __name__ == '__main__':
    main()
