"""
Generate PostgreSQL CREATE TABLE DDL from Oracle table metadata.
Based on Oracle_to_PostgreSQL_DDL_Migration_Script_2.sql logic.
"""
import sys
import logging
from db.oracle import OracleDB

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def map_oracle_to_postgres_type(data_type: str, data_length: int = None, 
                                 data_precision: int = None, 
                                 data_scale: int = None) -> str:
    """
    Map Oracle data type to PostgreSQL type (Oracle_to_PostgreSQL_DDL_Migration_Script_2.sql).

    Args:
        data_type: Oracle data type.
        data_length: Data length.
        data_precision: Precision.
        data_scale: Scale.

    Returns:
        PostgreSQL type string.
    """
    data_type_upper = data_type.upper()
    
    # 문자열 타입
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
    
    # Binary types
    elif data_type_upper == 'BLOB':
        return 'BYTEA'
    elif data_type_upper == 'RAW':
        return 'BYTEA'
    elif data_type_upper == 'LONG RAW':
        return 'BYTEA'
    
    # 숫자 타입 (EDB/ora2pg 표준 매핑 기준)
    elif data_type_upper == 'NUMBER':
        if data_precision is None and data_scale is None:
            return 'NUMERIC'
        elif data_scale == 0 or data_scale is None:
            # Integer: no precision -> NUMERIC; with precision -> INTEGER family
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
            # 소수형 처리
            return f'NUMERIC({data_precision},{data_scale})'
        else:
            return 'NUMERIC'
    elif data_type_upper.startswith('FLOAT'):
        return 'DOUBLE PRECISION'
    elif data_type_upper == 'BINARY_FLOAT':
        return 'REAL'
    elif data_type_upper == 'BINARY_DOUBLE':
        return 'DOUBLE PRECISION'
    
    # Date/time types
    elif data_type_upper == 'DATE':
        return 'TIMESTAMP'
    elif data_type_upper == 'TIMESTAMP':
        return 'TIMESTAMP'
    elif data_type_upper.startswith('TIMESTAMP'):
        if 'WITH TIME ZONE' in data_type_upper or 'WITH LOCAL TIME ZONE' in data_type_upper:
            return 'TIMESTAMP WITH TIME ZONE'
        else:
            return 'TIMESTAMP'
    
    # 인터벌 타입
    elif data_type_upper.startswith('INTERVAL'):
        return 'INTERVAL'
    
    # Other types
    elif data_type_upper == 'ROWID':
        return 'CHAR(18)'
    elif data_type_upper == 'UROWID':
        return 'VARCHAR(4000)'
    elif data_type_upper == 'SDO_GEOMETRY':
        return 'GEOMETRY(GEOMETRY,4326)'
    
    # Default
    else:
        return 'TEXT'


def get_pk_columns(oracle_db: OracleDB, table_name: str, schema: str) -> list:
    """
    테이블의 PRIMARY KEY 컬럼명 목록 조회 (Safety First: PK는 항상 BIGINT로 생성하기 위함)
    """
    sql_pk_cols = """
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
        result = oracle_db.execute_query(sql_pk_cols, {
            'schema': schema.upper(),
            'table_name': table_name.upper()
        })
        return [row[0].lower() for row in result] if result else []
    except Exception as e:
        logger.warning(f"Failed to get PK columns: {e}")
        return []


def generate_create_table_ddl(oracle_db: OracleDB, table_name: str, schema: str) -> str:
    """
    CREATE TABLE DDL 문 생성 (1단계: Safety First)
    - PK/FK/ID 컬럼은 미래 21억 건 장애 방지를 위해 NUMBER → BIGINT 로 고정
    - 그 외는 Oracle precision/scale 기반 표준 매핑
    """
    # 1. Get column info
    sql_columns = """
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
        columns_result = oracle_db.execute_query(sql_columns, {
            'schema': schema.upper(),
            'table_name': table_name.upper()
        })
        
        if not columns_result:
            logger.warning(f"Table {schema}.{table_name} not found or no columns")
            return None

        # PK columns (Safety First: PK -> BIGINT)
        pk_columns = get_pk_columns(oracle_db, table_name, schema)
        
        # DDL 생성
        ddl_lines = []
        ddl_lines.append(f"CREATE TABLE {schema.lower()}.{table_name.lower()} (")
        
        column_definitions = []
        for row in columns_result:
            col_name = row[0].lower()
            data_type = row[1]
            data_length = row[2]
            data_precision = row[3]
            data_scale = row[4]
            nullable = row[5]
            
            # PK and NUMBER -> always BIGINT (safety)
            if col_name in pk_columns and data_type and data_type.upper() == 'NUMBER':
                pg_type = 'BIGINT'
            else:
                pg_type = map_oracle_to_postgres_type(
                    data_type, data_length, data_precision, data_scale
                )
            
            # 컬럼 정의 생성
            col_def = f"    {col_name} {pg_type}"
            
            # NULL constraint
            if nullable == 'N':
                col_def += " NOT NULL"
            
            column_definitions.append(col_def)
        
        # 컬럼 정의들을 합치기
        ddl_lines.append(",\n".join(column_definitions))
        ddl_lines.append(");")
        
        return "\n".join(ddl_lines)
        
    except Exception as e:
        logger.error(f"Failed to generate CREATE TABLE DDL: {e}")
        raise


def generate_primary_key_ddl(oracle_db: OracleDB, table_name: str, schema: str) -> str:
    """
    PRIMARY KEY 제약조건 DDL 생성
    
    Args:
        oracle_db: OracleDB 객체
        table_name: 테이블명
        schema: 스키마명
    
    Returns:
        ALTER TABLE ADD PRIMARY KEY DDL 문자열 (없으면 None)
    """
    sql_pk = """
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
        pk_result = oracle_db.execute_query(sql_pk, {
            'schema': schema.upper(),
            'table_name': table_name.upper()
        })
        
        if not pk_result:
            return None
        
        constraint_name = pk_result[0][0].lower()
        pk_columns = pk_result[0][1].lower()
        
        ddl = f"ALTER TABLE {schema.lower()}.{table_name.lower()} "
        ddl += f"ADD CONSTRAINT {constraint_name} "
        ddl += f"PRIMARY KEY ({pk_columns});"
        
        return ddl
        
    except Exception as e:
        logger.error(f"Failed to generate PRIMARY KEY DDL: {e}")
        return None


def generate_table_ddl(oracle_db: OracleDB, table_name: str, schema: str):
    """
    테이블의 전체 DDL 생성 (CREATE TABLE + PRIMARY KEY)
    
    Args:
        oracle_db: OracleDB 객체
        table_name: 테이블명
        schema: 스키마명
    """
    print("=" * 80)
    print(f"PostgreSQL DDL 생성: {schema}.{table_name}")
    print("=" * 80)
    print()
    
    try:
        # CREATE TABLE
        create_table_ddl = generate_create_table_ddl(oracle_db, table_name, schema)
        if create_table_ddl:
            print("-- CREATE TABLE")
            print(create_table_ddl)
            print()
        
        # PRIMARY KEY 제약조건 생성
        pk_ddl = generate_primary_key_ddl(oracle_db, table_name, schema)
        if pk_ddl:
            print("-- PRIMARY KEY")
            print(pk_ddl)
            print()
        
        print("=" * 80)
        
    except Exception as e:
        logger.error(f"Failed to generate DDL: {e}")
        print(f"❌ Error: {e}")
        return False
    
    return True


def main():
    """Main entry — config.yaml only (no hardcoding)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config.connection_config import load_config, get_oracle_connection_params, get_migration_config

    config = load_config()
    if not config:
        print("❌ config.yaml 로드 실패.")
        return False
    op = get_oracle_connection_params(config)
    if not op or not op.get("connection_string"):
        print("❌ config.yaml 에 oracle.dsn 을 설정하세요.")
        return False
    mig = get_migration_config(config)
    schema = (mig.get("source_schema") or "").strip()
    if not schema:
        print("❌ config.yaml 에 migration.source_schema 를 설정하세요.")
        return False
    tables = mig.get("tables") or []
    table_name = (tables[0] if tables else "").strip() if tables else ""
    if not table_name:
        print("❌ config.yaml 에 migration.tables 를 설정하세요.")
        return False
    schema = schema.upper()
    table_name = table_name.upper()

    print("=" * 80)
    print("Oracle to PostgreSQL DDL Generator")
    print("=" * 80)
    print(f"Table: {schema}.{table_name}")
    print("=" * 80)
    print()

    oracle_db = OracleDB(
        connection_string=op["connection_string"],
        username=op["username"],
        password=op["password"],
    )
    
    try:
        # 접속
        print("접속 중...")
        oracle_db.connect()
        print("✅ 연결 성공")
        print()
        
        # DDL 생성
        success = generate_table_ddl(oracle_db, table_name, schema)
        
        if success:
            print("✅ DDL 생성 완료")
        else:
            print("❌ DDL 생성 실패")
        
        return success
        
    except Exception as e:
        print()
        print("=" * 80)
        print("❌ Error!")
        print("=" * 80)
        print(f"Error: {e}")
        print()
        print("확인 사항:")
        print("1. 접속 정보가 올바른지 확인")
        print("2. config.yaml 의 migration.source_schema, migration.tables 가 올바른지 확인")
        print("3. 사용자 권한 확인 (DBA_TAB_COLUMNS, DBA_CONSTRAINTS 접근 권한 필요)")
        print("=" * 80)
        return False
        
    finally:
        # Disconnect
        try:
            oracle_db.disconnect()
            print("연결이 종료되었습니다.")
        except:
            pass


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
