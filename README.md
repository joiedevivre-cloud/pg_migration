# Data Validation System (Oracle → PostgreSQL)

A unified tool for **data consistency validation** and **intelligent DDL recommendations** between Oracle and PostgreSQL.

**Core value**: Validation-based migration decisions + automated type recommendations (date/time, numeric, PK/FK).

---

## Features

### 1. SQL-centric validation
- Hash computation inside the database (no bulk fetch into Python).
- Oracle: `STANDARD_HASH` (SHA256); PostgreSQL: `encode(digest(..., 'sha256'), 'hex')`.
- Deterministic ordering via PK-based keyset pagination (no ROWNUM/OFFSET).

### 2. Partition-aware validation
- Detects Oracle partitioned tables and builds per-partition validation tasks.
- Config: `validation_mode.type: partition_aware`, `partition_key`, `immutable_before`.

### 3. Parallel load control
- `Semaphore` limits concurrent DB sessions.
- `ThreadPoolExecutor` controls worker count.

### 4. Drill-down workflow
- Chunk hash comparison first; row-level sampling only on mismatched chunks.
- Early exit via `max_diffs_per_chunk`.

### 5. Row canonicalization
- Decimal/NULL/empty string/bytes/datetime normalization.
- Configurable empty string vs NULL policy.

### 6. LOB handling
- Length-first + sampled hash (leading/trailing/random offsets).

### 7. Aggregate-based verification
- COUNT, MIN/MAX, COUNT DISTINCT, SUM, AVG with decimal tolerance and scale quantize (no float).

### 8. PK chunk strategy
- BETWEEN (single PK) or keyset pagination (composite PK). Deterministic ordering.

### 9. Single config source
- All settings in **config.yaml**; loaded via `config/connection_config.py`. No hardcoded schemas/tables/ports.

### 10. Intelligent DDL recommendation
- **DATE vs TIMESTAMP**: `has_non_zero_time` profiling; rationale in report.
- **Numeric**: precision/range → SMALLINT/INTEGER/BIGINT; **PK/FK always BIGINT**.
- **HAS_FRACTION**: direct `col != TRUNC(col)` check; report: `HAS_FRACTION: Y/N (fraction_row_count: N)`.
- **OUT_OF_RANGE**: `count_out_of_range_rows` before SMALLINT/INTEGER; `OUT_OF_RANGE_VERIFIED` when excluded.
- **Dry-run CAST**: `dry_run_cast_loss_count` on PG; report: `Dry-run CAST loss rows: N`.
- **Index / null·distinct ratio / table size**: `In index: Y/N`, distinct/null ratio hints, `Table size (PG): X.XX MB`.

### 11. JSON and DDL reports
- Validation: summary, JSON, aggregate, DDL under **reports/**.
- DDL report: `reports/{schema}_{table}_ddl_{timestamp}.sql` and `.json` with Oracle Type, rationale, and safety/effect metadata.

---

## Entry points

| Script | Role |
|--------|------|
| **run_ddl_report.py** | DDL recommendation report only. Args: `[table] [schema]`. `--create` creates the PG table with recommended types. |
| **run_validation.py** | Full validation pipeline (aggregate → Chunk Hash → sampling → DDL report). |
| **data_migrator.py** | Data migration + post-migration validation (Chunk Hash, aggregate). |
| **db_sync_tool.py** | CREATE TABLE + PK from Oracle metadata → PostgreSQL. |

---

## Configuration

1. Copy the example config and edit with your credentials:  
   `copy config.yaml.example config.yaml` (Windows) or `cp config.yaml.example config.yaml` (Linux/macOS)  
2. Do **not** commit `config.yaml` (it is in `.gitignore`; it may contain passwords).

Use a single **config.yaml** (see `config/connection_config.py` for keys):

- **oracle** / **postgres**: connection params (DSN, host, port, database, user, password).
- **migration** / **validation**: source_schema, target_schema, tables, chunk_size, max_workers, decimal_tolerance, null_empty_policy, etc.
- **ddl_report_schema** / **ddl_report_table**: for DDL report targets.

Table-specific validation profiles (e.g. partition_aware) can be defined in YAML; see `profiles/tables.yaml.example`.

---

## Usage examples

### DDL report only

```bash
python run_ddl_report.py MY_TABLE MY_SCHEMA
# With PG table creation from recommended types:
python run_ddl_report.py MY_TABLE MY_SCHEMA --create
```

Output: console + `reports/{schema}_{table}_ddl_{timestamp}.sql` and `.json`.

### Full validation (with DDL report)

```bash
python run_validation.py
```

Uses tables/schemas from config (or overrides). Runs aggregate → Chunk Hash → sampling → DDL report; writes summary, JSON, aggregate, and DDL files under **reports/**.

### Programmatic (engine)

```python
from db.oracle import OracleDB
from db.postgres import PostgresDB
from validator.core.engine import ValidationEngine
from config.connection_config import load_config, get_oracle_connection_params, get_postgres_connection_params, get_validation_config

config = load_config("config.yaml")
ora_params = get_oracle_connection_params(config)
pg_params = get_postgres_connection_params(config)
val_config = get_validation_config(config)

oracle_db = OracleDB(**ora_params)
postgres_db = PostgresDB(**pg_params)

engine = ValidationEngine(
    oracle_db=oracle_db,
    postgres_db=postgres_db,
    max_workers=val_config.get("max_workers", 5),
    max_concurrent_db_sessions=val_config.get("max_concurrent_db_sessions", 10),
    decimal_tolerance=val_config.get("decimal_tolerance", 0.0001),
    null_empty_policy=val_config.get("null_empty_policy", "DISTINCT"),
)

result = engine.validate_table(
    table_name="my_table",
    schema="my_schema",
    columns=["col1", "col2", "col3"],
    generate_report=True,
)
print(result["report_path"], result["aggregate_results"].get("match"))
```

---

## Report layout

| Source | Location | Contents |
|--------|----------|----------|
| run_ddl_report.py | reports/{schema}_{table}_ddl_{timestamp}.sql | Full DDL script; comments: Table size (PG), Oracle Type, HAS_FRACTION, In index, Distinct/Null ratio, Dry-run CAST loss rows |
| run_ddl_report.py | reports/{schema}_{table}_ddl_{timestamp}.json | Same report in JSON |
| run_validation.py | reports/ | *_summary_*.txt, *.json, *_aggregate_*.json, *_ddl_*.sql |

---

## Testing

Tests are pytest-based and use mocks (no live DB required).

```bash
# Run all tests
pytest

# Single file
pytest tests/test_engine.py

# Verbose
pytest -v

# With coverage
pytest --cov=. --cov-report=html
```

**Test modules**: `test_engine.py` (ValidationEngine), `test_aggregate.py` (stats and aggregate checks), `test_chunk_hash.py` (chunk hash), `test_row_decimal.py` (decimal normalization), `test_integration.py` (integration).

---

## Documentation

- **ARCHITECTURE_EN.md** — Module APIs, implementation details (config, entry points, DDL pipeline, HAS_FRACTION, OUT_OF_RANGE, Dry-run CAST, index, null/distinct ratio, table size).
- **SYSTEM_ARCHITECTURE_DIAGRAM.md** — High-level flow and diagrams.

---

## Requirements

See **requirements.txt**. Python 3.x with Oracle (cx_Oracle/oracledb) and PostgreSQL (psycopg2) drivers.
