#!/usr/bin/env python3
"""
单独启动 ATEQ 仪器的脚本
启动线圈 0x0001 写 ON 后立即复位 OFF
"""

import time

from modbus_utils import write_single_coil

def start_device():
    """启动仪器测试 (线圈地址 0x01)"""
    print("=" * 50)
    print("启动 ATEQ 仪器测试")
    print("=" * 50)
    
    response_on = write_single_coil(0x0001, True)
    if response_on:
        print(f"\nON响应: {response_on}")
        time.sleep(0.1)
        response_off = write_single_coil(0x0001, False)
        print(f"OFF响应: {response_off}")
        print("\n✅ 启动成功! 启动位已复位")
        return True

    print("\n❌ 启动失败! 未收到响应")
    return False

if __name__ == '__main__':
    start_device()
