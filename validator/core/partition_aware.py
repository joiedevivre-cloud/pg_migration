"""
파티션 인식 검증 모듈
로그/시계열 테이블을 위한 파티션별 검증 전략 관리
"""
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, date
from db.oracle import OracleDB
from db.postgres import PostgresDB

logger = logging.getLogger(__name__)


class PartitionAwareValidator:
    """파티션 인식 검증 클래스"""
    
    def __init__(self, oracle_db: OracleDB, postgres_db: PostgresDB):
        self.oracle_db = oracle_db
        self.postgres_db = postgres_db
    
    def get_partition_metadata(self, table_name: str, schema: str) -> Dict[str, Any]:
        """
        Oracle과 PostgreSQL에서 파티션 메타데이터 통합 조회
        
        Args:
            table_name: 테이블명
            schema: 스키마명
        
        Returns:
            통합 파티션 메타데이터
        """
        # Oracle 파티션 정보
        oracle_partition_info = self.oracle_db.get_partition_info(table_name, schema)
        
        # PostgreSQL 파티션 정보
        pg_schema = schema.lower()
        pg_table = table_name.lower()
        postgres_partition_info = self.postgres_db.get_partition_info(pg_table, pg_schema)
        
        # 통합
        if oracle_partition_info and postgres_partition_info:
            # 두 DB의 파티션 정보를 매칭
            oracle_partitions = oracle_partition_info.get('partitions', [])
            postgres_partitions = postgres_partition_info.get('partitions', [])
            
            # 파티션명 기준으로 매칭
            matched_partitions = []
            for ora_part in oracle_partitions:
                ora_name = ora_part['partition_name'].lower()
                pg_part = next(
                    (p for p in postgres_partitions if p['partition_name'].lower() == ora_name),
                    None
                )
                
                if pg_part:
                    matched_partitions.append({
                        'name': ora_name,
                        'range': ora_part.get('range', pg_part.get('range', [None, None])),
                        'oracle_info': ora_part,
                        'postgres_info': pg_part,
                        'partition_key_columns': ora_part.get('partition_key_columns', [])
                    })
            
            return {
                'is_partitioned': True,
                'partition_count': len(matched_partitions),
                'partitions': matched_partitions,
                'partition_key_columns': oracle_partitions[0].get('partition_key_columns', []) if oracle_partitions else []
            }
        elif oracle_partition_info:
            # Oracle만 파티션 정보 있음
            return {
                'is_partitioned': True,
                'partition_count': oracle_partition_info['partition_count'],
                'partitions': [
                    {
                        'name': p['partition_name'].lower(),
                        'range': p.get('range', [None, None]),
                        'oracle_info': p,
                        'postgres_info': None,
                        'partition_key_columns': p.get('partition_key_columns', [])
                    }
                    for p in oracle_partition_info.get('partitions', [])
                ],
                'partition_key_columns': oracle_partition_info.get('partitions', [{}])[0].get('partition_key_columns', []) if oracle_partition_info.get('partitions') else []
            }
        else:
            return {
                'is_partitioned': False,
                'partition_count': 0,
                'partitions': []
            }
    
    def determine_partition_strategy(self, partition: Dict[str, Any],
                                    partition_key: str,
                                    immutable_before: Optional[str] = None) -> str:
        """
        파티션별 검증 전략 결정
        
        Args:
            partition: 파티션 정보
            partition_key: 파티션 키 컬럼명
            immutable_before: 불변 기준 날짜 (YYYY-MM-DD 형식)
        
        Returns:
            'light': 경량 검증 (오래된 파티션)
            'full': 전체 검증 (최근 파티션)
        """
        if not immutable_before:
            # immutable_before가 설정되지 않으면 전체 검증
            return 'full'
        
        try:
            # 범위 종료 날짜 확인
            range_end = partition.get('range', [None, None])[1]
            
            if range_end is None:
                return 'full'  # 범위를 알 수 없으면 전체 검증
            
            # 날짜 비교
            if isinstance(range_end, (datetime, date)):
                range_end_date = range_end.date() if isinstance(range_end, datetime) else range_end
            elif isinstance(range_end, str):
                # 문자열을 날짜로 변환
                range_end_date = datetime.strptime(range_end, '%Y-%m-%d').date()
            else:
                return 'full'  # 날짜가 아니면 전체 검증
            
            immutable_date = datetime.strptime(immutable_before, '%Y-%m-%d').date()
            
            if range_end_date < immutable_date:
                return 'light'  # 불변 파티션
            else:
                return 'full'  # 최근 파티션
        except Exception as e:
            logger.warning(f"Failed to determine partition strategy: {e}, using 'full'")
            return 'full'
    
    def create_partition_validation_tasks(self, table_name: str, schema: str,
                                         partition_key: Optional[str] = None,
                                         immutable_before: Optional[str] = None,
                                         base_where_clause: str = "") -> List[Dict[str, Any]]:
        """
        파티션별 검증 작업 생성 (파티션 인식 모드)
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            partition_key: 파티션 키 컬럼명
            immutable_before: 불변 기준 날짜
            base_where_clause: 기본 WHERE 절
        
        Returns:
            파티션별 검증 작업 리스트
        """
        partition_metadata = self.get_partition_metadata(table_name, schema)
        
        if not partition_metadata.get('is_partitioned'):
            # 파티션 테이블이 아니면 일반 검증 작업 반환
            return [{
                'table_name': table_name,
                'schema': schema,
                'partition_name': None,
                'where_clause': base_where_clause,
                'is_partitioned': False,
                'strategy': 'full'
            }]
        
        tasks = []
        partitions = partition_metadata.get('partitions', [])
        
        # 파티션 키 자동 감지
        if not partition_key and partitions:
            partition_key_columns = partitions[0].get('partition_key_columns', [])
            if partition_key_columns:
                partition_key = partition_key_columns[0]
        
        for partition in partitions:
            partition_name = partition['name']
            
            # 검증 전략 결정
            strategy = self.determine_partition_strategy(
                partition, partition_key or '', immutable_before
            )
            
            # 파티션 범위 WHERE 절 생성
            partition_where = self._build_partition_where_clause(
                partition, partition_key, base_where_clause
            )
            
            tasks.append({
                'table_name': table_name,
                'schema': schema,
                'partition_name': partition_name,
                'where_clause': partition_where,
                'is_partitioned': True,
                'strategy': strategy,
                'partition_info': partition,
                'partition_key': partition_key
            })
        
        return tasks
    
    def _build_partition_where_clause(self, partition: Dict[str, Any],
                                     partition_key: Optional[str],
                                     base_where_clause: str = "") -> str:
        """
        파티션 범위 WHERE 절 생성
        
        Args:
            partition: 파티션 정보
            partition_key: 파티션 키 컬럼명
            base_where_clause: 기본 WHERE 절
        
        Returns:
            WHERE 절 문자열
        """
        if not partition_key:
            return base_where_clause
        
        range_start, range_end = partition.get('range', [None, None])
        
        conditions = []
        
        # 범위 조건 추가
        if range_start is not None:
            if isinstance(range_start, (datetime, date)):
                start_str = range_start.strftime('%Y-%m-%d')
            else:
                start_str = str(range_start)
            conditions.append(f'"{partition_key.upper()}" >= DATE \'{start_str}\'')
        
        if range_end is not None:
            if isinstance(range_end, (datetime, date)):
                end_str = range_end.strftime('%Y-%m-%d')
            else:
                end_str = str(range_end)
            conditions.append(f'"{partition_key.upper()}" < DATE \'{end_str}\'')
        
        partition_where = " AND ".join(conditions) if conditions else ""
        
        # 기본 WHERE 절과 결합
        if base_where_clause and partition_where:
            return f"({base_where_clause}) AND ({partition_where})"
        elif base_where_clause:
            return base_where_clause
        elif partition_where:
            return partition_where
        else:
            return ""
    
    def build_partition_scoped_sql(self, sql_template: str, table_name: str, schema: str,
                                   partition_name: Optional[str],
                                   db_type: str = 'oracle') -> str:
        """
        파티션 범위 SQL 생성
        
        Args:
            sql_template: SQL 템플릿 (테이블명 부분을 {table}로 표시)
            table_name: 테이블명
            schema: 스키마명
            partition_name: 파티션명
            db_type: 'oracle' 또는 'postgres'
        
        Returns:
            파티션 범위 SQL
        """
        if not partition_name:
            # 파티션이 없으면 일반 SQL
            if db_type == 'oracle':
                full_table = f'"{schema}"."{table_name}"'
            else:
                full_table = f'{schema.lower()}.{table_name.lower()}'
            return sql_template.format(table=full_table)
        
        # 파티션 범위 SQL
        if db_type == 'oracle':
            # Oracle: PARTITION 절 사용
            full_table = f'"{schema}"."{table_name}" PARTITION("{partition_name.upper()}")'
        else:
            # PostgreSQL: Child 테이블 직접 접근
            full_table = f'{schema.lower()}.{partition_name.lower()}'
        
        return sql_template.format(table=full_table)
