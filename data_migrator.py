"""
데이터 이관 도구
Oracle에서 데이터를 읽어와 PostgreSQL로 이관
config.yaml 파일을 사용하여 설정 관리
"""
# -*- coding: utf-8 -*-
import sys
import os
import re
import logging
from pathlib import Path
from db.oracle import OracleDB
from db.postgres import PostgresDB
from checks.chunk_hash import ChunkHashValidator
from checks.aggregate_validation import AggregateValidator
from checks.row_canonicalize import RowCanonicalizer, NullEmptyPolicy

# YAML parsing (simple version)
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False
    def parse_simple_yaml(content):
        """간단한 YAML 파서 (딕셔너리, 리스트, 문자열만 지원)"""
        result = {}
        current_section = None
        current_subsection = None
        
        lines = content.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            
            # Skip comments and blank lines
            if not stripped or stripped.startswith('#'):
                i += 1
                continue
            
            # List item
            if stripped.startswith('- '):
                value = stripped[2:].strip().strip('"\'')
                if current_section == 'migration' and current_subsection == 'tables':
                    if 'migration' not in result:
                        result['migration'] = {}
                    if 'tables' not in result['migration']:
                        result['migration']['tables'] = []
                    elif not isinstance(result['migration']['tables'], list):
                        result['migration']['tables'] = []
                    result['migration']['tables'].append(value)
                i += 1
                continue
            
            # 키: 값 처리
            if ':' in stripped:
                parts = stripped.split(':', 1)
                key = parts[0].strip()
                value = parts[1].strip().strip('"\'') if len(parts) > 1 else ''
                
                # Top-level section
                if indent == 0:
                    if not value or value == 'null':
                        current_section = key
                        if key not in result:
                            result[key] = {}
                        current_subsection = None
                    else:
                        result[key] = value
                        current_section = None
                        current_subsection = None
                # Sub-section
                elif indent > 0 and current_section:
                    if not value or value == 'null':
                        # 리스트 시작인지 확인 (다음 줄이 - 로 시작하면)
                        if i + 1 < len(lines):
                            next_line = lines[i + 1].lstrip()
                            if next_line.startswith('- '):
                                current_subsection = key
                                if key not in result[current_section]:
                                    result[current_section][key] = []
                            else:
                                current_subsection = key
                                if key not in result[current_section]:
                                    result[current_section][key] = {}
                    else:
                        # Boolean
                        if isinstance(value, str) and value.lower().strip() in ('true', 'false'):
                            value = value.lower().strip() == 'true'
                        # Null
                        elif isinstance(value, str) and value.lower().strip() == 'null':
                            value = None
                        # Try numeric
                        elif isinstance(value, str) and (value.isdigit() or (value.startswith('-') and value[1:].isdigit())):
                            try:
                                value = int(value)
                            except ValueError:
                                pass
                        
                        if current_subsection:
                            result[current_section][current_subsection] = value
                            current_subsection = None
                        else:
                            result[current_section][key] = value
            
            i += 1
        
        return result

# Windows 콘솔 인코딩 설정
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yaml") -> dict:
    """
    config.yaml 파일에서 설정 로드
    
    Args:
        config_path: 설정 파일 경로
    
    Returns:
        설정 딕셔너리
    """
    config_file = Path(config_path)
    
    if not config_file.exists():
        raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {config_path}")
    
    with open(config_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if HAS_YAML:
        config = yaml.safe_load(content)
    else:
        config = parse_simple_yaml(content)
        # Manual tables list parsing (parser fallback)
        if 'migration' in config and 'tables' in config['migration']:
            # tables가 문자열이면 리스트로 변환 시도
            if isinstance(config['migration']['tables'], str):
                # Parse from YAML list format
                tables = []
                in_tables_section = False
                for line in content.split('\n'):
                    stripped = line.strip()
                    if 'tables:' in stripped:
                        in_tables_section = True
                        continue
                    if in_tables_section:
                        if stripped.startswith('- '):
                            table = stripped[2:].strip().strip('"\'')
                            tables.append(table)
                        elif stripped and not stripped.startswith('#'):
                            # 다른 키가 나오면 종료
                            if ':' in stripped and not stripped.startswith('-'):
                                break
                config['migration']['tables'] = tables if tables else []
    
    return config


class DataMigrator:
    """데이터 이관 클래스"""
    
    def __init__(self, oracle_db: OracleDB, postgres_db: PostgresDB):
        """
        Args:
            oracle_db: Oracle DB 연결 객체
            postgres_db: PostgreSQL DB 연결 객체
        """
        self.oracle_db = oracle_db
        self.postgres_db = postgres_db
    
    def get_table_columns(self, table_name: str, schema: str) -> list:
        """
        테이블의 컬럼 목록 조회
        
        Args:
            table_name: 테이블명
            schema: 스키마명
        
        Returns:
            컬럼명 리스트
        """
        sql = """
            SELECT COLUMN_NAME
            FROM DBA_TAB_COLUMNS
            WHERE OWNER = :schema
              AND TABLE_NAME = :table_name
            ORDER BY COLUMN_ID
        """
        
        result = self.oracle_db.execute_query(sql, {
            'schema': schema.upper(),
            'table_name': table_name.upper()
        })
        
        return [row[0].lower() for row in result]
    
    def _get_pk_columns(self, table_name: str, schema: str) -> list:
        """
        Primary Key 컬럼 목록 조회
        
        Args:
            table_name: 테이블명
            schema: 스키마명
        
        Returns:
            PK 컬럼명 리스트
        """
        try:
            sql = """
                SELECT ccc.COLUMN_NAME
                FROM DBA_CONSTRAINTS cc
                JOIN DBA_CONS_COLUMNS ccc 
                    ON cc.OWNER = ccc.OWNER 
                    AND cc.CONSTRAINT_NAME = ccc.CONSTRAINT_NAME
                WHERE cc.CONSTRAINT_TYPE = 'P'
                  AND cc.OWNER = :schema
                  AND cc.TABLE_NAME = :table_name
                ORDER BY ccc.POSITION
            """
            result = self.oracle_db.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            return [row[0].lower() for row in result]
        except Exception as e:
            logger.warning(f"Failed to get PK columns: {e}")
            return []
    
    def _get_numeric_columns(self, table_name: str, schema: str, all_columns: list) -> list:
        """
        숫자형 컬럼 목록 조회
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            all_columns: 전체 컬럼 리스트
        
        Returns:
            숫자형 컬럼명 리스트
        """
        try:
            col_list = "', '".join([col.upper() for col in all_columns])
            sql = f"""
                SELECT COLUMN_NAME
                FROM DBA_TAB_COLUMNS
                WHERE OWNER = :schema
                  AND TABLE_NAME = :table_name
                  AND COLUMN_NAME IN ('{col_list}')
                  AND DATA_TYPE IN ('NUMBER', 'FLOAT', 'BINARY_FLOAT', 'BINARY_DOUBLE', 'INTEGER')
                ORDER BY COLUMN_ID
            """
            result = self.oracle_db.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            return [row[0].lower() for row in result]
        except Exception as e:
            logger.warning(f"Failed to get numeric columns: {e}")
            return []
    
    def get_row_count(self, table_name: str, schema: str, where_clause: str = "") -> int:
        """
        테이블의 행 수 조회
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            where_clause: WHERE 절 (옵션)
        
        Returns:
            행 수
        """
        where_sql = f"WHERE {where_clause}" if where_clause else ""
        
        sql = f"""
            SELECT COUNT(*)
            FROM "{schema}"."{table_name}"
            {where_sql}
        """
        
        result = self.oracle_db.execute_query(sql)
        return result[0][0] if result else 0
    
    def migrate_table_data(self, table_name: str, schema: str, 
                          target_schema: str = None, 
                          truncate_before_insert: bool = False,
                          batch_size: int = 1000):
        """
        테이블 데이터 이관
        
        Args:
            table_name: 테이블명
            schema: Oracle 스키마명
            target_schema: PostgreSQL 스키마명 (None이면 schema와 동일)
            truncate_before_insert: 삽입 전 TRUNCATE 여부
            batch_size: 배치 크기 (한 번에 처리할 행 수)
        """
        pg_schema = (target_schema or schema).lower()
        pg_table = table_name.lower()
        
        print("=" * 80)
        print(f"Data migration: {schema}.{table_name} -> {pg_schema}.{pg_table}")
        print("=" * 80)
        print()
        
        try:
            # Get column list
            columns = self.get_table_columns(table_name, schema)
            if not columns:
                print("Columns not found.")
                return False
            
            print(f"Column count: {len(columns)}")
            print(f"Columns: {', '.join(columns)}")
            print()
            
            # Oracle row count
            oracle_count = self.get_row_count(table_name, schema)
            print(f"Oracle row count: {oracle_count:,}")
            
            if oracle_count == 0:
                print("No data to migrate.")
                return True
            
            # PostgreSQL TRUNCATE (옵션)
            if truncate_before_insert:
                try:
                    truncate_sql = f'TRUNCATE TABLE {pg_schema}.{pg_table} CASCADE;'
                    self.postgres_db.execute_ddl(truncate_sql)
                    print("Existing data truncated.")
                    print()
                except Exception as e:
                    logger.warning(f"TRUNCATE failed: {e}")
                    print(f"TRUNCATE failed: {e}")
                    print("   Duplicate key errors may occur if target already has data.")
                    print()
            
            # Migrate in batches
            print()
            print("Migration started...")
            print("-" * 80)
            
            # 컬럼명을 문자열로 구성 (Oracle은 따옴표 없이 사용)
            # DBA_TAB_COLUMNS에서 조회한 컬럼명은 대문자이므로 그대로 사용
            col_list_upper = ", ".join([col.upper() for col in columns])
            
            # PK-based keyset pagination (no ROWNUM, no OFFSET)
            pk_columns = self._get_pk_columns(table_name, schema)
            if not pk_columns:
                logger.error(f"No PK columns found for {schema}.{table_name} - cannot use keyset pagination")
                print("PK columns not found; keyset pagination unavailable.")
                return False
            
            is_composite_pk = len(pk_columns) > 1
            total_inserted = 0
            last_pk_values = None  # 마지막 PK 값 저장 (keyset pagination용)
            
            # Keyset pagination: fetch and insert batches (deterministic order, no OFFSET)
            while True:
                # Keyset Pagination 쿼리 생성 (결정적 ORDER BY 강제)
                if is_composite_pk:
                    # Composite PK: tuple comparison (k1,k2,...) > (:k1,:k2,...)
                    pk_col_list = ", ".join([f'"{col.upper()}"' for col in pk_columns])
                    order_by_list = ", ".join([f'"{col.upper()}"' for col in pk_columns])
                    
                    if last_pk_values is None:
                        # 첫 배치: ORDER BY로 시작
                        fetch_sql = f"""
                            SELECT {col_list_upper}
                            FROM "{schema}"."{table_name}"
                            ORDER BY {order_by_list}
                            FETCH FIRST {batch_size} ROWS ONLY
                        """
                        params = {}
                    else:
                        # Next batch: tuple comparison (k1>:k1) OR (k1=:k1 AND k2>:k2) OR ...
                        tuple_conditions = []
                        for i in range(len(pk_columns)):
                            if i == 0:
                                tuple_conditions.append(f'"{pk_columns[i].upper()}" > :pk_{i}')
                            else:
                                # 이전 컬럼들이 모두 같을 때만 다음 컬럼 비교
                                prev_equals = " AND ".join([
                                    f'"{pk_columns[j].upper()}" = :pk_{j}'
                                    for j in range(i)
                                ])
                                tuple_conditions.append(
                                    f'({prev_equals} AND "{pk_columns[i].upper()}" > :pk_{i})'
                                )
                        
                        tuple_condition = " OR ".join([f"({cond})" for cond in tuple_conditions])
                        fetch_sql = f"""
                            SELECT {col_list_upper}
                            FROM "{schema}"."{table_name}"
                            WHERE {tuple_condition}
                            ORDER BY {order_by_list}
                            FETCH FIRST {batch_size} ROWS ONLY
                        """
                        params = {f'pk_{i}': last_pk_values[i] for i in range(len(pk_columns))}
                else:
                    # Single PK: WHERE pk > :last_pk ORDER BY pk FETCH FIRST :n ROWS
                    pk_col = pk_columns[0]
                    if last_pk_values is None:
                        # 첫 배치: ORDER BY로 시작
                        fetch_sql = f"""
                            SELECT {col_list_upper}
                            FROM "{schema}"."{table_name}"
                            ORDER BY "{pk_col.upper()}"
                            FETCH FIRST {batch_size} ROWS ONLY
                        """
                        params = {}
                    else:
                        # 다음 배치: WHERE pk > :last_pk
                        fetch_sql = f"""
                            SELECT {col_list_upper}
                            FROM "{schema}"."{table_name}"
                            WHERE "{pk_col.upper()}" > :last_pk
                            ORDER BY "{pk_col.upper()}"
                            FETCH FIRST {batch_size} ROWS ONLY
                        """
                        params = {'last_pk': last_pk_values[0]}
                
                oracle_rows = self.oracle_db.execute_query(fetch_sql, params)
                
                if not oracle_rows:
                    break
                
                # Store last row PK for next batch
                last_row = oracle_rows[-1]
                pk_col_indices = [columns.index(col) for col in pk_columns]
                if is_composite_pk:
                    last_pk_values = tuple(last_row[idx] for idx in pk_col_indices)
                else:
                    last_pk_values = (last_row[pk_col_indices[0]],)
                
                # PostgreSQL에 배치 삽입
                pg_col_list = ", ".join([f'"{col}"' for col in columns])
                placeholders = ", ".join(['%s'] * len(columns))
                
                insert_sql = f"""
                    INSERT INTO {pg_schema}.{pg_table} ({pg_col_list})
                    VALUES ({placeholders})
                """
                
                # Batch insert (executemany)
                if not self.postgres_db.connection:
                    self.postgres_db.connect()
                
                cursor = self.postgres_db.connection.cursor()
                try:
                    # executemany를 사용한 Bulk Insert
                    cursor.executemany(insert_sql, oracle_rows)
                    self.postgres_db.connection.commit()
                    inserted_count = len(oracle_rows)
                    total_inserted += inserted_count
                    
                    progress = (total_inserted / oracle_count) * 100
                    print(f"  Progress: {total_inserted:,}/{oracle_count:,} ({progress:.1f}%)")
                    
                except Exception as e:
                    self.postgres_db.connection.rollback()
                    logger.error(f"Batch insert failed: {e}")
                    raise
                finally:
                    cursor.close()
                
                # Last batch if fewer rows than batch size
                if len(oracle_rows) < batch_size:
                    break
            
            print("-" * 80)
            print(f"Migration complete: {total_inserted:,} rows")
            print()
            
            # PostgreSQL 행 수 확인
            pg_count_sql = f'SELECT COUNT(*) FROM {pg_schema}.{pg_table}'
            pg_result = self.postgres_db.execute_query(pg_count_sql)
            pg_count = pg_result[0][0] if pg_result else 0
            
            print(f"PostgreSQL row count: {pg_count:,}")
            
            if pg_count != oracle_count:
                print(f"Row count mismatch: Oracle={oracle_count:,}, PostgreSQL={pg_count:,}")
                print("=" * 80)
                return False
            
            print("Row count match verified.")
            print()
            
            # ============================================
            # Detailed validation (ARCHITECTURE.md)
            # ============================================
            print("=" * 80)
            print("Detailed validation (drill-down)")
            print("=" * 80)
            print()
            
            # Step 1: Chunk Hash comparison (SQL level)
            print("Step 1: Chunk Hash comparison (SQL level)")
            print("-" * 80)
            
            try:
                canonicalizer = RowCanonicalizer(null_empty_policy=NullEmptyPolicy.DISTINCT)
                pk_columns = self._get_pk_columns(table_name, schema)
                
                chunk_validator = ChunkHashValidator(
                    self.oracle_db,
                    self.postgres_db,
                    pk_columns=pk_columns if pk_columns else None,
                    canonicalizer=canonicalizer,
                )
                
                hash_result = chunk_validator.compare_chunk_hash(
                    table_name=table_name,
                    schema=schema,
                    columns=columns
                )
                
                if hash_result['match']:
                    print("Chunk Hash match - data verified.")
                    if hash_result.get('oracle_hash'):
                        print(f"   Oracle Hash: {hash_result['oracle_hash'][:32]}...")
                    if hash_result.get('postgres_hash'):
                        print(f"   PostgreSQL Hash: {hash_result['postgres_hash'][:32]}...")
                else:
                    print("Chunk Hash mismatch - detailed verification needed.")
                    print(f"   Oracle Hash: {hash_result.get('oracle_hash', 'N/A')}")
                    print(f"   PostgreSQL Hash: {hash_result.get('postgres_hash', 'N/A')}")
                    print()
                    print("Hash mismatch may indicate data discrepancy.")
                    print("   Row-by-row verification recommended.")
            except Exception as e:
                logger.error(f"Chunk hash validation failed: {e}")
                print(f"Chunk Hash validation failed: {e}")
                hash_result = {'match': False}
            
            print()
            
            # Step 2: Aggregate validation (COUNT, MIN, MAX, COUNT DISTINCT, SUM)
            print("Step 2: Aggregate validation (COUNT, MIN, MAX, COUNT DISTINCT, SUM)")
            print("-" * 80)
            
            try:
                aggregate_validator = AggregateValidator(
                    self.oracle_db,
                    self.postgres_db,
                    decimal_tolerance=0.0001
                )
                
                # Aggregate check numeric columns only
                numeric_columns = self._get_numeric_columns(table_name, schema, columns)
                
                if numeric_columns:
                    agg_result = aggregate_validator.validate_aggregates(
                        table_name=table_name,
                        schema=schema,
                        columns=numeric_columns,
                        where_clause=""
                    )
                    
                    if agg_result['match']:
                        print("Aggregate validation passed.")
                        for col, col_result in agg_result['columns'].items():
                            comp = col_result['comparison']
                            oracle_agg = col_result['oracle']
                            postgres_agg = col_result['postgres']
                            print(f"   {col}:")
                            print(f"     COUNT: {comp['count_match']} (Oracle={oracle_agg.get('count')}, PG={postgres_agg.get('count')})")
                            print(f"     MIN: {comp['min_match']} (Oracle={oracle_agg.get('min')}, PG={postgres_agg.get('min')})")
                            print(f"     MAX: {comp['max_match']} (Oracle={oracle_agg.get('max')}, PG={postgres_agg.get('max')})")
                            print(f"     SUM: {comp['sum_match']} (Oracle={oracle_agg.get('sum')}, PG={postgres_agg.get('sum')}, tolerance applied)")
                    else:
                        print("Aggregate validation failed.")
                        for col, col_result in agg_result['columns'].items():
                            if not col_result.get('match', False):
                                comp = col_result['comparison']
                                oracle_agg = col_result['oracle']
                                postgres_agg = col_result['postgres']
                                print(f"   {col}: mismatch")
                                print(f"     COUNT: {comp['count_match']} (Oracle={oracle_agg.get('count')}, PG={postgres_agg.get('count')})")
                                print(f"     MIN: {comp['min_match']} (Oracle={oracle_agg.get('min')}, PG={postgres_agg.get('min')})")
                                print(f"     MAX: {comp['max_match']} (Oracle={oracle_agg.get('max')}, PG={postgres_agg.get('max')})")
                                print(f"     SUM: {comp['sum_match']} (Oracle={oracle_agg.get('sum')}, PG={postgres_agg.get('sum')})")
                else:
                    print("No numeric columns; skipping aggregate validation.")
                    agg_result = {'match': True}  # 숫자형 컬럼이 없으면 통과로 간주
            except Exception as e:
                logger.error(f"Aggregate validation failed: {e}")
                print(f"Aggregate validation failed: {e}")
                agg_result = {'match': False}
            
            print()
            print("=" * 80)
            
            # Final validation result
            hash_match = hash_result.get('match', False)
            agg_match = agg_result.get('match', False)
            
            if hash_match and agg_match:
                print("✅ 전체 검증 통과: 데이터가 정확히 이관되었습니다.")
                final_match = True
            elif hash_match and not numeric_columns:
                print("✅ Hash 검증 통과: 데이터가 정확히 이관되었습니다.")
                final_match = True
            else:
                if not hash_match:
                    print("❌ 검증 실패: Hash 불일치로 인해 데이터 불일치 가능성")
                if not agg_match:
                    print("❌ 검증 실패: 집계 검증에서 불일치 발견")
                final_match = False
            
            print("=" * 80)
            return final_match
            
        except Exception as e:
            logger.error(f"Failed to migrate table data: {e}")
            print(f"Data migration failed: {e}")
            print("=" * 80)
            import traceback
            traceback.print_exc()
            return False
    
    def verify_table_data(self, table_name: str, schema: str, 
                        target_schema: str = None):
        """
        테이블 데이터 검증만 실행 (이관 없이)
        ARCHITECTURE.md 기반 검증
        
        Args:
            table_name: 테이블명
            schema: Oracle 스키마명
            target_schema: PostgreSQL 스키마명 (None이면 schema와 동일)
        """
        pg_schema = (target_schema or schema).lower()
        pg_table = table_name.lower()
        
        print("=" * 80)
        print(f"Data verification: {schema}.{table_name} <-> {pg_schema}.{pg_table}")
        print("=" * 80)
        print()
        
        try:
            # Get column list
            columns = self.get_table_columns(table_name, schema)
            if not columns:
                print("Columns not found.")
                return False
            
            print(f"Column count: {len(columns)}")
            print()
            
            # Oracle row count
            oracle_count = self.get_row_count(table_name, schema)
            print(f"Oracle row count: {oracle_count:,}")
            
            # PostgreSQL row count
            pg_count_sql = f'SELECT COUNT(*) FROM {pg_schema}.{pg_table}'
            pg_result = self.postgres_db.execute_query(pg_count_sql)
            pg_count = pg_result[0][0] if pg_result else 0
            print(f"PostgreSQL row count: {pg_count:,}")
            print()
            
            if pg_count != oracle_count:
                print(f"Row count mismatch: Oracle={oracle_count:,}, PostgreSQL={pg_count:,}")
                print("=" * 80)
                return False
            
            print("Row count match verified.")
            print()
            
            # ============================================
            # Detailed validation (ARCHITECTURE.md)
            # ============================================
            print("=" * 80)
            print("Detailed validation (drill-down)")
            print("=" * 80)
            print()
            
            # Step 1: Chunk Hash comparison (SQL level)
            print("Step 1: Chunk Hash comparison (SQL level)")
            print("-" * 80)
            
            try:
                canonicalizer = RowCanonicalizer(null_empty_policy=NullEmptyPolicy.DISTINCT)
                pk_columns = self._get_pk_columns(table_name, schema)
                
                chunk_validator = ChunkHashValidator(
                    self.oracle_db,
                    self.postgres_db,
                    pk_columns=pk_columns if pk_columns else None,
                    canonicalizer=canonicalizer,
                )
                
                hash_result = chunk_validator.compare_chunk_hash(
                    table_name=table_name,
                    schema=schema,
                    columns=columns
                )
                
                if hash_result['match']:
                    print("Chunk Hash match - data verified.")
                    if hash_result.get('oracle_hash'):
                        print(f"   Oracle Hash: {hash_result['oracle_hash'][:32]}...")
                    if hash_result.get('postgres_hash'):
                        print(f"   PostgreSQL Hash: {hash_result['postgres_hash'][:32]}...")
                else:
                    print("Chunk Hash mismatch - detailed verification needed.")
                    print(f"   Oracle Hash: {hash_result.get('oracle_hash', 'N/A')}")
                    print(f"   PostgreSQL Hash: {hash_result.get('postgres_hash', 'N/A')}")
                    print()
                    print("Hash mismatch may indicate data discrepancy.")
                    print("   Row-by-row verification recommended.")
            except Exception as e:
                logger.error(f"Chunk hash validation failed: {e}")
                print(f"Chunk Hash validation failed: {e}")
                hash_result = {'match': False}
            
            print()
            
            # Step 2: Aggregate validation (COUNT, MIN, MAX, COUNT DISTINCT, SUM)
            print("Step 2: Aggregate validation (COUNT, MIN, MAX, COUNT DISTINCT, SUM)")
            print("-" * 80)
            
            try:
                aggregate_validator = AggregateValidator(
                    self.oracle_db,
                    self.postgres_db,
                    decimal_tolerance=0.0001
                )
                
                # Aggregate check numeric columns only
                numeric_columns = self._get_numeric_columns(table_name, schema, columns)
                
                if numeric_columns:
                    agg_result = aggregate_validator.validate_aggregates(
                        table_name=table_name,
                        schema=schema,
                        columns=numeric_columns,
                        where_clause=""
                    )
                    
                    if agg_result['match']:
                        print("Aggregate validation passed.")
                        for col, col_result in agg_result['columns'].items():
                            comp = col_result['comparison']
                            oracle_agg = col_result['oracle']
                            postgres_agg = col_result['postgres']
                            print(f"   {col}:")
                            print(f"     COUNT: {comp['count_match']} (Oracle={oracle_agg.get('count')}, PG={postgres_agg.get('count')})")
                            print(f"     MIN: {comp['min_match']} (Oracle={oracle_agg.get('min')}, PG={postgres_agg.get('min')})")
                            print(f"     MAX: {comp['max_match']} (Oracle={oracle_agg.get('max')}, PG={postgres_agg.get('max')})")
                            print(f"     SUM: {comp['sum_match']} (Oracle={oracle_agg.get('sum')}, PG={postgres_agg.get('sum')}, tolerance applied)")
                    else:
                        print("Aggregate validation failed.")
                        for col, col_result in agg_result['columns'].items():
                            if not col_result.get('match', False):
                                comp = col_result['comparison']
                                oracle_agg = col_result['oracle']
                                postgres_agg = col_result['postgres']
                                print(f"   {col}: mismatch")
                                print(f"     COUNT: {comp['count_match']} (Oracle={oracle_agg.get('count')}, PG={postgres_agg.get('count')})")
                                print(f"     MIN: {comp['min_match']} (Oracle={oracle_agg.get('min')}, PG={postgres_agg.get('min')})")
                                print(f"     MAX: {comp['max_match']} (Oracle={oracle_agg.get('max')}, PG={postgres_agg.get('max')})")
                                print(f"     SUM: {comp['sum_match']} (Oracle={oracle_agg.get('sum')}, PG={postgres_agg.get('sum')})")
                else:
                    print("No numeric columns; skipping aggregate validation.")
                    agg_result = {'match': True}  # 숫자형 컬럼이 없으면 통과로 간주
            except Exception as e:
                logger.error(f"Aggregate validation failed: {e}")
                print(f"Aggregate validation failed: {e}")
                agg_result = {'match': False}
            
            print()
            print("=" * 80)
            
            # Final validation result
            hash_match = hash_result.get('match', False)
            agg_match = agg_result.get('match', False)
            
            if hash_match and agg_match:
                print("All validations passed: data matches exactly.")
                final_match = True
            elif hash_match and not numeric_columns:
                print("Hash validation passed: data matches exactly.")
                final_match = True
            else:
                if not hash_match:
                    print("Validation failed: hash mismatch - possible data discrepancy.")
                if not agg_match:
                    print("Validation failed: aggregate mismatch.")
                final_match = False
            
            print("=" * 80)
            return final_match
            
        except Exception as e:
            logger.error(f"Failed to verify table data: {e}")
            print(f"Data verification failed: {e}")
            print("=" * 80)
            import traceback
            traceback.print_exc()
            return False
            
        except Exception as e:
            logger.error(f"Failed to migrate table data: {e}")
            print(f"Data migration failed: {e}")
            print("=" * 80)
            import traceback
            traceback.print_exc()
            return False


def main():
    """메인 함수"""
    
    try:
        # config.yaml 파일에서 설정 로드
        print("=" * 80)
        print("Loading config")
        print("=" * 80)
        
        from config.connection_config import (
            load_config as load_app_config,
            get_oracle_connection_params,
            get_postgres_connection_params,
            get_migration_config,
        )
        config = load_app_config("config.yaml")
        if not config:
            print("Failed to load config.yaml.")
            return False
        op = get_oracle_connection_params(config)
        pp = get_postgres_connection_params(config)
        if not op or not op.get("connection_string"):
            print("Set oracle.dsn in config.yaml.")
            return False
        if not pp or not pp.get("host"):
            print("Set postgres connection in config.yaml.")
            return False
        conn_str = op["connection_string"]
        oracle_config = {
            'user': op['username'],
            'password': op['password'],
            'host': conn_str.split('/')[0].split(':')[0],
            'port': int(conn_str.split('/')[0].split(':')[1]) if ':' in conn_str.split('/')[0] else None,
            'service_name': conn_str.split('/')[-1] if '/' in conn_str else '',
        }
        postgres_config = {
            'host': pp['host'],
            'port': pp['port'],
            'database': pp['database'],
            'user': pp['username'],
            'password': pp['password'],
        }
        migration_config = get_migration_config(config)
        source_schema = (migration_config.get("source_schema") or "").strip()
        if not source_schema:
            print("Set migration.source_schema in config.yaml.")
            return False
        target_schema = migration_config.get("target_schema")
        tables = migration_config.get("tables", [])
        
        # boolean 변환 (YAML에서 'true'/'false' 문자열로 올 수 있음)
        truncate_before_insert = migration_config.get('truncate_before_insert', False)
        if isinstance(truncate_before_insert, str):
            truncate_before_insert = truncate_before_insert.lower().strip() in ('true', '1', 'yes', 'on')
        elif not isinstance(truncate_before_insert, bool):
            # 숫자나 None인 경우
            truncate_before_insert = bool(truncate_before_insert) if truncate_before_insert is not None else False
        
        # 검증만 실행 옵션 (데이터 이관 없이 검증만)
        verify_only = migration_config.get('verify_only', False)
        if isinstance(verify_only, str):
            verify_only = verify_only.lower().strip() in ('true', '1', 'yes', 'on')
        
        # 숫자 변환
        batch_size = migration_config.get('batch_size', 1000)
        if isinstance(batch_size, str):
            try:
                batch_size = int(batch_size)
            except ValueError:
                batch_size = 1000
        
        print(f"Oracle schema: {source_schema}")
        print(f"PostgreSQL schema: {target_schema or source_schema}")
        print(f"Tables: {tables if tables else 'all tables'}")
        print(f"TRUNCATE before insert: {truncate_before_insert}")
        print(f"Batch size: {batch_size}")
        print(f"Verify only: {verify_only}")
        print("=" * 80)
        print()
        
        # DB 연결
        print("=" * 80)
        print("Connecting to databases")
        print("=" * 80)
        
        # Oracle 연결
        connection_string = f"{oracle_config['host']}:{oracle_config['port']}/{oracle_config['service_name']}"
        oracle_db = OracleDB(
            connection_string=connection_string,
            username=oracle_config['user'],
            password=oracle_config['password']
        )
        oracle_db.connect()
        print("Oracle connected.")
        
        # PostgreSQL 연결
        postgres_db = PostgresDB(
            host=postgres_config['host'],
            port=postgres_config['port'],
            database=postgres_config['database'],
            username=postgres_config['user'],
            password=postgres_config['password']
        )
        postgres_db.connect()
        print("PostgreSQL connected.")
        print("=" * 80)
        print()
        
        # DataMigrator 객체 생성
        migrator = DataMigrator(oracle_db, postgres_db)
        
        # 테이블 목록 결정
        if not tables:
            # 테이블 목록이 비어있으면 스키마의 모든 테이블 조회
            print("=" * 80)
            print(f"Listing all tables in schema {source_schema}...")
            print("=" * 80)
            
            sql = """
                SELECT table_name 
                FROM all_tables 
                WHERE owner = :schema 
                ORDER BY table_name
            """
            result = oracle_db.execute_query(sql, {'schema': source_schema.upper()})
            tables = [row[0] for row in result]
            
            if not tables:
                print(f"No tables found in schema {source_schema}.")
                return False
            
            print(f"Found {len(tables)} table(s):")
            for idx, table in enumerate(tables, 1):
                print(f"  {idx}. {table}")
            print()
        
        if verify_only:
            # 검증만 실행
            print("=" * 80)
            print("Verify-only mode")
            print("=" * 80)
            print()
            
            success_count = 0
            fail_count = 0
            
            for table_name in tables:
                print(f"\n[{success_count + fail_count + 1}/{len(tables)}] Verifying: {source_schema}.{table_name}")
                print("-" * 80)
                
                success = migrator.verify_table_data(
                    table_name=table_name,
                    schema=source_schema,
                    target_schema=target_schema
                )
                
                if success:
                    success_count += 1
                else:
                    fail_count += 1
        else:
            # 데이터 이관 실행
            print("=" * 80)
            print("Data migration started")
            print("=" * 80)
            print()
            
            success_count = 0
            fail_count = 0
            
            for table_name in tables:
                print(f"\n[{success_count + fail_count + 1}/{len(tables)}] Migrating: {source_schema}.{table_name}")
                print("-" * 80)
                
                success = migrator.migrate_table_data(
                    table_name=table_name,
                    schema=source_schema,
                    target_schema=target_schema,
                    truncate_before_insert=truncate_before_insert,
                    batch_size=batch_size
                )
                
                if success:
                    success_count += 1
                else:
                    fail_count += 1
        
        # 결과 요약
        print()
        print("=" * 80)
        print("Summary")
        print("=" * 80)
        print(f"Total tables: {len(tables)}")
        print(f"Success: {success_count}")
        print(f"Failed: {fail_count}")
        print("=" * 80)
        
        if fail_count > 0:
            print("Some table(s) failed.")
            return False
        
        print("All tasks completed.")
        return True
        
    except FileNotFoundError as e:
        logger.error(f"Config file not found: {e}")
        print(f"Config file not found: {e}")
        print("Create config.yaml.")
        return False
    except Exception as e:
        logger.error(f"Error in main: {e}")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        # 연결 종료
        if 'oracle_db' in locals():
            try:
                oracle_db.disconnect()
            except:
                pass
        if 'postgres_db' in locals():
            try:
                postgres_db.disconnect()
            except:
                pass
        print("Connections closed.")


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
