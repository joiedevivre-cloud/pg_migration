"""
Data validation runner. Uses ValidationEngine for Oracle vs PostgreSQL validation; config from config.yaml.
"""
# -*- coding: utf-8 -*-
import sys
import os
import io
import logging
from pathlib import Path
from db.oracle import OracleDB
from db.postgres import PostgresDB
from validator.core.engine import ValidationEngine
from checks.row_canonicalize import NullEmptyPolicy
from config.profile_loader import ProfileLoader
from config.connection_config import (
    load_config as load_app_config,
    get_oracle_connection_params,
    get_postgres_connection_params,
    get_validation_config,
)

# Windows console encoding
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# YAML parsing (same approach as data_migrator.py)
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

if not HAS_YAML:
    def parse_simple_yaml(content):
        """Simple YAML parser (same as data_migrator.py)."""
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
                if current_section:
                    if current_subsection:
                        if current_subsection not in result[current_section]:
                            result[current_section][current_subsection] = []
                        result[current_section][current_subsection].append(value)
                    else:
                        if current_section not in result:
                            result[current_section] = []
                        if not isinstance(result[current_section], list):
                            result[current_section] = []
                        result[current_section].append(value)
                i += 1
                continue
            
            # Key: value
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
                        # Check if list starts (next line starts with -)
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
                        # boolean 변환
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


def load_config(config_path: str = None) -> dict:
    """Load config file (uses config.connection_config, single file)."""
    return load_app_config(config_path or "config.yaml")


def main():
    """Main entry."""
    print("=" * 80)
    print("Data validation")
    print("=" * 80)
    print()
    
    # Load config
    print("=" * 80)
    print("Loading config")
    print("=" * 80)
    
    config = load_config()
    if not config:
        print("❌ 설정 파일 로드를 실패했습니다.")
        return False
    
    # Oracle connection (same as data_migrator.py)
    oracle_params = get_oracle_connection_params(config)
    if not oracle_params or not oracle_params.get("connection_string"):
        print("Set oracle.dsn in config.yaml.")
        return False
    oracle_config_raw = config.get('oracle', {})
    oracle_config = {
        'user': oracle_config_raw.get('user', ''),
        'password': oracle_config_raw.get('password', ''),
        'host': oracle_config_raw.get('dsn', '').split(':')[0] if oracle_config_raw.get('dsn') else '',
        'port': int(oracle_config_raw.get('dsn', '').split(':')[1].split('/')[0]) if oracle_config_raw.get('dsn') and ':' in oracle_config_raw.get('dsn', '') and '/' in oracle_config_raw.get('dsn', '') else 1521,
        'service_name': oracle_config_raw.get('dsn', '').split('/')[-1] if oracle_config_raw.get('dsn') and '/' in oracle_config_raw.get('dsn', '') else 'orcl'
    }
    
    connection_string = f"{oracle_config['host']}:{oracle_config['port']}/{oracle_config['service_name']}"
    
    # PostgreSQL connection
    postgres_config_raw = config.get('postgres', {})
    postgres_config = {
        'host': postgres_config_raw.get('host', 'localhost'),
        'port': postgres_config_raw.get('port', 5432),
        'database': postgres_config_raw.get('dbname', 'postgres'),
        'user': postgres_config_raw.get('user', 'postgres'),
        'password': postgres_config_raw.get('password', '')
    }
    
    # Validation config
    validation_config = get_validation_config(config)
    source_schema = (validation_config.get('source_schema') or "").strip()
    if not source_schema:
        print("❌ config.yaml 에 validation.source_schema 를 설정하세요.")
        return False
    target_schema = validation_config.get('target_schema', source_schema)
    tables = validation_config.get('tables', [])
    
    # If table given on command line, validate only that table (e.g. python run_validation.py CF01)
    if len(sys.argv) > 1:
        tables = [sys.argv[1].strip()]
        print(f"(CLI override: validating {tables[0]} only)")
    
    # tables가 리스트가 아닌 경우 처리
    elif not isinstance(tables, list):
        if isinstance(tables, str):
            tables = [tables]
        else:
            tables = []
    
    # ValidationEngine config
    max_workers = validation_config.get('max_workers', 5)
    max_concurrent_db_sessions = validation_config.get('max_concurrent_db_sessions', 10)
    chunk_size = validation_config.get('chunk_size', 75)
    chunk_size_by_pattern = validation_config.get('chunk_size_by_pattern') or {}
    if not isinstance(chunk_size_by_pattern, dict):
        chunk_size_by_pattern = {}
    decimal_tolerance = validation_config.get('decimal_tolerance', 0.0001)
    tolerance_by_column = validation_config.get('tolerance_by_column', {}) or {}
    null_empty_policy_str = validation_config.get('null_empty_policy', 'DISTINCT')
    null_empty_policy = getattr(NullEmptyPolicy, null_empty_policy_str, NullEmptyPolicy.DISTINCT)
    max_diffs_per_chunk = validation_config.get('max_diffs_per_chunk', 10)
    
    # Profile loader
    profile_path = validation_config.get('profile_path', 'profiles/tables.yaml')
    profile_loader = None
    if Path(profile_path).exists():
        profile_loader = ProfileLoader(profile_path)
        profile_loader.load_profiles()
    
    print(f"Oracle schema: {source_schema}")
    print(f"PostgreSQL schema: {target_schema}")
    print(f"Tables: {tables if tables else 'all tables'}")
    print(f"Chunk size: {chunk_size}" + (f" (pattern override: {chunk_size_by_pattern})" if chunk_size_by_pattern else ""))
    print(f"Max workers: {max_workers}")
    print(f"Max concurrent DB sessions: {max_concurrent_db_sessions}")
    print(f"Decimal tolerance: {decimal_tolerance}")
    print(f"NULL/Empty policy: {null_empty_policy_str}")
    print("=" * 80)
    print()
    
    # DB connections
    print("=" * 80)
    print("Connecting to databases")
    print("=" * 80)
    
    # Oracle connection (same as data_migrator.py)
    oracle_db = OracleDB(
        connection_string=connection_string,
        username=oracle_config['user'],
        password=oracle_config['password']
    )
    try:
        oracle_db.connect()
        print("Oracle connected.")
    except Exception as e:
        print(f"Oracle connection failed: {e}")
        logger.error(f"Oracle connection failed: {e}")
        return False
    
    # PostgreSQL 연결
    postgres_db = PostgresDB(
        host=postgres_config['host'],
        port=postgres_config['port'],
        database=postgres_config['database'],
        username=postgres_config['user'],
        password=postgres_config['password']
    )
    postgres_db.connect()
    print("✅ PostgreSQL 연결 성공")
    print("=" * 80)
    print()
    
    # Create ValidationEngine
    engine = ValidationEngine(
        oracle_db=oracle_db,
        postgres_db=postgres_db,
        max_workers=max_workers,
        max_concurrent_db_sessions=max_concurrent_db_sessions,
        chunk_size=chunk_size,
        chunk_size_by_pattern=chunk_size_by_pattern,
        null_empty_policy=null_empty_policy,
        decimal_tolerance=decimal_tolerance,
        profile_loader=profile_loader
    )
    # Extra config
    engine.max_diffs_per_chunk = max_diffs_per_chunk
    engine.tolerance_by_column = tolerance_by_column  # Per-column tolerance
    
    # Determine table list
    if not tables:
        # If empty, get all tables in schema
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
            print(f"❌ {source_schema} 스키마에 테이블이 없습니다.")
            return False
        
        print(f"Found {len(tables)} table(s):")
        for idx, table in enumerate(tables, 1):
            print(f"  {idx}. {table}")
        print()
    
    # Run validation
    print("=" * 80)
    print("Starting data validation...")
    print("=" * 80)
    print()
    
    success_count = 0
    fail_count = 0
    
    for table_name in tables:
        print(f"\n[{success_count + fail_count + 1}/{len(tables)}] Validating: {source_schema}.{table_name}")
        print("-" * 80)
        
        try:
            # 컬럼이 지정되지 않은 경우 Oracle에서 자동 조회 (data_migrator.py와 동일한 방식)
            columns = None
            try:
                # 먼저 DBA_TAB_COLUMNS 시도
                sql = """
                    SELECT COLUMN_NAME
                    FROM DBA_TAB_COLUMNS
                    WHERE OWNER = :schema
                      AND TABLE_NAME = :table_name
                    ORDER BY COLUMN_ID
                """
                result = oracle_db.execute_query(sql, {
                    'schema': source_schema.upper(),
                    'table_name': table_name.upper()
                })
                columns = [row[0].lower() for row in result] if result else None
            except Exception as e:
                logger.debug(f"DBA_TAB_COLUMNS not accessible, trying ALL_TAB_COLUMNS: {e}")
                try:
                    # Fallback to ALL_TAB_COLUMNS
                    sql = """
                        SELECT COLUMN_NAME
                        FROM ALL_TAB_COLUMNS
                        WHERE OWNER = :schema
                          AND TABLE_NAME = :table_name
                        ORDER BY COLUMN_ID
                    """
                    result = oracle_db.execute_query(sql, {
                        'schema': source_schema.upper(),
                        'table_name': table_name.upper()
                    })
                    columns = [row[0].lower() for row in result] if result else None
                except Exception as e2:
                    logger.warning(f"Failed to auto-load columns: {e2}")
            
            if columns:
                logger.info(f"Auto-loaded {len(columns)} columns for {source_schema}.{table_name}")
            else:
                logger.warning(f"No columns found for {source_schema}.{table_name}")
            
            if not columns:
                print("Columns not found; skipping validation.")
                fail_count += 1
                continue
            
            # PostgreSQL에 테이블이 존재하는지 확인
            pg_schema_str = str(target_schema).lower() if isinstance(target_schema, str) else str(source_schema).lower()
            pg_table_exists = postgres_db.table_exists(table_name.lower(), pg_schema_str)
            if not pg_table_exists:
                print(f"⚠️ PostgreSQL에 테이블 {pg_schema_str}.{table_name}이 존재하지 않아 검증을 건너뜁니다.")
                logger.warning(f"PostgreSQL table {pg_schema_str}.{table_name} does not exist, skipping validation")
                fail_count += 1
                continue
            
            result = engine.validate_table(
                table_name=table_name,
                schema=source_schema,
                columns=columns,  # Auto-fetched or from profile
                base_where_clause="",
                generate_report=True
            )
            
            if result.get('error'):
                print(f"Validation failed: {result.get('error')}")
                fail_count += 1
            else:
                # Analyze validation result
                validation_results = result.get('validation_results', [])
                aggregate_results = result.get('aggregate_results', {})
                # Chunk validation result
                chunk_matches = [r for r in validation_results if r.get('match', False)]
                chunk_mismatches = [r for r in validation_results if not r.get('match', False) and 'error' not in r]
                chunk_errors = [r for r in validation_results if 'error' in r]
                
                # 집계 검증 결과 확인
                aggregate_match = aggregate_results.get('match', False)
                
                # Overall success
                all_chunks_match = len(chunk_mismatches) == 0 and len(chunk_errors) == 0
                overall_match = all_chunks_match and aggregate_match
                
                if overall_match:
                    print("Validation passed: data matches.")
                    print(f"   - Chunk validation: {len(chunk_matches)} match(es)")
                    if aggregate_match:
                        print("   - Aggregate validation: passed")
                    
                    # DDL report info
                    ddl_report = result.get('ddl_report')
                    if ddl_report:
                        changeable_count = len(ddl_report.get('changeable_columns', []))
                        if changeable_count > 0:
                            print(f"   - Type-optimizable columns: {changeable_count}")
                    
                    if result.get('summary_report_path'):
                        print(f"   - Summary report: {result.get('summary_report_path')}")
                    if result.get('report_path'):
                        print(f"   - JSON report: {result.get('report_path')}")
                    if result.get('ddl_report_path'):
                        print(f"   - DDL report: {result.get('ddl_report_path')}")
                    
                    success_count += 1
                else:
                    print("Validation failed: data mismatch.")
                    if chunk_mismatches:
                        print(f"   - Mismatched chunks: {len(chunk_mismatches)}")
                    if chunk_errors:
                        print(f"   - Error chunks: {len(chunk_errors)}")
                    if not aggregate_match:
                        print("   - Aggregate validation: failed")
                    
                    # DDL report info
                    ddl_report = result.get('ddl_report')
                    if ddl_report:
                        changeable_count = len(ddl_report.get('changeable_columns', []))
                        if changeable_count > 0:
                            print(f"   - Type-optimizable columns: {changeable_count}")
                    
                    if result.get('summary_report_path'):
                        print(f"   - Summary report: {result.get('summary_report_path')}")
                    if result.get('report_path'):
                        print(f"   - JSON report: {result.get('report_path')}")
                    if result.get('ddl_report_path'):
                        print(f"   - DDL report: {result.get('ddl_report_path')}")
                    
                    fail_count += 1
        except Exception as e:
            logger.error(f"Validation failed for {table_name}: {e}")
            print(f"Validation failed: {e}")
            import traceback
            traceback.print_exc()
            fail_count += 1
    
    print()
    print("=" * 80)
    print("Summary")
    print("=" * 80)
    print(f"Total tables: {len(tables)}")
    print(f"Success: {success_count}")
    print(f"Failed: {fail_count}")
    print("=" * 80)
    
    if fail_count > 0:
        print("Some table(s) failed validation.")
    else:
        print("All validations completed.")
    
    # Disconnect
    oracle_db.disconnect()
    postgres_db.disconnect()
    print()
    print("Connections closed.")
    
    return fail_count == 0


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n작업이 사용자에 의해 중단되었습니다.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error in main: {e}")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
