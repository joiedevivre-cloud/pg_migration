"""
Aggregate stats collection module.
Collects MAX, MIN, fraction presence, etc. for type recommendation during validation.
"""
import logging
import re
from datetime import date, datetime
from typing import Dict, Any, List, Optional
from decimal import Decimal
from db.oracle import OracleDB
from db.postgres import PostgresDB

logger = logging.getLogger(__name__)


class AggregateStatsCollector:
    """Aggregate statistics collector."""
    
    def __init__(self, oracle_db: OracleDB, postgres_db: PostgresDB):
        self.oracle_db = oracle_db
        self.postgres_db = postgres_db
    
    def _get_numeric_columns(self, table_name: str, schema: str, columns: List[str]) -> List[str]:
        """Return list of numeric column names."""
        try:
            col_list = "', '".join([col.upper() for col in columns])
            sql = f"""
                SELECT COLUMN_NAME
                FROM DBA_TAB_COLUMNS
                WHERE OWNER = :schema
                  AND TABLE_NAME = :table_name
                  AND COLUMN_NAME IN ('{col_list}')
                  AND DATA_TYPE IN ('NUMBER', 'FLOAT', 'BINARY_FLOAT', 'BINARY_DOUBLE')
            """
            result = self.oracle_db.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            # Oracle returns column names in uppercase; normalize to lowercase
            return [row[0].lower() for row in result] if result else []
        except Exception as e:
            logger.warning(f"Failed to get numeric columns: {e}")
            return []
    
    def get_pk_columns(self, table_name: str, schema: str) -> List[str]:
        """
        Return PRIMARY KEY column names for the table (for validation layer auto-collection).
        Uses Oracle DBA_CONSTRAINTS / DBA_CONS_COLUMNS.
        """
        try:
            sql = """
                SELECT ccc.COLUMN_NAME
                FROM DBA_CONSTRAINTS cc
                JOIN DBA_CONS_COLUMNS ccc
                    ON cc.OWNER = ccc.OWNER AND cc.CONSTRAINT_NAME = ccc.CONSTRAINT_NAME
                WHERE cc.CONSTRAINT_TYPE = 'P'
                  AND cc.OWNER = :schema
                  AND cc.TABLE_NAME = :table_name
                ORDER BY ccc.POSITION
            """
            result = self.oracle_db.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            return [row[0].lower() for row in result] if result else []
        except Exception as e:
            logger.warning(f"Failed to get PK columns: {e}")
            return []
    
    def get_indexed_columns(self, table_name: str, schema: str, columns: Optional[List[str]] = None) -> List[str]:
        """
        Return columns included in indexes (DBA_IND_COLUMNS). Used for index size effect in reports.
        """
        try:
            col_filter = ""
            if columns:
                col_list = "', '".join([c.upper() for c in columns])
                col_filter = f" AND ccc.COLUMN_NAME IN ('{col_list}')"
            sql = f"""
                SELECT DISTINCT ccc.COLUMN_NAME
                FROM DBA_INDEXES i
                JOIN DBA_IND_COLUMNS ccc
                    ON i.OWNER = ccc.INDEX_OWNER AND i.INDEX_NAME = ccc.INDEX_NAME
                WHERE i.TABLE_OWNER = :schema
                  AND i.TABLE_NAME = :table_name
                  {col_filter}
            """
            result = self.oracle_db.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            return [row[0].lower() for row in result] if result else []
        except Exception as e:
            logger.warning(f"Failed to get indexed columns: {e}")
            return []
    
    def get_pg_table_size_bytes(self, table_name: str, schema: str) -> Optional[int]:
        """PostgreSQL table total size (pg_total_relation_size). Used for report effect estimation."""
        try:
            pg_schema = schema.lower()
            pg_table = table_name.lower()
            r = self.postgres_db.execute_query(
                f"SELECT pg_total_relation_size('{pg_schema}.{pg_table}'::regclass)"
            )
            return r[0][0] if r else None
        except Exception as e:
            logger.warning(f"Failed to get PG table size: {e}")
            return None
    
    def get_fk_columns(self, table_name: str, schema: str) -> List[str]:
        """
        Return FOREIGN KEY column names for the table (child side only). Uses Oracle DBA_CONSTRAINTS (type 'R').
        """
        try:
            sql = """
                SELECT ccc.COLUMN_NAME
                FROM DBA_CONSTRAINTS cc
                JOIN DBA_CONS_COLUMNS ccc
                    ON cc.OWNER = ccc.OWNER AND cc.CONSTRAINT_NAME = ccc.CONSTRAINT_NAME
                WHERE cc.CONSTRAINT_TYPE = 'R'
                  AND cc.OWNER = :schema
                  AND cc.TABLE_NAME = :table_name
                ORDER BY ccc.POSITION
            """
            result = self.oracle_db.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            # Deduplicate while preserving order (same column can participate in multiple FKs)
            seen = set()
            out = []
            for row in result or []:
                col = row[0].lower()
                if col not in seen:
                    seen.add(col)
                    out.append(col)
            return out
        except Exception as e:
            logger.warning(f"Failed to get FK columns: {e}")
            return []
    
    _ORACLE_DATE_TYPES = ('DATE', 'TIMESTAMP', 'TIMESTAMP WITH TIME ZONE', 'TIMESTAMP WITH LOCAL TIME ZONE')

    def collect_column_stats(self, table_name: str, schema: str,
                            columns: List[str], where_clause: str = "",
                            oracle_column_types: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
        """
        Collect per-column statistics. If oracle_column_types is provided, collect has_non_zero_time for DATE/TIMESTAMP columns.
        """
        stats = {}
        numeric_columns = self._get_numeric_columns(table_name, schema, columns)
        types = oracle_column_types or {}

        for column in columns:
            try:
                is_numeric = column in numeric_columns
                ora_type = (types.get(column) or types.get(column.upper()) or '').upper()
                is_date_type = ora_type in self._ORACLE_DATE_TYPES

                oracle_stats = self._collect_oracle_stats(
                    table_name, schema, column, where_clause, is_numeric, is_date_type
                )
                postgres_stats = self._collect_postgres_stats(
                    table_name, schema, column, where_clause, is_numeric, is_date_type
                )

                stats[column] = {
                    'oracle': oracle_stats,
                    'postgres': postgres_stats,
                    'column_name': column,
                    'table_name': table_name,
                    'schema': schema
                }
            except Exception as e:
                logger.error(f"Failed to collect stats for column {column}: {e}")
                stats[column] = {'error': str(e), 'column_name': column}

        return stats
    
    def _collect_oracle_stats(self, table_name: str, schema: str,
                             column: str, where_clause: str = "", is_numeric: bool = False,
                             is_date_type: bool = False) -> Dict[str, Any]:
        """Collect column stats from Oracle. For date types adds has_non_zero_time; for numeric adds HAS_FRACTION (TRUNC) and null_count."""
        where_sql = f"WHERE {where_clause}" if where_clause else ""
        # HAS_FRACTION: any row with fractional part? col != TRUNC(col)
        if is_numeric:
            has_decimal_expr = f'SUM(CASE WHEN "{column.upper()}" IS NOT NULL AND "{column.upper()}" != TRUNC("{column.upper()}") THEN 1 ELSE 0 END)'
            null_count_expr = f'SUM(CASE WHEN "{column.upper()}" IS NULL THEN 1 ELSE 0 END)'
        else:
            has_decimal_expr = '0'
            null_count_expr = '0'
        sum_expr = f'SUM("{column.upper()}")' if is_numeric else 'NULL'
        time_check = (
            f', MAX(CASE WHEN TO_CHAR("{column.upper()}", \'HH24:MI:SS\') != \'00:00:00\' THEN 1 ELSE 0 END) as has_non_zero_time'
            if is_date_type else ''
        )
        extra_numeric = f', {null_count_expr} as null_count' if is_numeric else ''
        sql = f"""
            SELECT
                MIN("{column.upper()}") as min_val,
                MAX("{column.upper()}") as max_val,
                COUNT(*) as row_count,
                COUNT(DISTINCT "{column.upper()}") as distinct_count,
                {has_decimal_expr} as has_decimal_count,
                {sum_expr} as sum_val
                {extra_numeric}
                {time_check}
            FROM "{schema}"."{table_name}"
            {where_sql}
        """
        result = self.oracle_db.execute_query(sql)
        if result and len(result) > 0:
            row = result[0]
            out = {
                'min_value': row[0],
                'max_value': row[1],
                'row_count': row[2],
                'distinct_count': row[3],
                'has_decimal_count': row[4],
                'has_decimal': row[4] > 0 if row[4] else False,
                'sum_value': row[5],
                'has_fraction': row[4] > 0 if row[4] else False,
                'fraction_row_count': row[4] if row[4] is not None else 0,
            }
            if is_numeric and len(row) > 6:
                out['null_count'] = row[6] or 0
            if is_date_type:
                idx = 7 if is_numeric else 6
                if len(row) > idx:
                    out['has_non_zero_time'] = 1 if (row[idx] and row[idx] > 0) else 0
            return out
        return {}
    
    def _collect_postgres_stats(self, table_name: str, schema: str,
                               column: str, where_clause: str = "", is_numeric: bool = False,
                               is_date_type: bool = False) -> Dict[str, Any]:
        """Collect column stats from PostgreSQL. For date types adds has_non_zero_time; for numeric adds HAS_FRACTION, null_count."""
        where_sql = f"WHERE {where_clause}" if where_clause else ""
        pg_schema = schema.lower()
        pg_table = table_name.lower()
        if is_numeric:
            has_decimal_expr = f'SUM(CASE WHEN "{column}" IS NOT NULL AND "{column}" != TRUNC("{column}") THEN 1 ELSE 0 END)'
            null_count_expr = f'SUM(CASE WHEN "{column}" IS NULL THEN 1 ELSE 0 END)'
        else:
            has_decimal_expr = '0'
            null_count_expr = '0'
        sum_expr = f'SUM("{column}")' if is_numeric else 'NULL'
        time_check = (
            f', MAX(CASE WHEN ("{column}"::timestamp::time) != \'00:00:00\'::time THEN 1 ELSE 0 END) as has_non_zero_time'
            if is_date_type else ''
        )
        extra_numeric_pg = f', {null_count_expr} as null_count' if is_numeric else ''
        sql = f"""
            SELECT
                MIN("{column}") as min_val,
                MAX("{column}") as max_val,
                COUNT(*) as row_count,
                COUNT(DISTINCT "{column}") as distinct_count,
                {has_decimal_expr} as has_decimal_count,
                {sum_expr} as sum_val
                {extra_numeric_pg}
                {time_check}
            FROM {pg_schema}.{pg_table}
            {where_sql}
        """
        result = self.postgres_db.execute_query(sql)
        if result and len(result) > 0:
            row = result[0]
            out = {
                'min_value': row[0],
                'max_value': row[1],
                'row_count': row[2],
                'distinct_count': row[3],
                'has_decimal_count': row[4],
                'has_decimal': row[4] > 0 if row[4] else False,
                'sum_value': row[5],
                'has_fraction': row[4] > 0 if row[4] else False,
                'fraction_row_count': row[4] if row[4] is not None else 0,
            }
            if is_numeric and len(row) > 6:
                out['null_count'] = row[6] or 0
            if is_date_type:
                idx = 7 if is_numeric else 6
                if len(row) > idx:
                    out['has_non_zero_time'] = 1 if (row[idx] and row[idx] > 0) else 0
            return out
        return {}
    
    def count_out_of_range_rows(self, table_name: str, schema: str, column: str,
                                 target_type: str, where_clause: str = "") -> int:
        """
        OUT_OF_RANGE safety check: count rows outside target_type range (Oracle + Postgres).
        SMALLINT: < -32768 or > 32767; INTEGER: < -2147483648 or > 2147483647; BIGINT: not checked (returns 0).
        """
        if target_type == 'BIGINT':
            return 0
        where_sql = f" AND ({where_clause})" if where_clause else ""
        if target_type == 'SMALLINT':
            low, high = -32768, 32767
        elif target_type == 'INTEGER':
            low, high = -2147483648, 2147483647
        else:
            return 0
        total = 0
        pred = f'("{column.upper()}" < :low OR "{column.upper()}" > :high)'
        where_ora = f'WHERE {pred}' if not where_clause else f'WHERE ({where_clause}) AND {pred}'
        try:
            sql_ora = f'SELECT COUNT(*) FROM "{schema}"."{table_name}" {where_ora}'
            r = self.oracle_db.execute_query(sql_ora, {'low': low, 'high': high})
            if r:
                total += r[0][0] or 0
        except Exception as e:
            logger.warning(f"Oracle out-of-range check failed for {column}: {e}")
        pg_schema = schema.lower()
        pg_table = table_name.lower()
        pred_pg = f'("{column}" < %s OR "{column}" > %s)'
        where_pg = f'WHERE {pred_pg}' if not where_clause else f'WHERE ({where_clause}) AND {pred_pg}'
        try:
            sql_pg = f'SELECT COUNT(*) FROM {pg_schema}.{pg_table} {where_pg}'
            r = self.postgres_db.execute_query(sql_pg, (low, high))
            if r:
                total += r[0][0] or 0
        except Exception as e:
            logger.warning(f"Postgres out-of-range check failed for {column}: {e}")
        return total
    
    def dry_run_cast_loss_count(self, table_name: str, schema: str, column: str,
                                 target_type: str, where_clause: str = "") -> Optional[int]:
        """
        Dry-run CAST simulation: count PG rows where col::target_type would change the value (potential loss).
        Detects before ALTER. target_type is smallint/integer/bigint.
        """
        pg_schema = schema.lower()
        pg_table = table_name.lower()
        # Loss when col::target_type::numeric != col
        cast_type = target_type.lower()
        and_user = f"({where_clause}) AND " if where_clause else ""
        sql = f'''
            SELECT COUNT(*) FROM {pg_schema}.{pg_table}
            WHERE {and_user}"{column}" IS NOT NULL
              AND "{column}"::numeric != "{column}"::{cast_type}::numeric
        '''
        try:
            r = self.postgres_db.execute_query(sql)
            return r[0][0] if r else None
        except Exception as e:
            logger.warning(f"Dry-run cast check failed for {column}: {e}")
            return None
    
    def recommend_numeric_type(self, stats: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
        """
        Recommend types from stats (all columns).
        - 8-digit integer (YYYYMMDD) or hyphen date string -> date-like -> DATE/TIMESTAMP
        - has_non_zero_time -> TIMESTAMP; all 00:00:00 -> DATE
        - precision<=4 range -> SMALLINT -> INTEGER -> BIGINT
        """
        recommendations = {}

        def looks_like_date(val) -> bool:
            """True if hyphen date (e.g. 2026-01-11) or datetime/date instance."""
            if val is None:
                return False
            if isinstance(val, (datetime, date)):
                return True
            if isinstance(val, str) and re.match(r'\d{4}-\d{2}-\d{2}', val):
                return True
            return False

        def is_8digit_yyyymmdd(val) -> bool:
            """True if value is in 8-digit YYYYMMDD range."""
            if val is None:
                return False
            try:
                v = int(float(val))
                return 19000101 <= v <= 99991231
            except (ValueError, TypeError):
                return False

        for column, column_stats in stats.items():
            if 'error' in column_stats:
                continue

            oracle_stats = column_stats.get('oracle', {})
            postgres_stats = column_stats.get('postgres', {})

            # 8-digit YYYYMMDD -> date-like, no time -> DATE
            min_v, max_v = oracle_stats.get('min_value'), oracle_stats.get('max_value')
            if is_8digit_yyyymmdd(min_v) and is_8digit_yyyymmdd(max_v):
                recommendations[column] = 'DATE'
                continue
            min_v, max_v = postgres_stats.get('min_value'), postgres_stats.get('max_value')
            if is_8digit_yyyymmdd(min_v) and is_8digit_yyyymmdd(max_v):
                recommendations[column] = 'DATE'
                continue

            # Hyphen/date string or datetime -> DATE vs TIMESTAMP by has_non_zero_time
            if looks_like_date(oracle_stats.get('min_value')) or looks_like_date(oracle_stats.get('max_value')):
                has_time = (
                    oracle_stats.get('has_non_zero_time', 0) or
                    postgres_stats.get('has_non_zero_time', 0)
                )
                # Any row with time -> TIMESTAMP; all 00:00:00 -> DATE (when profile has result)
                if has_time:
                    recommendations[column] = 'TIMESTAMP'
                elif 'has_non_zero_time' in oracle_stats or 'has_non_zero_time' in postgres_stats:
                    recommendations[column] = 'DATE'
                else:
                    recommendations[column] = 'TIMESTAMP'  # Conservative when no profile
                continue
            if looks_like_date(postgres_stats.get('min_value')) or looks_like_date(postgres_stats.get('max_value')):
                has_time = (
                    oracle_stats.get('has_non_zero_time', 0) or
                    postgres_stats.get('has_non_zero_time', 0)
                )
                if has_time:
                    recommendations[column] = 'TIMESTAMP'
                elif 'has_non_zero_time' in oracle_stats or 'has_non_zero_time' in postgres_stats:
                    recommendations[column] = 'DATE'
                else:
                    recommendations[column] = 'TIMESTAMP'
                continue

            # If either Oracle or PostgreSQL has fraction, keep NUMERIC
            has_decimal = (
                oracle_stats.get('has_decimal', False) or
                postgres_stats.get('has_decimal', False)
            )

            if has_decimal:
                recommendations[column] = 'NUMERIC'
                continue

            # When no fraction, recommend by range (PAGE 2: p<=4 -> SMALLINT)
            def safe_numeric_value(val):
                """Safely convert to numeric value."""
                if val is None:
                    return None
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    return val
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return None

            oracle_max = safe_numeric_value(oracle_stats.get('max_value'))
            oracle_min = safe_numeric_value(oracle_stats.get('min_value'))
            postgres_max = safe_numeric_value(postgres_stats.get('max_value'))
            postgres_min = safe_numeric_value(postgres_stats.get('min_value'))

            max_val = max(v for v in [oracle_max, postgres_max] if v is not None) if any([oracle_max, postgres_max]) else None
            min_val = min(v for v in [oracle_min, postgres_min] if v is not None) if any([oracle_min, postgres_min]) else None

            # [Global rule] precision<=4 range: SMALLINT -> INTEGER -> BIGINT (doc PAGE 2)
            if max_val is not None and min_val is not None:
                if min_val >= -32768 and max_val <= 32767:
                    recommendations[column] = 'SMALLINT'
                elif min_val >= -2147483648 and max_val <= 2147483647:
                    recommendations[column] = 'INTEGER'
                elif min_val >= -9223372036854775808 and max_val <= 9223372036854775807:
                    recommendations[column] = 'BIGINT'
                else:
                    recommendations[column] = 'NUMERIC'
            else:
                recommendations[column] = 'NUMERIC'

        return recommendations
