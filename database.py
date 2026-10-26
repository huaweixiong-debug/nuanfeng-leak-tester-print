#!/usr/bin/env python3
"""
ATEQ 测试数据数据库模块
使用 SQLite 存储测试记录

Plan refs: F10, PLAN_FINAL amendment 6.
- additive schema migration only (never drops/rewrites existing rows)
- unique cycle_id index; duplicate completion reuses the row
- daily_serial allocated and inserted in one transaction (BEGIN IMMEDIATE)
- legacy save_test_record(...) keeps its integer record-id return contract
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

_WRITE_LOCK = threading.RLock()
_INIT_LOCK = threading.RLock()
_INITIALIZED = False

NEW_COLUMNS = {
    'cycle_id': 'TEXT',
    'program1_actual': 'INTEGER',
    'program2_actual': 'INTEGER',
    'program1_result': 'TEXT',
    'program2_result': 'TEXT',
    'pressure1_unit': 'TEXT',
    'leak1_unit': 'TEXT',
    'pressure2_unit': 'TEXT',
    'leak2_unit': 'TEXT',
    'terminal_reason': 'TEXT',
    'started_at': 'TEXT',
    'finished_at': 'TEXT',
    'print_state': 'TEXT',
    'print_message': 'TEXT',
    'print_attempted_at': 'TEXT',
    'label_template': 'TEXT',
    'supplier_code': 'TEXT',
}


def _db_path() -> str:
    return os.environ.get(
        'ATEQ_TEST_DB',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ateq_test_data.db'),
    )


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def _connection():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def _to_iso(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return str(value)
    return str(value)


def init_database():
    """Create/migrate the schema. Index failure is a failed initialization."""
    global _INITIALIZED
    with _INIT_LOCK:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS test_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    test_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    daily_serial INTEGER NOT NULL,
                    qr_code TEXT,
                    pressure1 REAL,
                    leak1 REAL,
                    pressure2 REAL,
                    leak2 REAL,
                    test_result TEXT NOT NULL,
                    product_model TEXT,
                    operator TEXT,
                    serial_number TEXT UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            existing = {row['name'] for row in cursor.execute(
                'PRAGMA table_info(test_records)').fetchall()}
            for name, column_type in NEW_COLUMNS.items():
                if name not in existing:
                    cursor.execute(
                        f'ALTER TABLE test_records ADD COLUMN {name} {column_type}')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_test_time ON test_records(test_time)')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_daily_serial ON test_records(daily_serial)')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_qr_code ON test_records(qr_code)')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_serial_number ON test_records(serial_number)')
            try:
                cursor.execute(
                    'CREATE UNIQUE INDEX IF NOT EXISTS idx_test_records_cycle_id '
                    'ON test_records(cycle_id)')
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise RuntimeError(
                    f'database initialization failed: duplicate cycle_id rows ({exc})')
            conn.commit()
            _INITIALIZED = True
            print(f"数据库初始化完成: {_db_path()}")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _ensure_initialized():
    if not _INITIALIZED:
        init_database()


def _allocate_daily_serial(cursor, conn) -> int:
    today = datetime.now().strftime('%Y-%m-%d')
    cursor.execute('''
        SELECT COALESCE(MAX(daily_serial), 0) + 1
        FROM test_records
        WHERE date(test_time) = date(?)
    ''', (today,))
    return int(cursor.fetchone()[0])


PRINT_AUTO_PENDING = 'auto_pending'
PRINT_MANUAL_PENDING = 'manual_pending'
PRINT_SUBMITTED = 'submitted'
PRINT_FAILED = 'failed'
PRINT_TIMEOUT = 'timeout'
PRINT_DISABLED = 'disabled'
PRINT_SKIPPED = 'skipped'
PRINT_CANCELLED = 'cancelled'
PRINT_UNKNOWN = 'unknown'
PRINT_PENDING_STATES = (PRINT_AUTO_PENDING, PRINT_MANUAL_PENDING)
# Manual reprint is only admitted from a terminal state (auto work confirmed ended).
PRINT_MANUAL_ALLOWED_FROM = (
    None, 'pending', PRINT_SUBMITTED, PRINT_FAILED, PRINT_TIMEOUT,
    PRINT_DISABLED, PRINT_SKIPPED, PRINT_CANCELLED, PRINT_UNKNOWN,
)


def _insert_record(conn, *, qr_code, pressure1, leak1, pressure2, leak2,
                   test_result, product_model, operator, serial_number,
                   cycle_id=None, program1_actual=None, program2_actual=None,
                   program1_result=None, program2_result=None,
                   pressure1_unit=None, leak1_unit=None, pressure2_unit=None,
                   leak2_unit=None, terminal_reason=None, started_at=None,
                   finished_at=None, label_template=None,
                   supplier_code=None, print_state=None) -> tuple:
    cursor = conn.cursor()
    daily_serial = _allocate_daily_serial(cursor, conn)
    attempted_at = (datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if print_state in PRINT_PENDING_STATES else None)
    cursor.execute('''
        INSERT INTO test_records
        (test_time, daily_serial, qr_code, pressure1, leak1, pressure2, leak2,
         test_result, product_model, operator, serial_number,
         cycle_id, program1_actual, program2_actual, program1_result, program2_result,
         pressure1_unit, leak1_unit, pressure2_unit, leak2_unit, terminal_reason,
         started_at, finished_at, label_template, supplier_code,
         print_state, print_attempted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        daily_serial, qr_code, pressure1, leak1, pressure2, leak2,
        test_result, product_model, operator, serial_number,
        cycle_id, program1_actual, program2_actual, program1_result, program2_result,
        pressure1_unit, leak1_unit, pressure2_unit, leak2_unit, terminal_reason,
        _to_iso(started_at), _to_iso(finished_at), label_template, supplier_code,
        print_state, attempted_at,
    ))
    return cursor.lastrowid, daily_serial


def claim_manual_print(record_id: int) -> dict:
    """Atomic record-level manual print admission.

    Claims only from a terminal (non-pending) state; while an automatic job is
    pending (auto_pending/manual_pending) the claim fails and reports the state.
    """
    if record_id is None:
        return {'claimed': False, 'state': None}
    _ensure_initialized()
    allowed = [state for state in PRINT_MANUAL_ALLOWED_FROM]
    with _WRITE_LOCK:
        with _connection() as conn:
            cursor = conn.cursor()
            cursor.execute('BEGIN IMMEDIATE')
            row = cursor.execute(
                'SELECT print_state FROM test_records WHERE id = ?',
                (int(record_id),)).fetchone()
            if row is None:
                conn.rollback()
                return {'claimed': False, 'state': None, 'error': 'not_found'}
            current = row['print_state']
            if current not in allowed:
                conn.rollback()
                return {'claimed': False, 'state': current}
            # IN 列表与 PRINT_MANUAL_ALLOWED_FROM 的 9 个状态一一对应；
            # 全静态 SQL，不做任何字符串拼接
            cursor.execute('''
                UPDATE test_records
                SET print_state = ?, print_attempted_at = ?
                WHERE id = ? AND (print_state IS NULL OR print_state IN
                (?, ?, ?, ?, ?, ?, ?, ?, ?))
            ''', (PRINT_MANUAL_PENDING,
                  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  int(record_id)) + tuple(allowed))
            claimed = cursor.rowcount == 1
            conn.commit()
            return {'claimed': claimed,
                    'state': PRINT_MANUAL_PENDING if claimed else current}


def recover_pending_prints() -> int:
    """Startup: pending print jobs become 'unknown' and are never auto-retried."""
    _ensure_initialized()
    with _WRITE_LOCK:
        with _connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE test_records
                SET print_state = ?, print_message = ?
                WHERE print_state IN (?, ?)
            ''', (PRINT_UNKNOWN, '重启未确认，未自动重试',
                  PRINT_AUTO_PENDING, PRINT_MANUAL_PENDING))
            count = cursor.rowcount
            conn.commit()
            return count


def find_record_by_cycle(cycle_id: str) -> Optional[Dict[str, Any]]:
    if not cycle_id:
        return None
    _ensure_initialized()
    with _connection() as conn:
        row = conn.execute(
            'SELECT * FROM test_records WHERE cycle_id = ?', (cycle_id,)).fetchone()
        return dict(row) if row else None


def get_record(record_id: int) -> Optional[Dict[str, Any]]:
    _ensure_initialized()
    with _connection() as conn:
        row = conn.execute(
            'SELECT * FROM test_records WHERE id = ?', (int(record_id),)).fetchone()
        return dict(row) if row else None


def update_print_status(record_id: int, state: str, message: str = '') -> None:
    if record_id is None:
        return
    _ensure_initialized()
    with _WRITE_LOCK:
        with _connection() as conn:
            conn.execute('''
                UPDATE test_records
                SET print_state = ?, print_message = ?, print_attempted_at = ?
                WHERE id = ?
            ''', (state, message, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  int(record_id)))
            conn.commit()


def get_next_daily_serial() -> int:
    """Legacy helper: next daily serial (allocation is atomic inside save paths)."""
    _ensure_initialized()
    with _WRITE_LOCK:
        conn = _connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            value = _allocate_daily_serial(conn.cursor(), conn)
            conn.commit()
            return value
        finally:
            conn.close()


def save_test_record(
    qr_code: Optional[str],
    pressure1: Optional[float],
    leak1: Optional[float],
    pressure2: Optional[float],
    leak2: Optional[float],
    test_result: str,
    product_model: Optional[str],
    operator: Optional[str],
    serial_number: Optional[str] = None,
    *,
    cycle_id: Optional[str] = None,
    program1_actual: Optional[int] = None,
    program2_actual: Optional[int] = None,
    program1_result: Optional[str] = None,
    program2_result: Optional[str] = None,
    pressure1_unit: Optional[str] = None,
    leak1_unit: Optional[str] = None,
    pressure2_unit: Optional[str] = None,
    leak2_unit: Optional[str] = None,
    terminal_reason: Optional[str] = None,
    started_at=None,
    finished_at=None,
    label_template: Optional[str] = None,
    supplier_code: Optional[str] = None,
    print_state: Optional[str] = None,
) -> int:
    """Save one record; duplicate cycle_id reuses the existing row (legacy int id)."""
    record_id, _created = save_record_ex(
        qr_code=qr_code, pressure1=pressure1, leak1=leak1,
        pressure2=pressure2, leak2=leak2, test_result=test_result,
        product_model=product_model, operator=operator,
        serial_number=serial_number, cycle_id=cycle_id,
        program1_actual=program1_actual, program2_actual=program2_actual,
        program1_result=program1_result, program2_result=program2_result,
        pressure1_unit=pressure1_unit, leak1_unit=leak1_unit,
        pressure2_unit=pressure2_unit, leak2_unit=leak2_unit,
        terminal_reason=terminal_reason, started_at=started_at,
        finished_at=finished_at, label_template=label_template,
        supplier_code=supplier_code, print_state=print_state,
    )
    return record_id


def save_record_ex(*, qr_code, pressure1, leak1, pressure2, leak2, test_result,
                   product_model, operator, serial_number=None, cycle_id=None,
                   program1_actual=None, program2_actual=None,
                   program1_result=None, program2_result=None,
                   pressure1_unit=None, leak1_unit=None, pressure2_unit=None,
                   leak2_unit=None, terminal_reason=None, started_at=None,
                   finished_at=None, label_template=None,
                   supplier_code=None, print_state=None) -> tuple:
    """Return (record_id, created). Idempotent by cycle_id under the unique index."""
    _ensure_initialized()
    if cycle_id:
        existing = find_record_by_cycle(cycle_id)
        if existing is not None:
            return int(existing['id']), False
    with _WRITE_LOCK:
        conn = _connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            try:
                record_id, _daily = _insert_record(
                    conn, qr_code=qr_code, pressure1=pressure1, leak1=leak1,
                    pressure2=pressure2, leak2=leak2, test_result=test_result,
                    product_model=product_model, operator=operator,
                    serial_number=serial_number, cycle_id=cycle_id,
                    program1_actual=program1_actual, program2_actual=program2_actual,
                    program1_result=program1_result, program2_result=program2_result,
                    pressure1_unit=pressure1_unit, leak1_unit=leak1_unit,
                    pressure2_unit=pressure2_unit, leak2_unit=leak2_unit,
                    terminal_reason=terminal_reason, started_at=started_at,
                    finished_at=finished_at, label_template=label_template,
                    supplier_code=supplier_code, print_state=print_state,
                )
                conn.commit()
                return int(record_id), True
            except sqlite3.IntegrityError:
                conn.rollback()
                if cycle_id:
                    existing = find_record_by_cycle(cycle_id)
                    if existing is not None:
                        return int(existing['id']), False
                raise
        finally:
            conn.close()


def get_records_by_date(date_str: str) -> List[Dict[str, Any]]:
    _ensure_initialized()
    with _connection() as conn:
        rows = conn.execute('''
            SELECT * FROM test_records
            WHERE date(test_time) = date(?)
            ORDER BY daily_serial
        ''', (date_str,)).fetchall()
        records = [dict(row) for row in rows]
        return _attach_model_serial(records)


def get_records_by_qr_code(qr_code: str) -> List[Dict[str, Any]]:
    _ensure_initialized()
    with _connection() as conn:
        rows = conn.execute('''
            SELECT * FROM test_records
            WHERE qr_code = ?
            ORDER BY test_time DESC
        ''', (qr_code,)).fetchall()
        return [dict(row) for row in rows]


def get_latest_records(limit: int = 100) -> List[Dict[str, Any]]:
    _ensure_initialized()
    with _connection() as conn:
        rows = conn.execute('''
            SELECT * FROM test_records
            ORDER BY test_time DESC
            LIMIT ?
        ''', (limit,)).fetchall()
        records = [dict(row) for row in rows]
        return _attach_model_serial(records)


def get_statistics_by_date(date_str: str) -> Dict[str, Any]:
    _ensure_initialized()
    with _connection() as conn:
        row = conn.execute('''
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN test_result = 'PASS' THEN 1 ELSE 0 END) as pass_count,
                SUM(CASE WHEN test_result = 'FAIL' THEN 1 ELSE 0 END) as fail_count
            FROM test_records
            WHERE date(test_time) = date(?)
        ''', (date_str,)).fetchone()

    total = row['total']
    pass_count = row['pass_count'] or 0
    fail_count = row['fail_count'] or 0

    return {
        'total': total,
        'pass_count': pass_count,
        'fail_count': fail_count,
        'pass_rate': round(pass_count / total * 100, 2) if total > 0 else 0
    }


def _attach_model_serial(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """为每条记录补 model_serial：同一产品型号在当日内的顺序号（时间正序，从 1 起）。

    按全表（同型号+同日期的所有记录）计算，不受分页/筛选影响；
    daily_serial 保持原样（全局每日流水号），两者并存。
    """
    if not records:
        return records
    _ensure_initialized()
    keys = [(r.get('product_model') or '', str(r.get('test_time') or '')[:10])
            for r in records]
    with _connection() as conn:
        for (model, day) in dict.fromkeys(keys):
            rows = conn.execute('''
                SELECT id FROM test_records
                WHERE date(test_time) = date(?)
                  AND product_model IS ? AND product_model != ''
                ORDER BY test_time ASC, id ASC
            ''', (day, model)).fetchall()
            for pos, row in enumerate(rows, 1):
                for r in records:
                    if r.get('id') == row['id']:
                        r['model_serial'] = pos
    for r in records:
        r.setdefault('model_serial', None)
    return records


def query_records(start_date: str = None, end_date: str = None,
                  product_model: str = None, result: str = None,
                  qr_code: str = None, serial: str = None,
                  limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    _ensure_initialized()
    with _connection() as conn:
        # 可选过滤条件统一用"参数为 NULL 则不过滤"的静态 SQL 表达，
        # 不拼接任何 WHERE 片段
        start_date = start_date or None
        end_date = end_date or None
        product_model = product_model or None
        result = result or None
        qr_code = qr_code or None
        serial = serial or None
        filter_params = (
            start_date, start_date,
            end_date, end_date,
            product_model, product_model,
            result, result,
            qr_code, (f'%{qr_code}%' if qr_code else None),
            serial, (f'%{serial}%' if serial else None),
        )

        rows = conn.execute('''
            SELECT * FROM test_records
            WHERE (? IS NULL OR date(test_time) >= date(?))
              AND (? IS NULL OR date(test_time) <= date(?))
              AND (? IS NULL OR product_model = ?)
              AND (? IS NULL OR test_result = ?)
              AND (? IS NULL OR qr_code LIKE ?)
              AND (? IS NULL OR serial_number LIKE ?)
            ORDER BY test_time DESC
            LIMIT ? OFFSET ?
        ''', filter_params + (limit, offset)).fetchall()
        records = [dict(row) for row in rows]

        row = conn.execute('''
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN test_result = 'PASS' THEN 1 ELSE 0 END) as pass_count,
                SUM(CASE WHEN test_result = 'FAIL' THEN 1 ELSE 0 END) as fail_count
            FROM test_records
            WHERE (? IS NULL OR date(test_time) >= date(?))
              AND (? IS NULL OR date(test_time) <= date(?))
              AND (? IS NULL OR product_model = ?)
              AND (? IS NULL OR test_result = ?)
              AND (? IS NULL OR qr_code LIKE ?)
              AND (? IS NULL OR serial_number LIKE ?)
        ''', filter_params).fetchone()

    total = row['total'] or 0
    pass_count = row['pass_count'] or 0
    fail_count = row['fail_count'] or 0

    return {
        'records': _attach_model_serial(records),
        'total': total,
        'pass_count': pass_count,
        'fail_count': fail_count,
        'pass_rate': round(pass_count / total * 100, 2) if total > 0 else 0
    }


def get_all_product_models() -> List[str]:
    _ensure_initialized()
    with _connection() as conn:
        rows = conn.execute('''
            SELECT DISTINCT product_model
            FROM test_records
            WHERE product_model IS NOT NULL AND product_model != ''
            ORDER BY product_model
        ''').fetchall()
        return [row[0] for row in rows if row[0]]


if __name__ == '__main__':
    init_database()
    print("数据库表结构已创建")
