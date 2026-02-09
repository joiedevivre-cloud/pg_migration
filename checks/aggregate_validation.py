"""
집계 중심 검증 모듈
COUNT, MIN/MAX, COUNT DISTINCT, SUM/AVG (Decimal 통일 + scale 기준 quantize 비교)
"""
import logging
from decimal import Decimal, getcontext
from typing import Dict, Any, List, Optional
from db.oracle import OracleDB
from db.postgres import PostgresDB

getcontext().prec = 28

logger = logging.getLogger(__name__)


def _to_decimal(v: Any) -> Optional[Decimal]:
    """모든 수치를 Decimal로 통일 (비교용)."""
    if v is None:
        return None
    return Decimal(str(v))


def _canon_decimal(v: Decimal, scale: int) -> Decimal:
    """scale 기준으로 quantize (동일 자리수로 정규화)."""
    if v is None:
        return v
    q = Decimal(10) ** (-scale)  # scale=0 -> 1, scale=2 -> 0.01
    return v.quantize(q)


class AggregateValidator:
    """집계 검증 클래스"""
    
    def __init__(self, oracle_db: OracleDB, postgres_db: PostgresDB,
                 decimal_tolerance: float = 0.0001):
        """
        Args:
            oracle_db: Oracle DB 연결
            postgres_db: PostgreSQL DB 연결
            decimal_tolerance: SUM 비교 시 허용 오차
        """
        self.oracle_db = oracle_db
        self.postgres_db = postgres_db
        self.decimal_tolerance = decimal_tolerance
    
    def validate_aggregates(self, table_name: str, schema: str,
                           columns: List[str], where_clause: str = "",
                           partition_name: Optional[str] = None) -> Dict[str, Any]:
        """
        집계 함수로 검증 (COUNT, MIN, MAX, COUNT DISTINCT, SUM)
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            columns: 컬럼 리스트
            where_clause: WHERE 절 조건
            partition_name: 파티션명 (옵션)
        
        Returns:
            검증 결과 딕셔너리
        """
        results = {}
        
        # 숫자형 컬럼만 집계 검증 (SUM, AVG는 숫자형에만 적용)
        numeric_columns = self._get_numeric_columns(table_name, schema, columns)
        # numeric 컬럼별 scale 조회 (ROUND(SUM/AVG, scale)로 Oracle/PG 누적·반올림 차이 제거)
        numeric_scale = self._get_numeric_scale(table_name, schema, columns, numeric_columns)
        
        for column in columns:
            try:
                is_numeric = column in numeric_columns or (column and column.lower() in numeric_columns)
                scale = (numeric_scale.get(column) or numeric_scale.get(column.lower())) if is_numeric else None
                
                # Oracle 집계
                oracle_agg = self._get_oracle_aggregates(
                    table_name, schema, column, where_clause, partition_name, is_numeric, scale
                )
                
                # PostgreSQL 집계
                postgres_agg = self._get_postgres_aggregates(
                    table_name, schema, column, where_clause, partition_name, is_numeric, scale
                )
                
                # 비교 (metric 단위 비교, dict 비교 금지)
                comparison = self._compare_aggregates(
                    oracle_agg, postgres_agg, is_numeric, scale
                )
                
                results[column] = {
                    'oracle': oracle_agg,
                    'postgres': postgres_agg,
                    'comparison': comparison,
                    'match': comparison['match']
                }
            except Exception as e:
                logger.error(f"Aggregate validation failed for column {column}: {e}")
                results[column] = {
                    'error': str(e),
                    'match': False
                }
        
        # 전체 매치 여부
        all_match = all(
            result.get('match', False) 
            for result in results.values()
        )
        
        return {
            'match': all_match,
            'columns': results,
            'table_name': table_name,
            'schema': schema
        }
    
    def _get_numeric_columns(self, table_name: str, schema: str, columns: List[str]) -> List[str]:
        """숫자형 컬럼 목록 조회"""
        try:
            col_list = "', '".join([col.upper() for col in columns])
            sql = f"""
                SELECT COLUMN_NAME
                FROM DBA_TAB_COLUMNS
                WHERE OWNER = :schema
                  AND TABLE_NAME = :table_name
                  AND COLUMN_NAME IN ('{col_list}')
                  AND DATA_TYPE IN ('NUMBER', 'FLOAT', 'BINARY_FLOAT', 'BINARY_DOUBLE', 'INTEGER')
            """
            result = self.oracle_db.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            return [row[0].lower() for row in result] if result else []
        except Exception as e:
            logger.warning(f"Failed to get numeric columns: {e}")
            return []
    
    def _get_numeric_scale(self, table_name: str, schema: str, columns: List[str],
                           numeric_columns: List[str]) -> Dict[str, int]:
        """numeric 컬럼별 scale 조회 (집계 시 ROUND 적용용). Oracle 메타데이터 사용."""
        scale_by_col = {}
        if not numeric_columns:
            return scale_by_col
        try:
            meta = self.oracle_db.get_column_metadata(table_name, schema, columns)
            for col in numeric_columns:
                m = meta.get(col) or meta.get(col.upper(), {})
                s = m.get('data_scale')
                if s is not None:
                    try:
                        scale_by_col[col] = int(s)
                    except (TypeError, ValueError):
                        scale_by_col[col] = 0
                else:
                    scale_by_col[col] = 0  # INTEGER 등 scale 없음
        except Exception as e:
            logger.warning(f"Failed to get numeric scale: {e}")
        return scale_by_col
    
    def _get_oracle_aggregates(self, table_name: str, schema: str,
                               column: str, where_clause: str = "",
                               partition_name: Optional[str] = None,
                               is_numeric: bool = False,
                               scale: Optional[int] = None) -> Dict[str, Any]:
        """Oracle 집계 함수 실행. numeric인 경우 scale 있으면 ROUND(SUM/AVG, scale) 적용."""
        full_table = f'"{schema}"."{table_name}"'
        if partition_name:
            full_table = f'"{schema}"."{table_name}" PARTITION("{partition_name}")'
        
        where_sql = f"WHERE {where_clause}" if where_clause else ""
        
        # 숫자형: scale 있으면 ROUND(SUM/AVG, scale)로 누적·반올림 차이 제거
        if is_numeric and scale is not None:
            sum_expr = f'ROUND(SUM("{column.upper()}"), {scale})'
            avg_expr = f'ROUND(AVG("{column.upper()}"), {scale})'
        elif is_numeric:
            sum_expr = f'SUM("{column.upper()}")'
            avg_expr = f'AVG("{column.upper()}")'
        else:
            sum_expr = 'NULL'
            avg_expr = 'NULL'
        
        sql = f"""
            SELECT 
                COUNT(*) as cnt,
                MIN("{column.upper()}") as min_val,
                MAX("{column.upper()}") as max_val,
                COUNT(DISTINCT "{column.upper()}") as distinct_cnt,
                {sum_expr} as sum_val,
                {avg_expr} as avg_val
            FROM {full_table}
            {where_sql}
        """
        
        result = self.oracle_db.execute_query(sql)
        if result and len(result) > 0:
            row = result[0]
            return {
                'count': row[0],
                'min': row[1],
                'max': row[2],
                'distinct_count': row[3],
                'sum': row[4],
                'avg': row[5]
            }
        return {}
    
    def _get_postgres_aggregates(self, table_name: str, schema: str,
                                column: str, where_clause: str = "",
                                partition_name: Optional[str] = None,
                                is_numeric: bool = False,
                                scale: Optional[int] = None) -> Dict[str, Any]:
        """PostgreSQL 집계 함수 실행. numeric인 경우 scale 있으면 ROUND(SUM/AVG::numeric, scale) 적용."""
        where_sql = f"WHERE {where_clause}" if where_clause else ""
        
        # PostgreSQL 스키마는 일반적으로 소문자로 처리 (data_migrator.py와 동일한 방식)
        pg_schema = schema.lower()
        
        # 파티션 범위 SQL 지원
        if partition_name:
            pg_table = partition_name.lower()
        else:
            pg_table = table_name.lower()
        
        # 숫자형: scale 있으면 ROUND(::numeric, scale)로 Oracle과 동일 비교
        if is_numeric and scale is not None:
            sum_expr = f'ROUND(SUM("{column}")::numeric, {scale})'
            avg_expr = f'ROUND(AVG("{column}")::numeric, {scale})'
        elif is_numeric:
            sum_expr = f'SUM("{column}")'
            avg_expr = f'AVG("{column}")'
        else:
            sum_expr = 'NULL'
            avg_expr = 'NULL'
        
        sql = f"""
            SELECT 
                COUNT(*) as cnt,
                MIN("{column}") as min_val,
                MAX("{column}") as max_val,
                COUNT(DISTINCT "{column}") as distinct_cnt,
                {sum_expr} as sum_val,
                {avg_expr} as avg_val
            FROM {pg_schema}.{pg_table}
            {where_sql}
        """
        
        result = self.postgres_db.execute_query(sql)
        if result and len(result) > 0:
            row = result[0]
            return {
                'count': row[0],
                'min': row[1],
                'max': row[2],
                'distinct_count': row[3],
                'sum': row[4],
                'avg': row[5]
            }
        return {}
    
    def _compare_aggregates(self, oracle_agg: Dict[str, Any],
                           postgres_agg: Dict[str, Any],
                           is_numeric: bool = False,
                           scale: Optional[int] = None) -> Dict[str, Any]:
        """집계 결과 비교: 모든 수치를 Decimal로 통일 후 scale 기준 quantize, metric 단위 비교 (dict 비교 금지)."""
        # COUNT, MIN, MAX, DISTINCT_COUNT (비수치 또는 수치 모두 동일 로직)
        count_match = oracle_agg.get('count') == postgres_agg.get('count')
        distinct_count_match = oracle_agg.get('distinct_count') == postgres_agg.get('distinct_count')

        # MIN/MAX: 수치형이면 Decimal 통일 후 비교
        def _min_max_match(o_val, p_val, numeric: bool) -> bool:
            if o_val is None and p_val is None:
                return True
            if o_val is None or p_val is None:
                return False
            if not numeric:
                return o_val == p_val
            do = _to_decimal(o_val)
            dp = _to_decimal(p_val)
            s = scale if scale is not None else 0
            return _canon_decimal(do, s) == _canon_decimal(dp, s)

        min_match = _min_max_match(
            oracle_agg.get('min'), postgres_agg.get('min'), is_numeric
        )
        max_match = _min_max_match(
            oracle_agg.get('max'), postgres_agg.get('max'), is_numeric
        )

        # SUM/AVG: 숫자형만 Decimal 통일 + scale 기준 quantize 후 동등 비교
        sum_match = True
        avg_match = True
        comparison = {
            'count_match': count_match,
            'min_match': min_match,
            'max_match': max_match,
            'distinct_count_match': distinct_count_match,
            'sum_match': sum_match,
            'avg_match': avg_match,
        }

        if is_numeric:
            sc = scale if scale is not None else 0
            oracle_sum = _to_decimal(oracle_agg.get('sum'))
            pg_sum = _to_decimal(postgres_agg.get('sum'))
            oracle_avg = _to_decimal(oracle_agg.get('avg'))
            pg_avg = _to_decimal(postgres_agg.get('avg'))

            if oracle_sum is None and pg_sum is None:
                sum_match = True
                comparison['oracle_sum'] = None
                comparison['postgres_sum'] = None
            elif oracle_sum is None or pg_sum is None:
                sum_match = False
                comparison['oracle_sum'] = str(oracle_sum) if oracle_sum is not None else None
                comparison['postgres_sum'] = str(pg_sum) if pg_sum is not None else None
            else:
                o_sum = _canon_decimal(oracle_sum, sc)
                p_sum = _canon_decimal(pg_sum, sc)
                sum_match = o_sum == p_sum
                comparison['oracle_sum'] = str(o_sum)
                comparison['postgres_sum'] = str(p_sum)

            if oracle_avg is None and pg_avg is None:
                avg_match = True
                comparison['oracle_avg'] = None
                comparison['postgres_avg'] = None
            elif oracle_avg is None or pg_avg is None:
                avg_match = False
                comparison['oracle_avg'] = str(oracle_avg) if oracle_avg is not None else None
                comparison['postgres_avg'] = str(pg_avg) if pg_avg is not None else None
            else:
                o_avg = _canon_decimal(oracle_avg, sc)
                p_avg = _canon_decimal(pg_avg, sc)
                avg_match = o_avg == p_avg
                comparison['oracle_avg'] = str(o_avg)
                comparison['postgres_avg'] = str(p_avg)
        else:
            comparison['oracle_sum'] = None
            comparison['postgres_sum'] = None
            comparison['oracle_avg'] = None
            comparison['postgres_avg'] = None

        comparison['sum_match'] = sum_match
        comparison['avg_match'] = avg_match
        comparison['match'] = (
            count_match and min_match and max_match
            and distinct_count_match and sum_match and avg_match
        )
        return comparison
