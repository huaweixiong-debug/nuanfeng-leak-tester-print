#!/usr/bin/env python3
"""
测试stepcode监控功能
"""

import time
from program_selector import read_step_code, write_program, start_test, reset_device

print("测试stepcode监控功能")
print("=" * 50)

# 重置设备
print("\n1. 重置设备...")
reset_device()
time.sleep(0.5)

# 读取初始stepcode
step = read_step_code()
print(f"   初始stepcode: {step}")

# 选择程序1
print("\n2. 选择程序1...")
write_program(1)
time.sleep(0.3)

# 启动测试
print("\n3. 启动测试...")
start_test()

# 监控stepcode变化
print("\n4. 监控stepcode变化...")
print("   等待stepcode >= 4...")
start_time = time.time()
last_stepcode = 0

while time.time() - start_time < 30:
    stepcode = read_step_code()
    
    if stepcode is not None:
        if stepcode >= 4:
            last_stepcode = stepcode
            print(f"   stepcode: {stepcode}")
        elif last_stepcode >= 4 and stepcode == 0:
            print(f"   stepcode回到0，测试完成!")
            break
    
    time.sleep(0.1)

print("\n测试完成!")
