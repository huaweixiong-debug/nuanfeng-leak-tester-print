#!/usr/bin/env python3
"""
测试完成自动打印标签
集成 S7-200 SMART PLC 通讯和 BarTender 标签打印
"""

import time
import logging
from s7_communication import S7200SmartClient
from bartender_print import BarTenderPrinter

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('test_and_print.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('TestAndPrint')


class TestAndPrintSystem:
    """测试完成自动打印系统"""
    
    def __init__(self, plc_ip: str = '192.168.2.1', template_folder: str = None):
        """
        初始化系统
        
        Args:
            plc_ip: PLC IP地址
            template_folder: 标签模板文件夹路径
        """
        self.plc = S7200SmartClient(plc_ip=plc_ip)
        self.printer = BarTenderPrinter(template_folder=template_folder)
        self.running = False
        logger.info("初始化测试完成自动打印系统")
    
    def connect(self) -> bool:
        """连接PLC"""
        return self.plc.connect()
    
    def disconnect(self):
        """断开PLC连接"""
        self.plc.disconnect()
    
    def check_test_complete(self) -> bool:
        """
        检查测试是否完成
        通过读取PLC的测试完成标志位
        
        Returns:
            bool: 测试是否完成
        """
        # 假设测试完成标志位在 M28.0
        # 您可以根据实际情况修改地址
        return self.plc.read_bool_m(28, 0)
    
    def get_test_result(self) -> dict:
        """
        获取测试结果数据
        
        Returns:
            dict: 测试结果数据
        """
        # 读取测试数据（根据实际地址配置）
        test_data = {
            "pressure1": self.plc.read_real_md(0),      # MD0 - 压力1
            "leak1": self.plc.read_real_md(4),          # MD4 - 泄漏量1
            "pressure2": self.plc.read_real_md(8),      # MD8 - 压力2
            "leak2": self.plc.read_real_md(12),         # MD12 - 泄漏量2
            "test_passed": self.plc.read_bool_m(28, 1)  # M28.1 - 测试通过标志
        }
        return test_data
    
    def print_test_label(self, 
                        template_name: str,
                        serial_number: str,
                        product_model: str,
                        test_data: dict) -> bool:
        """
        打印测试标签
        
        Args:
            template_name: 模板名称
            serial_number: 序列号
            product_model: 产品型号
            test_data: 测试数据
            
        Returns:
            bool: 打印是否成功
        """
        # 构建变量字典
        variables = {
            "SerialNumber": serial_number,
            "ProductModel": product_model,
            "Pressure1": f"{test_data.get('pressure1', 0):.3f}",
            "Leak1": f"{test_data.get('leak1', 0):.6f}",
            "Pressure2": f"{test_data.get('pressure2', 0):.3f}",
            "Leak2": f"{test_data.get('leak2', 0):.6f}",
            "Result": "PASS" if test_data.get('test_passed', False) else "FAIL",
            "QRCode": serial_number
        }
        
        return self.printer.print_label(template_name, variables=variables)
    
    def run(self, 
           template_name: str,
           serial_number: str,
           product_model: str,
           poll_interval: float = 0.5):
        """
        运行测试完成检测和打印循环
        
        Args:
            template_name: 标签模板名称
            serial_number: 序列号
            product_model: 产品型号
            poll_interval: 轮询间隔（秒）
        """
        if not self.connect():
            logger.error("❌ 无法连接到PLC")
            return
        
        self.running = True
        printed = False
        
        logger.info(f"开始监控测试完成状态...")
        logger.info(f"  模板: {template_name}")
        logger.info(f"  序列号: {serial_number}")
        logger.info(f"  产品型号: {product_model}")
        
        try:
            while self.running:
                # 检查测试是否完成
                if self.check_test_complete():
                    logger.info("✅ 检测到测试完成信号")
                    
                    if not printed:
                        # 获取测试结果
                        test_data = self.get_test_result()
                        logger.info(f"测试结果: {test_data}")
                        
                        # 打印标签
                        if self.print_test_label(template_name, serial_number, product_model, test_data):
                            logger.info("✅ 标签打印成功")
                            printed = True
                            
                            # 发送打印完成信号给PLC（可选）
                            # self.plc.write_bool_m(29, 0, True)
                        else:
                            logger.error("❌ 标签打印失败")
                    
                    # 测试完成后退出循环
                    break
                
                time.sleep(poll_interval)
                
        except KeyboardInterrupt:
            logger.info("⚠️ 用户中断程序")
        finally:
            self.disconnect()
            logger.info("系统已停止")
    
    def stop(self):
        """停止系统"""
        self.running = False


def test_system():
    """测试系统"""
    print("=" * 60)
    print("测试完成自动打印系统")
    print("=" * 60)
    
    # 创建系统实例
    system = TestAndPrintSystem(plc_ip='192.168.2.1')
    
    # 测试参数
    template_name = "product_label"  # 产品设置中的标签模板名称
    serial_number = "20260322-MODEL001-0001"
    product_model = "MODEL001"
    
    print(f"\n📄 配置信息:")
    print(f"  标签模板: {template_name}")
    print(f"  序列号: {serial_number}")
    print(f"  产品型号: {product_model}")
    
    # 运行系统（监控测试完成并打印）
    print("\n⏳ 等待测试完成信号...")
    print("按 Ctrl+C 停止\n")
    
    system.run(
        template_name=template_name,
        serial_number=serial_number,
        product_model=product_model,
        poll_interval=0.5
    )
    
    print("\n" + "=" * 60)


if __name__ == '__main__':
    test_system()
