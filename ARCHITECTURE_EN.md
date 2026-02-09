# Project Architecture (Detailed Technical Documentation)

> **Purpose**: Module APIs, implementation details. **Audience**: Developers.  
> **Overall flow / diagrams**: See `SYSTEM_ARCHITECTURE_DIAGRAM.md`.

---

## 1. Core Concept

This project performs **data consistency validation** and **intelligent DDL recommendations** between Oracle and PostgreSQL.  
**Core value**: Validation-based migration decision engine + automated type recommendations (date/time, numeric, code columns).

---

## 2. Configuration (Config) — Single Source

- **Single config file**: `config.yaml`  
  All connection, migration, validation, and DDL-report schema/table settings are managed here. No hardcoding in code.
- **Loader**: `config/connection_config.py`
  - `load_config(path)` → full config dict
  - `get_oracle_connection_params(config)` → Oracle DSN, user, password
  - `get_postgres_connection_params(config)` → host, port, database, user, password
  - `get_migration_config(config)` → source_schema, target_schema, tables, truncate_before_insert, batch_size, verify_only, ddl_report_table, ddl_report_schema
  - `get_validation_config(config)` → source_schema, target_schema, tables, chunk_size, max_workers, decimal_tolerance, null_empty_policy, etc.

**Used by**: `run_validation.py`, `run_ddl_report.py`, `db_sync_tool.py`, `data_migrator.py`, `generate_ddl.py` — all use this loader only.

---

## 3. Entry Points

| Script | Role |
|--------|------|
| **run_ddl_report.py** | DDL recommendation report only. Args: `[table] [schema]`. `--create` creates the PG table with recommended types. |
| **run_validation.py** | Full validation pipeline (aggregate → Chunk Hash → sampling → DDL report). |
| **data_migrator.py** | Data migration + post-migration validation (Chunk Hash, aggregate). |
| **db_sync_tool.py** | CREATE TABLE + PK from Oracle metadata → PostgreSQL. |
| **generate_ddl.py** | DDL generation library (ResultProcessor, mapping, etc.) — used by run_ddl_report, db_sync_tool, engine. |

---

## 4. Overall Architecture

```
                    config.yaml (single source)
                              │
    ┌─────────────────────────┼─────────────────────────┐
    ▼                         ▼                         ▼
run_ddl_report.py      run_validation.py         data_migrator.py
    │                         │                         │
    │  Oracle + PG metadata   │  ValidationEngine       │  Keyset Pagination
    │  and stats → DDL report │  → Aggregate → Chunk    │  → post-migration
    │  → reports/*.sql, *.json│  Hash → DDL report       │    validation
    ▼                         ▼                         ▼
validator/core/result  validator/core/engine    checks/aggregate
checks/aggregate       checks/chunk_hash        checks/chunk_hash
db/oracle, postgres    db/chunk_strategy       db/oracle, postgres
```

- **DB Layer**: `db/oracle.py`, `db/postgres.py`, `db/chunk_strategy.py`
- **Check Layer**: `checks/aggregate.py` (stats, type recommendation), `checks/chunk_hash.py`, `checks/aggregate_validation.py`, `checks/row_decimal.py`, `checks/row_canonicalize.py`, `validator/core/partition_aware.py`, LOB, etc.
- **Core**: `validator/core/engine.py`, `validator/core/result.py`, `validator/core/report.py`

---

## 5. Intelligent DDL Recommendation Pipeline (3 Stages)

### 5.1 Stage Overview

| Stage | Content |
|-------|---------|
| **Stage 1 (Safety)** | PK/FK columns **always remain BIGINT** (operational stability, >2.1B rows). No SMALLINT/INTEGER downcast for them. |
| **Stage 2 (Profiling)** | Oracle metadata + stats collection. For DATE/TIMESTAMP columns, **has_non_zero_time** is collected. |
| **Stage 3 (Recommendation)** | Type recommendations from data and metadata → report output (Oracle Type for all columns; DATE/TIMESTAMP with rationale). |

### 5.2 Date/Time Classification (Pattern + Time Portion)

- **Date-like data**
  - **8-digit integer (YYYYMMDD)**: `19000101`–`99991231` → no time → recommend **DATE**.
  - **Hyphen date/datetime**: Strings like `2026-01-11`, `2026-01-11 01:43:14` or `datetime`/`date` instances → treated as date-like.
- **DATE vs TIMESTAMP by time portion**
  - **Case A (DATE)**: All rows have time `00:00:00` in Oracle/Postgres.  
    → **Rationale**: "Optimizes storage (4 bytes saved per row) as time data is empty."
  - **Case B (TIMESTAMP)**: At least one row has time ≠ `00:00:00`.  
    → **Rationale**: "Preserves time precision captured in Oracle."
- **Stats collection**: When `collect_column_stats(..., oracle_column_types=...)` is called, for Oracle DATE/TIMESTAMP columns:
  - Oracle: `MAX(CASE WHEN TO_CHAR(col, 'HH24:MI:SS') != '00:00:00' THEN 1 ELSE 0 END)` → `has_non_zero_time`
  - PostgreSQL: `MAX(CASE WHEN (col::timestamp::time) != '00:00:00' THEN 1 ELSE 0 END)` → `has_non_zero_time`

### 5.3 Numeric Recommendations (Document PAGE 2)

- **precision ≤ 4** (scale 0 or None) → **SMALLINT** (index/memory savings).
- **precision ≤ 9** → **INTEGER**, **precision ≤ 18** → **BIGINT**.  
  Data range is also used: `-32768..32767` → SMALLINT, `-2147483648..2147483647` → INTEGER, otherwise → BIGINT.
- **PK/FK**: Regardless of the above, **always BIGINT**.
- **OUT_OF_RANGE**: If actual MIN/MAX exceed BIGINT range, keep NUMERIC (excluded from downcast candidates).

### 5.4 HAS_FRACTION Direct Check (Numeric)

- **Purpose**: Answer "Does this column have any fractional data?" via **direct query**. Metadata alone (e.g. NUMBER(NULL,NULL)) cannot tell integer vs fractional.
- **Oracle**: `SUM(CASE WHEN col IS NOT NULL AND col != TRUNC(col) THEN 1 ELSE 0 END)` → `has_decimal_count` / `has_fraction` / `fraction_row_count`.
- **PostgreSQL**: Same logic using `col != TRUNC(col)`.
- **Report**: For changeable columns, output `HAS_FRACTION: Y/N (fraction_row_count: N)`.

### 5.5 OUT_OF_RANGE Safety Verification

- **Purpose**: Before recommending SMALLINT/INTEGER, verify that **no row actually exceeds** the target type range.
- **API**: `count_out_of_range_rows(table_name, schema, column, target_type)`  
  - SMALLINT: `col < -32768 OR col > 32767`  
  - INTEGER: `col < -2147483648 OR col > 2147483647`  
  - Count on Oracle and Postgres separately, then sum.
- **Behavior**: Call before SMALLINT/INTEGER recommendation. If any row is out of range → `can_downcast = False`, `reasons.append('OUT_OF_RANGE_VERIFIED')`.

### 5.6 Dry-Run CAST Simulation

- **Purpose**: Before ALTER, detect how many rows **change value** when casting to target type on PG (SUM_MISMATCH / loss risk).
- **API**: `dry_run_cast_loss_count(table_name, schema, column, target_type)`  
  - PG: `COUNT(*) WHERE col::numeric != col::smallint|integer|bigint::numeric`.
- **Report**: Store `dry_run_loss_count` in numeric_downcast_candidates; output comment `Dry-run CAST loss rows: N`.

### 5.7 Index, NULL Ratio, Table Size (Effect Estimation)

- **Index**: `get_indexed_columns(table_name, schema, columns)` — DBA_INDEXES + DBA_IND_COLUMNS. Report: `In index: Y/N`.
- **NULL ratio / Cardinality**: Collect `null_count` in stats → `null_ratio = null_count/row_count`, `distinct_ratio = distinct_count/row_count`.  
  - `distinct_ratio < 0.01` → "(code/flag candidate)"  
  - `null_ratio > 0.9` → "(type change effect minimal)"
- **Table size**: `get_pg_table_size_bytes(table_name, schema)` — `pg_total_relation_size`. Report header: `Table size (PG): X.XX MB`.

### 5.8 DDL Report Output Format

- **All changeable columns**: **Oracle Type** (NUMBER, DATE, VARCHAR2, etc.). `oracle_column_types` from `get_column_metadata()`.
- **Numeric**: `HAS_FRACTION: Y/N (fraction_row_count: N)`, `In index: Y/N`, `Distinct ratio`, `Null ratio` (with code/flag or effect minimal text when applicable).
- **DATE/TIMESTAMP recommended columns**: Four additional lines:
  - `Oracle Type`: Actual Oracle type or e.g. "NUMBER (Stored as YYYY-MM-DD HH24:MI:SS)"
  - `Profiling Result`: "Time portion detected (e.g., 01:43:14)" or "No time values found (All 00:00:00)"
  - `Recommended`: DATE or TIMESTAMP
  - `Rationale`: Text for Case A or B above.
- **numeric_downcast_candidates**: `Dry-run CAST loss rows: N` when applicable.

---

## 6. Data Flow (Validation Process)

1. **User** → `ValidationEngine.validate_table()` or `run_ddl_report.py` (table/schema args or config).
2. **Config**: `config.yaml` → connection_config load. Schema/table: CLI args > config (no hardcoding).
3. **Oracle metadata**: `get_column_metadata()` → data_type, precision, scale. `oracle_column_types` used for DDL report and stats.
4. **Stats collection**: `AggregateStatsCollector.collect_column_stats(..., oracle_column_types=...)` → per-column min/max, has_decimal, **has_non_zero_time** (DATE/TIMESTAMP columns).
5. **Type recommendation**: `recommend_numeric_type(stats)` → date pattern and has_non_zero_time → DATE/TIMESTAMP; numeric range → SMALLINT/INTEGER/BIGINT; PK/FK fixed to BIGINT upstream.
6. **Step 1 Aggregate**: COUNT, MIN, MAX, COUNT DISTINCT, SUM, AVG. Decimal normalization, scale quantize, metric-only comparison.
7. **Step 2 Chunk Hash**: Column concat `col1||'|'||col2||...`, Oracle `LOWER(RAWTOHEX(STANDARD_HASH(CONVERT(concat,'AL32UTF8'),'SHA256')))`, Postgres `encode(digest(...,'sha256'),'hex')` + column names lower(). Compare hashes only.
8. **Step 3 Sampling**: fetchone() row-by-row only in error chunks; early exit on max_diffs_per_chunk.
9. **DDL generation**: Numeric Precision Decision (HAS_FRACTION, OUT_OF_RANGE, **OUT_OF_RANGE_VERIFIED**, TOLERANCE_EXCEEDED, SUM_MISMATCH, HASH_MISMATCH) → numeric_downcast_candidates, ALTER TABLE recommendations. **count_out_of_range_rows** before SMALLINT/INTEGER recommendation. **dry_run_cast_loss_count** for PG cast loss row count. **get_indexed_columns**, **get_pg_table_size_bytes**, null_ratio and distinct_ratio reflected. DATE/TIMESTAMP get Profiling Result and Rationale in changeable_columns.
10. **Reports**: JSON, aggregate, summary, DDL. `run_ddl_report.py` additionally writes **reports/** `{schema}_{table}_ddl_{timestamp}.sql` and `.json`.

---

## 7. Module Roles and API

### config/connection_config.py
- **Role**: Load config.yaml; return Oracle/Postgres connection and migration/validation settings.
- **Functions**: `load_config()`, `get_oracle_connection_params()`, `get_postgres_connection_params()`, `get_migration_config()`, `get_validation_config()`.

### run_ddl_report.py
- **Role**: Run DDL recommendation report only. Table/schema from CLI args or config.
- **Behavior**: Connect Oracle/PG → column list and metadata (precision, scale, data_type) → `generate_ddl_report(oracle_column_types=...)` → console output + **reports/** `.sql` and `.json`. With `--create`, creates PG table with recommended types and PK.

### validator/core/engine.py
- **Role**: Validation orchestration, parallel control, drill-down. Passes `oracle_column_types` when generating DDL report.
- **Methods**: `validate_table()`, `validate_with_sampling()`, `validate_chunk_sample()`, `validate_chunks_parallel()`, `validate_rows_parallel()`.

### checks/aggregate.py
- **Role**: Per-column stats + **type recommendation** + **safety checks and effect estimation**.
- **API**:
  - `collect_column_stats(table_name, schema, columns, where_clause, oracle_column_types=None)`  
    → DATE/TIMESTAMP: `has_non_zero_time`. Numeric: **HAS_FRACTION** (Oracle/Postgres `col != TRUNC(col)` → `has_fraction`, `fraction_row_count`), `null_count`.
  - `recommend_numeric_type(stats)`  
    → 8-digit integer (YYYYMMDD) → DATE; hyphen/datetime + has_non_zero_time → DATE or TIMESTAMP; numeric range → SMALLINT/INTEGER/BIGINT.
  - `count_out_of_range_rows(table_name, schema, column, target_type, where_clause)`  
    → Count of rows exceeding SMALLINT/INTEGER range (Oracle + Postgres). Used before downcast recommendation.
  - `dry_run_cast_loss_count(table_name, schema, column, target_type, where_clause)`  
    → PG rows where `col::target_type::numeric != col::numeric` (cast loss).
  - `get_indexed_columns(table_name, schema, columns)`  
    → DBA_INDEXES + DBA_IND_COLUMNS. List of columns in indexes.
  - `get_pg_table_size_bytes(table_name, schema)`  
    → `pg_total_relation_size`. Table size in bytes.
  - `get_pk_columns`, `get_fk_columns`  
    → PK/FK columns (used in result.py to keep BIGINT).
- **PK/FK**: Handled in result.py (BIGINT). Aggregate applies column-agnostic rules only.

### validator/core/result.py
- **Role**: DDL statement generation, DDL report generation, report formatting (SQL comments with Oracle Type and rationale).
- **API**:
  - `generate_ddl_report(..., oracle_column_types=None)`  
    → Calls `collect_column_stats(..., oracle_column_types=...)`; sets `oracle_type_display`, `profiling_result`, `rationale` (for DATE/TIMESTAMP) in changeable_columns.
  - `format_ddl_output(report)`  
    → For changeable_columns with rationale: output Oracle Type / Profiling Result / Recommended / Rationale block; others: Oracle Type + Current/Recommended/Migration Type, etc.
  - `map_oracle_number_to_postgres(precision, scale, has_decimal)`  
    → precision≤4 → SMALLINT, ≤9 → INTEGER, ≤18 → BIGINT (when scale 0/None). PK/FK kept BIGINT in generate_alter_table_ddl and numeric_downcast logic.

### db/oracle.py
- **Role**: Oracle connection, PK/partition/**column metadata (data_type, precision, scale)**.
- **Methods**: `get_primary_key_columns()`, `is_composite_primary_key()`, `create_partition_validation_tasks()`, `get_column_metadata()`.

### db/postgres.py
- **Role**: PostgreSQL connection, query execution, DDL execution.

### db/chunk_strategy.py
- **Role**: Chunk creation (10,000 rows). BETWEEN (single PK), Keyset (composite PK). Deterministic sort.

### checks/chunk_hash.py
- **Role**: Chunk-level hash. Canonicalization, column `'|'` concat, Oracle AL32UTF8+LOWER(RAWTOHEX), Postgres encode(digest)+column names lower().

### checks/aggregate_validation.py
- **Role**: Step 1 aggregate. Decimal normalization, scale quantize, metric-only comparison.

### validator/core/report.py
- **Role**: JSON, summary, aggregate report generation. Default output dir **reports/**.

---

## 8. Report Output Location and Types

- **Common output directory**: **reports/** (relative to project root; `ReportGenerator(output_dir="reports")`, engine also writes DDL files under `reports/`).
- **run_ddl_report.py**:
  - `reports/{schema}_{table_name}_ddl_{timestamp}.sql` — full DDL script. Comments include **Table size (PG)**, Oracle Type, **HAS_FRACTION**, **In index**, **Distinct ratio**, **Null ratio**, DATE/TIMESTAMP rationale, **Dry-run CAST loss rows**.
  - `reports/{schema}_{table_name}_ddl_{timestamp}.json` — same content in machine-readable form.
- **run_validation.py** (when validation runs):
  - Summary: `*_summary_*.txt`
  - JSON: `*.json` (includes ddl_recommendations, numeric_downcast_candidates)
  - Aggregate: `*_aggregate_*.json`
  - DDL: `*_ddl_*.sql`

---

## 9. Data Normalization (Canonicalization) in Detail

(Unchanged) Cross-DB numeric precision alignment — Oracle `TO_CHAR(..., 'FM...')`, Postgres `::numeric(p,s)` + same format.  
Implemented in `checks/chunk_hash.py` (metadata PRECISION/SCALE), Postgres via information_schema.

---

## 10. Core Design Principles

| Principle | Content |
|-----------|---------|
| Single config source | config.yaml + connection_config only. No hardcoded ports/schemas/tables. |
| Deterministic paging | No ROWNUM/OFFSET; Keyset Pagination only. |
| Hash normalization | Column `'|'` concat, Oracle CONVERT(AL32UTF8)+LOWER(RAWTOHEX), Postgres encode(digest)+column names lower(). |
| Decimal / metric | No float; aggregate: to_decimal → scale quantize → `==`. |
| PK/FK BIGINT | All PK/FK columns excluded from SMALLINT/INTEGER recommendation; remain BIGINT. |
| Date recommendation | 8-digit integer → DATE; has_non_zero_time → DATE vs TIMESTAMP; rationale in report. |
| Numeric recommendation | precision≤4 → SMALLINT (PAGE 2); data range → SMALLINT/INTEGER/BIGINT; Oracle Type shown for every column in report. |

---

## 11. Partition-Aware Validation (Summary)

- **Purpose**: Efficient per-partition validation for large log/time-series tables.
- **Config**: `validation_mode.type: partition_aware`, `partition_key`, `immutable_before`.
- **Implementation**: db/oracle.py, postgres.py partition metadata, validator/core/partition_aware.py, engine integration.

---

## 12. Extension Points

- New validation: add modules under `checks/`.
- New reports: extend `validator/core/report.py`.
- New chunk strategy: extend `db/chunk_strategy.py`.

---

## 13. Roadmap

- **Phase 0–1**: Stability and partition-aware completed.
- **Phase 2**: Intelligent DDL recommendation completed. Added: **HAS_FRACTION** (TRUNC-based), **OUT_OF_RANGE verification** (count_out_of_range_rows), **Dry-run CAST** loss row count, **index info**, **NULL/distinct ratio**, **table size** for effect estimation. Performance risk, REPLAY-lite, ops reports planned.
- **Phase 3**: Dashboard, scheduling, notifications.
