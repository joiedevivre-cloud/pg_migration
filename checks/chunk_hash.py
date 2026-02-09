"""
Chunk Hash 검증 모듈
- row_hash는 DB(SQL)에서 이미 동일하게 계산됨. Python에서 row 데이터를 재구성/재포맷하지 않음.
- chunk_hash: DB에서 받은 row_hash만 수집 → strip(숨은 개행/공백 제거) → 소문자 통일 → 정렬 → 구분자/개행 없이 join → utf-8 인코딩 후 SHA256.
"""
import hashlib
import logging
from typing import Optional, Dict, Any, List
from db.oracle import OracleDB
from db.postgres import PostgresDB
from checks.row_canonicalize import RowCanonicalizer

logger = logging.getLogger(__name__)


def _chunk_hash_from_row_hashes(row_hash_list: List[str]) -> str:
    """
    DB에서 받은 row_hash만 사용. 새 줄/구분자/리포맷 추가 금지.
    - strip: 드라이버나 전송 과정에서 붙은 \\n/\\r/공백 제거
    - lower: hex 대소문자 통일
    - sort: DB 반환 순서 차이 제거
    - join: 구분자·개행 없이 '' 로만 연결
    - encode: utf-8 만 사용
    """
    normalized = [str(h).strip().lower() for h in row_hash_list]
    normalized.sort()
    chunk_input = ''.join(normalized)  # no delimiter, no newline
    logger.debug("chunk_input repr: %s", repr(chunk_input))
    logger.debug("chunk_input length: %s", len(chunk_input))
    return hashlib.sha256(chunk_input.encode('utf-8')).hexdigest()


class ChunkHashValidator:
    """Chunk 단위 해시 검증 클래스"""
    
    def __init__(self, oracle_db: OracleDB, postgres_db: PostgresDB,
                 pk_columns: Optional[List[str]] = None,
                 canonicalizer: Optional[RowCanonicalizer] = None):
        """
        Args:
            oracle_db: Oracle DB 연결
            postgres_db: PostgreSQL DB 연결
            pk_columns: Primary Key 컬럼 리스트 (ORDER BY에 사용)
            canonicalizer: Row 정규화 객체 (옵션)
        """
        self.oracle_db = oracle_db
        self.postgres_db = postgres_db
        self.pk_columns = pk_columns or []
        self.canonicalizer = canonicalizer
    
    def calculate_oracle_hash(self, table_name: str, schema: str, 
                             columns: list, where_clause: str = "",
                             partition_name: Optional[str] = None) -> Optional[str]:
        """
        Oracle 하이브리드 해시: DB에서 행별 해시 문자열만 XMLAGG로 결합해 반환, 최종 SHA256은 Python hashlib에서 계산.
        (LISTAGG 한계 우회, DB 권한/엔진 부하 최소화)
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            columns: 컬럼 리스트
            where_clause: WHERE 절 조건
            partition_name: 파티션명 (옵션)
        
        Returns:
            청크 최종 해시(hex 문자열) 또는 None
        """
        try:
            # 컬럼 메타데이터 조회하여 타입별로 안전하게 변환
            try:
                metadata = self.oracle_db.get_column_metadata(table_name, schema, columns)
            except:
                metadata = {}
            
            # 컬럼을 ||로 결합 (STANDARD_HASH는 단일 표현식을 요구)
            # 타입별로 안전하게 문자열로 변환
            # Oracle에서 모든 타입을 문자열로 변환하는 가장 안전한 방법:
            # CAST를 사용하여 대부분의 타입을 VARCHAR2로 변환
            col_parts = []
            for col in columns:
                col_upper = col.upper()
                col_meta = metadata.get(col.lower(), metadata.get(col, {}))
                data_type = col_meta.get('data_type', '').upper() if col_meta else ''
                precision = col_meta.get('data_precision') if col_meta else None
                scale = col_meta.get('data_scale') if col_meta else None
                if scale is not None and isinstance(scale, str):
                    try:
                        scale = int(scale)
                    except (ValueError, TypeError):
                        scale = None
                if precision is not None and isinstance(precision, str):
                    try:
                        precision = int(precision)
                    except (ValueError, TypeError):
                        precision = None

                # Canonicalization: NULL → '<NULL>', 수치형 → PRECISION/SCALE 고정 포맷, 문자열 → TRIM, DATE/TIMESTAMP → ISO-8601
                if not data_type:
                    # 메타데이터 없음: TO_CHAR + TRIM (문자열 공백 통일)
                    col_expr = f'CASE WHEN "{col_upper}" IS NULL THEN \'<NULL>\' ELSE TRIM(TO_CHAR("{col_upper}")) END'
                elif data_type in ('BLOB', 'RAW', 'LONG RAW'):
                    # LOB: length + sampled content
                    col_expr = f'CASE WHEN "{col_upper}" IS NULL THEN \'<NULL>\' ELSE \'LEN:\' || LENGTH("{col_upper}") || \':\' || SUBSTR(RAWTOHEX("{col_upper}"), 1, 100) END'
                elif data_type in ('CLOB', 'NCLOB', 'LONG'):
                    # CLOB: length + sampled content (최대 100자)
                    col_expr = f'CASE WHEN "{col_upper}" IS NULL THEN \'<NULL>\' ELSE \'LEN:\' || LENGTH("{col_upper}") || \':\' || SUBSTR(DBMS_LOB.SUBSTR("{col_upper}", 100, 1), 1, 100) END'
                elif data_type == 'DATE':
                    # DATE: ISO-8601 포맷 (YYYY-MM-DD HH24:MI:SS)
                    col_expr = f'CASE WHEN "{col_upper}" IS NULL THEN \'<NULL>\' ELSE TO_CHAR("{col_upper}", \'YYYY-MM-DD"T"HH24:MI:SS\') END'
                elif data_type in ('TIMESTAMP', 'TIMESTAMP WITH TIME ZONE', 'TIMESTAMP WITH LOCAL TIME ZONE'):
                    # TIMESTAMP: 초 단위까지만 (FF는 DB별 0~9자리 → 해시 불일치 방지)
                    col_expr = f'CASE WHEN "{col_upper}" IS NULL THEN \'<NULL>\' ELSE TO_CHAR("{col_upper}", \'YYYY-MM-DD"T"HH24:MI:SS\') END'
                elif data_type in ('VARCHAR2', 'CHAR', 'NVARCHAR2', 'NCHAR'):
                    # 문자열: TRIM 적용 (공백 차이 제거)
                    col_expr = f'CASE WHEN "{col_upper}" IS NULL THEN \'<NULL>\' WHEN "{col_upper}" = \'\' THEN \'<EMPTY>\' ELSE TRIM(TO_CHAR("{col_upper}")) END'
                elif data_type in ('NUMBER', 'FLOAT', 'BINARY_FLOAT', 'BINARY_DOUBLE', 'INTEGER'):
                    # NUMBER: Strong Casting — 메타데이터 scale로 소수 자릿수 강제 (50.5 → 50.50000, Postgres와 동일 문자열)
                    if precision is not None and scale is not None and scale > 0:
                        # FM + 정수자리(9) + 0 + . + 소수자리(0) → trailing zero 강제
                        int_places = max(0, precision - scale)
                        fmt = 'FM' + ('9' * int_places) + '0.' + ('0' * scale)
                        col_expr = f'CASE WHEN "{col_upper}" IS NULL THEN \'<NULL>\' ELSE TRIM(TO_CHAR("{col_upper}", \'{fmt}\')) END'
                    elif precision is not None and scale is not None and scale == 0:
                        fmt = 'FM' + ('9' * max(0, precision - 1)) + '0'
                        col_expr = f'CASE WHEN "{col_upper}" IS NULL THEN \'<NULL>\' ELSE TRIM(TO_CHAR("{col_upper}", \'{fmt}\')) END'
                    else:
                        # precision/scale 없는 NUMBER → fallback TM9
                        col_expr = f'CASE WHEN "{col_upper}" IS NULL THEN \'<NULL>\' ELSE TRIM(TO_CHAR("{col_upper}", \'TM9\')) END'
                else:
                    # 기타 타입: TO_CHAR + TRIM (문자열 공백 통일)
                    col_expr = f'CASE WHEN "{col_upper}" IS NULL THEN \'<NULL>\' ELSE TRIM(TO_CHAR("{col_upper}")) END'
                
                # 각 컬럼을 CAST로 감싸서 타입 안정성 확보
                col_parts.append(f'CAST({col_expr} AS VARCHAR2(4000))')
            
            # 컬럼 결합 시 '|' 구분자 필수 (Oracle/Postgres 동일)
            col_concat = " || '|' || ".join(col_parts)
            
            # 테이블명 구성 (파티션 범위 SQL 지원)
            if partition_name:
                # Oracle: PARTITION 절 사용
                full_table = f'"{schema}"."{table_name}" PARTITION("{partition_name.upper()}")'
            else:
                full_table = f'"{schema}"."{table_name}"'
            
            # WHERE 절 구성
            where_sql = f"WHERE {where_clause}" if where_clause else ""
            
            # STANDARD_HASH 입력을 AL32UTF8로 고정, RAWTOHEX는 LOWER로 통일
            sql = f"""
                SELECT LOWER(RAWTOHEX(STANDARD_HASH(CONVERT({col_concat}, 'AL32UTF8'), 'SHA256'))) as row_hash
                FROM {full_table}
                {where_sql}
            """
            result = self.oracle_db.execute_query(sql)
            if not result or len(result) == 0:
                return None
            # DB에서 받은 값만 사용. CLOB 등은 문자열로 변환, 개행/공백 제거(strip)
            row_hash_list = []
            for row in result:
                v = row[0]
                s = v.read() if hasattr(v, 'read') else str(v)
                if s is not None:
                    s = s.strip()
                row_hash_list.append(s or '')
            return _chunk_hash_from_row_hashes(row_hash_list)
            
        except Exception as e:
            logger.error(f"Oracle hash calculation failed: {e}")
            return None
    
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
        # 예: "ITM_NO" -> "itm_no"
        import re
        # "COLUMN_NAME" 패턴을 찾아서 소문자로 변환
        def lower_quoted_column(match):
            return match.group(0).lower()
        
        # "COLUMN_NAME" 패턴 매칭 및 변환
        pg_where = re.sub(r'"([A-Z_][A-Z0-9_]*)"', lambda m: f'"{m.group(1).lower()}"', where_clause)
        return pg_where
    
    def calculate_postgres_hash(self, table_name: str, schema: str,
                               columns: list, where_clause: str = "") -> Optional[str]:
        """
        PostgreSQL에서 pgcrypto digest(..., 'sha256')를 사용하여 해시 계산 (PK 기반 ORDER BY).
        encode(digest(..., 'sha256'), 'hex')로 Oracle STANDARD_HASH(..., 'SHA256')와 동일 hex 출력.
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            columns: 컬럼 리스트
            where_clause: WHERE 절 조건
        
        Returns:
            해시값 또는 None
        """
        try:
            # 컬럼 메타데이터 조회 (타입별 정규화를 위해)
            try:
                # PostgreSQL information_schema에서 컬럼 타입 조회
                col_list = "', '".join([col.lower() for col in columns])
                metadata_sql = f"""
                    SELECT column_name, data_type, numeric_precision, numeric_scale
                    FROM information_schema.columns
                    WHERE table_schema = %(schema)s
                      AND table_name = %(table_name)s
                      AND column_name IN ('{col_list}')
                """
                metadata_result = self.postgres_db.execute_query(metadata_sql, {
                    'schema': schema.lower(),
                    'table_name': table_name.lower()
                })
                metadata = {row[0]: {'data_type': row[1], 'precision': row[2], 'scale': row[3]} 
                           for row in metadata_result} if metadata_result else {}
            except Exception as e:
                logger.warning(f"Failed to get PostgreSQL metadata: {e}")
                metadata = {}
            
            # Canonicalization + quoted identifier는 반드시 소문자 (Postgres case-sensitive)
            col_parts = []
            for col in columns:
                c = col.lower()
                col_meta = metadata.get(c, {})
                data_type = col_meta.get('data_type', '').upper() if col_meta else ''
                
                if not data_type:
                    col_expr = f"CASE WHEN \"{c}\" IS NULL THEN '<NULL>' WHEN \"{c}\"::text = '' THEN '<EMPTY>' ELSE TRIM(\"{c}\"::text) END"
                elif data_type in ('BYTEA'):
                    col_expr = f"CASE WHEN \"{c}\" IS NULL THEN '<NULL>' ELSE 'LEN:' || LENGTH(\"{c}\") || ':' || SUBSTR(ENCODE(\"{c}\", 'hex'), 1, 100) END"
                elif data_type in ('TEXT', 'VARCHAR', 'CHAR', 'CHARACTER VARYING', 'CHARACTER'):
                    col_expr = f"CASE WHEN \"{c}\" IS NULL THEN '<NULL>' WHEN \"{c}\"::text = '' THEN '<EMPTY>' ELSE TRIM(\"{c}\"::text) END"
                elif data_type in ('DATE', 'TIMESTAMP WITHOUT TIME ZONE', 'TIMESTAMP WITH TIME ZONE'):
                    col_expr = f"CASE WHEN \"{c}\" IS NULL THEN '<NULL>' ELSE TO_CHAR(\"{c}\", 'YYYY-MM-DD\"T\"HH24:MI:SS') END"
                elif data_type in ('NUMERIC', 'DECIMAL', 'INTEGER', 'BIGINT', 'SMALLINT', 'REAL', 'DOUBLE PRECISION'):
                    # NUMBER: SCALE 기반 고정 표현으로 Oracle과 동일 문자열 보장 (SHA256 해시 일치)
                    p = col_meta.get('precision') if col_meta else None
                    s = col_meta.get('scale') if col_meta else None
                    if p is not None and isinstance(p, str):
                        try:
                            p = int(p)
                        except (ValueError, TypeError):
                            p = None
                    if s is not None and isinstance(s, str):
                        try:
                            s = int(s)
                        except (ValueError, TypeError):
                            s = None

                    if (data_type in ('NUMERIC', 'DECIMAL') and s is not None and s > 0 and p is not None and p > 0):
                        int_places = max(0, p - s)
                        fmt = 'FM' + ('9' * int_places) + '0.' + ('0' * s)
                        col_expr = f"CASE WHEN \"{c}\" IS NULL THEN '<NULL>' ELSE TRIM(TO_CHAR(\"{c}\"::numeric({p},{s}), '{fmt}')) END"
                    elif s == 0 or (s is None and data_type in ('INTEGER', 'BIGINT', 'SMALLINT')):
                        if p and p > 0:
                            format_str = 'FM' + '9' * min(p, 38)
                        else:
                            format_str = 'FM99999999999999999999999999999999999999'
                        col_expr = f"CASE WHEN \"{c}\" IS NULL THEN '<NULL>' ELSE TRIM(TO_CHAR(\"{c}\", '{format_str}')) END"
                    else:
                        if p and s is not None and s > 0:
                            int_part = min((p - s) if p else 38, 38)
                            frac_part = min(s, 9)
                            format_str = f'FM{"9" * int_part}.{"9" * frac_part}'
                        else:
                            format_str = 'FM999999999999999999.999999999'
                        col_expr = f"CASE WHEN \"{c}\" IS NULL THEN '<NULL>' ELSE TRIM(TO_CHAR(\"{c}\", '{format_str}')) END"
                else:
                    col_expr = f"CASE WHEN \"{c}\" IS NULL THEN '<NULL>' ELSE TRIM(\"{c}\"::text) END"
                
                col_parts.append(col_expr)
            
            # 컬럼 결합 시 '|' 구분자 필수 (Oracle과 동일)
            col_concat = " || '|' || ".join(col_parts)
            
            # PostgreSQL 스키마는 일반적으로 소문자로 처리 (data_migrator.py와 동일한 방식)
            # unquoted identifier로 사용 (data_migrator.py와 일치)
            pg_schema = schema.lower()
            
            # 파티션 범위 SQL 지원
            if where_clause and 'PARTITION' not in where_clause.upper():
                # 파티션명이 WHERE 절에 포함되어 있지 않으면 일반 테이블
                pg_table = table_name.lower()
            else:
                # 파티션명이 있으면 Child 테이블 직접 접근
                # WHERE 절에서 파티션명 추출 시도 (구현 필요 시 추가)
                pg_table = table_name.lower()
            
            # WHERE 절 구성 (PostgreSQL 형식으로 변환)
            pg_where_clause = self._convert_where_clause_to_postgres(where_clause)
            where_sql = f"WHERE {pg_where_clause}" if pg_where_clause else ""
            
            # row_hash만 행 단위로 조회 (chunk_hash는 Python에서 소문자 통일·정렬 후 계산)
            sql = f"""
                SELECT encode(digest({col_concat}, 'sha256'), 'hex') as row_hash
                FROM {pg_schema}.{pg_table}
                {where_sql}
            """
            result = self.postgres_db.execute_query(sql)
            if not result or len(result) == 0:
                return None
            # DB에서 받은 값만 사용. 개행/공백 제거(strip)
            row_hash_list = [str(row[0]).strip() if row[0] is not None else '' for row in result]
            return _chunk_hash_from_row_hashes(row_hash_list)
            
        except Exception as e:
            logger.error(f"PostgreSQL hash calculation failed: {e}")
            return None
    
    def compare_chunk_hash(self, table_name: str, schema: str,
                          columns: list, where_clause: str = "",
                          partition_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Chunk 단위 해시 비교
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            columns: 컬럼 리스트
            where_clause: WHERE 절 조건
            partition_name: 파티션명 (옵션)
        
        Returns:
            비교 결과 딕셔너리
        """
        oracle_hash = self.calculate_oracle_hash(
            table_name, schema, columns, where_clause, partition_name
        )
        postgres_hash = self.calculate_postgres_hash(
            table_name, schema, columns, where_clause
        )
        
        match = (oracle_hash == postgres_hash) if (oracle_hash and postgres_hash) else False
        
        return {
            'match': match,
            'oracle_hash': oracle_hash,
            'postgres_hash': postgres_hash,
            'hash_algo': 'sha256',
            'table_name': table_name,
            'schema': schema,
            'where_clause': where_clause,
            'partition_name': partition_name
        }
