#!/usr/bin/env python3
"""
BarTender 打印示例
展示如何在测试完成后打印标签
"""

from bartender_print import BarTenderPrinter
from datetime import datetime


def print_after_test():
    """
    测试完成后打印标签示例
    这个函数应该在测试完成后被调用
    """
    print("=" * 60)
    print("测试完成 - 打印标签")
    print("=" * 60)
    
    # 创建打印机实例
    # 模板文件夹默认为当前目录下的 templates 文件夹
    printer = BarTenderPrinter()
    
    # 测试数据（实际使用时从测试系统获取）
    test_data = {
        "serial_number": "20260322-MODEL001-0001",
        "product_model": "MODEL001",
        "pressure1": 223.800,
        "leak1": 0.023,
        "pressure2": 223.600,
        "leak2": 0.000,
        "test_result": "PASS",
        "operator": "张三",
        "test_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    # 从数据库获取产品配置（包括标签模板名称）
    # 这里使用示例数据
    label_template = "MODEL001_Label"  # 产品设置中配置的标签模板
    
    print(f"\n📄 打印信息:")
    print(f"  标签模板: {label_template}")
    print(f"  序列号: {test_data['serial_number']}")
    print(f"  产品型号: {test_data['product_model']}")
    print(f"  测试结果: {test_data['test_result']}")
    
    # 方式1: 使用标准数据格式打印
    print("\n🖨️  方式1: 使用标准数据格式打印")
    success = printer.print_with_data(
        template_name=label_template,
        serial_number=test_data['serial_number'],
        product_model=test_data['product_model'],
        qr_code=test_data['serial_number'],
        copies=1
    )
    
    if success:
        print("✅ 打印成功")
    else:
        print("❌ 打印失败")
    
    # 方式2: 使用自定义变量打印（更灵活）
    print("\n🖨️  方式2: 使用自定义变量打印")
    
    # 构建变量字典（根据BarTender模板中的变量名）
    variables = {
        "SerialNumber": test_data['serial_number'],
        "ProductModel": test_data['product_model'],
        "Pressure1": f"{test_data['pressure1']:.3f} kPa",
        "Leak1": f"{test_data['leak1']:.6f} mL/min",
        "Pressure2": f"{test_data['pressure2']:.3f} kPa",
        "Leak2": f"{test_data['leak2']:.6f} mL/min",
        "TestResult": test_data['test_result'],
        "Operator": test_data['operator'],
        "TestTime": test_data['test_time'],
        "QRCode": test_data['serial_number']  # 二维码内容
    }
    
    success = printer.print_label(
        template_name=label_template,
        copies=1,
        variables=variables
    )
    
    if success:
        print("✅ 打印成功")
    else:
        print("❌ 打印失败")
    
    print("\n" + "=" * 60)

if __name__ == '__main__':
    print_after_test()
