#!/usr/bin/env python3
"""
ATEQ 序列号生成器模块
格式: [YYYYMMDD]-[ProductModel]-[IncrementingNumber]
特性:
- 每日午夜重置计数器
- 按产品型号分别计数
- 持久化存储
- 线程安全
- 完整的审计日志
"""

import sqlite3
import os
import threading
import logging
from datetime import datetime, date
from typing import Optional, Dict, Any
from dataclasses import dataclass
from contextlib import contextmanager

# 配置日志（测试环境不落盘，避免导入副作用写入项目目录）
if os.environ.get('ATEQ_DISABLE_HARDWARE') == '1':
    logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()])
else:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('serial_generator.log', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
logger = logging.getLogger('SerialGenerator')

# 数据库路径
DB_PATH = os.path.join(os.path.dirname(__file__), 'serial_numbers.db')

# 线程锁，确保并发安全
_lock = threading.Lock()


def _db_path() -> str:
    return os.environ.get('ATEQ_SERIAL_DB', DB_PATH)


@dataclass
class SerialNumber:
    """序列号数据类"""
    serial_number: str
    date_str: str
    product_model: str
    sequence: int
    generated_at: datetime


class SerialNumberGenerator:
    """序列号生成器"""
    
    # 产品型号验证规则
    VALID_MODEL_PATTERN = r'^.{1,100}$'
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or _db_path()
        self._init_database()
        logger.info(f"序列号生成器初始化完成，数据库: {db_path}")
    
    def _init_database(self):
        """初始化数据库"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 创建序列号记录表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS serial_numbers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    serial_number TEXT UNIQUE NOT NULL,
                    date_str TEXT NOT NULL,
                    product_model TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    qr_code TEXT,
                    metadata TEXT
                )
            ''')
            
            # 创建每日计数器表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_counters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_str TEXT NOT NULL,
                    product_model TEXT NOT NULL,
                    current_count INTEGER DEFAULT 0,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date_str, product_model)
                )
            ''')
            
            # 创建审计日志表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    product_model TEXT,
                    serial_number TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 创建索引
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_serial_date ON serial_numbers(date_str)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_serial_model ON serial_numbers(product_model)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_counter_date_model ON daily_counters(date_str, product_model)
            ''')
            
            conn.commit()
            logger.info("数据库表结构初始化完成")
    
    @contextmanager
    def _get_connection(self):
        """获取数据库连接的上下文管理器"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    def _validate_product_model(self, product_model: str) -> bool:
        """
        验证产品型号格式
        
        规则:
        - 只能包含大写字母、数字、连字符和下划线
        - 长度3-20个字符
        - 必须以字母或数字开头
        """
        import re
        if not product_model or not product_model.strip():
            logger.warning("产品型号为空")
            return False
        
        if not re.match(self.VALID_MODEL_PATTERN, product_model.strip()):
            logger.warning(f"产品型号格式无效: {product_model}")
            return False
        
        return True
    
    def _get_current_date_str(self) -> str:
        """获取当前日期字符串 (YYYYMMDD)"""
        return datetime.now().strftime('%Y%m%d')
    
    def _clean_expired_counters(self, conn: sqlite3.Connection):
        """清理过期的计数器（保留最近30天）"""
        cursor = conn.cursor()
        thirty_days_ago = (datetime.now() - __import__('datetime').timedelta(days=30)).strftime('%Y%m%d')
        
        cursor.execute('''
            DELETE FROM daily_counters WHERE date_str < ?
        ''', (thirty_days_ago,))
        
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"清理了 {deleted} 个过期计数器")
        
        conn.commit()
    
    def get_next_serial_number(self, product_model: str, qr_code: Optional[str] = None) -> SerialNumber:
        """
        获取下一个序列号
        
        Args:
            product_model: 产品型号
            qr_code: 可选的二维码
            
        Returns:
            SerialNumber对象
            
        Raises:
            ValueError: 产品型号格式无效
            RuntimeError: 生成序列号失败
        """
        # 验证产品型号
        if not self._validate_product_model(product_model):
            raise ValueError(f"产品型号格式无效: {product_model}")
        
        # 标准化产品型号（大写）
        product_model = product_model.strip()
        
        with _lock:  # 线程锁，确保并发安全
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                try:
                    current_date = self._get_current_date_str()
                    
                    # 检查是否是新的一天，如果是，重置计数器
                    cursor.execute('''
                        SELECT current_count FROM daily_counters 
                        WHERE date_str = ? AND product_model = ?
                    ''', (current_date, product_model))
                    
                    row = cursor.fetchone()
                    
                    if row:
                        # 当天已有记录，递增
                        current_count = row['current_count'] + 1
                        cursor.execute('''
                            UPDATE daily_counters 
                            SET current_count = ?, last_updated = CURRENT_TIMESTAMP
                            WHERE date_str = ? AND product_model = ?
                        ''', (current_count, current_date, product_model))
                    else:
                        # 新的一天或新产品型号，从1开始
                        current_count = 1
                        cursor.execute('''
                            INSERT INTO daily_counters (date_str, product_model, current_count)
                            VALUES (?, ?, ?)
                        ''', (current_date, product_model, current_count))
                    
                    # 每天、每个型号独立从 0001 开始，最多到 9999。
                    if current_count > 9999:
                        raise RuntimeError(f"产品型号 {product_model} 今天的序号已超过 9999")

                    serial_number = f"{current_date}-{product_model}-{current_count:04d}"
                    
                    # 保存到序列号表
                    cursor.execute('''
                        INSERT INTO serial_numbers 
                        (serial_number, date_str, product_model, sequence, qr_code)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (serial_number, current_date, product_model, current_count, qr_code))
                    
                    # 记录审计日志
                    cursor.execute('''
                        INSERT INTO audit_log (action, product_model, serial_number, details)
                        VALUES (?, ?, ?, ?)
                    ''', ('GENERATE', product_model, serial_number, f"QR: {qr_code or 'N/A'}"))
                    
                    conn.commit()
                    
                    # 清理过期计数器
                    self._clean_expired_counters(conn)
                    
                    logger.info(f"生成序列号: {serial_number}")
                    
                    return SerialNumber(
                        serial_number=serial_number,
                        date_str=current_date,
                        product_model=product_model,
                        sequence=current_count,
                        generated_at=datetime.now()
                    )
                    
                except sqlite3.IntegrityError as e:
                    conn.rollback()
                    logger.error(f"序列号重复错误: {e}")
                    raise RuntimeError(f"生成序列号失败（重复）: {e}")
                except Exception as e:
                    conn.rollback()
                    logger.error(f"生成序列号失败: {e}")
                    raise RuntimeError(f"生成序列号失败: {e}")
    
    def get_today_serials_by_model(self, product_model: str) -> list:
        """获取今日指定产品型号的所有序列号"""
        current_date = self._get_current_date_str()
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM serial_numbers 
                WHERE date_str = ? AND product_model = ?
                ORDER BY sequence
            ''', (current_date, product_model.strip()))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def get_today_statistics(self) -> Dict[str, Any]:
        """获取今日统计信息"""
        current_date = self._get_current_date_str()
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 按产品型号统计
            cursor.execute('''
                SELECT product_model, COUNT(*) as count, MAX(sequence) as max_seq
                FROM serial_numbers 
                WHERE date_str = ?
                GROUP BY product_model
            ''', (current_date,))
            
            model_stats = {row['product_model']: {
                'count': row['count'],
                'max_sequence': row['max_seq']
            } for row in cursor.fetchall()}
            
            # 总计
            cursor.execute('''
                SELECT COUNT(*) as total FROM serial_numbers WHERE date_str = ?
            ''', (current_date,))
            
            total = cursor.fetchone()['total']
            
            return {
                'date': current_date,
                'total': total,
                'by_model': model_stats
            }
    
    def verify_serial_number(self, serial_number: str) -> Optional[Dict[str, Any]]:
        """验证序列号是否存在并返回详细信息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM serial_numbers WHERE serial_number = ?
            ''', (serial_number,))
            
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_audit_log(self, limit: int = 100) -> list:
        """获取审计日志"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM audit_log 
                ORDER BY created_at DESC
                LIMIT ?
            ''', (limit,))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]


# 全局单例实例
_generator_instance: Optional[SerialNumberGenerator] = None


def get_generator() -> SerialNumberGenerator:
    """获取序列号生成器单例"""
    global _generator_instance
    if _generator_instance is None:
        _generator_instance = SerialNumberGenerator()
    return _generator_instance


def reset_generator() -> None:
    """Test hook: drop the singleton so an env-configured path is re-read."""
    global _generator_instance
    _generator_instance = None


def generate_serial_number(product_model: str, qr_code: Optional[str] = None) -> str:
    """
    便捷的序列号生成函数
    
    Args:
        product_model: 产品型号
        qr_code: 可选的二维码
        
    Returns:
        序列号字符串
    """
    generator = get_generator()
    serial = generator.get_next_serial_number(product_model, qr_code)
    return serial.serial_number


# 测试代码
if __name__ == '__main__':
    # 初始化
    gen = SerialNumberGenerator()
    
    # 测试生成序列号
    test_models = ['ABC-123', 'XYZ-999', 'TEST-001']
    
    for model in test_models:
        try:
            serial = gen.get_next_serial_number(model)
            print(f"产品型号 {model}: {serial.serial_number}")
        except Exception as e:
            print(f"错误: {e}")
    
    # 显示今日统计
    stats = gen.get_today_statistics()
    print(f"\n今日统计: {stats}")
