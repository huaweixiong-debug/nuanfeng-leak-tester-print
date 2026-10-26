import snap7
import time
import torch
import struct


# ---------------------- 工具函数（位操作+浮点数转换）----------------------
def get_bit(byte_data, bit_num):
    """提取字节中的指定位（用于M区Bool读取）"""
    if not byte_data:
        return False
    return (byte_data[0] >> bit_num) & 1 == 1


def bytes_to_real(byte_data):
    """4字节转换为Real浮点数（适配MDx）"""
    return struct.unpack('!f', byte_data)[0]


class S7200SmartClient:
    def __init__(self, plc_ip, rack=0, slot=1):
        self.plc = snap7.client.Client()
        self.plc_ip = plc_ip
        self.rack = rack
        self.slot = slot

    def connect(self):
        """连接PLC"""
        try:
            self.plc.connect(self.plc_ip, self.rack, self.slot)
            if self.plc.get_connected():
                print(f"✅ 成功连接PLC：{self.plc_ip}")
                return True
            else:
                print(f"❌ PLC连接失败：{self.plc_ip}")
                return False
        except Exception as e:
            print(f"❌ 连接异常：{str(e)}")
            return False

    def disconnect(self):
        """断开PLC连接"""
        if self.plc.get_connected():
            self.plc.disconnect()
            print(f"✅ 已断开与PLC：{self.plc_ip} 的连接")

    def read_real_md(self, md_start_byte):
        """
        读取M区MD地址的Real数据（核心读取方法）
        :param md_start_byte: MD地址起始字节（MD0→0，MD4→4...）
        :return: 保留6位小数的Real值 / None（读取失败）
        """
        try:
            # 读取MD对应的4字节（Real类型占4字节）
            data = self.plc.mb_read(md_start_byte, 4)
            if not data or len(data) < 4:
                print(f"❌ 读取MD{md_start_byte}失败：地址超出范围或读取异常")
                return None

            real_value = bytes_to_real(data)
            return round(real_value, 6)
        except Exception as e:
            print(f"❌ 读取MD{md_start_byte}异常：{str(e)}")
            return None

    def read_bool_m(self, start_byte, bit_num):
        """读取M区Bool（M0.0拍照信号状态）"""
        try:
            data = self.plc.mb_read(start_byte, 1)
            if not data:
                print(f"❌ 读取M{start_byte}.{bit_num}失败")
                return False
            return get_bit(data, bit_num)
        except Exception as e:
            print(f"❌ 读取Bool异常：{str(e)}")
            return False

    def write_bool_m(self, start_byte, bit_num, value):
        """写入M区Bool（控制拍照信号）"""
        try:
            data = self.plc.mb_read(start_byte, 1)
            if not data:
                print(f"❌ 写入M{start_byte}.{bit_num}失败")
                return False
            byte_data = bytearray(data)
            if value:
                byte_data[0] |= (1 << bit_num)
            else:
                byte_data[0] &= ~(1 << bit_num)
            self.plc.mb_write(start_byte, 1, byte_data)
            print(f"✅ 成功设置M{start_byte}.{bit_num} = {'ON' if value else 'OFF'}")
            return True
        except Exception as e:
            print(f"❌ 写入Bool异常：{str(e)}")
            return False


# -------------------------- 核心测试代码（仅读取MD系列）--------------------------
if __name__ == "__main__":
    # PLC连接配置
    PLC_IP = "192.168.2.1"
    plc_client = S7200SmartClient(plc_ip=PLC_IP)

    # 1. 连接PLC
    if not plc_client.connect():
        exit(1)

    try:
        # 配置要读取的MD地址（MD0~MD16）
        md_addresses = {
            "MD0": 0,  # 起始字节0 → M0~M3
            "MD4": 4,  # 起始字节4 → M4~M7
            "MD8": 8,  # 起始字节8 → M8~M11
            "MD12": 12,  # 起始字节12 → M12~M15
            "MD16": 1012  # 起始字节16 → M16~M19
        }

        # 2. 批量读取MD系列Real数据（核心功能）
        print("\n📊 读取M区Real数据（MD0~MD16）：")
        md_read_results = {}  # 存储读取结果
        for name, start_byte in md_addresses.items():
            value = plc_client.read_real_md(start_byte)
            md_read_results[name] = value
            if value is not None:
                print(f"  {name}: {value:.6f}")
            else:
                print(f"  {name}: 读取失败")

        # 3. M0.0拍照信号状态查询+控制（可选保留）
        BOOL_BYTE = 0  # M0.0 → 字节0
        BOOL_BIT = 0  # M0.0 → 位0
        current_state = plc_client.read_bool_m(BOOL_BYTE, BOOL_BIT)
        print(f"\n🔍 M{BOOL_BYTE}.{BOOL_BIT}（开始拍照）当前状态：{'ON' if current_state else 'OFF'}")

        # （可选）发送拍照信号（如需关闭，注释以下3行）
        print(f"\n📸 发送开始拍照信号（M{BOOL_BYTE}.{BOOL_BIT} = ON）...")
        if plc_client.write_bool_m(BOOL_BYTE, BOOL_BIT, value=True):
            time.sleep(1)
            plc_client.write_bool_m(BOOL_BYTE, BOOL_BIT, value=False)
            print(f"📸 拍照信号已复位（M{BOOL_BYTE}.{BOOL_BIT} = OFF）")

        # 4. PyTorch集成（读取结果转Tensor）
        valid_md_values = [v for v in md_read_results.values() if v is not None]
        if valid_md_values:
            plc_tensor = torch.tensor(valid_md_values, dtype=torch.float32)
            print(f"\n🔥 PyTorch Tensor（有效MD数据）：{plc_tensor}")
        else:
            print("\n⚠️  无有效MD数据，无法转换为PyTorch Tensor")

    except KeyboardInterrupt:
        print("\n⚠️ 用户中断程序")
    finally:
        # 5. 断开PLC连接（必须执行）
        plc_client.disconnect()