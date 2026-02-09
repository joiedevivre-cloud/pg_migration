# 프로젝트 아키텍처 (상세 기술 문서)

> **목적**: 모듈별 API, 구현 세부사항. **대상**: 개발자.  
> **전체 흐름·다이어그램**: `SYSTEM_ARCHITECTURE_DIAGRAM.md`, `시스템_아키텍처_다이어그램.md` 참조.

---

## 1. 핵심 개념

이 프로젝트는 Oracle과 PostgreSQL 간 **데이터 일치 검증** 및 **지능형 DDL 추천**을 수행하는 통합 도구다.  
**핵심 가치**: DMS/ora2pg를 넘어서는 검증 기반 마이그레이션 의사결정 + 타입 추천(날짜/숫자/코드) 자동화.

---

## 2. 설정 (Config) — 단일 소스

- **단일 설정 파일**: `config.yaml`  
  접속 정보·이관·검증·DDL 리포트용 스키마/테이블을 모두 여기서 관리. 코드 내 하드코딩 없음.
- **로더**: `config/connection_config.py`
  - `load_config(path)` → 전체 설정 딕셔너리
  - `get_oracle_connection_params(config)` → Oracle DSN, user, password
  - `get_postgres_connection_params(config)` → host, port, database, user, password
  - `get_migration_config(config)` → source_schema, target_schema, tables, truncate_before_insert, batch_size, verify_only, ddl_report_table, ddl_report_schema
  - `get_validation_config(config)` → source_schema, target_schema, tables, chunk_size, max_workers, decimal_tolerance, null_empty_policy 등

**사용처**: `run_validation.py`, `run_ddl_report.py`, `db_sync_tool.py`, `data_migrator.py`, `generate_ddl.py` — 모두 위 로더만 사용.

---

## 3. 진입점 (Entry Points)

| 스크립트 | 역할 |
|----------|------|
| **run_ddl_report.py** | DDL 추천 리포트만 실행. 인자: `[테이블명] [스키마]`. `--create` 시 추천 타입으로 PG 테이블 생성. |
| **run_validation.py** | 검증 파이프라인 전체 실행 (집계 → Chunk Hash → 샘플링 → DDL 리포트). |
| **data_migrator.py** | 데이터 이관 + 이관 후 검증(Chunk Hash, 집계). |
| **db_sync_tool.py** | Oracle 메타 기반 CREATE TABLE + PK → PostgreSQL. |
| **generate_ddl.py** | DDL 생성 라이브러리(ResultProcessor·매핑 등) — 주로 run_ddl_report/db_sync_tool/engine에서 사용. |

---

## 4. 전체 아키텍처

```
                    config.yaml (단일 설정)
                              │
    ┌─────────────────────────┼─────────────────────────┐
    ▼                         ▼                         ▼
run_ddl_report.py      run_validation.py         data_migrator.py
    │                         │                         │
    │  Oracle + PG 메타/통계   │  ValidationEngine       │  Keyset Pagination
    │  → DDL 추천 리포트      │  → 집계 → Chunk Hash    │  → 이관 후 검증
    │  → reports/*.sql, *.json│  → DDL 리포트·리포트    │
    ▼                         ▼                         ▼
validator/core/result  validator/core/engine    checks/aggregate
checks/aggregate       checks/chunk_hash       checks/chunk_hash
db/oracle, postgres    db/chunk_strategy       db/oracle, postgres
```

- **DB Layer**: `db/oracle.py`, `db/postgres.py`, `db/chunk_strategy.py`
- **Check Layer**: `checks/aggregate.py`(통계·타입 추천), `checks/chunk_hash.py`, `checks/aggregate_validation.py`, `checks/row_decimal.py`, `checks/row_canonicalize.py`, `validator/core/partition_aware.py`, LOB 등
- **Core**: `validator/core/engine.py`, `validator/core/result.py`, `validator/core/report.py`

---

## 5. 지능형 DDL 추천 파이프라인 (3단계)

### 5.1 단계 개요

| 단계 | 내용 |
|------|------|
| **Stage 1 (안전성)** | PK/FK 컬럼은 **항상 BIGINT 유지** (운영 안정성, 21억 건 이상 대비). SMALLINT/INTEGER로 다운캐스트 추천하지 않음. |
| **Stage 2 (프로파일링)** | Oracle 메타 + 통계 수집. DATE/TIMESTAMP 컬럼에 대해 **시분초 존재 여부(has_non_zero_time)** 수집. |
| **Stage 3 (추천)** | 데이터·메타 기반으로 타입 추천 → 리포트 출력(모든 컬럼에 Oracle Type, DATE/TIMESTAMP는 추천 근거 포함). |

### 5.2 날짜/시간 판정 (문자열·숫자 패턴 + 시분초 유무)

- **날짜성 데이터 분류**
  - **8자리 정수 (YYYYMMDD)**: `19000101` ~ `99991231` → 시간 없음 → **DATE** 추천.
  - **하이픈 날짜/날짜시간**: `2026-01-11`, `2026-01-11 01:43:14` 형태 문자열 또는 `datetime`/`date` 인스턴스 → 날짜성으로 분류.
- **시분초 기반 DATE vs TIMESTAMP**
  - **Case A (DATE)**: Oracle/Postgres에서 **모든 행**의 시간이 `00:00:00`인 경우.  
    → **Rationale**: "Optimizes storage (4 bytes saved per row) as time data is empty."
  - **Case B (TIMESTAMP)**: **한 행이라도** 시분초가 `00:00:00`이 아닌 경우.  
    → **Rationale**: "Preserves time precision captured in Oracle."
- **통계 수집**: `collect_column_stats(..., oracle_column_types=...)` 호출 시, Oracle이 DATE/TIMESTAMP인 컬럼에 대해:
  - Oracle: `MAX(CASE WHEN TO_CHAR(col, 'HH24:MI:SS') != '00:00:00' THEN 1 ELSE 0 END)` → `has_non_zero_time`
  - PostgreSQL: `MAX(CASE WHEN (col::timestamp::time) != '00:00:00' THEN 1 ELSE 0 END)` → `has_non_zero_time`

### 5.3 숫자형 추천 (문서 PAGE 2 기준)

- **precision ≤ 4** (및 scale 0 또는 None) → **SMALLINT** (인덱스·메모리 절감).
- **precision ≤ 9** → **INTEGER**, **precision ≤ 18** → **BIGINT**.  
  데이터 범위도 함께 사용: `-32768..32767` → SMALLINT, `-2147483648..2147483647` → INTEGER, 그 이상 → BIGINT.
- **PK/FK**: 위 규칙과 무관하게 **항상 BIGINT** 유지.
- **OUT_OF_RANGE**: 실제 MIN/MAX가 BIGINT 범위를 넘으면 NUMERIC 유지(다운캐스트 후보에서 제외).

### 5.4 HAS_FRACTION 직접 체크 (숫자형)

- **목적**: "이 컬럼에 소수점 데이터가 한 건이라도 있는가?"를 **직접 쿼리**로 판단. NUMBER(NULL,NULL) 등 메타만으로는 정수 여부를 알 수 없음.
- **Oracle**: `SUM(CASE WHEN col IS NOT NULL AND col != TRUNC(col) THEN 1 ELSE 0 END)` → `has_decimal_count` / `has_fraction` / `fraction_row_count`
- **PostgreSQL**: 동일 의미로 `col != TRUNC(col)` 사용.
- **리포트**: changeable 컬럼에 `HAS_FRACTION: Y/N (fraction_row_count: N)` 출력.

### 5.5 OUT_OF_RANGE 안전 검증

- **목적**: MIN/MAX로 SMALLINT/INTEGER를 추천해도, **실제로 범위를 벗어난 행이 있는지** 검증.
- **API**: `count_out_of_range_rows(table_name, schema, column, target_type)`  
  - SMALLINT: `col < -32768 OR col > 32767`  
  - INTEGER: `col < -2147483648 OR col > 2147483647`  
  - Oracle·Postgres 각각 COUNT 후 합산.
- **동작**: SMALLINT/INTEGER 추천 전에 위 검증 호출. 1건이라도 초과 시 `can_downcast = False`, `reasons.append('OUT_OF_RANGE_VERIFIED')`.

### 5.6 Dry-Run CAST 시뮬레이션

- **목적**: ALTER 전에 PG에서 `col::target_type` 변환 시 **값이 달라지는 행 수**를 사전 탐지 (SUM_MISMATCH·손실 가능성).
- **API**: `dry_run_cast_loss_count(table_name, schema, column, target_type)`  
  - PG: `COUNT(*) WHERE col::numeric != col::smallint|integer|bigint::numeric`
- **리포트**: numeric_downcast_candidates에 `dry_run_loss_count` 반영, 주석으로 `Dry-run CAST loss rows: N` 출력.

### 5.7 인덱스·NULL 비율·테이블 크기 (효과 추정)

- **인덱스**: `get_indexed_columns(table_name, schema, columns)` — DBA_INDEXES + DBA_IND_COLUMNS. 리포트에 `In index: Y/N`.
- **NULL 비율 / Cardinality**: 통계에 `null_count` 수집 → `null_ratio = null_count/row_count`, `distinct_ratio = distinct_count/row_count`.  
  - `distinct_ratio < 0.01` → "(code/flag candidate)"  
  - `null_ratio > 0.9` → "(type change effect minimal)"
- **테이블 크기**: `get_pg_table_size_bytes(table_name, schema)` — `pg_total_relation_size`. 리포트 상단에 `Table size (PG): X.XX MB`.

### 5.8 DDL 리포트 출력 형식

- **모든 changeable 컬럼**: **Oracle Type** 표시 (NUMBER, DATE, VARCHAR2 등). `oracle_column_types`는 `get_column_metadata()`에서 조회해 전달.
- **숫자형**: `HAS_FRACTION: Y/N (fraction_row_count: N)`, `In index: Y/N`, `Distinct ratio`, `Null ratio` (해당 시 code/flag·effect minimal 문구).
- **DATE/TIMESTAMP 추천 컬럼**: 다음 4줄 추가.
  - `Oracle Type`: Oracle 실제 타입 또는 "NUMBER (Stored as YYYY-MM-DD HH24:MI:SS)" 등
  - `Profiling Result`: "Time portion detected (e.g., 01:43:14)" 또는 "No time values found (All 00:00:00)"
  - `Recommended`: DATE 또는 TIMESTAMP
  - `Rationale`: 위 Case A/B에 해당하는 문구
- **numeric_downcast_candidates**: `Dry-run CAST loss rows: N` (해당 시).

---

## 6. 데이터 흐름 (검증 프로세스)

1. **사용자** → `ValidationEngine.validate_table()` 또는 `run_ddl_report.py`(테이블·스키마 인자 또는 config).
2. **설정**: `config.yaml` → connection_config 로드. 스키마/테이블은 인자 > config (하드코딩 없음).
3. **Oracle 메타**: `get_column_metadata()` → data_type, precision, scale. `oracle_column_types`는 DDL 리포트·통계 수집에 사용.
4. **통계 수집**: `AggregateStatsCollector.collect_column_stats(..., oracle_column_types=...)` → 컬럼별 min/max, has_decimal, **has_non_zero_time**(DATE/TIMESTAMP 컬럼).
5. **타입 추천**: `recommend_numeric_type(stats)` → 날짜 패턴·has_non_zero_time → DATE/TIMESTAMP; 숫자 범위 → SMALLINT/INTEGER/BIGINT; PK/FK는 상위에서 BIGINT 고정.
6. **1단계 집계**: COUNT, MIN, MAX, COUNT DISTINCT, SUM, AVG. Decimal 통일·scale quantize·metric 단위 비교.
7. **2단계 Chunk Hash**: 컬럼 결합 `col1||'|'||col2||...`, Oracle `LOWER(RAWTOHEX(STANDARD_HASH(CONVERT(concat,'AL32UTF8'),'SHA256')))`, Postgres `encode(digest(...,'sha256'),'hex')` + 컬럼명 lower(). Hash만 비교.
8. **3단계 샘플링**: 오류 구간만 fetchone() Row-by-row, max_diffs_per_chunk 조기 종료.
9. **DDL 생성**: Numeric Precision Decision (HAS_FRACTION, OUT_OF_RANGE, **OUT_OF_RANGE_VERIFIED**, TOLERANCE_EXCEEDED, SUM_MISMATCH, HASH_MISMATCH) → numeric_downcast_candidates, ALTER TABLE 권고. SMALLINT/INTEGER 추천 전 **count_out_of_range_rows** 검증. **dry_run_cast_loss_count**로 PG 변환 손실 행 수 수집. DATE/TIMESTAMP는 changeable_columns에서 Rationale·Profiling Result 포함. **get_indexed_columns**, **get_pg_table_size_bytes**, null_ratio·distinct_ratio 반영.
10. **리포트**: JSON, 집계, 요약, DDL. `run_ddl_report.py`는 추가로 **reports/** 에 `{schema}_{table}_ddl_{timestamp}.sql`, `.json` 저장.

---

## 7. 모듈별 역할 및 API

### config/connection_config.py
- **역할**: config.yaml 단일 소스 로드, Oracle/Postgres 접속·이관·검증 설정 반환.
- **함수**: `load_config()`, `get_oracle_connection_params()`, `get_postgres_connection_params()`, `get_migration_config()`, `get_validation_config()`.

### run_ddl_report.py
- **역할**: DDL 추천 리포트만 실행. 테이블·스키마는 CLI 인자 또는 config.
- **동작**: Oracle/PG 연결 → 컬럼 목록·메타( precision/scale/data_type) → `generate_ddl_report(oracle_column_types=...)` → 콘솔 출력 + **reports/** 에 .sql·.json 저장. `--create` 시 추천 타입으로 PG에 CREATE TABLE + PK 실행.

### validator/core/engine.py
- **역할**: 검증 오케스트레이션, 병렬 제어, Drill-down. DDL 리포트 생성 시 `oracle_column_types` 전달.
- **메서드**: `validate_table()`, `validate_with_sampling()`, `validate_chunk_sample()`, `validate_chunks_parallel()`, `validate_rows_parallel()`.

### checks/aggregate.py
- **역할**: 컬럼별 통계 수집 + **타입 추천** + **안전 검증·효과 추정**.
- **API**:
  - `collect_column_stats(table_name, schema, columns, where_clause, oracle_column_types=None)`  
    → DATE/TIMESTAMP: `has_non_zero_time`. 숫자형: **HAS_FRACTION** (Oracle/Postgres `col != TRUNC(col)` 기반 `has_fraction`, `fraction_row_count`), `null_count`.
  - `recommend_numeric_type(stats)`  
    → 8자리 정수(YYYYMMDD)→DATE; 하이픈/날짜시간 + has_non_zero_time→DATE 또는 TIMESTAMP; 숫자 범위→SMALLINT/INTEGER/BIGINT.
  - `count_out_of_range_rows(table_name, schema, column, target_type, where_clause)`  
    → SMALLINT/INTEGER 범위 초과 행 수 (Oracle+Postgres). 추천 전 안전 검증용.
  - `dry_run_cast_loss_count(table_name, schema, column, target_type, where_clause)`  
    → PG에서 `col::target_type::numeric != col::numeric` 인 행 수 (변환 손실 가능).
  - `get_indexed_columns(table_name, schema, columns)`  
    → DBA_INDEXES + DBA_IND_COLUMNS. 인덱스 포함 컬럼 목록.
  - `get_pg_table_size_bytes(table_name, schema)`  
    → `pg_total_relation_size`. 테이블 크기(바이트).
  - `get_pk_columns`, `get_fk_columns`  
    → PK/FK 컬럼 (result.py에서 BIGINT 유지에 사용).
- **PK/FK**: result.py에서 처리(BIGINT 유지). aggregate는 컬럼명 무관 규칙만 적용.

### validator/core/result.py
- **역할**: DDL 구문 생성, DDL 리포트 생성, 리포트 포맷팅(SQL 주석으로 Oracle Type·추천 근거 출력).
- **API**:
  - `generate_ddl_report(..., oracle_column_types=None)`  
    → `collect_column_stats(..., oracle_column_types=...)` 호출, changeable_columns에 `oracle_type_display`, `profiling_result`, `rationale` (DATE/TIMESTAMP 시) 설정.
  - `format_ddl_output(report)`  
    → changeable_columns에서 rationale 있으면 Oracle Type / Profiling Result / Recommended / Rationale 블록 출력; 나머지 컬럼은 Oracle Type + Current/Recommended/Migration Type 등 기존 형식.
  - `map_oracle_number_to_postgres(precision, scale, has_decimal)`  
    → precision≤4 → SMALLINT, ≤9 → INTEGER, ≤18 → BIGINT (scale 0/None 시). PK/FK는 generate_alter_table_ddl·numeric_downcast 쪽에서 BIGINT 유지.

### db/oracle.py
- **역할**: Oracle 연결, PK·파티션·**컬럼 메타데이터(data_type, precision, scale)** 조회.
- **메서드**: `get_primary_key_columns()`, `is_composite_primary_key()`, `create_partition_validation_tasks()`, `get_column_metadata()`.

### db/postgres.py
- **역할**: PostgreSQL 연결, 쿼리 실행, DDL 실행.

### db/chunk_strategy.py
- **역할**: Chunk 10,000건 생성. BETWEEN(단일 PK), Keyset(복합 PK). 결정적 정렬.

### checks/chunk_hash.py
- **역할**: Chunk 단위 해시. Canonicalization, 컬럼 `'|'` 결합, Oracle AL32UTF8+LOWER(RAWTOHEX), Postgres encode(digest)+컬럼명 lower().

### checks/aggregate_validation.py
- **역할**: 1단계 집계. Decimal 통일, scale quantize, metric 단위 비교.

### validator/core/report.py
- **역할**: JSON·요약·집계 리포트 생성. 출력 디렉터리 기본값 `reports/`.

---

## 8. 리포트 출력 위치 및 종류

- **공통 출력 디렉터리**: **reports/** (프로젝트 루트 기준. `ReportGenerator(output_dir="reports")`, engine에서 DDL 파일도 `reports/`에 저장).
- **run_ddl_report.py**:
  - `reports/{schema}_{table_name}_ddl_{timestamp}.sql` — 전체 DDL 스크립트. 주석 포함: **Table size (PG)**, Oracle Type, **HAS_FRACTION**, **In index**, **Distinct ratio**, **Null ratio**, DATE/TIMESTAMP 추천 근거, **Dry-run CAST loss rows**.
  - `reports/{schema}_{table_name}_ddl_{timestamp}.json` — 동일 내용의 머신 리드 가능 포맷.
- **run_validation.py (검증 실행 시)**:
  - 요약: `*_summary_*.txt`
  - JSON: `*.json` (ddl_recommendations, numeric_downcast_candidates 포함)
  - 집계: `*_aggregate_*.json`
  - DDL: `*_ddl_*.sql`

---

## 9. 데이터 정규화(Canonicalization) 상세

(기존과 동일) 이기종 DB 간 수치 정밀도 일치화 — Oracle `TO_CHAR(..., 'FM...')`, Postgres `::numeric(p,s)` + 동일 포맷.  
구현: `checks/chunk_hash.py` (메타데이터 PRECISION/SCALE 기반), Postgres information_schema 기반.

---

## 10. 핵심 설계 원칙

| 원칙 | 내용 |
|------|------|
| 설정 단일 소스 | config.yaml + connection_config만 사용. 포트·스키마·테이블 하드코딩 없음. |
| 결정적 페이징 | ROWNUM/OFFSET 금지. Keyset Pagination만. |
| 해시 정규화 | 컬럼 `'|'` 결합, Oracle CONVERT(AL32UTF8)+LOWER(RAWTOHEX), Postgres encode(digest)+컬럼명 lower(). |
| Decimal·metric | Float 금지. 집계는 to_decimal→scale quantize→`==` 비교. |
| PK/FK BIGINT | 모든 PK/FK 컬럼은 SMALLINT/INTEGER 추천 제외, BIGINT 유지. |
| 날짜 추천 | 8자리 정수→DATE; 시분초 유무(has_non_zero_time)→DATE vs TIMESTAMP; 추천 근거 출력. |
| 숫자 추천 | precision≤4 → SMALLINT(PAGE 2); 데이터 범위로 SMALLINT/INTEGER/BIGINT; 리포트에 Oracle Type 전 컬럼 표기. |

---

## 11. 파티션 인식 검증 (요약)

- **목적**: 로그/시계열 대용량 테이블을 파티션별로 효율 검증.
- **설정**: `validation_mode.type: partition_aware`, `partition_key`, `immutable_before`.
- **구현**: db/oracle.py·postgres.py 파티션 메타, validator/core/partition_aware.py, engine 연동.

---

## 12. 확장 포인트

- 새 검증 방식: `checks/` 모듈 추가.
- 새 리포트: `validator/core/report.py` 확장.
- 새 Chunk 전략: `db/chunk_strategy.py` 확장.

---

## 13. 로드맵

- **Phase 0·1**: 안정성·파티션 인식 완료.
- **Phase 2**: 지능형 DDL 추천 완료. 추가 반영: **HAS_FRACTION**(TRUNC 기반), **OUT_OF_RANGE 검증**(count_out_of_range_rows), **Dry-run CAST** 손실 행 수, **인덱스 정보**·**NULL/distinct 비율**·**테이블 크기** 효과 추정. 성능 리스크·REPLAY-lite·운영 리포트는 계획.
- **Phase 3**: 대시보드, 스케줄링, 알림.
