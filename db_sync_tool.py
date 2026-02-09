"""
Oracle과 PostgreSQL 동시 접속 및 동기화 도구
두 데이터베이스에 동시에 접속하여 검증 및 동기화 작업 수행
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

# YAML parsing (simple version)
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False
    def parse_simple_yaml(content):
        """Simple YAML parser (dict, list, string only)."""
        result = {}
        current_section = None
        current_subsection = None
        
        lines = content.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            
            # 주석이나 빈 줄 건너뛰기
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


class DBSyncTool:
    """Oracle과 PostgreSQL 동기화 도구 클래스"""
    
    def __init__(self, oracle_config: dict, postgres_config: dict):
        """
        Args:
            oracle_config: Oracle 접속 정보 딕셔너리
                {
                    'user': 'imsi',
                    'password': 'oracle123',
                    'host': '192.168.137.30',
                    'port': 1521,
                    'service_name': 'adgdb'
                }
            postgres_config: PostgreSQL 접속 정보 딕셔너리
                {
                    'host': '192.168.137.101',
                    'port': 5432,
                    'database': 'postgres',
                    'user': 'postgres',
                    'password': 'oracle123'
                }
        """
        self.oracle_config = oracle_config
        self.postgres_config = postgres_config
        self.oracle_db = None
        self.postgres_db = None
    
    def connect_oracle(self):
        """Connect to Oracle."""
        try:
            connection_string = f"{self.oracle_config['host']}:{self.oracle_config['port']}/{self.oracle_config['service_name']}"
            
            self.oracle_db = OracleDB(
                connection_string=connection_string,
                username=self.oracle_config['user'],
                password=self.oracle_config['password']
            )
            
            self.oracle_db.connect()
            logger.info("Oracle database connected successfully")
            return True
            
        except Exception as e:
            logger.error(f"Oracle connection failed: {e}")
            return False
    
    def connect_postgres(self):
        """PostgreSQL DB 연결"""
        try:
            self.postgres_db = PostgresDB(
                host=self.postgres_config['host'],
                port=self.postgres_config['port'],
                database=self.postgres_config['database'],
                username=self.postgres_config['user'],
                password=self.postgres_config['password']
            )
            
            self.postgres_db.connect()
            logger.info("PostgreSQL database connected successfully")
            return True
            
        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e}")
            return False
    
    def connect_all(self):
        """Connect all DBs."""
        print("=" * 80)
        print("데이터베이스 연결")
        print("=" * 80)
        
        oracle_success = self.connect_oracle()
        postgres_success = self.connect_postgres()
        
        print()
        if oracle_success:
            print("✅ Oracle 연결 성공")
        else:
            print("❌ Oracle 연결 실패")
        
        if postgres_success:
            print("✅ PostgreSQL 연결 성공")
        else:
            print("❌ PostgreSQL 연결 실패")
        
        print("=" * 80)
        print()
        
        return oracle_success and postgres_success
    
    def disconnect_all(self):
        """모든 데이터베이스 연결 종료"""
        try:
            if self.oracle_db:
                self.oracle_db.disconnect()
                logger.info("Oracle database disconnected")
        except Exception as e:
            logger.error(f"Oracle disconnect error: {e}")
        
        try:
            if self.postgres_db:
                self.postgres_db.disconnect()
                logger.info("PostgreSQL database disconnected")
        except Exception as e:
            logger.error(f"PostgreSQL disconnect error: {e}")
    
    def test_connections(self):
        """Test connections and show basic info."""
        print("=" * 80)
        print("연결 테스트 및 정보 조회")
        print("=" * 80)
        print()
        
        # Oracle 정보 조회
        if self.oracle_db:
            try:
                print("📊 Oracle Database:")
                print("-" * 80)
                
                # Current time
                result = self.oracle_db.execute_query("SELECT SYSDATE FROM DUAL")
                if result:
                    print(f"  현재 시간: {result[0][0]}")
                
                # 버전 정보
                result = self.oracle_db.execute_query("SELECT * FROM v$version WHERE banner LIKE 'Oracle%'")
                if result:
                    print(f"  버전: {result[0][0]}")
                
                # Current user
                result = self.oracle_db.execute_query("SELECT USER FROM DUAL")
                if result:
                    print(f"  사용자: {result[0][0]}")
                
                print()
            except Exception as e:
                logger.error(f"Oracle query failed: {e}")
                print(f"  ❌ Oracle 쿼리 실행 실패: {e}")
                print()
        
        # PostgreSQL 정보 조회
        if self.postgres_db:
            try:
                print("📊 PostgreSQL Database:")
                print("-" * 80)
                
                # Current time
                result = self.postgres_db.execute_query("SELECT NOW()")
                if result:
                    print(f"  현재 시간: {result[0][0]}")
                
                # 버전 정보
                result = self.postgres_db.execute_query("SELECT version()")
                if result:
                    version = result[0][0].split(',')[0]  # First part only
                    print(f"  버전: {version}")
                
                # Current user
                result = self.postgres_db.execute_query("SELECT current_user")
                if result:
                    print(f"  사용자: {result[0][0]}")
                
                # 현재 데이터베이스
                result = self.postgres_db.execute_query("SELECT current_database()")
                if result:
                    print(f"  데이터베이스: {result[0][0]}")
                
                print()
            except Exception as e:
                logger.error(f"PostgreSQL query failed: {e}")
                print(f"  ❌ PostgreSQL 쿼리 실행 실패: {e}")
                print()
        
        print("=" * 80)
    
    def get_oracle_db(self):
        """Return Oracle DB instance."""
        return self.oracle_db
    
    def get_postgres_db(self):
        """PostgreSQL DB 객체 반환"""
        return self.postgres_db
    
    def map_oracle_to_postgres_type(self, data_type: str, data_length: int = None, 
                                     data_precision: int = None, 
                                     data_scale: int = None) -> str:
        """
        Oracle 데이터 타입을 PostgreSQL 타입으로 매핑
        Oracle_to_PostgreSQL_DDL_Migration_Script_2.sql 로직 기반
        
        Args:
            data_type: Oracle 데이터 타입
            data_length: 데이터 길이
            data_precision: 정밀도
            data_scale: 스케일
        
        Returns:
            PostgreSQL 데이터 타입 문자열
        """
        data_type_upper = data_type.upper()
        
        # String types
        if data_type_upper == 'CLOB':
            return 'TEXT'
        elif data_type_upper == 'NCLOB':
            return 'TEXT'
        elif data_type_upper == 'LONG':
            return 'TEXT'
        elif data_type_upper == 'XMLTYPE':
            return 'XML'
        elif data_type_upper == 'NVARCHAR2':
            if data_length and data_length > 0:
                return f'VARCHAR({data_length})'
            else:
                return 'VARCHAR'
        elif data_type_upper == 'VARCHAR2':
            if data_length and data_length > 0:
                return f'VARCHAR({data_length})'
            else:
                return 'VARCHAR'
        elif data_type_upper == 'CHAR':
            if data_length and data_length > 0:
                return f'CHAR({data_length})'
            else:
                return 'CHAR'
        elif data_type_upper == 'NCHAR':
            if data_length and data_length > 0:
                return f'CHAR({data_length})'
            else:
                return 'CHAR'
        
        # 바이너리 타입
        elif data_type_upper == 'BLOB':
            return 'BYTEA'
        elif data_type_upper == 'RAW':
            return 'BYTEA'
        elif data_type_upper == 'LONG RAW':
            return 'BYTEA'
        
        # Numeric types (EDB/ora2pg)
        elif data_type_upper == 'NUMBER':
            if data_precision is None and data_scale is None:
                return 'NUMERIC'
            elif data_scale == 0 or data_scale is None:
                # 정수형 처리 - precision 없으면 NUMERIC, 있으면 INTEGER 계열
                if data_precision is None:
                    return 'NUMERIC'
                if data_precision <= 4:
                    return 'SMALLINT'
                elif data_precision <= 9:
                    return 'INTEGER'
                elif data_precision <= 18:
                    return 'BIGINT'
                else:
                    return f'NUMERIC({data_precision})'
            elif data_scale and data_scale > 0:
                # Decimal
                return f'NUMERIC({data_precision},{data_scale})'
            else:
                return 'NUMERIC'
        elif data_type_upper.startswith('FLOAT'):
            return 'DOUBLE PRECISION'
        elif data_type_upper == 'BINARY_FLOAT':
            return 'REAL'
        elif data_type_upper == 'BINARY_DOUBLE':
            return 'DOUBLE PRECISION'
        
        # 날짜/시간 타입
        elif data_type_upper == 'DATE':
            return 'TIMESTAMP'
        elif data_type_upper == 'TIMESTAMP':
            return 'TIMESTAMP'
        elif data_type_upper.startswith('TIMESTAMP'):
            if 'WITH TIME ZONE' in data_type_upper or 'WITH LOCAL TIME ZONE' in data_type_upper:
                return 'TIMESTAMP WITH TIME ZONE'
            else:
                return 'TIMESTAMP'
        
        # Interval types
        elif data_type_upper.startswith('INTERVAL'):
            return 'INTERVAL'
        
        # 기타 타입
        elif data_type_upper == 'ROWID':
            return 'CHAR(18)'
        elif data_type_upper == 'UROWID':
            return 'VARCHAR(4000)'
        elif data_type_upper == 'SDO_GEOMETRY':
            return 'GEOMETRY(GEOMETRY,4326)'
        
        # Default
        else:
            return 'TEXT'
    
    def get_oracle_table_schema(self, table_name: str, schema: str):
        """
        Oracle 테이블 스키마 조회
        
        Args:
            table_name: 테이블명
            schema: 스키마명
        
        Returns:
            컬럼 정보 리스트
        """
        if not self.oracle_db:
            raise Exception("Oracle DB가 연결되지 않았습니다.")
        
        sql = """
            SELECT 
                tc.COLUMN_NAME,
                tc.DATA_TYPE,
                tc.DATA_LENGTH,
                tc.DATA_PRECISION,
                tc.DATA_SCALE,
                tc.NULLABLE,
                tc.COLUMN_ID
            FROM DBA_TAB_COLUMNS tc
            WHERE tc.OWNER = :schema
              AND tc.TABLE_NAME = :table_name
            ORDER BY tc.COLUMN_ID
        """
        
        try:
            result = self.oracle_db.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            
            if not result:
                raise Exception(f"테이블 {schema}.{table_name}을 찾을 수 없습니다.")
            
            columns = []
            for row in result:
                columns.append({
                    'column_name': row[0],
                    'data_type': row[1],
                    'data_length': row[2],
                    'data_precision': row[3],
                    'data_scale': row[4],
                    'nullable': row[5],
                    'column_id': row[6]
                })
            
            return columns
            
        except Exception as e:
            logger.error(f"Failed to get Oracle table schema: {e}")
            raise
    
    def generate_postgres_create_table_ddl(self, table_name: str, schema: str, 
                                          target_schema: str = None) -> str:
        """
        PostgreSQL CREATE TABLE DDL 문 생성
        
        Args:
            table_name: 테이블명
            schema: Oracle 스키마명
            target_schema: PostgreSQL 스키마명 (None이면 schema와 동일)
        
        Returns:
            CREATE TABLE DDL 문자열
        """
        if not self.oracle_db:
            raise Exception("Oracle DB가 연결되지 않았습니다.")
        
        # 스키마 조회
        columns = self.get_oracle_table_schema(table_name, schema)
        # PK columns (Safety First: PK -> BIGINT)
        pk_columns = self.get_pk_columns(table_name, schema)
        
        # PostgreSQL 스키마명 결정
        pg_schema = (target_schema or schema).lower()
        pg_table = table_name.lower()
        
        # Build DDL
        ddl_lines = []
        ddl_lines.append(f"CREATE TABLE IF NOT EXISTS {pg_schema}.{pg_table} (")
        
        column_definitions = []
        for col in columns:
            col_name = col['column_name'].lower()
            data_type = col['data_type']
            data_length = col['data_length']
            data_precision = col['data_precision']
            data_scale = col['data_scale']
            nullable = col['nullable']
            
            # PK 컬럼이면서 NUMBER 계열이면 무조건 BIGINT (21억 건 장애 방지)
            if col_name in pk_columns and data_type and data_type.upper() == 'NUMBER':
                pg_type = 'BIGINT'
            else:
                pg_type = self.map_oracle_to_postgres_type(
                    data_type, data_length, data_precision, data_scale
                )
            
            # 컬럼 정의 생성
            col_def = f"    {col_name} {pg_type}"
            
            # NULL 제약조건
            if nullable == 'N':
                col_def += " NOT NULL"
            
            column_definitions.append(col_def)
        
        # 컬럼 정의들을 합치기
        ddl_lines.append(",\n".join(column_definitions))
        ddl_lines.append(");")
        
        return "\n".join(ddl_lines)
    
    def create_postgres_table(self, table_name: str, schema: str, 
                             target_schema: str = None, drop_if_exists: bool = False):
        """
        Oracle 테이블 스키마를 기반으로 PostgreSQL에 테이블 생성
        
        Args:
            table_name: 테이블명
            schema: Oracle 스키마명
            target_schema: PostgreSQL 스키마명 (None이면 schema와 동일)
            drop_if_exists: 기존 테이블이 있으면 삭제할지 여부
        """
        if not self.postgres_db:
            raise Exception("PostgreSQL DB가 연결되지 않았습니다.")
        
        pg_schema = (target_schema or schema).lower()
        pg_table = table_name.lower()
        
        print("=" * 80)
        print(f"PostgreSQL 테이블 생성: {pg_schema}.{pg_table}")
        print("=" * 80)
        print()
        
        try:
            # 스키마 생성 (없으면)
            try:
                create_schema_sql = f"CREATE SCHEMA IF NOT EXISTS {pg_schema};"
                self.postgres_db.execute_ddl(create_schema_sql)
                print(f"✅ 스키마 확인/생성: {pg_schema}")
            except Exception as e:
                logger.warning(f"Schema creation failed (may already exist): {e}")
            
            # 기존 테이블 삭제 (옵션)
            if drop_if_exists:
                drop_sql = f"DROP TABLE IF EXISTS {pg_schema}.{pg_table} CASCADE;"
                try:
                    self.postgres_db.execute_ddl(drop_sql)
                    print(f"✅ 기존 테이블 삭제: {pg_schema}.{pg_table}")
                except Exception as e:
                    logger.warning(f"Drop table failed: {e}")
            
            # CREATE TABLE DDL 생성
            create_table_ddl = self.generate_postgres_create_table_ddl(
                table_name, schema, target_schema
            )
            
            print("생성할 DDL:")
            print("-" * 80)
            print(create_table_ddl)
            print("-" * 80)
            print()
            
            # PostgreSQL에 테이블 생성
            self.postgres_db.execute_ddl(create_table_ddl)
            print(f"✅ 테이블 생성 완료: {pg_schema}.{pg_table}")
            print()
            
            # PRIMARY KEY 제약조건 추가
            try:
                pk_ddl = self.generate_primary_key_ddl(table_name, schema, target_schema)
                if pk_ddl:
                    self.postgres_db.execute_ddl(pk_ddl)
                    print(f"✅ PRIMARY KEY 제약조건 추가 완료")
                    print()
            except Exception as e:
                logger.warning(f"Primary key creation failed: {e}")
            
            print("=" * 80)
            return True
            
        except Exception as e:
            logger.error(f"Failed to create PostgreSQL table: {e}")
            print(f"❌ 테이블 생성 실패: {e}")
            print("=" * 80)
            return False
    
    def get_pk_columns(self, table_name: str, schema: str) -> list:
        """
        PRIMARY KEY 컬럼명 목록 조회 (Safety First: PK는 항상 BIGINT로 생성하기 위함)
        """
        if not self.oracle_db:
            return []
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
        try:
            result = self.oracle_db.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            return [row[0].lower() for row in result] if result else []
        except Exception as e:
            logger.warning(f"Failed to get PK columns: {e}")
            return []

    def generate_primary_key_ddl(self, table_name: str, schema: str, 
                                 target_schema: str = None) -> str:
        """
        PRIMARY KEY 제약조건 DDL 생성
        
        Args:
            table_name: 테이블명
            schema: Oracle 스키마명
            target_schema: PostgreSQL 스키마명
        
        Returns:
            ALTER TABLE ADD PRIMARY KEY DDL 문자열 (없으면 None)
        """
        if not self.oracle_db:
            return None
        
        sql = """
            SELECT 
                cc.CONSTRAINT_NAME,
                LISTAGG(ccc.COLUMN_NAME, ', ') WITHIN GROUP (ORDER BY ccc.POSITION) as PK_COLUMNS
            FROM DBA_CONSTRAINTS cc
            JOIN DBA_CONS_COLUMNS ccc 
                ON cc.OWNER = ccc.OWNER 
                AND cc.CONSTRAINT_NAME = ccc.CONSTRAINT_NAME
            WHERE cc.CONSTRAINT_TYPE = 'P'
              AND cc.OWNER = :schema
              AND cc.TABLE_NAME = :table_name
            GROUP BY cc.CONSTRAINT_NAME
        """
        
        try:
            pk_result = self.oracle_db.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            
            if not pk_result:
                return None
            
            constraint_name = pk_result[0][0].lower()
            pk_columns = pk_result[0][1].lower()
            
            pg_schema = (target_schema or schema).lower()
            pg_table = table_name.lower()
            
            ddl = f"ALTER TABLE {pg_schema}.{pg_table} "
            ddl += f"ADD CONSTRAINT {constraint_name} "
            ddl += f"PRIMARY KEY ({pk_columns});"
            
            return ddl
            
        except Exception as e:
            logger.error(f"Failed to generate PRIMARY KEY DDL: {e}")
            return None


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
        # 간단한 YAML 파서 사용
        config = parse_simple_yaml(content)
        # 리스트 처리 (tables 섹션)
        if 'migration' in config and 'tables' in config['migration']:
            # tables를 리스트로 변환
            tables_str = str(config['migration']['tables'])
            # YAML 리스트 형식 파싱
            tables = []
            in_list = False
            for line in content.split('\n'):
                if 'tables:' in line:
                    in_list = True
                    continue
                if in_list and line.strip().startswith('- '):
                    table = line.strip()[2:].strip().strip('"\'')
                    tables.append(table)
                elif in_list and line.strip() and not line.strip().startswith('#'):
                    if not line.strip().startswith('-'):
                        break
            config['migration']['tables'] = tables
    
    return config


def get_table_list_from_oracle(sync_tool, schema: str) -> list:
    """
    Oracle에서 스키마의 모든 테이블 목록 조회
    
    Args:
        sync_tool: DBSyncTool 객체
        schema: 스키마명
    
    Returns:
        테이블명 리스트
    """
    if not sync_tool.oracle_db:
        return []
    
    try:
        sql = """
            SELECT table_name 
            FROM all_tables 
            WHERE owner = :schema 
            ORDER BY table_name
        """
        result = sync_tool.oracle_db.execute_query(sql, {'schema': schema.upper()})
        return [row[0] for row in result]
    except Exception as e:
        logger.error(f"Failed to get table list: {e}")
        return []


def main():
    """메인 함수"""
    
    try:
        # config.yaml 파일에서 설정 로드
        print("=" * 80)
        print("설정 파일 로드")
        print("=" * 80)
        
        from config.connection_config import (
            load_config as load_app_config,
            get_oracle_connection_params,
            get_postgres_connection_params,
            get_migration_config,
        )
        config = load_app_config("config.yaml")
        if not config:
            print("❌ config.yaml 로드 실패.")
            return False
        op = get_oracle_connection_params(config)
        pp = get_postgres_connection_params(config)
        if not op or not op.get("connection_string"):
            print("❌ config.yaml 에 oracle.dsn 을 설정하세요.")
            return False
        if not pp or not pp.get("host"):
            print("❌ config.yaml 에 postgres 접속 정보를 설정하세요.")
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
            print("❌ config.yaml 에 migration.source_schema 를 설정하세요.")
            return False
        target_schema = migration_config.get("target_schema")
        tables = migration_config.get("tables", [])
        drop_if_exists = migration_config.get("drop_if_exists", False)
        
        print(f"Oracle 스키마: {source_schema}")
        print(f"PostgreSQL 스키마: {target_schema or source_schema}")
        print(f"이관할 테이블: {tables if tables else '모든 테이블'}")
        print("=" * 80)
        print()
        
        # DBSyncTool 객체 생성
        sync_tool = DBSyncTool(oracle_config, postgres_config)
        
        # 모든 데이터베이스 연결
        if not sync_tool.connect_all():
            print("❌ 일부 데이터베이스 연결에 실패했습니다.")
            return False
        
        # 연결 테스트
        sync_tool.test_connections()
        
        # 테이블 목록 결정
        if not tables:
            # 테이블 목록이 비어있으면 스키마의 모든 테이블 조회
            print()
            print("=" * 80)
            print(f"{source_schema} 스키마의 모든 테이블 조회 중...")
            print("=" * 80)
            tables = get_table_list_from_oracle(sync_tool, source_schema)
            
            if not tables:
                print(f"❌ {source_schema} 스키마에 테이블이 없습니다.")
                return False
            
            print(f"📊 총 {len(tables)}개의 테이블을 찾았습니다:")
            for idx, table in enumerate(tables, 1):
                print(f"  {idx}. {table}")
            print()
        
        # 테이블 생성 실행
        print("=" * 80)
        print("테이블 생성 작업 시작")
        print("=" * 80)
        print()
        
        success_count = 0
        fail_count = 0
        
        for table_name in tables:
            print(f"\n[{success_count + fail_count + 1}/{len(tables)}] 처리 중: {source_schema}.{table_name}")
            print("-" * 80)
            
            success = sync_tool.create_postgres_table(
                table_name=table_name,
                schema=source_schema,
                target_schema=target_schema,
                drop_if_exists=drop_if_exists
            )
            
            if success:
                success_count += 1
                print(f"✅ {table_name} 생성 완료")
            else:
                fail_count += 1
                print(f"❌ {table_name} 생성 실패")
        
        # 결과 요약
        print()
        print("=" * 80)
        print("작업 완료 요약")
        print("=" * 80)
        print(f"총 테이블: {len(tables)}")
        print(f"성공: {success_count}")
        print(f"실패: {fail_count}")
        print("=" * 80)
        
        if fail_count > 0:
            print("⚠️ 일부 테이블 생성에 실패했습니다.")
            return False
        
        print("✅ 모든 작업이 완료되었습니다.")
        return True
        
    except FileNotFoundError as e:
        logger.error(f"Config file not found: {e}")
        print(f"❌ 설정 파일을 찾을 수 없습니다: {e}")
        print("\nconfig.yaml 파일을 생성하세요.")
        return False
    except Exception as e:
        logger.error(f"Error in main: {e}")
        print(f"❌ 오류 발생: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        # 연결 종료
        if 'sync_tool' in locals():
            sync_tool.disconnect_all()
        print("\n연결이 종료되었습니다.")


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
