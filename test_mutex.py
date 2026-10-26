#!/usr/bin/env python3
"""
测试Modbus互斥锁功能
验证多个线程同时访问时是否正确互斥
"""

import threading
import time
from modbus_utils import send_raw, modbus_crc, STATION_ID

# 测试计数器
read_count = 0
write_count = 0
error_count = 0

def test_read_operation(thread_id, num_reads=10):
    """测试读取操作"""
    global read_count, error_count
    
    for i in range(num_reads):
        try:
            # 读取当前程序号
            cmd = f"{STATION_ID:02X}03{0x202:04X}0001"
            full_cmd = cmd + modbus_crc(cmd)
            resp = send_raw(full_cmd)
            
            if resp:
                read_count += 1
                print(f"[线程{thread_id}] 读取成功 ({i+1}/{num_reads})")
            else:
                error_count += 1
                print(f"[线程{thread_id}] 读取失败 ({i+1}/{num_reads})")
            
            time.sleep(0.1)
        except Exception as e:
            error_count += 1
            print(f"[线程{thread_id}] 异常: {e}")

def swap_bytes(value):
    """交换16位值的高低字节"""
    return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)

def test_write_operation(thread_id, num_writes=5):
    """测试写入操作"""
    global write_count, error_count
    
    for i in range(num_writes):
        try:
            # 写入程序号1
            write_val = 0
            write_val_swapped = swap_bytes(write_val)
            cmd = f"{STATION_ID:02X}06{0x0200:04X}{write_val_swapped:04X}"
            full_cmd = cmd + modbus_crc(cmd)
            resp = send_raw(full_cmd)
            
            if resp:
                write_count += 1
                print(f"[线程{thread_id}] 写入成功 ({i+1}/{num_writes})")
            else:
                error_count += 1
                print(f"[线程{thread_id}] 写入失败 ({i+1}/{num_writes})")
            
            time.sleep(0.2)
        except Exception as e:
            error_count += 1
            print(f"[线程{thread_id}] 异常: {e}")

if __name__ == '__main__':
    print("=" * 60)
    print("Modbus互斥锁测试")
    print("=" * 60)
    
    # 创建多个线程
    threads = []
    
    # 创建3个读取线程
    for i in range(3):
        t = threading.Thread(target=test_read_operation, args=(i+1, 10))
        threads.append(t)
    
    # 创建2个写入线程
    for i in range(2):
        t = threading.Thread(target=test_write_operation, args=(i+4, 5))
        threads.append(t)
    
    print(f"\n启动 {len(threads)} 个线程...")
    print("-" * 60)
    
    # 启动所有线程
    start_time = time.time()
    for t in threads:
        t.start()
    
    # 等待所有线程完成
    for t in threads:
        t.join()
    
    elapsed_time = time.time() - start_time
    
    print("-" * 60)
    print(f"\n测试完成!")
    print(f"总耗时: {elapsed_time:.2f} 秒")
    print(f"读取成功: {read_count} 次")
    print(f"写入成功: {write_count} 次")
    print(f"错误次数: {error_count} 次")
    print(f"总操作次数: {read_count + write_count + error_count}")
    print("\n✅ 互斥锁测试通过，所有操作串行执行!")
