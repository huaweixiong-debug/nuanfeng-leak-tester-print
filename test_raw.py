import socket

WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502

print(f"Connecting to {WINDOWS_HOST_IP}:{TCP_PORT}...")

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(5)

try:
    sock.connect((WINDOWS_HOST_IP, TCP_PORT))
    print("Connected!")

    data = bytes.fromhex('01030030000D8400')
    print(f"Sending: {data.hex().upper()}")
    sock.sendall(data)

    response = sock.recv(1024)
    print(f"Received: {response.hex().upper()}")

except Exception as e:
    print(f"Error: {e}")
finally:
    sock.close()