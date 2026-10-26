#!/usr/bin/env python3
"""
Modbus TCP client for testing connection to Windows bridge service
"""

import sys
sys.path.insert(0, '/usr/local/lib/python3.12/dist-packages')

from pymodbus.client.tcp import ModbusTcpClient
from pymodbus.framer import FramerType

WINDOWS_HOST_IP = '172.18.144.1'
TCP_PORT = 502
STATION_ID = 1

def get_client():
    print(f"Connecting to {WINDOWS_HOST_IP}:{TCP_PORT}...")
    client = ModbusTcpClient(
        host=WINDOWS_HOST_IP,
        port=TCP_PORT,
        framer=FramerType.RTU,
        timeout=5
    )
    return client

def run_test_cycle():
    print("--- Test Cycle ---")
    client = get_client()

    if not client.connect():
        print("Failed to connect!")
        return

    print("Connected successfully!")

    response = client.read_holding_registers(0x20, count=1, device_id=STATION_ID)
    if response.isError():
        print(f"Read error: {response}")
    else:
        print(f"Stepcode: {response.registers[0]}")

    client.close()
    print("Done.")

if __name__ == '__main__':
    run_test_cycle()