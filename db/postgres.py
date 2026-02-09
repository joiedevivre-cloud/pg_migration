"""
PostgreSQL DB connection and query execution module.
"""
import logging
import psycopg2
from typing import List, Dict, Optional, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class PostgresDB:
    """PostgreSQL database connection and query execution class."""
    
    def __init__(self, host: str, port: int, database: str, username: str, password: str):
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.connection = None
    
    def connect(self):
        """Connect to the database."""
        try:
            self.connection = psycopg2.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.username,
                password=self.password
            )
            logger.info("PostgreSQL database connected")
        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e}")
            raise
    
    def disconnect(self):
        """Disconnect from the database."""
        if self.connection:
            self.connection.close()
            self.connection = None
            logger.info("PostgreSQL database disconnected")
    
    @contextmanager
    def get_cursor(self):
        """Cursor context manager."""
        if not self.connection:
            self.connect()
        cursor = self.connection.cursor()
        try:
            yield cursor
        finally:
            cursor.close()
    
    def execute_query(self, sql: str, params: Optional[Dict] = None) -> List[tuple]:
        """Execute query and return results."""
        try:
            # Rollback and retry when transaction is aborted
            if self.connection and self.connection.status == psycopg2.extensions.STATUS_IN_TRANSACTION:
                try:
                    with self.get_cursor() as test_cursor:
                        test_cursor.execute("SELECT 1")
                except psycopg2.errors.InFailedSqlTransaction:
                    self.connection.rollback()
                    logger.debug("Rolled back failed transaction")
            
            with self.get_cursor() as cursor:
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                return cursor.fetchall()
        except psycopg2.errors.InFailedSqlTransaction:
            if self.connection:
                self.connection.rollback()
            logger.warning("Transaction was aborted, rolled back and retrying query")
            # Retry after rollback
            with self.get_cursor() as cursor:
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Query execution failed: {e}\nSQL: {sql}")
            # Rollback on error
            if self.connection:
                try:
                    self.connection.rollback()
                except:
                    pass
            raise
    
    def execute_ddl(self, sql: str, params: Optional[Dict] = None):
        """
        Execute DDL (CREATE, ALTER, DROP, etc.; no result set).

        Args:
            sql: DDL SQL.
            params: Optional parameters.
        """
        try:
            if not self.connection:
                self.connect()
            
            with self.get_cursor() as cursor:
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                # DDL requires commit
                self.connection.commit()
        except Exception as e:
            if self.connection:
                self.connection.rollback()
            logger.error(f"DDL execution failed: {e}\nSQL: {sql}")
            raise
    
    def table_exists(self, table_name: str, schema: str = 'public') -> bool:
        """
        Check if table exists.

        Args:
            table_name: Table name (lowercase).
            schema: Schema name (lowercase).

        Returns:
            True if table exists.
        """
        try:
            sql = """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = %s
            """
            result = self.execute_query(sql, (schema.lower(), table_name.lower()))
            return result[0][0] > 0 if result else False
        except Exception as e:
            logger.error(f"Failed to check if table exists: {e}")
            return False
    
    def is_partitioned_table(self, table_name: str, schema: str = 'public') -> bool:
        """
        Check if table is partitioned.

        Args:
            table_name: Table name (lowercase).
            schema: Schema name (lowercase).
        """
        try:
            sql = """
                SELECT COUNT(*)
                FROM pg_inherits i
                JOIN pg_class c ON i.inhrelid = c.oid
                JOIN pg_class p ON i.inhparent = p.oid
                JOIN pg_namespace n ON p.relnamespace = n.oid
                WHERE p.relname = %s
                  AND n.nspname = %s
            """
            result = self.execute_query(sql, (table_name.lower(), schema.lower()))
            return result[0][0] > 0 if result else False
        except Exception as e:
            logger.error(f"Failed to check if table is partitioned: {e}")
            return False
    
    def get_partition_list(self, table_name: str, schema: str = 'public') -> List[Dict[str, Any]]:
        """
        Get partition list for partitioned table.

        Args:
            table_name: Table name (lowercase).
            schema: Schema name (lowercase).

        Returns:
            List of partition info dicts (partition_name, range, ...).
        """
        try:
            # Get partition key columns
            sql_key = """
                SELECT a.attname
                FROM pg_partitioned_table pt
                JOIN pg_class pc ON pt.partrelid = pc.oid
                JOIN pg_namespace pn ON pc.relnamespace = pn.oid
                JOIN pg_attribute a ON pt.partrelid = a.attrelid
                JOIN pg_partkey pk ON pt.partrelid = pk.partrelid
                WHERE pc.relname = %s
                  AND pn.nspname = %s
                  AND a.attnum = ANY(pk.partattrs)
                ORDER BY array_position(pk.partattrs, a.attnum)
            """
            key_result = self.execute_query(sql_key, (table_name.lower(), schema.lower()))
            partition_key_columns = [row[0] for row in key_result] if key_result else []
            
            # Get child partition tables
            sql = """
                SELECT 
                    c.relname AS partition_name,
                    pg_get_expr(c.relpartbound, c.oid) AS partition_bound,
                    pg_get_expr(c.relpartbound, c.oid, true) AS partition_bound_simple
                FROM pg_inherits i
                JOIN pg_class c ON i.inhrelid = c.oid
                JOIN pg_class p ON i.inhparent = p.oid
                JOIN pg_namespace n ON p.relnamespace = n.oid
                WHERE p.relname = %s
                  AND n.nspname = %s
                ORDER BY c.relname
            """
            result = self.execute_query(sql, (table_name.lower(), schema.lower()))
            
            partitions = []
            for row in result:
                partition_name = row[0]
                partition_bound = row[1]  # Full expression
                partition_bound_simple = row[2]  # Simplified
                # Parse range (FOR VALUES FROM ... TO ... or FOR VALUES IN (...))
                range_start, range_end = self._parse_partition_bound(partition_bound)
                
                partitions.append({
                    'partition_name': partition_name,
                    'partition_bound': partition_bound,
                    'range': [range_start, range_end],
                    'partition_key_columns': partition_key_columns
                })
            
            return partitions
        except Exception as e:
            logger.error(f"Failed to get partition list: {e}")
            return []
    
    def _parse_partition_bound(self, partition_bound: str) -> tuple:
        """
        Parse PostgreSQL partition bound expression.

        Args:
            partition_bound: e.g. "FOR VALUES FROM ('2024-01-01') TO ('2024-02-01')".

        Returns:
            (range_start, range_end) tuple.
        """
        if not partition_bound:
            return (None, None)
        
        try:
            import re
            # FOR VALUES FROM ... TO ... pattern
            match = re.search(r"FROM\s+\(([^)]+)\)\s+TO\s+\(([^)]+)\)", partition_bound, re.IGNORECASE)
            if match:
                start_str = match.group(1).strip().strip("'\"")
                end_str = match.group(2).strip().strip("'\"")
                return (start_str, end_str)
            
            # FOR VALUES IN (...) pattern (list partition)
            match = re.search(r"IN\s+\(([^)]+)\)", partition_bound, re.IGNORECASE)
            if match:
                values_str = match.group(1)
                # First and last value
                values = [v.strip().strip("'\"") for v in values_str.split(',')]
                if values:
                    return (values[0], values[-1] if len(values) > 1 else values[0])
            
            return (None, None)
        except Exception as e:
            logger.warning(f"Failed to parse partition bound: {partition_bound}, error: {e}")
            return (None, None)
    
    def get_partition_info(self, table_name: str, schema: str = 'public') -> Optional[Dict[str, Any]]:
        """
        Get partition info for table. Returns None if not partitioned.

        Args:
            table_name: Table name (lowercase).
            schema: Schema name (lowercase).
        """
        if not self.is_partitioned_table(table_name, schema):
            return None
        
        partitions = self.get_partition_list(table_name, schema)
        return {
            'is_partitioned': True,
            'partition_count': len(partitions),
            'partitions': partitions
        }