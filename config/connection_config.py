# -*- coding: utf-8 -*-
"""
DB connection and migration/validation settings — loaded from config.yaml only.
No hardcoding. Oracle/Postgres connection, migration and validation schemas all from config.
"""
from pathlib import Path
from typing import Dict, Any, Optional, List


def _bool(v: Any) -> bool:
    """Handle 'true'/'false' strings from YAML."""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower().strip() in ("true", "1", "yes", "on")
    return bool(v)


try:
    import yaml
except ImportError:
    yaml = None


def _parse_simple_yaml(content: str) -> dict:
    """Simple parser when YAML is not installed (oracle/postgres/migration/validation sections)."""
    result = {}
    current_section = None
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if stripped.startswith("- "):
            value = stripped[2:].strip().strip("'\"")
            if current_section and current_section in result:
                if not isinstance(result[current_section].get("tables"), list):
                    result[current_section]["tables"] = []
                result[current_section]["tables"].append(value)
            i += 1
            continue
        if ":" in stripped:
            parts = stripped.split(":", 1)
            key = parts[0].strip()
            value = (parts[1].strip().strip("'\"") if len(parts) > 1 else "").strip()
            if indent == 0:
                if not value or value == "null":
                    current_section = key
                    result[key] = {}
                else:
                    result[key] = value
                i += 1
                continue
            if indent > 0 and current_section and current_section in result:
                result[current_section][key] = value if value else None
        i += 1
    return result


def load_config(config_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load config.yaml. Uses project root config.yaml when path not specified."""
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    path = Path(config_path)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        if yaml:
            return yaml.safe_load(content)
        return _parse_simple_yaml(content)
    except Exception:
        return None


def get_oracle_connection_params(config: Dict[str, Any]) -> Dict[str, Any]:
    """Oracle connection params from config.yaml oracle section only (no hardcoding)."""
    o = config.get("oracle") or {}
    dsn = (o.get("dsn") or "").strip()
    if not dsn:
        return {}
    parts = dsn.replace("/", ":").split(":")
    host = parts[0] if len(parts) > 0 else ""
    port = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    service_name = parts[2] if len(parts) > 2 else ""
    if not host or port is None or not service_name:
        return {}
    return {
        "connection_string": f"{host}:{port}/{service_name}",
        "username": (o.get("user") or "").strip(),
        "password": (o.get("password") or "").strip(),
    }


def get_postgres_connection_params(config: Dict[str, Any]) -> Dict[str, Any]:
    """PostgreSQL connection params from config.yaml postgres section only (no hardcoding)."""
    p = config.get("postgres") or {}
    host = (p.get("host") or "").strip()
    if not host:
        return {}
    port = p.get("port")
    if port is None:
        return {}
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {}
    dbname = (p.get("dbname") or "").strip()
    if not dbname:
        return {}
    return {
        "host": host,
        "port": port,
        "database": dbname,
        "username": (p.get("user") or "").strip(),
        "password": (p.get("password") or "").strip(),
    }


def get_migration_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Migration config: source/target schema, tables, etc. from config only."""
    m = config.get("migration") or {}
    source = (m.get("source_schema") or "").strip() if m.get("source_schema") is not None else ""
    target = m.get("target_schema")
    if target is not None and isinstance(target, str):
        target = target.strip() or None
    tables = m.get("tables")
    if not isinstance(tables, list):
        tables = [tables] if tables else []
    tables = [str(t).strip() for t in tables if t]
    return {
        "source_schema": source,
        "target_schema": target or source,
        "tables": tables,
        "ddl_report_schema": (m.get("ddl_report_schema") or "").strip() or None,
        "ddl_report_table": (m.get("ddl_report_table") or "").strip() or None,
        "drop_if_exists": _bool(m.get("drop_if_exists", False)),
        "truncate_before_insert": _bool(m.get("truncate_before_insert", False)),
        "batch_size": int(m.get("batch_size", 1000)) if m.get("batch_size") is not None else 1000,
        "verify_only": _bool(m.get("verify_only", False)),
    }


def get_validation_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validation config: source/target schema, tables, chunk_size, etc. from config only."""
    v = config.get("validation") or {}
    source = (v.get("source_schema") or "").strip() if v.get("source_schema") is not None else ""
    target = v.get("target_schema")
    if target is not None and isinstance(target, str):
        target = target.strip() or None
    tables = v.get("tables")
    if not isinstance(tables, list):
        tables = [tables] if tables else []
    tables = [str(t).strip() for t in tables if t]
    return {
        "source_schema": source,
        "target_schema": target or source,
        "tables": tables,
        "chunk_size": int(v.get("chunk_size", 10000)) if v.get("chunk_size") is not None else 10000,
        "chunk_size_by_pattern": v.get("chunk_size_by_pattern") or {},
        "max_workers": int(v.get("max_workers", 5)) if v.get("max_workers") is not None else 5,
        "max_concurrent_db_sessions": int(v.get("max_concurrent_db_sessions", 10)) if v.get("max_concurrent_db_sessions") is not None else 10,
        "decimal_tolerance": float(v.get("decimal_tolerance", 0.0001)) if v.get("decimal_tolerance") is not None else 0.0001,
        "tolerance_by_column": v.get("tolerance_by_column") or {},
        "null_empty_policy": (v.get("null_empty_policy") or "DISTINCT").strip(),
        "max_diffs_per_chunk": int(v.get("max_diffs_per_chunk", 10)) if v.get("max_diffs_per_chunk") is not None else 10,
        "profile_path": (v.get("profile_path") or "profiles/tables.yaml").strip(),
    }
