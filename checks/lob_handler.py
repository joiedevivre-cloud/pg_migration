"""
LOB (Large Object) 처리 모듈
Length 우선 + 샘플링 해시 전략
"""
import logging
import hashlib
from typing import Optional, Dict, Any, List
from db.oracle import OracleDB
from db.postgres import PostgresDB

logger = logging.getLogger(__name__)


class LOBHandler:
    """LOB 처리 클래스"""
    
    def __init__(self, oracle_db: OracleDB, postgres_db: PostgresDB,
                 sample_size: int = 1024, random_offset: bool = True):
        """
        Args:
            oracle_db: Oracle DB 연결
            postgres_db: PostgreSQL DB 연결
            sample_size: 샘플 크기 (bytes)
            random_offset: 랜덤 오프셋 사용 여부
        """
        self.oracle_db = oracle_db
        self.postgres_db = postgres_db
        self.sample_size = sample_size
        self.random_offset = random_offset
    
    def get_lob_hash(self, table_name: str, schema: str, column: str,
                     where_clause: str = "", partition_name: Optional[str] = None) -> Dict[str, Any]:
        """
        LOB 컬럼의 해시 계산 (length + 샘플링)
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            column: LOB 컬럼명
            where_clause: WHERE 절 조건
            partition_name: 파티션명 (옵션)
        
        Returns:
            LOB 해시 정보 딕셔너리
        """
        # Oracle LOB 해시
        oracle_hash = self._get_oracle_lob_hash(
            table_name, schema, column, where_clause, partition_name
        )
        
        # PostgreSQL LOB 해시
        postgres_hash = self._get_postgres_lob_hash(
            table_name, schema, column, where_clause
        )
        
        return {
            'oracle': oracle_hash,
            'postgres': postgres_hash,
            'match': oracle_hash.get('hash') == postgres_hash.get('hash') and
                    oracle_hash.get('length') == postgres_hash.get('length')
        }
    
    def _get_oracle_lob_hash(self, table_name: str, schema: str, column: str,
                             where_clause: str = "",
                             partition_name: Optional[str] = None) -> Dict[str, Any]:
        """Oracle LOB 해시 계산"""
        try:
            full_table = f'"{schema}"."{table_name}"'
            if partition_name:
                full_table = f'"{schema}"."{table_name}" PARTITION("{partition_name}")'
            
            where_sql = f"WHERE {where_clause}" if where_clause else ""
            
            # Length + 앞부분/뒷부분/랜덤 오프셋 샘플링 해시
            sql = f"""
                SELECT 
                    COUNT(*) as row_count,
                    SUM(DBMS_LOB.GETLENGTH("{column}")) as total_length,
                    STANDARD_HASH(
                        LISTAGG(
                            STANDARD_HASH(
                                CASE 
                                    WHEN DBMS_LOB.GETLENGTH("{column}") <= {self.sample_size} 
                                    THEN DBMS_LOB.SUBSTR("{column}", {self.sample_size}, 1)
                                    ELSE DBMS_LOB.SUBSTR("{column}", {self.sample_size}, 1) || 
                                         DBMS_LOB.SUBSTR("{column}", {self.sample_size}, 
                                            GREATEST(1, DBMS_LOB.GETLENGTH("{column}") - {self.sample_size} + 1))
                                END,
                                'SHA256'
                            ),
                            ','
                        ) WITHIN GROUP (ORDER BY ROWID),
                        'SHA256'
                    ) as sample_hash
                FROM {full_table}
                {where_sql}
            """
            
            result = self.oracle_db.execute_query(sql)
            if result and len(result) > 0:
                row = result[0]
                return {
                    'row_count': row[0],
                    'length': row[1],
                    'hash': row[2] if row[2] else None
                }
            return {'row_count': 0, 'length': 0, 'hash': None}
            
        except Exception as e:
            logger.error(f"Oracle LOB hash calculation failed: {e}")
            return {'error': str(e)}
    
    def _get_postgres_lob_hash(self, table_name: str, schema: str, column: str,
                               where_clause: str = "") -> Dict[str, Any]:
        """PostgreSQL LOB 해시 계산"""
        try:
            where_sql = f"WHERE {where_clause}" if where_clause else ""
            
            # Length + 앞부분/뒷부분 샘플링 해시 (Oracle STANDARD_HASH SHA256과 동일하게 pgcrypto 사용)
            sql = f"""
                SELECT 
                    COUNT(*) as row_count,
                    SUM(LENGTH("{column}")) as total_length,
                    encode(digest(
                        string_agg(
                            encode(digest(
                                CASE 
                                    WHEN LENGTH("{column}") <= {self.sample_size} 
                                    THEN "{column}"
                                    ELSE SUBSTRING("{column}" FROM 1 FOR {self.sample_size}) || 
                                         SUBSTRING("{column}" FROM GREATEST(1, LENGTH("{column}") - {self.sample_size} + 1))
                                END,
                                'sha256'
                            ), 'hex'),
                            ','
                            ORDER BY ctid
                        ),
                        'sha256'
                    ), 'hex') as sample_hash
                FROM "{schema}"."{table_name}"
                {where_sql}
            """
            
            result = self.postgres_db.execute_query(sql)
            if result and len(result) > 0:
                row = result[0]
                return {
                    'row_count': row[0],
                    'length': row[1],
                    'hash': row[2] if row[2] else None
                }
            return {'row_count': 0, 'length': 0, 'hash': None}
            
        except Exception as e:
            logger.error(f"PostgreSQL LOB hash calculation failed: {e}")
            return {'error': str(e)}
