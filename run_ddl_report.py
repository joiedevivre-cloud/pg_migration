# -*- coding: utf-8 -*-
"""
Run intelligent DDL recommendation report only (single table).
- Args: python run_ddl_report.py [table_name] [schema]
- Create PG table with recommended types: python run_ddl_report.py --create [table_name] [schema]
"""
import sys
import os
import io
import json
from pathlib import Path
from datetime import datetime

# Windows console encoding
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add project root to path (same load_config as run_validation)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config.connection_config import (
    load_config,
    get_oracle_connection_params,
    get_postgres_connection_params,
    get_migration_config,
)


def main():
    config = load_config()
    if not config:
        print("Failed to load config.yaml. Check config.yaml.")
        return 1

    create_if_missing = "--create" in sys.argv
    argv_list = [a for a in sys.argv[1:] if a != "--create"]

    mig_cfg = get_migration_config(config)
    oracle_params = get_oracle_connection_params(config)
    postgres_params = get_postgres_connection_params(config)
    if not oracle_params or not oracle_params.get("connection_string"):
        print("Set oracle.dsn in config.yaml.")
        return 1
    if not postgres_params or not postgres_params.get("host"):
        print("Set postgres connection settings in config.yaml.")
        return 1

    # Schema: args override config only (no hardcoding)
    if len(argv_list) >= 2:
        schema = argv_list[1].strip()
    elif len(argv_list) >= 1:
        schema = (mig_cfg.get('ddl_report_schema') or mig_cfg.get('source_schema') or "").strip()
        if not schema:
            print("Set migration.source_schema in config.yaml.")
            return 1
    else:
        schema = (mig_cfg.get('ddl_report_schema') or mig_cfg.get('source_schema') or "").strip()
        if not schema:
            print("Set migration.source_schema in config.yaml.")
            return 1
    schema = schema.upper()

    # 테이블: 인자 > config 변수만 (하드코딩 없음)
    if len(argv_list) >= 1:
        table_name = argv_list[0].strip().upper()
    else:
        table_name = (mig_cfg.get('ddl_report_table') or "").strip()
        if not table_name and mig_cfg.get('tables'):
            table_name = mig_cfg['tables'][0]
        if not table_name or not str(table_name).strip():
            print("Set migration.tables or migration.ddl_report_table in config.yaml.")
            return 1
        table_name = str(table_name).strip().upper()

    from db.oracle import OracleDB
    from db.postgres import PostgresDB
    from checks.aggregate import AggregateStatsCollector
    from validator.core.result import ResultProcessor

    oracle_db = OracleDB(
        connection_string=oracle_params["connection_string"],
        username=oracle_params["username"],
        password=oracle_params["password"],
    )
    postgres_db = PostgresDB(
        host=postgres_params["host"],
        port=postgres_params["port"],
        database=postgres_params["database"],
        username=postgres_params["username"],
        password=postgres_params["password"],
    )

    print("=" * 80)
    print(f"DDL Recommendation Report: {schema}.{table_name}")
    print("=" * 80)

    try:
        oracle_db.connect()
        print("Oracle: connected")
    except Exception as e:
        print(f"Oracle connection failed: {e}")
        return 1

    try:
        postgres_db.connect()
        print("PostgreSQL: connected")
    except Exception as e:
        print(f"PostgreSQL connection failed: {e}")
        oracle_db.disconnect()
        return 1

    # Column list (Oracle)
    try:
        sql = """
            SELECT COLUMN_NAME FROM DBA_TAB_COLUMNS
            WHERE OWNER = :schema AND TABLE_NAME = :table_name
            ORDER BY COLUMN_ID
        """
        rows = oracle_db.execute_query(sql, {'schema': schema, 'table_name': table_name})
        columns = [r[0].lower() for r in rows] if rows else []
    except Exception:
        try:
            sql = """
                SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS
                WHERE OWNER = :schema AND TABLE_NAME = :table_name
                ORDER BY COLUMN_ID
            """
            rows = oracle_db.execute_query(sql, {'schema': schema, 'table_name': table_name})
            columns = [r[0].lower() for r in rows] if rows else []
        except Exception as e:
            print(f"Column lookup failed: {e}")
            oracle_db.disconnect()
            postgres_db.disconnect()
            return 1

    if not columns:
        print(f"Table {schema}.{table_name} not found or has no columns.")
        oracle_db.disconnect()
        postgres_db.disconnect()
        return 1

    pg_schema = schema.lower()
    if not postgres_db.table_exists(table_name.lower(), pg_schema):
        if create_if_missing:
            # Create PG table with recommended types (using already-connected oracle_db, postgres_db)
            from generate_ddl import generate_create_table_ddl, generate_primary_key_ddl
            create_ddl = generate_create_table_ddl(oracle_db, table_name, schema)
            if not create_ddl:
                print("CREATE TABLE DDL generation failed.")
                oracle_db.disconnect()
                postgres_db.disconnect()
                return 1
            create_ddl = create_ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
            try:
                postgres_db.execute_ddl(f"CREATE SCHEMA IF NOT EXISTS {pg_schema};")
                postgres_db.execute_ddl(create_ddl)
                pk_ddl = generate_primary_key_ddl(oracle_db, table_name, schema)
                if pk_ddl:
                    postgres_db.execute_ddl(pk_ddl)
            except Exception as e:
                print(f"Table creation failed: {e}")
                oracle_db.disconnect()
                postgres_db.disconnect()
                return 1
            print(f"\nTable created in PostgreSQL with recommended types: {pg_schema}.{table_name.lower()}")
            oracle_db.disconnect()
            postgres_db.disconnect()
            return 0
        print(f"Table {pg_schema}.{table_name} does not exist in PostgreSQL. For statistics-based recommendations, the same table must exist in PG.")
        print("(Summary below is based on Oracle metadata only.)")
        try:
            meta = oracle_db.get_column_metadata(table_name, schema, columns)
            proc = ResultProcessor(AggregateStatsCollector(oracle_db, postgres_db))
            print("\n-- Oracle column type mapping (PostgreSQL recommended)")
            for col in columns:
                m = meta.get(col, {})
                pt = proc.map_oracle_type_to_postgres(
                    m.get('data_type', 'VARCHAR2'),
                    m.get('data_length'),
                    m.get('data_precision'),
                    m.get('data_scale')
                )
                print(f"  {col}: {pt}")
        except Exception as e:
            print(f"Metadata summary failed: {e}")
        print("\nTo create the table in PG with recommended types: python run_ddl_report.py --create <TABLE> <SCHEMA>")
        oracle_db.disconnect()
        postgres_db.disconnect()
        return 0

    # Oracle metadata (precision, scale, data_type for DATE/TIMESTAMP)
    try:
        oracle_metadata = oracle_db.get_column_metadata(table_name, schema, columns)
        oracle_precision = {c: meta.get('data_precision') for c, meta in oracle_metadata.items() if meta.get('data_precision') is not None}
        oracle_scale = {c: meta.get('data_scale') for c, meta in oracle_metadata.items() if meta.get('data_scale') is not None}
        oracle_column_types = {c: meta.get('data_type') for c, meta in oracle_metadata.items() if meta.get('data_type')}
    except Exception:
        oracle_precision = None
        oracle_scale = None
        oracle_column_types = None

    stats_collector = AggregateStatsCollector(oracle_db, postgres_db)
    result_processor = ResultProcessor(stats_collector)

    try:
        report = result_processor.generate_ddl_report(
            table_name, schema, columns, where_clause="", target_db='postgres',
            oracle_precision=oracle_precision or None,
            oracle_scale=oracle_scale or None,
            oracle_column_types=oracle_column_types or None,
            validation_results=None,
            aggregate_results=None,
        )
    except Exception as e:
        print(f"DDL report generation failed: {e}")
        import traceback
        traceback.print_exc()
        oracle_db.disconnect()
        postgres_db.disconnect()
        return 1

    # Report output
    print()
    print("=" * 80)
    print("DDL Recommendation Summary")
    print("=" * 80)

    candidates = report.get('numeric_downcast_candidates', [])
    if candidates:
        for c in candidates:
            print(f"\n* {c.get('table')}.{c.get('column')}")
            print(f"  Type change: {c.get('from_type')} -> {c.get('to_type')}")
            print(f"  Recommendation: {c.get('recommendation_message', '')}")
            print(f"  Reasons: {', '.join(c.get('reason', []))}")
            print(f"  DDL: {c.get('ddl', '')}")
    else:
        print("\nNo downcast candidates (all NUMERIC kept or non-numeric columns).")

    ddl_text = result_processor.format_ddl_output(report)
    print()
    print("=" * 80)
    print("Full DDL Script (format_ddl_output)")
    print("=" * 80)
    print()
    print(ddl_text)

    # Save report to files (reports/): .sql and .json
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"{schema}_{table_name}_ddl_{timestamp}"

    ddl_file = reports_dir / f"{base_name}.sql"
    ddl_file.write_text(ddl_text, encoding="utf-8")

    json_file = reports_dir / f"{base_name}.json"
    report_for_json = {
        "metadata": {
            "table_name": table_name,
            "schema": schema,
            "target_db": report.get("target_db"),
            "generated_at": datetime.now().isoformat(),
        },
        "ddl_statements": report.get("ddl_statements", []),
        "changeable_columns": report.get("changeable_columns", []),
        "recommendations": report.get("recommendations", {}),
        "numeric_downcast_candidates": report.get("numeric_downcast_candidates", []),
        "stats": report.get("stats", {}),
    }
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(report_for_json, f, indent=2, ensure_ascii=False, default=str)

    print()
    print("=" * 80)
    print(f"Report saved: {ddl_file.resolve()}")
    print(f"JSON saved:   {json_file.resolve()}")
    print("=" * 80)

    oracle_db.disconnect()
    postgres_db.disconnect()
    print()
    print("Done. Connections closed.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
