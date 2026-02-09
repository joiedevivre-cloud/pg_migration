"""
Validation engine: parallel load control (Semaphore), drill-down (chunk hash then mismatch-only detail), PK-based chunks with keyset fallback.
"""
import fnmatch
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore
from typing import List, Dict, Any, Optional, Callable
from checks.chunk_hash import ChunkHashValidator
from checks.row_decimal import RowDecimalValidator
from checks.aggregate_validation import AggregateValidator
from checks.row_canonicalize import RowCanonicalizer, NullEmptyPolicy
from db.oracle import OracleDB
from db.postgres import PostgresDB
from db.chunk_strategy import ChunkStrategy
from config.profile_loader import ProfileLoader
from validator.core.report import ReportGenerator

logger = logging.getLogger(__name__)


class ValidationEngine:
    """Validation engine."""
    
    def __init__(self, oracle_db: OracleDB, postgres_db: PostgresDB,
                 max_workers: int = 5, max_concurrent_db_sessions: int = 10,
                 chunk_size: int = 75,
                 chunk_size_by_pattern: Optional[Dict[str, int]] = None,
                 pk_columns: Optional[List[str]] = None,
                 null_empty_policy: NullEmptyPolicy = NullEmptyPolicy.DISTINCT,
                 decimal_tolerance: float = 0.0001,
                 profile_loader: Optional[ProfileLoader] = None):
        """
        Args:
            oracle_db: Oracle DB connection.
            postgres_db: PostgreSQL DB connection.
            max_workers: Max worker threads.
            max_concurrent_db_sessions: Max concurrent DB sessions (Semaphore).
            chunk_size: Chunk size (default 75, avoid LISTAGG limit).
            chunk_size_by_pattern: Per-schema/pattern chunk size override (e.g. {"IMSI.%": 60}).
            pk_columns: Primary key column list.
            null_empty_policy: NULL vs empty string policy.
            decimal_tolerance: Decimal tolerance.
            profile_loader: Optional profile loader.
        """
        self.oracle_db = oracle_db
        self.postgres_db = postgres_db
        self.max_workers = max_workers
        self.session_semaphore = Semaphore(max_concurrent_db_sessions)
        self.chunk_size = chunk_size
        self.chunk_size_by_pattern = chunk_size_by_pattern or {}
        self.decimal_tolerance = decimal_tolerance
        self.tolerance_by_column = getattr(self, 'tolerance_by_column', {})
        self.profile_loader = profile_loader
        
        # Row 정규화 객체
        canonicalizer = RowCanonicalizer(
            null_empty_policy=null_empty_policy
        )
        
        # Validators
        self.chunk_validator = ChunkHashValidator(
            oracle_db, postgres_db, pk_columns=pk_columns,
            canonicalizer=canonicalizer
        )
        self.row_validator = RowDecimalValidator(oracle_db, postgres_db)
        self.aggregate_validator = AggregateValidator(
            oracle_db, postgres_db, decimal_tolerance=decimal_tolerance
        )
        
        # Chunk 전략 (config chunk_size 사용, 패턴별 오버라이드는 호출 시 적용)
        self.chunk_strategy = ChunkStrategy(oracle_db, chunk_size=chunk_size)
        
        # Report generator
        self.report_generator = ReportGenerator()
    
    def _effective_chunk_size(self, schema: str, table_name: str) -> int:
        """스키마/테이블에 적용할 Chunk 크기 (chunk_size_by_pattern 매칭 후 기본값)."""
        if not isinstance(self.chunk_size_by_pattern, dict):
            return self.chunk_size
        key = f"{schema}.{table_name}"
        for pattern, size in self.chunk_size_by_pattern.items():
            if fnmatch.fnmatch(key, pattern.replace("%", "*")):
                return size
        return self.chunk_size
    
    def validate_chunk_with_semaphore(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """
        Chunk validation with Semaphore-controlled DB sessions.

        Args:
            task: Validation task info.
        """
        # Limit concurrent DB sessions via Semaphore
        with self.session_semaphore:
            try:
                result = self.chunk_validator.compare_chunk_hash(
                    table_name=task['table_name'],
                    schema=task['schema'],
                    columns=task.get('columns', []),
                    where_clause=task.get('where_clause', ''),
                    partition_name=task.get('partition_name')
                )
                # Include task info for partition summary report
                result['task'] = task
                result['task'] = task
                return result
            except Exception as e:
                logger.error(f"Chunk validation failed for task {task}: {e}")
                return {
                    'match': False,
                    'error': str(e),
                    'task': task
                }
    
    def validate_row_decimal_with_semaphore(self, task: Dict[str, Any],
                                           decimal_tolerance: float = 0.0001) -> Dict[str, Any]:
        """
        Semaphore를 사용하여 DB 세션 점유를 제어하는 Row 단위 Decimal 검증
        
        Args:
            task: 검증 작업 정보
            decimal_tolerance: Decimal 허용 오차
        
        Returns:
            검증 결과
        """
        with self.session_semaphore:
            try:
                result = self.row_validator.compare_rows_with_tolerance(
                    table_name=task['table_name'],
                    schema=task['schema'],
                    columns=task.get('columns', []),
                    where_clause=task.get('where_clause', ''),
                    partition_name=task.get('partition_name'),
                    tolerance=decimal_tolerance
                )
                result['task'] = task
                return result
            except Exception as e:
                logger.error(f"Row decimal validation failed for task {task}: {e}")
                return {
                    'match': False,
                    'error': str(e),
                    'task': task
                }
    
    def validate_with_sampling(self, tasks: List[Dict[str, Any]],
                               decimal_tolerance: float = 0.0001,
                               sample_size: int = 10,
                               max_diffs_per_chunk: int = 10) -> List[Dict[str, Any]]:
        """
        3단계 검증 프로세스:
        1. 구간 검증 (Chunk Hash): PK 기준 10,000건 단위로 Hash 비교
        2. 샘플링 검증: 오류 구간 내에서 상위 10건만 Row-by-row 대조
        
        Args:
            tasks: 검증 작업 리스트
            decimal_tolerance: Decimal 허용 오차
            sample_size: 샘플링할 행 수 (기본 10건)
        
        Returns:
            검증 결과 리스트
        """
        results = []
        
        # Step 2: Chunk hash validation (10k rows per chunk)
        logger.info(f"2단계: 구간 검증 (Chunk Hash) - {len(tasks)}개 구간 검증 중...")
        chunk_results = self.validate_chunks_parallel(tasks)
        
        # 오류 구간 식별 (Hash 불일치 또는 계산 실패)
        error_chunks = [
            result for result in chunk_results
            if not result.get('match', False)  # match=False인 모든 경우
        ]
        
        if error_chunks:
            logger.info(f"3단계: 샘플링 검증 - {len(error_chunks)}개 오류 구간에서 상위 {sample_size}건씩 샘플링 검증")
            # Sample validation per error chunk
            for chunk_result in error_chunks:
                task = chunk_result.get('task')
                if not task:
                    continue
                # Sample: top sample_size rows per error chunk; early exit at max_diffs_per_chunk
                sample_result = self.validate_chunk_sample(
                    task, decimal_tolerance, sample_size, max_diffs_per_chunk
                )
                # 샘플링 결과를 chunk 결과에 병합
                chunk_result['sample_validation'] = sample_result
                chunk_result['sample_size'] = sample_size
                chunk_result['sampled_mismatches'] = sample_result.get('mismatches', [])
                results.append(chunk_result)
        else:
            logger.info("✅ 모든 구간 일치 - 샘플링 검증 불필요")
        
        # Include OK chunk results
        matched_results = [
            result for result in chunk_results
            if result.get('match', False)
        ]
        results.extend(matched_results)
        
        return results
    
    def validate_chunk_sample(self, task: Dict[str, Any],
                             decimal_tolerance: float = 0.0001,
                             sample_size: int = 10,
                             max_diffs_per_chunk: int = 10) -> Dict[str, Any]:
        """
        오류 구간 내에서 샘플링 검증 (상위 N건만 Row-by-row 검증)
        조기 종료: max_diffs_per_chunk에 도달하면 즉시 중단
        
        Args:
            task: 검증 작업 정보
            decimal_tolerance: Decimal 허용 오차
            sample_size: 샘플링할 행 수
            max_diffs_per_chunk: Chunk당 최대 불일치 허용 수 (조기 종료 임계값)
        
        Returns:
            샘플링 검증 결과
        """
        with self.session_semaphore:
            try:
                # PK 컬럼으로 정렬하여 상위 sample_size건만 조회
                pk_columns = task.get('pk_columns', [])
                if not pk_columns:
                    # No PK: full verification (with early exit)
                    return self.row_validator.compare_rows_with_tolerance_streaming(
                        table_name=task['table_name'],
                        schema=task['schema'],
                        columns=task.get('columns', []),
                        where_clause=task.get('where_clause', ''),
                        partition_name=task.get('partition_name'),
                        tolerance=decimal_tolerance,
                        max_diffs=max_diffs_per_chunk
                    )
                
                # PK 기준으로 상위 sample_size건만 조회
                pk_order_by = ", ".join([f'"{col.upper()}"' for col in pk_columns])
                
                # Oracle에서 샘플 조회
                oracle_sample = self._fetch_sample_rows(
                    'oracle', task, pk_order_by, sample_size
                )
                
                # Fetch sample from PostgreSQL
                postgres_sample = self._fetch_sample_rows(
                    'postgres', task, pk_order_by, sample_size
                )
                
                # 샘플 비교 (조기 종료 적용)
                mismatches = []
                diff_count = 0
                early_exit = False
                
                for idx, (ora_row, pg_row) in enumerate(zip(oracle_sample, postgres_sample)):
                    row_match, mismatch_details = self.row_validator._compare_row(
                        ora_row, pg_row, task.get('columns', []), decimal_tolerance
                    )
                    if not row_match:
                        diff_count += 1
                        mismatches.append({
                            'sample_index': idx,
                            'details': mismatch_details,
                            'oracle_row': dict(zip(task.get('columns', []), ora_row)),
                            'postgres_row': dict(zip(task.get('columns', []), pg_row))
                        })
                        
                        # Early exit at max_diffs_per_chunk
                        if diff_count >= max_diffs_per_chunk:
                            logger.info(f"조기 종료: {max_diffs_per_chunk}개 불일치 도달 (구간: {task.get('where_clause', '전체')})")
                            early_exit = True
                            break
                
                result = {
                    'match': len(mismatches) == 0,
                    'sample_size': len(oracle_sample),
                    'mismatch_count': len(mismatches),
                    'mismatches': mismatches,
                    'early_exit': early_exit,
                    'max_diffs_reached': diff_count >= max_diffs_per_chunk,
                    'note': f'상위 {sample_size}건 샘플링 검증 결과'
                }
                
                if early_exit:
                    result['note'] += f' (조기 종료: {max_diffs_per_chunk}개 불일치 도달)'
                
                return result
                
            except Exception as e:
                logger.error(f"Sample validation failed: {e}")
                return {
                    'match': False,
                    'error': str(e),
                    'note': '샘플링 검증 실패'
                }
    
    def _fetch_sample_rows(self, db_type: str, task: Dict[str, Any],
                          pk_order_by: str, sample_size: int) -> List[tuple]:
        """샘플 행 조회 (PK 기준 상위 N건)"""
        table_name = task['table_name']
        schema = task['schema']
        columns = task.get('columns', [])
        where_clause = task.get('where_clause', '')
        partition_name = task.get('partition_name')
        
        col_list = ", ".join([f'"{col.upper()}"' for col in columns])
        
        if db_type == 'oracle':
            full_table = f'"{schema}"."{table_name}"'
            if partition_name:
                full_table = f'"{schema}"."{table_name}" PARTITION("{partition_name}")'
            
            where_sql = f"WHERE {where_clause}" if where_clause else ""
            sql = f"""
                SELECT {col_list}
                FROM {full_table}
                {where_sql}
                ORDER BY {pk_order_by}
                FETCH FIRST {sample_size} ROWS ONLY
            """
            result = self.oracle_db.execute_query(sql)
            return result if result else []
        else:  # postgres
            pg_schema = schema.lower()
            
            # 파티션 범위 SQL 지원
            if partition_name:
                # PostgreSQL: access child table directly
                pg_table = partition_name.lower()
            else:
                pg_table = table_name.lower()
            
            where_sql = f"WHERE {where_clause.lower()}" if where_clause else ""
            
            col_list_pg = ", ".join([f'"{col.lower()}"' for col in columns])
            pk_order_by_pg = ", ".join([f'"{col.lower()}"' for col in task.get('pk_columns', [])])
            
            sql = f"""
                SELECT {col_list_pg}
                FROM {pg_schema}.{pg_table}
                {where_sql}
                ORDER BY {pk_order_by_pg}
                LIMIT {sample_size}
            """
            result = self.postgres_db.execute_query(sql)
            return result if result else []
    
    def validate_chunks_parallel(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Chunk 검증을 병렬로 실행
        
        Args:
            tasks: 검증 작업 리스트
        
        Returns:
            검증 결과 리스트
        """
        results = []
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            future_to_task = {
                executor.submit(self.validate_chunk_with_semaphore, task): task
                for task in tasks
            }
            
            # 완료된 작업부터 결과 수집
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    result = future.result()
                    results.append(result)
                    logger.debug(f"Chunk validation completed for {task.get('table_name')}")
                except Exception as e:
                    logger.error(f"Chunk validation failed for task {task}: {e}")
                    results.append({
                        'match': False,
                        'error': str(e),
                        'task': task
                    })
        
        return results
    
    def validate_rows_parallel(self, tasks: List[Dict[str, Any]],
                               decimal_tolerance: float = 0.0001) -> List[Dict[str, Any]]:
        """
        Row 단위 검증을 병렬로 실행
        
        Args:
            tasks: 검증 작업 리스트
            decimal_tolerance: Decimal 허용 오차
        
        Returns:
            검증 결과 리스트
        """
        results = []
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            future_to_task = {
                executor.submit(
                    self.validate_row_decimal_with_semaphore,
                    task,
                    decimal_tolerance
                ): task
                for task in tasks
            }
            
            # 완료된 작업부터 결과 수집
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    result = future.result()
                    results.append(result)
                    logger.debug(f"Row validation completed for {task.get('table_name')}")
                except Exception as e:
                    logger.error(f"Row validation failed for task {task}: {e}")
                    results.append({
                        'match': False,
                        'error': str(e),
                        'task': task
                    })
        
        return results
    
    def validate_table(self, table_name: str, schema: str,
                      columns: Optional[List[str]] = None,
                      base_where_clause: str = "",
                      generate_report: bool = True) -> Dict[str, Any]:
        """
        테이블 검증 실행 (파티션 대응 포함)
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            columns: 컬럼 리스트 (None이면 프로파일에서 로드)
            base_where_clause: 기본 WHERE 절 조건
            generate_report: JSON 리포트 생성 여부
        
        Returns:
            검증 결과 딕셔너리 (리포트 경로 포함)
        """
        # Load config from profile; clear PK from previous table
        self.chunk_validator.pk_columns = []
        
        if self.profile_loader:
            profile_columns = self.profile_loader.get_table_columns(schema, table_name)
            if profile_columns:
                columns = profile_columns or columns
            
            profile_pk = self.profile_loader.get_table_pk_columns(schema, table_name)
            if profile_pk:
                self.chunk_validator.pk_columns = profile_pk
        
        # PK 컬럼 조회 (프로파일에서 없으면 Oracle에서 조회)
        pk_columns = self.chunk_validator.pk_columns
        if not pk_columns:
            try:
                pk_columns = self.oracle_db.get_primary_key_columns(table_name, schema)
                if pk_columns:
                    self.chunk_validator.pk_columns = pk_columns
                    logger.info(f"Auto-loaded PK columns for {schema}.{table_name}: {pk_columns}")
                else:
                    logger.warning(f"No PK columns found for {schema}.{table_name} - will use full table scan")
            except Exception as e:
                logger.warning(f"Failed to get PK columns for {schema}.{table_name}: {e}")
                pk_columns = []
        
        # 복합 PK 여부 확인
        is_composite_pk = len(pk_columns) > 1 if pk_columns else False
        
        if not columns:
            logger.warning(f"No columns specified for {schema}.{table_name}")
            return {'error': 'No columns specified'}
        
        # Check partition-aware mode
        partition_aware_config = None
        if self.profile_loader:
            partition_aware_config = self.profile_loader.get_table_config(
                schema, table_name, 'validation_mode'
            )
        
        # 파티션 단위로 검증 작업 생성
        if partition_aware_config and partition_aware_config.get('type') == 'partition_aware':
            # Partition-aware mode
            from validator.core.partition_aware import PartitionAwareValidator
            partition_validator = PartitionAwareValidator(self.oracle_db, self.postgres_db)
            
            partition_key = partition_aware_config.get('partition_key')
            immutable_before = partition_aware_config.get('immutable_before')
            
            partition_tasks = partition_validator.create_partition_validation_tasks(
                table_name, schema, partition_key, immutable_before, base_where_clause
            )
            logger.info(f"Partition-aware validation enabled for {schema}.{table_name}")
        else:
            # 기본 파티션 처리
            partition_tasks = self.oracle_db.create_partition_validation_tasks(
                table_name, schema, base_where_clause
            )
        
        # Create chunks (composite PK forces keyset)
        effective_chunk_size = self._effective_chunk_size(schema, table_name)
        tasks = []
        for partition_task in partition_tasks:
            partition_where = partition_task.get('where_clause', base_where_clause)
            
            if pk_columns:
                # Composite PK: keyset only
                if is_composite_pk:
                    logger.info(f"Composite PK detected for {schema}.{table_name}, using keyset pagination")
                    chunks = self.chunk_strategy.create_chunks_by_keyset(
                        table_name, schema, pk_columns, partition_where,
                        chunk_size_override=effective_chunk_size
                    )
                else:
                    # 단일 PK인 경우 BETWEEN 시도, 실패 시 keyset
                    chunks = self.chunk_strategy.create_chunks(
                        table_name, schema, pk_columns, partition_where,
                        chunk_size_override=effective_chunk_size
                    )
                
                # Chunk to task
                for chunk in chunks:
                    task = {
                        'table_name': table_name,
                        'schema': schema,
                        'columns': columns,
                        'where_clause': chunk.get('where_clause', partition_where),
                        'partition_name': partition_task.get('partition_name'),
                        'pk_columns': pk_columns,
                        'is_composite_pk': is_composite_pk,
                        'strategy': chunk.get('strategy', 'keyset'),
                        'partition_strategy': partition_task.get('strategy', 'full'),  # 파티션별 검증 전략
                        'partition_info': partition_task.get('partition_info')  # 파티션 상세 정보
                    }
                    tasks.append(task)
            else:
                # No PK: partition-level only
                task = {
                    'table_name': table_name,
                    'schema': schema,
                    'columns': columns,
                    'where_clause': partition_where,
                    'partition_name': partition_task.get('partition_name'),
                    'pk_columns': [],
                    'is_composite_pk': False,
                    'partition_strategy': partition_task.get('strategy', 'full'),
                    'partition_info': partition_task.get('partition_info')
                }
                tasks.append(task)
        
        # 1단계: 집계 검증 (SUM, AVG, COUNT) - 먼저 실행
        logger.info("=" * 80)
        logger.info("1단계: 집계 검증 (Aggregated) - SUM, AVG, COUNT 비교")
        logger.info("=" * 80)
        # Per-partition aggregate when partition-aware; full-table aggregate once
        aggregate_results = self.aggregate_validator.validate_aggregates(
            table_name, schema, columns, base_where_clause
        )
        
        # 집계 검증 실패 시 경고
        if not aggregate_results.get('match', False):
            logger.warning("⚠️ 집계 검증 실패 - numeric 정밀도 설정을 확인하세요")
            # Log mismatch columns
            for col, result in aggregate_results.get('columns', {}).items():
                if not result.get('match', False):
                    logger.warning(f"  - {col}: 불일치")
                    comp = result.get('comparison', {})
                    if 'count' in comp and not comp['count']:
                        logger.warning(f"    COUNT 불일치: Oracle={comp.get('oracle_count')}, PG={comp.get('postgres_count')}")
                    if 'sum' in comp and not comp['sum']:
                        logger.warning(f"    SUM 불일치: Oracle={comp.get('oracle_sum')}, PG={comp.get('postgres_sum')}")
        else:
            logger.info("✅ 집계 검증 통과")
        
        # 2단계: 구간 검증 (Chunk Hash) - 10,000건 단위
        logger.info("=" * 80)
        logger.info("2단계: 구간 검증 (Chunk Hash) - 10,000건 단위 Hash 비교")
        logger.info("=" * 80)
        
        # Chunk hash for all partitions
        validation_results = self.validate_chunks_parallel(tasks)
        
        # 파티션별 검증 전략에 따라 필터링
        # 경량 검증(light) 파티션은 Row-level drilldown 건너뜀
        tasks_for_drilldown = []
        for task in tasks:
            partition_strategy = task.get('partition_strategy', 'full')
            if partition_strategy == 'full':
                # Full strategy only: row-level drilldown
                tasks_for_drilldown.append(task)
        
        # 3단계: 샘플링 검증 (오류 구간에서만, 전체 검증 파티션만)
        logger.info("=" * 80)
        logger.info("3단계: 샘플링 검증 (Sampling) - 오류 구간에서 상위 10건만 Row-by-row 검증")
        logger.info("=" * 80)
        
        # Filter to mismatch only
        mismatch_tasks = [
            task for task, result in zip(tasks, validation_results)
            if not result.get('match', False) and 'error' not in result
        ]
        
        # 전체 검증 전략 파티션만 drilldown 수행
        drilldown_tasks = [
            task for task in mismatch_tasks
            if task.get('partition_strategy', 'full') == 'full'
        ]
        
        if drilldown_tasks:
            max_diffs = getattr(self, 'max_diffs_per_chunk', 10)
            drilldown_results = self.validate_with_sampling(
                drilldown_tasks, self.decimal_tolerance, sample_size=10, max_diffs_per_chunk=max_diffs
            )
            
            # Merge drilldown into validation_results
            drilldown_dict = {id(task): result for task, result in zip(drilldown_tasks, drilldown_results)}
            for i, task in enumerate(tasks):
                if id(task) in drilldown_dict:
                    validation_results[i]['drilldown'] = drilldown_dict[id(task)]
        else:
            logger.info("No full-verification partitions require drilldown")
        
        # DDL 자동 생성 (통계 정보 기반 + Oracle 메타데이터)
        from checks.aggregate import AggregateStatsCollector
        from validator.core.result import ResultProcessor
        
        ddl_report = None
        try:
            # Get Oracle column metadata (precision, scale)
            oracle_metadata = self.oracle_db.get_column_metadata(table_name, schema, columns)
            oracle_precision = {
                col: meta.get('data_precision')
                for col, meta in oracle_metadata.items()
                if meta.get('data_precision') is not None
            }
            oracle_scale = {
                col: meta.get('data_scale')
                for col, meta in oracle_metadata.items()
                if meta.get('data_scale') is not None
            }
            oracle_column_types = {
                col: meta.get('data_type')
                for col, meta in oracle_metadata.items()
                if meta.get('data_type')
            }

            stats_collector = AggregateStatsCollector(self.oracle_db, self.postgres_db)
            result_processor = ResultProcessor(stats_collector)
            ddl_report = result_processor.generate_ddl_report(
                table_name, schema, columns, base_where_clause, target_db='postgres',
                oracle_precision=oracle_precision if oracle_precision else None,
                oracle_scale=oracle_scale if oracle_scale else None,
                oracle_column_types=oracle_column_types if oracle_column_types else None,
                validation_results=validation_results,
                aggregate_results=aggregate_results,
                decimal_tolerance=self.decimal_tolerance,
                tolerance_by_column=getattr(self, 'tolerance_by_column', None)
            )
        except Exception as e:
            logger.warning(f"Failed to generate DDL report: {e}")
        
        result = {
            'validation_results': validation_results,
            'aggregate_results': aggregate_results,
            'ddl_report': ddl_report,
            'table_name': table_name,
            'schema': schema
        }
        
        # JSON 리포트 생성
        if generate_report:
            try:
                metadata = {
                    'aggregate_match': aggregate_results.get('match'),
                    'ddl_available': ddl_report is not None
                }
                if ddl_report:
                    metadata['changeable_columns_count'] = len(ddl_report.get('changeable_columns', []))
                
                report_path = self.report_generator.generate_json_report(
                    validation_results, table_name, schema,
                    metadata=metadata,
                    include_ddl=True,
                    ddl_report=ddl_report
                )
                result['report_path'] = report_path
                
                # Also aggregate report
                agg_report_path = self.report_generator.generate_aggregate_json_report(
                    aggregate_results, table_name, schema
                )
                result['aggregate_report_path'] = agg_report_path
                
                # 읽기 쉬운 요약 리포트 생성
                summary_report_path = self.report_generator.generate_readable_summary(
                    validation_results, aggregate_results, table_name, schema, ddl_report
                )
                result['summary_report_path'] = summary_report_path
                
                # Save DDL report to file
                if ddl_report:
                    ddl_sql = result_processor.format_ddl_output(ddl_report)
                    from pathlib import Path
                    from datetime import datetime
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ddl_file = Path("reports") / f"{schema}_{table_name}_ddl_{timestamp}.sql"
                    ddl_file.parent.mkdir(parents=True, exist_ok=True)
                    ddl_file.write_text(ddl_sql, encoding='utf-8')
                    result['ddl_report_path'] = str(ddl_file)
                    logger.info(f"DDL report saved to {ddl_file}")
            except Exception as e:
                logger.error(f"Failed to generate report: {e}")
                result['report_error'] = str(e)
        
        return result
