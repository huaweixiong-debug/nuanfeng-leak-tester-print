#!/usr/bin/env python3
"""
ATEQ 数据解析程序
用于处理从站地址1接收到的响应数据
基于F5协议，参考手册第65页

请求格式：01 03 00 30 00 0D 84 00
响应格式：01 03 1A ... <Word0> <Word1> <Word2> <Word3> ... CRC

状态字位于响应数据中的第4个字（Word3），对应第7-8字节
"""

class ATEQResponseParser:
    """ATEQ响应数据解析器"""
    
    # 状态字位定义（F5协议）
    STATUS_BITS = {
        0: "合格（Pass part）",
        1: "测试件不合格（Fail test part）",
        2: "参考件不合格（Fail reference part）",
        3: "报警",
        4: "压力错误",
        5: "循环结束（Cycle end）",
        15: "按键存在"
    }
    
    @staticmethod
    def parse_response(response_hex):
        """
        解析ATEQ响应数据
        
        Args:
            response_hex: 十六进制响应数据字符串（如 "01031A0000080001002180FFFFD8FFFFFFE02E00000000000038C70000E938"）
        
        Returns:
            dict: 解析结果
        """
        try:
            # 验证响应格式
            if not response_hex or len(response_hex) < 10:
                return {
                    'success': False,
                    'error': '响应数据长度不足'
                }
            
            # 验证从站地址
            slave_id = int(response_hex[:2], 16)
            if slave_id != 1:
                return {
                    'success': False,
                    'error': f'从站地址错误，期望1，实际{slave_id}'
                }
            
            # 验证功能码
            function_code = int(response_hex[2:4], 16)
            if function_code != 3:
                return {
                    'success': False,
                    'error': f'功能码错误，期望3，实际{function_code}'
                }
            
            # 提取数据长度
            data_length = int(response_hex[4:6], 16)
            
            # 验证数据长度
            expected_length = 6 + data_length * 2  # 6字节头 + 数据 + CRC
            if len(response_hex) < expected_length:
                return {
                    'success': False,
                    'error': '响应数据长度与数据字段长度不匹配'
                }
            
            # 提取Word3（状态字）- 第4个字，对应第7-8字节（从0开始计数）
            # 每个字2字节，Word0: 6-9字符（3-4字节）, Word1: 10-13, Word2: 14-17, Word3: 18-21
            if len(response_hex) < 22:
                return {
                    'success': False,
                    'error': '响应数据中没有足够的字节来提取状态字'
                }
            
            # 提取Word3（状态字）
            status_word_hex = response_hex[18:22]  # 第18-21位（Python字符串索引）
            status_word = int(status_word_hex, 16)
            
            # 解析状态位
            status_bits = {}
            for bit, description in ATEQResponseParser.STATUS_BITS.items():
                status_bits[bit] = {
                    'value': (status_word >> bit) & 1,
                    'description': description
                }
            
            # 检查循环结束标志（位5）
            cycle_end = status_bits[5]['value']
            
            if not cycle_end:
                return {
                    'success': True,
                    'status_word': status_word,
                    'status_word_hex': status_word_hex,
                    'status_bits': status_bits,
                    'valid': False,
                    'message': '测试结果无效：循环结束标志未置位'
                }
            
            # 判定测试结果（按优先级）
            result = '未知状态'
            if status_bits[2]['value']:
                result = '参考件不合格'
            elif status_bits[1]['value']:
                result = '测试件不合格'
            elif status_bits[0]['value']:
                result = '合格'
            
            return {
                'success': True,
                'status_word': status_word,
                'status_word_hex': status_word_hex,
                'status_bits': status_bits,
                'valid': True,
                'result': result,
                'message': f'测试结果有效：{result}'
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': f'解析错误：{str(e)}'
            }
    
    @staticmethod
    def print_result(result):
        """
        打印解析结果
        
        Args:
            result: 解析结果字典
        """
        print("=" * 60)
        print("ATEQ 响应数据解析结果")
        print("=" * 60)
        
        if not result['success']:
            print(f"❌ 解析失败: {result['error']}")
            return
        
        print(f"状态字: 0x{result['status_word_hex']} ({result['status_word']})")
        print()
        print("状态位解析:")
        for bit, info in sorted(result['status_bits'].items()):
            status = "✓" if info['value'] else "✗"
            print(f"  位{bit}: {status} - {info['description']}")
        print()
        
        if not result['valid']:
            print(f"⚠️  {result['message']}")
        else:
            if result['result'] == '合格':
                print(f"✅ {result['message']}")
            else:
                print(f"❌ {result['message']}")
        print("=" * 60)

if __name__ == '__main__':
    # 测试用例
    test_cases = [
        # 测试用例1: 合格
        "01031A0000080001002180FFFFD8FFFFFFE02E00000000000038C70000E938",
        # 测试用例2: 测试件不合格
        "01031A0000080001002280FFFFD8FFFFFFE02E00000000000038C70000E938",
        # 测试用例3: 参考件不合格
        "01031A0000080001002480FFFFD8FFFFFFE02E00000000000038C70000E938",
        # 测试用例4: 循环未结束（无效）
        "01031A0000080001000180FFFFD8FFFFFFE02E00000000000038C70000E938",
        # 测试用例5: 数据长度不足
        "01031A0000080001",
        # 测试用例6: 循环结束 + 合格
        "01031A0000080001002180FFFFD8FFFFFFE02E00000000000038C70000E938",
        # 测试用例7: 循环结束 + 测试件不合格
        "01031A0000080001002280FFFFD8FFFFFFE02E00000000000038C70000E938",
    ]
    
    for i, test_data in enumerate(test_cases):
        print(f"\n测试用例 {i+1}:")
        print(f"原始数据: {test_data}")
        result = ATEQResponseParser.parse_response(test_data)
        ATEQResponseParser.print_result(result)
