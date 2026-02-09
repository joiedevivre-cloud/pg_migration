# Migration Project — Execution Guide

## Open the project

1. **In Cursor**
   - Run Cursor and choose `File > Open Folder`
   - Select your project folder (e.g. the folder containing `migration`)

2. **From the command line**
   ```powershell
   cd path\to\migration
   cursor .
   ```

## Environment setup

### 1. Install dependencies

From the project root:

```powershell
pip install -r requirements.txt
```

Main packages:
- **oracledb** / **cx_Oracle**: Oracle DB connection
- **psycopg2-binary**: PostgreSQL connection
- **PyYAML**: Config file parsing
- **pytest**: Running tests

### 2. Config file

Copy the example config and fill in your values:

```powershell
# Windows
copy config.yaml.example config.yaml

# Linux / macOS
cp config.yaml.example config.yaml
```

Edit **config.yaml** (do not commit it; it may contain passwords).

**Main sections:**
- **oracle**: Oracle connection (user, password, dsn)
- **postgres**: PostgreSQL connection (host, port, user, password, dbname)
- **migration**: Migration settings (source_schema, target_schema, tables, etc.)
- **validation**: Validation settings (tables, chunk_size, max_workers, etc.)

## How to run the project

### Option 1: Full validation (recommended)

```powershell
python run_validation.py
```

This script:
- Connects to Oracle and PostgreSQL using **config.yaml**
- Validates tables listed in `validation.tables`
- Runs aggregate checks → Chunk Hash → sampling → DDL report
- Writes summary, JSON, aggregate, and DDL files under **reports/**

### Option 2: DDL report only

```powershell
python run_ddl_report.py MY_TABLE MY_SCHEMA
# Create PG table from recommended types:
python run_ddl_report.py MY_TABLE MY_SCHEMA --create
```

Output: console plus `reports/{schema}_{table}_ddl_{timestamp}.sql` and `.json`.

### Option 3: Data migration + validation

```powershell
python data_migrator.py
```

Runs data migration and post-migration validation (Chunk Hash, aggregate).

### Option 4: Sync schema (CREATE TABLE + PK from Oracle)

```powershell
python db_sync_tool.py
```

Creates PostgreSQL tables and primary keys from Oracle metadata. Use this to set up the target schema before migration or validation.

### Option 5: Use from Python code

```python
from db.oracle import OracleDB
from db.postgres import PostgresDB
from validator.core.engine import ValidationEngine
from config.connection_config import (
    load_config,
    get_oracle_connection_params,
    get_postgres_connection_params,
    get_validation_config,
)

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
    table_name="MY_TABLE",
    schema="MY_SCHEMA",
    columns=None,  # None = auto-detect
    generate_report=True,
)
```

### Option 6: Run tests

```powershell
# All tests
pytest

# Single test file
pytest tests/test_engine.py

# Verbose
pytest -v

# With coverage
pytest --cov=. --cov-report=html
```

## Project layout (main items)

```
<project_root>/
├── run_validation.py      # Full validation pipeline
├── run_ddl_report.py       # DDL recommendation only
├── data_migrator.py        # Data migration + validation
├── db_sync_tool.py         # CREATE TABLE + PK from Oracle
├── config.yaml             # DB and validation config (create from config.yaml.example)
├── config.yaml.example     # Example config
├── requirements.txt       # Python dependencies
├── README.md               # Project overview
├── db/                     # DB clients
│   ├── oracle.py
│   ├── postgres.py
│   └── chunk_strategy.py
├── config/                 # Config loading
│   ├── connection_config.py  # load_config, get_oracle_connection_params, etc.
│   └── profile_loader.py
├── validator/              # Validation engine
│   └── core/
│       ├── engine.py
│       └── partition_aware.py
├── checks/                 # Validation checks (hash, aggregate, LOB, etc.)
├── profiles/               # Table-specific profiles (e.g. partition_aware)
│   └── tables.yaml.example
└── reports/                # Generated reports (JSON, DDL, summary)
```

## config.yaml example

```yaml
oracle:
  user: "your_oracle_user"
  password: "your_oracle_password"
  dsn: "host:1521/service_name"

postgres:
  host: "localhost"
  port: 5432
  user: "postgres"
  password: "your_postgres_password"
  dbname: "postgres"

migration:
  source_schema: "SOURCE_SCHEMA"
  target_schema: "target_schema"
  tables:
    - "TABLE1"
  drop_if_exists: false
  truncate_before_insert: true
  batch_size: 1000

validation:
  source_schema: "SOURCE_SCHEMA"
  target_schema: null   # null = same as source_schema
  tables:
    - "TABLE1"
  chunk_size: 10000
  max_workers: 5
  max_concurrent_db_sessions: 10
  decimal_tolerance: 0.0001
  null_empty_policy: "DISTINCT"
  max_diffs_per_chunk: 10
  profile_path: "profiles/tables.yaml"
```

## Validation reports

After validation, **reports/** contains:
- Summary and JSON results per table
- Chunk hash comparison results
- Aggregate verification (COUNT, SUM, etc.)
- DDL recommendation files (`*_ddl_*.sql`, `*_ddl_*.json`)
- Details when mismatches are found

## Partition-aware validation

For log/time-series tables you can use partition-aware validation.

### profiles/tables.yaml example

```yaml
IMSI:
  LOG_TABLE:
    columns:
      - log_id
      - log_date
      - log_level
      - message
    pk_columns:
      - log_id
    validation_mode:
      type: partition_aware
      partition_key: log_date
      immutable_before: "2024-01-01"   # Older partitions get lighter checks
```

### Behavior

- **Older partitions** (before `immutable_before`):
  - COUNT(*), SUM/MIN/MAX
  - Chunk hash only
  - No row-level drilldown (saves resources)

- **Recent partitions**:
  - Chunk hash
  - Limited row-level drilldown (`max_diffs_per_chunk`)
  - Optional full row verification

Reports include a `partition_summary` section: `total_partitions`, `fully_verified`, `light_verified`, `failed`.

## Troubleshooting

### DB connection fails
- Check **config.yaml** (oracle / postgres sections)
- Verify firewall and that Oracle/PostgreSQL are running
- Use **db_sync_tool.py**; it can run connection checks

### Package install errors
```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Encoding issues
- On Windows, UTF-8 is set where needed in the scripts
- If problems persist, check that config and source files are saved as UTF-8

## Tips

1. **Profiles**: Use **profiles/tables.yaml** for table-specific options (e.g. partition_aware).
2. **Performance**: Tune `max_workers` and `max_concurrent_db_sessions` in config.
3. **Large tables**: Partitioned tables are validated per partition when supported.
4. **Connection check**: Run **db_sync_tool.py** and use its connection test step to verify Oracle and PostgreSQL access.
