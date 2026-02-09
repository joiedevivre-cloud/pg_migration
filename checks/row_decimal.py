"""
Row 단위 Decimal 정밀 비교 모듈
Decimal tolerance를 고려한 행 단위 검증
"""
import logging
from decimal import Decimal
from typing import List, Dict, Any, Optional
from db.oracle import OracleDB
from db.postgres import PostgresDB

logger = logging.getLogger(__name__)


class RowDecimalValidator:
    """Row 단위 Decimal 검증 클래스"""
    
    def __init__(self, oracle_db: OracleDB, postgres_db: PostgresDB):
        self.oracle_db = oracle_db
        self.postgres_db = postgres_db
    
    def compare_rows_with_tolerance_streaming(self, table_name: str, schema: str,
                                             columns: List[str], where_clause: str = "",
                                             partition_name: Optional[str] = None,
                                             tolerance: float = 0.0001,
                                             max_diffs: int = 10) -> Dict[str, Any]:
        """
        Row-by-row로 Decimal tolerance를 고려한 비교 (Streaming 방식, 조기 종료 지원)
        
        Cursor를 직접 제어하여 한 행씩 읽으면서 비교하고,
        max_diffs에 도달하면 즉시 중단하여 리소스를 절약합니다.
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            columns: 컬럼 리스트
            where_clause: WHERE 절 조건
            partition_name: 파티션명 (옵션)
            tolerance: Decimal 허용 오차
            max_diffs: 최대 불일치 허용 수 (조기 종료 임계값)
        
        Returns:
            검증 결과 딕셔너리
        """
        try:
            # Oracle과 PostgreSQL에서 cursor를 직접 제어하여 한 행씩 읽기
            mismatches = []
            diff_count = 0
            total_rows_checked = 0
            early_exit = False
            
            # Oracle cursor
            ora_cursor = self._get_oracle_cursor(
                table_name, schema, columns, where_clause, partition_name
            )
            
            # PostgreSQL cursor (파티션명 전달)
            pg_cursor = self._get_postgres_cursor(
                table_name, schema, columns, where_clause, partition_name
            )
            
            try:
                # 한 행씩 읽으면서 비교 (조기 종료 적용)
                while True:
                    ora_row = ora_cursor.fetchone()
                    pg_row = pg_cursor.fetchone()
                    
                    # 한쪽이라도 끝나면 종료
                    if ora_row is None or pg_row is None:
                        if ora_row is not None or pg_row is not None:
                            # 행 수 불일치
                            return {
                                'match': False,
                                'reason': 'row_count_mismatch',
                                'total_rows_checked': total_rows_checked,
                                'early_exit': early_exit,
                                'table_name': table_name,
                                'schema': schema
                            }
                        break
                    
                    total_rows_checked += 1
                    
                    # 행 비교
                    row_match, mismatch_details = self._compare_row(
                        ora_row, pg_row, columns, tolerance
                    )
                    
                    if not row_match:
                        diff_count += 1
                        mismatches.append({
                            'row_index': total_rows_checked - 1,
                            'details': mismatch_details
                        })
                        
                        # 조기 종료: max_diffs에 도달하면 즉시 중단
                        if diff_count >= max_diffs:
                            logger.info(f"조기 종료: {max_diffs}개 불일치 도달 (검증된 행: {total_rows_checked}건)")
                            early_exit = True
                            break
                
                return {
                    'match': len(mismatches) == 0,
                    'total_rows_checked': total_rows_checked,
                    'mismatch_count': len(mismatches),
                    'mismatches': mismatches,
                    'early_exit': early_exit,
                    'max_diffs_reached': diff_count >= max_diffs,
                    'table_name': table_name,
                    'schema': schema
                }
                
            finally:
                # Cursor 정리
                if ora_cursor:
                    ora_cursor.close()
                if pg_cursor:
                    pg_cursor.close()
            
        except Exception as e:
            logger.error(f"Streaming row comparison failed: {e}")
            return {
                'match': False,
                'error': str(e),
                'table_name': table_name,
                'schema': schema
            }
    
    def compare_rows_with_tolerance(self, table_name: str, schema: str,
                                   columns: List[str], where_clause: str = "",
                                   partition_name: Optional[str] = None,
                                   tolerance: float = 0.0001) -> Dict[str, Any]:
        """
        Row-by-row로 Decimal tolerance를 고려한 비교
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            columns: 컬럼 리스트
            where_clause: WHERE 절 조건
            partition_name: 파티션명 (옵션)
            tolerance: Decimal 허용 오차
        
        Returns:
            검증 결과 딕셔너리
        """
        try:
            # Oracle에서 데이터 조회
            oracle_rows = self._fetch_oracle_rows(
                table_name, schema, columns, where_clause, partition_name
            )
            
            # PostgreSQL에서 데이터 조회 (파티션명 전달)
            postgres_rows = self._fetch_postgres_rows(
                table_name, schema, columns, where_clause, partition_name
            )
            
            # 행 수 비교
            if len(oracle_rows) != len(postgres_rows):
                return {
                    'match': False,
                    'reason': 'row_count_mismatch',
                    'oracle_count': len(oracle_rows),
                    'postgres_count': len(postgres_rows),
                    'table_name': table_name,
                    'schema': schema
                }
            
            # 각 행 비교
            mismatches = []
            for idx, (oracle_row, postgres_row) in enumerate(zip(oracle_rows, postgres_rows)):
                row_match, mismatch_details = self._compare_row(
                    oracle_row, postgres_row, columns, tolerance
                )
                if not row_match:
                    mismatches.append({
                        'row_index': idx,
                        'details': mismatch_details
                    })
            
            return {
                'match': len(mismatches) == 0,
                'total_rows': len(oracle_rows),
                'mismatch_count': len(mismatches),
                'mismatches': mismatches,
                'table_name': table_name,
                'schema': schema
            }
            
        except Exception as e:
            logger.error(f"Row comparison failed: {e}")
            return {
                'match': False,
                'error': str(e),
                'table_name': table_name,
                'schema': schema
            }
    
    def _fetch_oracle_rows(self, table_name: str, schema: str,
                           columns: List[str], where_clause: str = "",
                           partition_name: Optional[str] = None) -> List[tuple]:
        """Oracle에서 행 데이터 조회"""
        # Oracle은 대문자로 저장되므로 대문자로 변환
        col_list = ", ".join([f'"{col.upper()}"' for col in columns])
        full_table = f'"{schema}"."{table_name}"'
        if partition_name:
            full_table = f'"{schema}"."{table_name}" PARTITION("{partition_name}")'
        
        where_sql = f"WHERE {where_clause}" if where_clause else ""
        sql = f'SELECT {col_list} FROM {full_table} {where_sql} ORDER BY ROWID'
        
        return self.oracle_db.execute_query(sql)
    
    def _convert_where_clause_to_postgres(self, where_clause: str) -> str:
        """
        WHERE 절의 컬럼명을 PostgreSQL 형식으로 변환 (대문자 -> 소문자)
        
        Args:
            where_clause: Oracle 형식의 WHERE 절 (대문자 컬럼명)
        
        Returns:
            PostgreSQL 형식의 WHERE 절 (소문자 컬럼명)
        """
        if not where_clause:
            return ""
        
        # 대문자로 된 따옴표로 감싼 컬럼명을 소문자로 변환
        import re
        pg_where = re.sub(r'"([A-Z_][A-Z0-9_]*)"', lambda m: f'"{m.group(1).lower()}"', where_clause)
        return pg_where
    
    def _fetch_postgres_rows(self, table_name: str, schema: str,
                            columns: List[str], where_clause: str = "",
                            partition_name: Optional[str] = None) -> List[tuple]:
        """PostgreSQL에서 행 데이터 조회 (파티션 범위 SQL 지원)"""
        col_list = ", ".join([f'"{col}"' for col in columns])
        # PostgreSQL 스키마는 일반적으로 소문자로 처리 (data_migrator.py와 동일한 방식)
        # unquoted identifier로 사용 (data_migrator.py와 일치)
        pg_schema = schema.lower()
        
        # 파티션 범위 SQL 지원
        if partition_name:
            # PostgreSQL: Child 테이블 직접 접근
            pg_table = partition_name.lower()
        else:
            pg_table = table_name.lower()
        
        # WHERE 절을 PostgreSQL 형식으로 변환
        pg_where_clause = self._convert_where_clause_to_postgres(where_clause)
        where_sql = f"WHERE {pg_where_clause}" if pg_where_clause else ""
        sql = f'SELECT {col_list} FROM {pg_schema}.{pg_table} {where_sql} ORDER BY ctid'
        
        return self.postgres_db.execute_query(sql)
    
    def _get_oracle_cursor(self, table_name: str, schema: str,
                          columns: List[str], where_clause: str = "",
                          partition_name: Optional[str] = None):
        """Oracle cursor 반환 (한 행씩 읽기용)"""
        col_list = ", ".join([f'"{col.upper()}"' for col in columns])
        full_table = f'"{schema}"."{table_name}"'
        if partition_name:
            full_table = f'"{schema}"."{table_name}" PARTITION("{partition_name}")'
        
        where_sql = f"WHERE {where_clause}" if where_clause else ""
        sql = f'SELECT {col_list} FROM {full_table} {where_sql} ORDER BY ROWID'
        
        # Oracle connection에서 cursor 가져오기
        if not self.oracle_db.connection:
            self.oracle_db.connect()
        
        cursor = self.oracle_db.connection.cursor()
        cursor.execute(sql)
        return cursor
    
    def _get_postgres_cursor(self, table_name: str, schema: str,
                            columns: List[str], where_clause: str = "",
                            partition_name: Optional[str] = None):
        """PostgreSQL cursor 반환 (한 행씩 읽기용, 파티션 범위 SQL 지원)"""
        col_list = ", ".join([f'"{col}"' for col in columns])
        pg_schema = schema.lower()
        
        # 파티션 범위 SQL 지원
        if partition_name:
            # PostgreSQL: Child 테이블 직접 접근
            pg_table = partition_name.lower()
        else:
            pg_table = table_name.lower()
        
        pg_where_clause = self._convert_where_clause_to_postgres(where_clause)
        where_sql = f"WHERE {pg_where_clause}" if pg_where_clause else ""
        
        # PK가 없을 경우 (partition_key, ctid) 사용
        sql = f'SELECT {col_list} FROM {pg_schema}.{pg_table} {where_sql} ORDER BY ctid'
        
        # PostgreSQL connection에서 cursor 가져오기
        if not self.postgres_db.connection:
            self.postgres_db.connect()
        
        cursor = self.postgres_db.connection.cursor()
        cursor.execute(sql)
        return cursor
    
    def _compare_row(self, oracle_row: tuple, postgres_row: tuple,
                    columns: List[str], tolerance: float) -> tuple:
        """
        단일 행 비교
        
        Returns:
            (match: bool, mismatch_details: dict)
        """
        mismatch_details = {}
        
        for col_idx, col_name in enumerate(columns):
            oracle_val = oracle_row[col_idx]
            postgres_val = postgres_row[col_idx]
            
            # None 처리
            if oracle_val is None and postgres_val is None:
                continue
            if oracle_val is None or postgres_val is None:
                mismatch_details[col_name] = {
                    'oracle': oracle_val,
                    'postgres': postgres_val,
                    'reason': 'null_mismatch'
                }
                continue
            
            # Decimal/Numeric 타입 비교 (float 금지, Decimal만 사용)
            # Design Philosophy: Float comparisons 금지, Decimal 기반 비교만 사용
            if isinstance(oracle_val, (Decimal, int)) or isinstance(postgres_val, (Decimal, int)):
                try:
                    # float 타입도 Decimal로 변환하여 정확한 비교
                    oracle_decimal = Decimal(str(oracle_val)) if not isinstance(oracle_val, Decimal) else oracle_val
                    postgres_decimal = Decimal(str(postgres_val)) if not isinstance(postgres_val, Decimal) else postgres_val
                    diff = abs(oracle_decimal - postgres_decimal)
                    
                    if diff > Decimal(str(tolerance)):
                        mismatch_details[col_name] = {
                            'oracle': str(oracle_decimal),  # Decimal을 문자열로 저장
                            'postgres': str(postgres_decimal),
                            'difference': str(diff),
                            'tolerance': tolerance,
                            'reason': 'decimal_tolerance_exceeded'
                        }
                except Exception as e:
                    # Decimal 변환 실패 시 문자열 비교
                    if str(oracle_val) != str(postgres_val):
                        mismatch_details[col_name] = {
                            'oracle': oracle_val,
                            'postgres': postgres_val,
                            'reason': 'value_mismatch'
                        }
            elif isinstance(oracle_val, float) or isinstance(postgres_val, float):
                # float 타입도 Decimal로 변환하여 비교 (float 직접 비교 금지)
                try:
                    oracle_decimal = Decimal(str(oracle_val))
                    postgres_decimal = Decimal(str(postgres_val))
                    diff = abs(oracle_decimal - postgres_decimal)
                    
                    if diff > Decimal(str(tolerance)):
                        mismatch_details[col_name] = {
                            'oracle': str(oracle_decimal),
                            'postgres': str(postgres_decimal),
                            'difference': str(diff),
                            'tolerance': tolerance,
                            'reason': 'decimal_tolerance_exceeded'
                        }
                except Exception as e:
                    if str(oracle_val) != str(postgres_val):
                        mismatch_details[col_name] = {
                            'oracle': oracle_val,
                            'postgres': postgres_val,
                            'reason': 'value_mismatch'
                        }
            else:
                # 일반 값 비교
                if oracle_val != postgres_val:
                    mismatch_details[col_name] = {
                        'oracle': oracle_val,
                        'postgres': postgres_val,
                        'reason': 'value_mismatch'
                    }
        
        return (len(mismatch_details) == 0, mismatch_details)
