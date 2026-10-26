#!/usr/bin/env python3
"""
西门子 S7-200 SMART PLC 通讯模块
基于成功测试的方法
"""

import snap7
import time
import struct
import logging
from typing import Optional, Dict, Any

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('s7_plc.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('S7PLC')


def get_bit(byte_data, bit_num):
    """提取字节中的指定位（用于M区Bool读取）"""
    if not byte_data:
        return False
    return (byte_data[0] >> bit_num) & 1 == 1


def bytes_to_real(byte_data):
    """4字节转换为Real浮点数（适配MDx）"""
    return struct.unpack('!f', byte_data)[0]


class S7200SmartClient:
    """西门子 S7-200 SMART PLC 客户端"""
    
    def __init__(self, plc_ip: str = '192.168.2.1', rack: int = 0, slot: int = 1):
        """
        初始化PLC客户端
        
        Args:
            plc_ip: PLC IP地址
            rack: 机架号 (S7-200 SMART 通常为 0)
            slot: 槽号 (S7-200 SMART 通常为 1)
        """
        self.plc = snap7.client.Client()
        self.plc_ip = plc_ip
        self.rack = rack
        self.slot = slot
        logger.info(f"初始化 S7-200 SMART PLC 客户端: {plc_ip}, Rack: {rack}, Slot: {slot}")
    
    def connect(self) -> bool:
        """
        连接PLC
        
        Returns:
            bool: 连接是否成功
        """
        try:
            self.plc.connect(self.plc_ip, self.rack, self.slot)
            if self.plc.get_connected():
                logger.info(f"✅ 成功连接PLC: {self.plc_ip}")
                return True
            else:
                logger.error(f"❌ PLC连接失败: {self.plc_ip}")
                return False
        except Exception as e:
            logger.error(f"❌ 连接异常: {str(e)}")
            return False
    
    def disconnect(self):
        """断开PLC连接"""
        if self.plc.get_connected():
            self.plc.disconnect()
            logger.info(f"✅ 已断开与PLC: {self.plc_ip} 的连接")
    
    def is_connected(self) -> bool:
        """检查连接状态"""
        return self.plc.get_connected()
    
    def read_real_md(self, md_start_byte: int) -> Optional[float]:
        """
        读取M区MD地址的Real数据
        
        Args:
            md_start_byte: MD地址起始字节（MD0→0，MD4→4...）
            
        Returns:
            float: 保留6位小数的Real值，失败返回None
        """
        try:
            data = self.plc.mb_read(md_start_byte, 4)
            if not data or len(data) < 4:
                logger.error(f"❌ 读取MD{md_start_byte}失败：地址超出范围或读取异常")
                return None
            
            real_value = bytes_to_real(data)
            return round(real_value, 6)
        except Exception as e:
            logger.error(f"❌ 读取MD{md_start_byte}异常: {str(e)}")
            return None
    
    def write_real_md(self, md_start_byte: int, value: float) -> bool:
        """
        写入M区MD地址的Real数据
        
        Args:
            md_start_byte: MD地址起始字节
            value: 要写入的浮点数值
            
        Returns:
            bool: 写入是否成功
        """
        try:
            byte_data = struct.pack('!f', value)
            self.plc.mb_write(md_start_byte, 4, bytearray(byte_data))
            logger.info(f"✅ 成功写入MD{md_start_byte} = {value:.6f}")
            return True
        except Exception as e:
            logger.error(f"❌ 写入MD{md_start_byte}异常: {str(e)}")
            return False
    
    def read_bool_m(self, start_byte: int, bit_num: int) -> bool:
        """
        读取M区Bool
        
        Args:
            start_byte: 字节地址
            bit_num: 位号
            
        Returns:
            bool: 位状态
        """
        try:
            data = self.plc.mb_read(start_byte, 1)
            if not data:
                logger.error(f"❌ 读取M{start_byte}.{bit_num}失败")
                return False
            return get_bit(data, bit_num)
        except Exception as e:
            logger.error(f"❌ 读取Bool异常: {str(e)}")
            return False
    
    def write_bool_m(self, start_byte: int, bit_num: int, value: bool) -> bool:
        """
        写入M区Bool
        
        Args:
            start_byte: 字节地址
            bit_num: 位号
            value: 要写入的值
            
        Returns:
            bool: 写入是否成功
        """
        try:
            data = self.plc.mb_read(start_byte, 1)
            if not data:
                logger.error(f"❌ 写入M{start_byte}.{bit_num}失败")
                return False
            byte_data = bytearray(data)
            if value:
                byte_data[0] |= (1 << bit_num)
            else:
                byte_data[0] &= ~(1 << bit_num)
            self.plc.mb_write(start_byte, 1, byte_data)
            logger.info(f"✅ 成功设置M{start_byte}.{bit_num} = {'ON' if value else 'OFF'}")
            return True
        except Exception as e:
            logger.error(f"❌ 写入Bool异常: {str(e)}")
            return False
    
    def read_int_mw(self, mw_start_byte: int) -> Optional[int]:
        """
        读取M区MW地址的Int数据
        
        Args:
            mw_start_byte: MW地址起始字节
            
        Returns:
            int: 整数值，失败返回None
        """
        try:
            data = self.plc.mb_read(mw_start_byte, 2)
            if not data or len(data) < 2:
                logger.error(f"❌ 读取MW{mw_start_byte}失败")
                return None
            return struct.unpack('!h', data)[0]
        except Exception as e:
            logger.error(f"❌ 读取MW{mw_start_byte}异常: {str(e)}")
            return None
    
    def write_int_mw(self, mw_start_byte: int, value: int) -> bool:
        """
        写入M区MW地址的Int数据
        
        Args:
            mw_start_byte: MW地址起始字节
            value: 要写入的整数值
            
        Returns:
            bool: 写入是否成功
        """
        try:
            byte_data = struct.pack('!h', value)
            self.plc.mb_write(mw_start_byte, 2, bytearray(byte_data))
            logger.info(f"✅ 成功写入MW{mw_start_byte} = {value}")
            return True
        except Exception as e:
            logger.error(f"❌ 写入MW{mw_start_byte}异常: {str(e)}")
            return False
    
    def read_batch_md(self, md_addresses: Dict[str, int]) -> Dict[str, Optional[float]]:
        """
        批量读取MD系列Real数据
        
        Args:
            md_addresses: 地址字典 {"MD0": 0, "MD4": 4, ...}
            
        Returns:
            dict: 读取结果字典
        """
        results = {}
        for name, start_byte in md_addresses.items():
            value = self.read_real_md(start_byte)
            results[name] = value
            if value is not None:
                logger.info(f"  {name}: {value:.6f}")
            else:
                logger.warning(f"  {name}: 读取失败")
        return results
    
    def read_batch_bool(self, bool_addresses: Dict[str, tuple]) -> Dict[str, bool]:
        """
        批量读取Bool位状态
        
        Args:
            bool_addresses: 地址字典 {"报警标志位": (27, 0), "手动/自动": (26, 0), ...}
            
        Returns:
            dict: 读取结果字典
        """
        results = {}
        for name, (byte_addr, bit_addr) in bool_addresses.items():
            value = self.read_bool_m(byte_addr, bit_addr)
            results[name] = value
            status = "ON" if value else "OFF"
            logger.info(f"  {name} (M{byte_addr}.{bit_addr}): {status}")
        return results
    
    def __enter__(self):
        """上下文管理器入口"""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.disconnect()


def test_plc_communication():
    """测试PLC通讯"""
    print("=" * 60)
    print("西门子 S7-200 SMART PLC 通讯测试")
    print("=" * 60)
    
    plc_client = S7200SmartClient(plc_ip='192.168.2.1')
    
    if not plc_client.connect():
        print("\n请检查:")
        print("  1. PLC 是否已开机")
        print("  2. IP 地址是否正确 (192.168.2.1)")
        print("  3. 网络连接是否正常")
        print("  4. PLC 是否允许远程连接")
        return
    
    try:
        # 测试指定地址
        print("\n📊 测试指定Bool地址状态：")
        test_addresses = {
            "报警标志位": (27, 0),
            "手动/自动": (26, 0),
            "屏蔽安全门": (26, 1),
            "手动移载": (26, 2),
            "手动封堵": (26, 3),
            "手动盖章": (26, 4),
            "手动排气": (26, 5)
        }
        
        results = plc_client.read_batch_bool(test_addresses)
        
        print("\n" + "-" * 60)
        print("📋 测试结果汇总：")
        print("-" * 60)
        for name, state in results.items():
            byte_addr, bit_addr = test_addresses[name]
            status = "🟢 ON" if state else "⚪ OFF"
            print(f"  {name:12s} (M{byte_addr}.{bit_addr}): {status}")
    
    except KeyboardInterrupt:
        print("\n⚠️ 用户中断程序")
    finally:
        plc_client.disconnect()
    
    print("\n" + "=" * 60)


if __name__ == '__main__':
    test_plc_communication()
