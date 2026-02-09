"""
리포트 생성 모듈
JSON 리포트 출력
"""
import logging
import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ReportGenerator:
    """리포트 생성 클래스"""
    
    def __init__(self, output_dir: str = "reports"):
        """
        Args:
            output_dir: 리포트 출력 디렉토리
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def generate_json_report(self, validation_results: List[Dict[str, Any]],
                            table_name: str, schema: str,
                            metadata: Optional[Dict[str, Any]] = None,
                            include_ddl: bool = False,
                            ddl_report: Optional[Dict[str, Any]] = None) -> str:
        """
        JSON 리포트 생성
        
        Args:
            validation_results: 검증 결과 리스트
            table_name: 테이블명
            schema: 스키마명
            metadata: 추가 메타데이터
        
        Returns:
            생성된 리포트 파일 경로
        """
        # 리포트 구조 구성
        report = {
            'metadata': {
                'table_name': table_name,
                'schema': schema,
                'generated_at': datetime.now().isoformat(),
                **(metadata or {})
            },
            'summary': self._generate_summary(validation_results),
            'results': validation_results
        }
        
        # DDL 리포트 포함 (옵션)
        if include_ddl and ddl_report:
            report['ddl_recommendations'] = {
                'changeable_columns': ddl_report.get('changeable_columns', []),
                'ddl_statements': ddl_report.get('ddl_statements', []),
                'recommendations': ddl_report.get('recommendations', {}),
                'numeric_downcast_candidates': ddl_report.get('numeric_downcast_candidates', [])  # 새로 추가
            }
        
        # 파일명 생성
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{schema}_{table_name}_{timestamp}.json"
        filepath = self.output_dir / filename
        
        # JSON 파일 저장
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False, default=str)
            
            logger.info(f"JSON report generated: {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Failed to generate JSON report: {e}")
            raise
    
    def _generate_summary(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """검증 결과 요약 생성"""
        total_chunks = len(results)
        matched_chunks = sum(1 for r in results if r.get('match', False))
        mismatched_chunks = total_chunks - matched_chunks
        
        # 에러 발생한 Chunk 수
        error_chunks = sum(1 for r in results if 'error' in r)
        
        # Mismatch 상세 정보
        mismatch_details = []
        for result in results:
            if not result.get('match', False) and 'error' not in result:
                mismatch_details.append({
                    'where_clause': result.get('where_clause', ''),
                    'partition_name': result.get('partition_name'),
                    'oracle_hash': result.get('oracle_hash'),
                    'postgres_hash': result.get('postgres_hash')
                })
        
        summary = {
            'total_chunks': total_chunks,
            'matched_chunks': matched_chunks,
            'mismatched_chunks': mismatched_chunks,
            'error_chunks': error_chunks,
            'match_rate': matched_chunks / total_chunks if total_chunks > 0 else 0.0,
            'mismatch_details': mismatch_details[:10]  # 최대 10개만
        }
        
        # 파티션 요약 (파티션 인식 검증 모드인 경우)
        partition_summary = self._generate_partition_summary(results)
        if partition_summary:
            summary['partition_summary'] = partition_summary
        
        return summary
    
    def _generate_partition_summary(self, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        파티션 요약 생성
        
        Args:
            results: 검증 결과 리스트
        
        Returns:
            파티션 요약 딕셔너리 또는 None
        """
        # 파티션별 결과 집계
        partition_results = {}
        
        for result in results:
            partition_name = result.get('partition_name')
            if not partition_name:
                continue
            
            # task 정보에서 파티션 전략 가져오기
            task = result.get('task', {})
            partition_strategy = task.get('partition_strategy', 'full')
            
            if partition_name not in partition_results:
                partition_results[partition_name] = {
                    'partition': partition_name,
                    'strategy': partition_strategy,
                    'status': 'OK' if result.get('match', False) else 'FAILED',
                    'checks': [],
                    'rows': 0,
                    'hash_status': 'match' if result.get('match', False) else 'mismatch',
                    'drilldown_status': 'skipped'
                }
            
            # Hash 검증 결과
            if result.get('match', False):
                if 'hash' not in partition_results[partition_name]['checks']:
                    partition_results[partition_name]['checks'].append('hash')
            
            # Drilldown 결과
            if 'drilldown' in result:
                partition_results[partition_name]['drilldown_status'] = 'limited'
                if 'drilldown' not in partition_results[partition_name]['checks']:
                    partition_results[partition_name]['checks'].append('drilldown')
        
        if not partition_results:
            return None
        
        # 요약 통계
        total_partitions = len(partition_results)
        fully_verified = sum(1 for p in partition_results.values() if p['strategy'] == 'full' and p['status'] == 'OK')
        light_verified = sum(1 for p in partition_results.values() if p['strategy'] == 'light' and p['status'] == 'OK')
        failed = sum(1 for p in partition_results.values() if p['status'] == 'FAILED')
        
        return {
            'total_partitions': total_partitions,
            'fully_verified': fully_verified,
            'light_verified': light_verified,
            'failed': failed,
            'partition_results': list(partition_results.values())
        }
    
    def generate_aggregate_json_report(self, aggregate_results: Dict[str, Any],
                                       table_name: str, schema: str) -> str:
        """
        집계 검증 JSON 리포트 생성
        
        Args:
            aggregate_results: 집계 검증 결과
            table_name: 테이블명
            schema: 스키마명
        
        Returns:
            생성된 리포트 파일 경로
        """
        report = {
            'metadata': {
                'table_name': table_name,
                'schema': schema,
                'generated_at': datetime.now().isoformat(),
                'report_type': 'aggregate_validation'
            },
            'summary': {
                'match': aggregate_results.get('match', False),
                'total_columns': len(aggregate_results.get('columns', {})),
                'matched_columns': sum(
                    1 for col_result in aggregate_results.get('columns', {}).values()
                    if col_result.get('match', False)
                )
            },
            'results': aggregate_results
        }
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{schema}_{table_name}_aggregate_{timestamp}.json"
        filepath = self.output_dir / filename
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False, default=str)
            
            logger.info(f"Aggregate JSON report generated: {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Failed to generate aggregate JSON report: {e}")
            raise
    
    def generate_readable_summary(self, validation_results: List[Dict[str, Any]],
                                  aggregate_results: Dict[str, Any],
                                  table_name: str, schema: str,
                                  ddl_report: Optional[Dict[str, Any]] = None) -> str:
        """
        읽기 쉬운 텍스트 요약 리포트 생성
        
        Args:
            validation_results: 검증 결과 리스트
            aggregate_results: 집계 검증 결과
            table_name: 테이블명
            schema: 스키마명
            ddl_report: DDL 리포트 (옵션)
        
        Returns:
            생성된 리포트 파일 경로
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{schema}_{table_name}_summary_{timestamp}.txt"
        filepath = self.output_dir / filename
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                # 헤더
                f.write("=" * 80 + "\n")
                f.write(f"데이터 검증 결과 요약\n")
                f.write("=" * 80 + "\n\n")
                
                f.write(f"테이블: {schema}.{table_name}\n")
                f.write(f"생성 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                
                # 1. 전체 검증 결과
                f.write("-" * 80 + "\n")
                f.write("📊 전체 검증 결과\n")
                f.write("-" * 80 + "\n\n")
                
                summary = self._generate_summary(validation_results)
                total_chunks = summary['total_chunks']
                matched_chunks = summary['matched_chunks']
                mismatched_chunks = summary['mismatched_chunks']
                error_chunks = summary['error_chunks']
                match_rate = summary['match_rate']
                
                # Chunk 검증 결과
                if total_chunks > 0:
                    status_icon = "✅" if match_rate == 1.0 else "❌"
                    f.write(f"{status_icon} Chunk 검증: {matched_chunks}/{total_chunks} 일치 ({match_rate*100:.1f}%)\n")
                    if mismatched_chunks > 0:
                        f.write(f"   ⚠️ 불일치 Chunk: {mismatched_chunks}개\n")
                    if error_chunks > 0:
                        f.write(f"   ❌ 오류 Chunk: {error_chunks}개\n")
                else:
                    f.write("⚠️ 검증 결과 없음\n")
                
                f.write("\n")
                
                # 2. 집계 검증 결과
                f.write("-" * 80 + "\n")
                f.write("📈 집계 검증 결과\n")
                f.write("-" * 80 + "\n\n")
                
                agg_match = aggregate_results.get('match', False)
                agg_status_icon = "✅" if agg_match else "❌"
                f.write(f"{agg_status_icon} 집계 검증: {'통과' if agg_match else '실패'}\n\n")
                
                # 컬럼별 결과
                columns = aggregate_results.get('columns', {})
                if columns:
                    matched_cols = sum(1 for col in columns.values() if col.get('match', False))
                    total_cols = len(columns)
                    f.write(f"   컬럼 일치: {matched_cols}/{total_cols}개\n\n")
                    
                    # 불일치 컬럼 상세
                    mismatch_cols = [name for name, col in columns.items() if not col.get('match', False)]
                    if mismatch_cols:
                        f.write("   ⚠️ 불일치 컬럼:\n")
                        for col_name in mismatch_cols[:5]:  # 최대 5개만
                            col_result = columns[col_name]
                            comp = col_result.get('comparison', {})
                            f.write(f"      - {col_name}: ")
                            issues = []
                            if not comp.get('count_match'):
                                issues.append("COUNT 불일치")
                            if not comp.get('min_match'):
                                issues.append("MIN 불일치")
                            if not comp.get('max_match'):
                                issues.append("MAX 불일치")
                            if not comp.get('sum_match'):
                                issues.append("SUM 불일치")
                            f.write(", ".join(issues) if issues else "통계 불일치")
                            f.write("\n")
                        if len(mismatch_cols) > 5:
                            f.write(f"      ... 외 {len(mismatch_cols) - 5}개\n")
                
                f.write("\n")
                
                # 3. 최종 판정
                f.write("-" * 80 + "\n")
                f.write("🎯 최종 판정\n")
                f.write("-" * 80 + "\n\n")
                
                chunk_ok = match_rate == 1.0 and error_chunks == 0
                agg_ok = agg_match
                final_ok = chunk_ok and agg_ok
                
                if final_ok:
                    f.write("✅ 검증 통과: 모든 데이터가 일치합니다.\n")
                else:
                    f.write("❌ 검증 실패: 데이터 불일치가 발견되었습니다.\n")
                    if not chunk_ok:
                        f.write("   - Chunk 해시 검증 실패\n")
                    if not agg_ok:
                        f.write("   - 집계 검증 실패\n")
                
                f.write("\n")
                
                # 4. DDL 권장사항 (간단히)
                if ddl_report:
                    changeable = ddl_report.get('changeable_columns', [])
                    if changeable:
                        f.write("-" * 80 + "\n")
                        f.write("💡 DDL 최적화 권장사항\n")
                        f.write("-" * 80 + "\n\n")
                        f.write(f"타입 최적화 가능한 컬럼: {len(changeable)}개\n")
                        f.write("(상세 내용은 DDL 리포트 파일 참조)\n\n")
                
                # 5. 상세 리포트 파일 안내
                f.write("=" * 80 + "\n")
                f.write("📄 상세 리포트\n")
                f.write("=" * 80 + "\n\n")
                f.write(f"JSON 리포트: {schema}_{table_name}_{timestamp}.json\n")
                f.write(f"집계 리포트: {schema}_{table_name}_aggregate_{timestamp}.json\n")
                if ddl_report:
                    f.write(f"DDL 리포트: {schema}_{table_name}_ddl_{timestamp}.sql\n")
                f.write("\n")
            
            logger.info(f"Readable summary report generated: {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Failed to generate readable summary: {e}")
            raise
