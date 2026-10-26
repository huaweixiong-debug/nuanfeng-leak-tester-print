#!/usr/bin/env python3
"""
单独复位 ATEQ 仪器的脚本
发送命令: 01 05 00 00 FF 00 8C 0A
"""

from modbus_utils import send_raw, modbus_crc, STATION_ID

def reset_device():
    """复位/停止仪器 (线圈地址 0x00)"""
    print("=" * 50)
    print("复位/停止 ATEQ 仪器")
    print("=" * 50)
    
    # 构建命令: 01 05 00 00 FF 00 8C 0A
    # 01 = 站号, 05 = 功能码(写单个线圈), 0000 = 线圈地址(复位), FF00 = 置ON
    cmd = f"{STATION_ID:02X}050000FF00"
    cmd_with_crc = cmd + modbus_crc(cmd)
    
    print(f"\n命令: {cmd_with_crc}")
    print(f"格式: {cmd_with_crc[:2]} {cmd_with_crc[2:4]} {cmd_with_crc[4:8]} {cmd_with_crc[8:12]} {cmd_with_crc[12:]}")
    
    response = send_raw(cmd_with_crc)
    
    if response:
        print(f"\n响应: {response}")
        if response == cmd_with_crc:
            print("\n✅ 复位成功! 仪器已确认接收复位命令")
        else:
            print(f"\n⚠️  响应不匹配，响应内容: {response}")
        return True
    else:
        print("\n❌ 复位失败! 未收到响应")
        return False

if __name__ == '__main__':
    reset_device()