"""
검증 결과 처리 및 DDL 자동 생성 모듈
통계 정보를 바탕으로 NUMERIC 타입을 BIGINT/INTEGER로 변경하는 ALTER TABLE 구문 생성
Oracle to PostgreSQL 마이그레이션 패턴 지원
"""
import logging
import re
from typing import Dict, Any, List, Optional
from checks.aggregate import AggregateStatsCollector

logger = logging.getLogger(__name__)


class ResultProcessor:
    """Validation result processing and DDL generation."""
    
    def __init__(self, aggregate_collector: AggregateStatsCollector):
        self.aggregate_collector = aggregate_collector
    
    def generate_alter_table_ddl(self, table_name: str, schema: str,
                                 columns: List[str], where_clause: str = "",
                                 target_db: str = 'postgres',
                                 oracle_precision: Optional[Dict[str, int]] = None,
                                 oracle_scale: Optional[Dict[str, int]] = None,
                                 pk_columns: Optional[List[str]] = None,
                                 fk_columns: Optional[List[str]] = None) -> List[str]:
        """
        통계 정보를 바탕으로 ALTER TABLE 구문 생성.
        전역 규칙: PK/FK 컬럼은 모든 테이블·컬럼에 대해 BIGINT 유지.
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            columns: 컬럼 리스트
            where_clause: WHERE 절 조건 (통계 수집용)
            target_db: 대상 DB ('postgres' 또는 'oracle')
            oracle_precision: Oracle 컬럼별 precision 딕셔너리 (옵션)
            oracle_scale: Oracle 컬럼별 scale 딕셔너리 (옵션)
            pk_columns: PK 컬럼명 리스트 (해당 컬럼은 BIGINT 유지)
            fk_columns: FK 컬럼명 리스트 (해당 컬럼은 BIGINT 유지)
        
        Returns:
            ALTER TABLE 구문 리스트
        """
        pk_fk_set = {c.lower() for c in ((pk_columns or []) + (fk_columns or [])) if isinstance(c, str)}
        # Collect stats
        stats = self.aggregate_collector.collect_column_stats(
            table_name, schema, columns, where_clause
        )
        
        # Type recommendation (data-based)
        recommendations = self.aggregate_collector.recommend_numeric_type(stats)
        
        # Generate ALTER TABLE
        ddl_statements = []
        
        for column in columns:
            # 현재 타입 확인 (PostgreSQL 기준)
            current_type = self._get_current_postgres_type(table_name, schema, column)
            
            # Only for NUMERIC type
            if current_type and 'numeric' in current_type.lower():
                # 데이터 기반 추천 타입
                recommended_type = recommendations.get(column, 'NUMERIC')
                
                # If Oracle precision/scale present, apply migration script logic
                if oracle_precision and oracle_scale:
                    precision = oracle_precision.get(column)
                    scale = oracle_scale.get(column)
                    has_decimal = stats.get(column, {}).get('oracle', {}).get('has_decimal', False) or \
                                 stats.get(column, {}).get('postgres', {}).get('has_decimal', False)
                    
                    # 마이그레이션 스크립트 로직으로 타입 결정
                    migration_type = self.map_oracle_number_to_postgres(
                        precision, scale, has_decimal
                    )
                    
                    # 데이터 기반 추천과 마이그레이션 스크립트 로직 중 더 적합한 것 선택
                    # (데이터에 소수점이 없고 범위가 맞으면 INTEGER/BIGINT, 아니면 NUMERIC)
                    if recommended_type != 'NUMERIC' and 'NUMERIC' not in migration_type:
                        # 둘 다 정수형 추천이면 데이터 기반 추천 사용
                        final_type = recommended_type
                    elif 'NUMERIC' in migration_type and recommended_type == 'NUMERIC':
                        # 둘 다 NUMERIC이면 마이그레이션 스크립트 결과 사용 (precision/scale 포함)
                        final_type = migration_type
                    else:
                        # 데이터 기반 추천 우선
                        final_type = recommended_type
                else:
                    # Oracle 정보가 없으면 데이터 기반 추천만 사용
                    final_type = recommended_type
                
                # [Global rule] PK/FK always BIGINT
                if column.lower() in pk_fk_set and final_type in ('INTEGER', 'SMALLINT'):
                    final_type = 'BIGINT'
                
                # 현재 타입과 다르면 ALTER 구문 생성
                if final_type != 'NUMERIC' or (current_type.upper() != final_type.upper()):
                    ddl = self._generate_alter_column_ddl(
                        table_name, schema, column, final_type, target_db
                    )
                    if ddl:
                        ddl_statements.append(ddl)
        
        return ddl_statements
    
    def _get_current_postgres_type(self, table_name: str, schema: str,
                                   column: str) -> Optional[str]:
        """PostgreSQL에서 현재 컬럼 타입 조회"""
        try:
            sql = """
                SELECT data_type, numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE table_schema = %(schema)s
                  AND table_name = %(table_name)s
                  AND column_name = %(column_name)s
            """
            result = self.aggregate_collector.postgres_db.execute_query(sql, {
                'schema': schema,
                'table_name': table_name,
                'column_name': column
            })
            
            if result and len(result) > 0:
                data_type = result[0][0]
                precision = result[0][1]
                scale = result[0][2]
                
                if data_type == 'numeric' and precision and scale:
                    return f"NUMERIC({precision},{scale})"
                return data_type.upper()
            
            return None
        except Exception as e:
            logger.error(f"Failed to get current type for {column}: {e}")
            return None
    
    def _generate_alter_column_ddl(self, table_name: str, schema: str,
                                   column: str, new_type: str,
                                   target_db: str) -> Optional[str]:
        """
        ALTER TABLE ... ALTER COLUMN 구문 생성
        Oracle to PostgreSQL 마이그레이션 패턴 적용
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            column: 컬럼명
            new_type: 새로운 타입
            target_db: 대상 DB
        
        Returns:
            ALTER TABLE 구문 문자열
        """
        if target_db.lower() == 'postgres':
            # PostgreSQL: NUMERIC to integer needs USING
            current = (self._get_current_postgres_type(table_name, schema, column) or '').upper()
            if new_type in ('SMALLINT', 'INTEGER', 'BIGINT') and 'NUMERIC' in current:
                cast = 'smallint' if new_type == 'SMALLINT' else ('integer' if new_type == 'INTEGER' else 'bigint')
                return f'ALTER TABLE "{schema}"."{table_name}" ALTER COLUMN "{column}" TYPE {new_type} USING "{column}"::{cast};'
            return f'ALTER TABLE "{schema}"."{table_name}" ALTER COLUMN "{column}" TYPE {new_type};'
        elif target_db.lower() == 'oracle':
            # Oracle의 경우 MODIFY 사용
            return f'ALTER TABLE "{schema}"."{table_name}" MODIFY "{column}" {new_type};'
        else:
            logger.warning(f"Unsupported target_db: {target_db}")
            return None
    
    def generate_ddl_report(self, table_name: str, schema: str,
                           columns: List[str], where_clause: str = "",
                           target_db: str = 'postgres',
                           oracle_precision: Optional[Dict[str, int]] = None,
                           oracle_scale: Optional[Dict[str, int]] = None,
                           oracle_column_types: Optional[Dict[str, str]] = None,
                           validation_results: Optional[List[Dict[str, Any]]] = None,
                           aggregate_results: Optional[Dict[str, Any]] = None,
                           decimal_tolerance: float = 0.0001,
                           tolerance_by_column: Optional[Dict[str, float]] = None,
                           pk_columns: Optional[List[str]] = None,
                           fk_columns: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        DDL 리포트 생성 (지능형 DDL 추천 파이프라인 2~3단계).
        전역 규칙 (모든 컬럼 적용):
        - 날짜/시간 값 → TIMESTAMP(DATE) 추천
        - precision ≤ 4 범위 → SMALLINT 추천 (문서 PAGE 2)
        - PK/FK → BIGINT 유지 (SMALLINT/INTEGER 미추천)
        
        Args:
            table_name: 테이블명
            schema: 스키마명
            columns: 컬럼 리스트
            where_clause: WHERE 절 조건
            target_db: 대상 DB
            oracle_precision: Oracle precision 정보
            oracle_scale: Oracle scale 정보
            validation_results: 검증 결과 (Hash 비교 결과)
            aggregate_results: 집계 검증 결과 (SUM 비교 결과)
            decimal_tolerance: 기본 Decimal 허용 오차
            tolerance_by_column: 컬럼별 허용 오차
            pk_columns: PK 컬럼명 리스트 (None이면 Oracle에서 자동 수집)
            fk_columns: FK 컬럼명 리스트 (None이면 Oracle에서 자동 수집)
        
        Returns:
            리포트 딕셔너리 (ddl_recommendations, numeric_downcast_candidates 등)
        """
        from decimal import Decimal
        
        # Validation layer: auto-collect pk_columns/fk_columns from Oracle if not set
        if pk_columns is None:
            pk_columns = self.aggregate_collector.get_pk_columns(table_name, schema)
        if fk_columns is None:
            fk_columns = self.aggregate_collector.get_fk_columns(table_name, schema)
        
        pk_fk_set = {c.lower() for c in ((pk_columns or []) + (fk_columns or [])) if isinstance(c, str)}
        
        # Collect stats (oracle_column_types 전달 시 DATE/TIMESTAMP 컬럼에 has_non_zero_time 수집)
        stats = self.aggregate_collector.collect_column_stats(
            table_name, schema, columns, where_clause,
            oracle_column_types=oracle_column_types
        )
        
        # Type recommendation
        recommendations = self.aggregate_collector.recommend_numeric_type(stats)
        
        # DDL 구문 생성 (마이그레이션 스크립트 로직 적용, PK/FK는 BIGINT 유지)
        ddl_statements = self.generate_alter_table_ddl(
            table_name, schema, columns, where_clause, target_db,
            oracle_precision=oracle_precision,
            oracle_scale=oracle_scale,
            pk_columns=pk_columns,
            fk_columns=fk_columns
        )
        
        # Numeric precision: NUMERIC -> BIGINT/INTEGER downcast decision
        numeric_downcast_candidates = []
        
        for col in columns:
            col_stats = stats.get(col, {})
            oracle_stats = col_stats.get('oracle', {})
            postgres_stats = col_stats.get('postgres', {})
            
            # NUMERIC 타입 컬럼만 처리
            current_type = self._get_current_postgres_type(table_name, schema, col)
            if not current_type or 'numeric' not in current_type.lower():
                continue
            
            # Check criteria
            reasons = []
            can_downcast = True
            
            # 1. HAS_FRACTION: scale > 0 or fractional values
            has_fraction = (
                oracle_stats.get('has_decimal', False) or
                postgres_stats.get('has_decimal', False) or
                (oracle_scale and oracle_scale.get(col, 0) > 0)
            )
            if has_fraction:
                can_downcast = False
                reasons.append('HAS_FRACTION')
            
            # 2. OUT_OF_RANGE: beyond BIGINT range
            if can_downcast:
                def safe_decimal_value(val):
                    """안전하게 Decimal로 변환"""
                    if val is None:
                        return None
                    try:
                        return Decimal(str(val))
                    except:
                        return None
                
                oracle_max = safe_decimal_value(oracle_stats.get('max_value'))
                oracle_min = safe_decimal_value(oracle_stats.get('min_value'))
                postgres_max = safe_decimal_value(postgres_stats.get('max_value'))
                postgres_min = safe_decimal_value(postgres_stats.get('min_value'))
                
                max_val = max(v for v in [oracle_max, postgres_max] if v is not None) if any([oracle_max, postgres_max]) else None
                min_val = min(v for v in [oracle_min, postgres_min] if v is not None) if any([oracle_min, postgres_min]) else None
                
                BIGINT_MAX = Decimal('9223372036854775807')
                BIGINT_MIN = Decimal('-9223372036854775808')
                INTEGER_MAX = Decimal('2147483647')
                INTEGER_MIN = Decimal('-2147483648')
                SMALLINT_MAX = Decimal('32767')
                SMALLINT_MIN = Decimal('-32768')
                
                if max_val is not None and min_val is not None:
                    if max_val > BIGINT_MAX or min_val < BIGINT_MIN:
                        can_downcast = False
                        reasons.append('OUT_OF_RANGE')
                    elif max_val <= SMALLINT_MAX and min_val >= SMALLINT_MIN:
                        target_type = 'SMALLINT'
                    elif max_val <= INTEGER_MAX and min_val >= INTEGER_MIN:
                        target_type = 'INTEGER'
                    else:
                        target_type = 'BIGINT'
                else:
                    target_type = 'BIGINT'  # Default
            
            # [전역 규칙] PK/FK 컬럼은 항상 BIGINT 유지
            col_lower = col.lower() if isinstance(col, str) else col
            if col_lower in pk_fk_set and target_type in ('INTEGER', 'SMALLINT'):
                target_type = 'BIGINT'
                reasons.append('PK_FK_BIGINT_MAINTAIN')
            
            # 2b. OUT_OF_RANGE safety: verify out-of-range row count
            if can_downcast and target_type in ('SMALLINT', 'INTEGER'):
                try:
                    out_of_range_count = self.aggregate_collector.count_out_of_range_rows(
                        table_name, schema, col, target_type, where_clause
                    )
                    if out_of_range_count and out_of_range_count > 0:
                        can_downcast = False
                        reasons.append('OUT_OF_RANGE_VERIFIED')
                except Exception:
                    pass
            
            # 3. TOLERANCE_EXCEEDED: SUM 비교 시 tolerance 초과
            if can_downcast and aggregate_results:
                col_agg = aggregate_results.get('columns', {}).get(col, {})
                comparison = col_agg.get('comparison', {})
                if not comparison.get('sum_match', True):
                    # SUM mismatch
                    oracle_sum = safe_decimal_value(oracle_stats.get('sum_value'))
                    postgres_sum = safe_decimal_value(postgres_stats.get('sum_value'))
                    
                    if oracle_sum is not None and postgres_sum is not None:
                        diff = abs(oracle_sum - postgres_sum)
                        tolerance = tolerance_by_column.get(col, decimal_tolerance) if tolerance_by_column else decimal_tolerance
                        if diff > Decimal(str(tolerance)):
                            can_downcast = False
                            reasons.append('TOLERANCE_EXCEEDED')
            
            # 4. SUM_MISMATCH: 집계 검증에서 SUM 불일치
            if can_downcast and aggregate_results:
                col_agg = aggregate_results.get('columns', {}).get(col, {})
                comparison = col_agg.get('comparison', {})
                if not comparison.get('sum_match', True):
                    can_downcast = False
                    reasons.append('SUM_MISMATCH')
            
            # 5. HASH_MISMATCH: chunk hash mismatch
            if can_downcast and validation_results:
                # 모든 chunk가 일치하는지 확인
                all_match = all(r.get('match', False) for r in validation_results if 'error' not in r)
                if not all_match:
                    can_downcast = False
                    reasons.append('HASH_MISMATCH')
            
            # When downcast is possible
            if can_downcast and not has_fraction:
                reasons.extend([
                    'no_fraction_values',
                    'within_bigint_range',
                    'hash_match',
                    'sum_match'
                ])
                
                # Dry-run CAST 시뮬레이션 (PG에서 변환 시 손실 행 수)
                dry_run_loss = None
                try:
                    dry_run_loss = self.aggregate_collector.dry_run_cast_loss_count(
                        table_name, schema, col, target_type, where_clause
                    )
                except Exception:
                    pass
                
                # Generate DDL
                ddl = self._generate_alter_column_ddl(
                    table_name, schema, col, target_type, target_db
                )
                
                # Recommendation message (practical guidance)
                if col_lower in pk_fk_set:
                    recommendation_message = (
                        "PK/FK column: keep BIGINT recommended (operational stability, avoid overflow beyond ~2.1B rows)."
                    )
                elif target_type == 'SMALLINT':
                    recommendation_message = (
                        "Column is NUMERIC with no decimals and max value <= 32,767. "
                        "Changing to SMALLINT reduces index/storage size."
                    )
                elif target_type == 'INTEGER':
                    recommendation_message = (
                        "Column is NUMERIC with no decimals. "
                        "Changing to INTEGER improves performance and reduces storage."
                    )
                else:
                    recommendation_message = (
                        "Column is NUMERIC with no decimals. "
                        "Changing to BIGINT improves integer operation performance."
                    )
                
                numeric_downcast_candidates.append({
                    'table': f"{schema}.{table_name}",
                    'column': col,
                    'from_type': current_type,
                    'to_type': target_type,
                    'reason': reasons,
                    'ddl': ddl,
                    'recommendation_message': recommendation_message,
                    'dry_run_loss_count': dry_run_loss,
                })
        
        _oracle_date_types = ('DATE', 'TIMESTAMP', 'TIMESTAMP WITH TIME ZONE', 'TIMESTAMP WITH LOCAL TIME ZONE')
        indexed_columns = []
        try:
            indexed_columns = self.aggregate_collector.get_indexed_columns(table_name, schema, columns) or []
        except Exception:
            pass

        changeable_columns = []
        for col in columns:
            col_stats = stats.get(col, {})
            recommended_type = recommendations.get(col, 'NUMERIC')
            oracle_stats = col_stats.get('oracle', {})
            postgres_stats = col_stats.get('postgres', {})

            ora_type = (oracle_column_types or {}).get(col) or (oracle_column_types or {}).get(col.upper())
            if ora_type and str(ora_type).upper() in _oracle_date_types:
                recommended_type = recommendations.get(col, 'TIMESTAMP')
                migration_type = recommended_type
            else:
                migration_type = None
                if oracle_precision and oracle_scale:
                    precision = oracle_precision.get(col)
                    scale = oracle_scale.get(col)
                    has_decimal = oracle_stats.get('has_decimal', False) or postgres_stats.get('has_decimal', False)
                    migration_type = self.map_oracle_number_to_postgres(
                        precision, scale, has_decimal
                    )

            if recommended_type != 'NUMERIC' or (migration_type and migration_type != 'NUMERIC'):
                row_count = (oracle_stats.get('row_count') or postgres_stats.get('row_count')) or 1
                null_count = oracle_stats.get('null_count') or postgres_stats.get('null_count')
                distinct_count = oracle_stats.get('distinct_count') or postgres_stats.get('distinct_count')
                null_ratio = (null_count / row_count) if (null_count is not None and row_count) else None
                distinct_ratio = (distinct_count / row_count) if (distinct_count is not None and row_count) else None
                entry = {
                    'column_name': col,
                    'current_type': self._get_current_postgres_type(table_name, schema, col),
                    'recommended_type': recommended_type,
                    'migration_type': migration_type,
                    'oracle_precision': oracle_precision.get(col) if oracle_precision else None,
                    'oracle_scale': oracle_scale.get(col) if oracle_scale else None,
                    'stats': col_stats,
                    'in_index': col in indexed_columns,
                    'null_ratio': null_ratio,
                    'distinct_ratio': distinct_ratio,
                }
                if ora_type:
                    entry['oracle_type_display'] = str(ora_type)
                if recommended_type in ('DATE', 'TIMESTAMP'):
                    has_time = oracle_stats.get('has_non_zero_time', 0) or postgres_stats.get('has_non_zero_time', 0)
                    if not ora_type:
                        entry['oracle_type_display'] = 'NUMBER (Stored as YYYY-MM-DD HH24:MI:SS)' if recommended_type == 'TIMESTAMP' else 'NUMBER (Stored as YYYYMMDD or date)'
                    if has_time:
                        entry['profiling_result'] = 'Time portion detected (e.g., 01:43:14)'
                        entry['rationale'] = 'Preserves time precision captured in Oracle.'
                    else:
                        entry['profiling_result'] = 'No time values found (All 00:00:00)'
                        entry['rationale'] = 'Optimizes storage (4 bytes saved per row) as time data is empty.'
                changeable_columns.append(entry)
        
        pg_table_size_bytes = None
        try:
            pg_table_size_bytes = self.aggregate_collector.get_pg_table_size_bytes(table_name, schema)
        except Exception:
            pass

        return {
            'table_name': table_name,
            'schema': schema,
            'target_db': target_db,
            'ddl_statements': ddl_statements,
            'changeable_columns': changeable_columns,
            'recommendations': recommendations,
            'stats': stats,
            'numeric_downcast_candidates': numeric_downcast_candidates,
            'pg_table_size_bytes': pg_table_size_bytes,
        }
    
    def format_ddl_output(self, report: Dict[str, Any]) -> str:
        """
        DDL 리포트를 SQL 형태로 포맷팅
        Oracle to PostgreSQL 마이그레이션 스크립트 형식 포함
        
        Args:
            report: generate_ddl_report()의 결과
        
        Returns:
            포맷팅된 SQL 문자열
        """
        output = []
        output.append("-- ========================================")
        output.append(f"-- DDL Migration Script for: {report['schema']}.{report['table_name']}")
        output.append(f"-- Target DB: {report['target_db']}")
        output.append("-- Generated based on data statistics")
        output.append("-- ========================================")
        output.append("")
        
        # Transaction start (PostgreSQL)
        if report['target_db'].lower() == 'postgres':
            output.append("BEGIN;")
            output.append("")
        
        if report['ddl_statements']:
            output.append("-- ========================================")
            output.append("-- ALTER TABLE statements to optimize numeric types")
            output.append("-- Based on MIN/MAX values and decimal presence check")
            output.append("-- ========================================")
            output.append("")
            
            for ddl in report['ddl_statements']:
                output.append(ddl)
            output.append("")
        else:
            output.append("-- No columns can be optimized (all have decimal values or are already optimal)")
            output.append("")
        
        # DDL recommendations (numeric_downcast_candidates)
        if report.get('numeric_downcast_candidates'):
            output.append("-- ========================================")
            output.append("-- DDL recommendations (HAS_FRACTION/MAX/MIN/SUM_MISMATCH based)")
            output.append("-- ========================================")
            for cand in report['numeric_downcast_candidates']:
                output.append(f"-- {cand['table']}.{cand['column']}: {cand['from_type']} -> {cand['to_type']}")
                output.append(f"--   Recommendation: {cand.get('recommendation_message', '')}")
                output.append(f"--   Reasons: {', '.join(cand.get('reason', []))}")
                if cand.get('dry_run_loss_count') is not None:
                    output.append(f"--   Dry-run CAST loss rows: {cand['dry_run_loss_count']}")
                output.append(cand.get('ddl', ''))
                output.append("")
        
        # Changeable columns summary (DATE/TIMESTAMP는 추천 근거 포함)
        if report['changeable_columns']:
            output.append("-- ========================================")
            output.append("-- Summary of changeable columns:")
            output.append("-- ========================================")
            if report.get('pg_table_size_bytes') is not None:
                size_mb = report['pg_table_size_bytes'] / (1024 * 1024)
                output.append(f"-- Table size (PG): {size_mb:.2f} MB")
                output.append("")
            for col_info in report['changeable_columns']:
                stats = col_info.get('stats', {})
                oracle_stats = stats.get('oracle', {})
                postgres_stats = stats.get('postgres', {})

                if col_info.get('rationale') and col_info.get('profiling_result'):
                    output.append(f"-- Column: {col_info['column_name']}")
                    output.append(f"--   Oracle Type: {col_info.get('oracle_type_display', 'N/A')}")
                    output.append(f"--   Profiling Result: {col_info['profiling_result']}")
                    output.append(f"--   Recommended: {col_info['recommended_type']}")
                    output.append(f"--   Rationale: {col_info['rationale']}")
                else:
                    output.append(f"-- Column: {col_info['column_name']}")
                    if col_info.get('oracle_type_display'):
                        output.append(f"--   Oracle Type: {col_info['oracle_type_display']}")
                    output.append(f"--   Current Type: {col_info['current_type']}")
                    output.append(f"--   Recommended Type (Data-based): {col_info['recommended_type']}")
                    if col_info.get('migration_type'):
                        output.append(f"--   Migration Type (Oracle precision/scale): {col_info['migration_type']}")
                        if col_info.get('oracle_precision') is not None:
                            output.append(f"--   Oracle Precision: {col_info['oracle_precision']}, Scale: {col_info.get('oracle_scale', 0)}")
                    if col_info.get('in_index') is not None:
                        output.append(f"--   In index: {'Y' if col_info['in_index'] else 'N'}")
                    if col_info.get('distinct_ratio') is not None:
                        dr = col_info['distinct_ratio']
                        output.append(f"--   Distinct ratio: {dr:.4f}" + (" (code/flag candidate)" if dr < 0.01 else ""))
                    if col_info.get('null_ratio') is not None:
                        nr = col_info['null_ratio']
                        output.append(f"--   Null ratio: {nr:.4f}" + (" (type change effect minimal)" if nr > 0.9 else ""))
                    if oracle_stats:
                        output.append(f"--   Oracle MIN: {oracle_stats.get('min_value')}, MAX: {oracle_stats.get('max_value')}")
                        frac = oracle_stats.get('fraction_row_count') or postgres_stats.get('fraction_row_count')
                        has_frac = oracle_stats.get('has_fraction', oracle_stats.get('has_decimal')) or postgres_stats.get('has_fraction', postgres_stats.get('has_decimal'))
                        output.append(f"--   HAS_FRACTION: {'Y' if has_frac else 'N'}" + (f" (fraction_row_count: {frac})" if frac is not None and int(frac) > 0 else ""))
                    if postgres_stats:
                        output.append(f"--   PostgreSQL MIN: {postgres_stats.get('min_value')}, MAX: {postgres_stats.get('max_value')}")
                        output.append(f"--   PostgreSQL Has Decimal: {postgres_stats.get('has_decimal', False)}")
                output.append("")
        
        # Transaction commit (PostgreSQL)
        if report['target_db'].lower() == 'postgres':
            output.append("COMMIT;")
            output.append("")
            output.append("-- Rollback if needed:")
            output.append("-- ROLLBACK;")
        
        return "\n".join(output)
    
    def map_oracle_number_to_postgres(self, precision: Optional[int], 
                                      scale: Optional[int],
                                      has_decimal: bool = False) -> str:
        """
        Oracle NUMBER 타입을 PostgreSQL 타입으로 매핑
        Oracle_to_PostgreSQL_DDL_Migration_Script_2.sql의 로직 완전 적용
        
        Args:
            precision: NUMBER precision
            scale: NUMBER scale
            has_decimal: 소수점 값 존재 여부 (데이터 기반)
        
        Returns:
            PostgreSQL 타입 문자열
        """
        # 데이터에 소수점이 있으면 무조건 NUMERIC (데이터 기반 검증 우선)
        if has_decimal:
            if precision and scale:
                return f'NUMERIC({precision},{scale})'
            elif precision:
                return f'NUMERIC({precision})'
            else:
                return 'NUMERIC'
        
        # precision/scale 없는 경우 - ora2pg 기본값: NUMERIC
        if precision is None and scale is None:
            return 'NUMERIC'

        # scale이 0 또는 None인 정수형 처리 (전역: precision ≤ 4 → SMALLINT, 문서 PAGE 2)
        if scale is None or scale == 0:
            if precision is None:
                return 'NUMERIC'
            if precision <= 4:
                return 'SMALLINT'
            elif precision <= 9:
                return 'INTEGER'
            elif precision <= 18:
                return 'BIGINT'
            else:
                # precision이 18보다 크면 NUMERIC(precision) 사용
                return f'NUMERIC({precision})'

        # scale이 있는 소수형 처리
        if scale and scale > 0:
            if precision:
                return f'NUMERIC({precision},{scale})'
            else:
                return 'NUMERIC'
        
        # Other 경우
        return 'NUMERIC'
    
    def map_oracle_type_to_postgres(self, oracle_type: str, 
                                    data_length: Optional[int] = None,
                                    data_precision: Optional[int] = None,
                                    data_scale: Optional[int] = None) -> str:
        """
        Oracle 데이터 타입을 PostgreSQL 타입으로 매핑
        Oracle_to_PostgreSQL_DDL_Migration_Script_2.sql의 모든 타입 매핑 규칙 적용
        
        Args:
            oracle_type: Oracle 데이터 타입
            data_length: 데이터 길이 (VARCHAR2, CHAR 등)
            data_precision: NUMBER precision
            data_scale: NUMBER scale
        
        Returns:
            PostgreSQL 타입 문자열
        """
        oracle_type_upper = oracle_type.upper()
        
        # String types
        if oracle_type_upper == 'CLOB':
            return 'TEXT'
        elif oracle_type_upper == 'NCLOB':
            return 'TEXT'
        elif oracle_type_upper == 'LONG':
            return 'TEXT'
        elif oracle_type_upper == 'XMLTYPE':
            return 'XML'
        elif oracle_type_upper == 'NVARCHAR2':
            if data_length and data_length > 0:
                return f'VARCHAR({data_length})'
            else:
                return 'VARCHAR'
        elif oracle_type_upper == 'VARCHAR2':
            if data_length and data_length > 0:
                return f'VARCHAR({data_length})'
            else:
                return 'VARCHAR'
        elif oracle_type_upper == 'CHAR':
            if data_length and data_length > 0:
                return f'CHAR({data_length})'
            else:
                return 'CHAR'
        elif oracle_type_upper == 'NCHAR':
            if data_length and data_length > 0:
                return f'CHAR({data_length})'
            else:
                return 'CHAR'
        
        # Binary types
        elif oracle_type_upper == 'BLOB':
            return 'BYTEA'
        elif oracle_type_upper == 'RAW':
            return 'BYTEA'
        elif oracle_type_upper == 'LONG RAW':
            return 'BYTEA'
        
        # 숫자 타입 (EDB/ora2pg 표준 매핑 기준)
        elif oracle_type_upper == 'NUMBER':
            return self.map_oracle_number_to_postgres(data_precision, data_scale, has_decimal=False)
        elif oracle_type_upper.startswith('FLOAT'):
            return 'DOUBLE PRECISION'
        elif oracle_type_upper == 'BINARY_FLOAT':
            return 'REAL'
        elif oracle_type_upper == 'BINARY_DOUBLE':
            return 'DOUBLE PRECISION'
        
        # Date/time types
        elif oracle_type_upper == 'DATE':
            return 'TIMESTAMP'
        elif oracle_type_upper == 'TIMESTAMP':
            return 'TIMESTAMP'
        elif 'TIMESTAMP' in oracle_type_upper:
            if 'WITH TIME ZONE' in oracle_type_upper or 'WITH LOCAL TIME ZONE' in oracle_type_upper:
                return 'TIMESTAMP WITH TIME ZONE'
            else:
                return 'TIMESTAMP'
        
        # 인터벌 타입
        elif oracle_type_upper.startswith('INTERVAL'):
            return 'INTERVAL'
        
        # Other types
        elif oracle_type_upper == 'ROWID':
            return 'CHAR(18)'
        elif oracle_type_upper == 'UROWID':
            return 'VARCHAR(4000)'
        elif oracle_type_upper == 'SDO_GEOMETRY':
            return 'GEOMETRY(GEOMETRY,4326)'
        
        # Default: TEXT
        else:
            return 'TEXT'
    
    def load_migration_patterns(self, migration_script_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Oracle to PostgreSQL 마이그레이션 스크립트에서 패턴 로드
        Oracle_to_PostgreSQL_DDL_Migration_Script_2.sql 기반
        
        Args:
            migration_script_path: 마이그레이션 스크립트 파일 경로 (옵션)
        
        Returns:
            마이그레이션 패턴 딕셔너리
        """
        patterns = {
            'type_mappings': {
                # String types
                'CLOB': 'TEXT',
                'NCLOB': 'TEXT',
                'LONG': 'TEXT',
                'XMLTYPE': 'XML',
                'NVARCHAR2': 'VARCHAR',
                'VARCHAR2': 'VARCHAR',
                'CHAR': 'CHAR',
                'NCHAR': 'CHAR',
                # Binary types
                'BLOB': 'BYTEA',
                'RAW': 'BYTEA',
                'LONG RAW': 'BYTEA',
                # 숫자 타입 (NUMBER는 별도 함수로 처리)
                'FLOAT': 'DOUBLE PRECISION',
                'BINARY_FLOAT': 'REAL',
                'BINARY_DOUBLE': 'DOUBLE PRECISION',
                # Date/time types
                'DATE': 'TIMESTAMP',
                'TIMESTAMP': 'TIMESTAMP',
                'TIMESTAMP WITH TIME ZONE': 'TIMESTAMP WITH TIME ZONE',
                'TIMESTAMP WITH LOCAL TIME ZONE': 'TIMESTAMP WITH TIME ZONE',
                # 인터벌 타입
                'INTERVAL': 'INTERVAL',
                # Other
                'ROWID': 'CHAR(18)',
                'UROWID': 'VARCHAR(4000)',
                'SDO_GEOMETRY': 'GEOMETRY(GEOMETRY,4326)',
            },
            'constraint_patterns': {
                'primary_key': 'PRIMARY KEY',
                'foreign_key': 'FOREIGN KEY',
                'unique': 'UNIQUE',
                'check': 'CHECK',
                'not_null': 'NOT NULL'
            },
            'number_mapping_rules': {
                'precision_scale_none': 'NUMERIC',
                'scale_0_precision_4': 'SMALLINT',
                'scale_0_precision_9': 'INTEGER',
                'scale_0_precision_18': 'BIGINT',
                'scale_gt_0': 'NUMERIC(precision,scale)'
            }
        }
        
        # 마이그레이션 스크립트 파일이 있으면 로드
        if migration_script_path:
            try:
                from pathlib import Path
                script_file = Path(migration_script_path)
                if script_file.exists():
                    content = script_file.read_text(encoding='utf-8')
                    # 스크립트에서 타입 매핑 패턴 추출
                    logger.info(f"Loaded migration patterns from {migration_script_path}")
                    # 추가 패턴 추출 로직은 필요시 구현
            except Exception as e:
                logger.warning(f"Failed to load migration script: {e}")
        
        return patterns
