"""
Oracle DB connection and query execution module. Includes partition table support.
"""
import logging
try:
    import oracledb as cx_Oracle
except ImportError:
    import cx_Oracle
from typing import List, Dict, Optional, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class OracleDB:
    """Oracle database connection and query execution class."""
    
    def __init__(self, connection_string: str, username: str, password: str):
        self.connection_string = connection_string
        self.username = username
        self.password = password
        self.connection = None
    
    def connect(self):
        """Connect to the database."""
        try:
            # oracledb / cx_Oracle compatibility
            if hasattr(cx_Oracle, 'makedsn'):
                # oracledb: connection_string format "host:port/service_name"
                if ':' in self.connection_string and '/' in self.connection_string:
                    # Use as-is when already in correct format
                    dsn = self.connection_string
                else:
                    dsn = self.connection_string
                
                # oracledb: user, password, dsn as keyword args
                self.connection = cx_Oracle.connect(
                    user=self.username,
                    password=self.password,
                    dsn=dsn
                )
            else:
                # Legacy cx_Oracle style
                self.connection = cx_Oracle.connect(
                    self.username,
                    self.password,
                    self.connection_string
                )
            logger.info("Oracle database connected")
        except Exception as e:
            logger.error(f"Oracle connection failed: {e}")
            raise
    
    def disconnect(self):
        """Disconnect from the database."""
        if self.connection:
            self.connection.close()
            self.connection = None
            logger.info("Oracle database disconnected")
    
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
            with self.get_cursor() as cursor:
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Query execution failed: {e}\nSQL: {sql}")
            raise
    
    def is_partitioned_table(self, table_name: str, schema: str) -> bool:
        """
        Check if table is partitioned.

        Args:
            table_name: Table name.
            schema: Schema name.

        Returns:
            True if partitioned.
        """
        try:
            sql = """
                SELECT COUNT(*)
                FROM all_tables
                WHERE owner = :schema
                  AND table_name = :table_name
                  AND partitioned = 'YES'
            """
            result = self.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            return result[0][0] > 0 if result else False
        except Exception as e:
            logger.error(f"Failed to check if table is partitioned: {e}")
            return False
    
    def get_partition_key_columns(self, table_name: str, schema: str) -> List[str]:
        """
        Get partition key columns.

        Args:
            table_name: Table name.
            schema: Schema name.

        Returns:
            List of partition key column names.
        """
        try:
            # Try DBA_PART_KEY_COLUMNS
            sql = """
                SELECT column_name
                FROM dba_part_key_columns
                WHERE owner = :schema
                  AND name = :table_name
                ORDER BY column_position
            """
            result = self.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            
            if result:
                return [row[0].lower() for row in result]
            
            # Try ALL_PART_KEY_COLUMNS
            sql = """
                SELECT column_name
                FROM all_part_key_columns
                WHERE owner = :schema
                  AND name = :table_name
                ORDER BY column_position
            """
            result = self.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            
            if result:
                return [row[0].lower() for row in result]
            
            return []
        except Exception as e:
            logger.warning(f"Failed to get partition key columns: {e}")
            return []
    
    def _parse_high_value(self, high_value: str, data_type: str = 'DATE') -> Optional[Any]:
        """
        Parse Oracle HIGH_VALUE string.

        Args:
            high_value: HIGH_VALUE string (e.g. TO_DATE(...)).
            data_type: Data type (DATE, NUMBER, VARCHAR2, etc.).

        Returns:
            Parsed value or None.
        """
        if not high_value:
            return None
        
        try:
            # DATE type
            if 'TO_DATE' in high_value.upper():
                # TO_DATE(' ... ', 'SYYYY-MM-DD HH24:MI:SS', ...)
                import re
                match = re.search(r"TO_DATE\('([^']+)'", high_value)
                if match:
                    date_str = match.group(1).strip()
                    from datetime import datetime
                    # Try common date formats
                    for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d']:
                        try:
                            return datetime.strptime(date_str, fmt)
                        except:
                            continue
                    return date_str
            
            # NUMBER type
            elif data_type == 'NUMBER':
                try:
                    return float(high_value)
                except:
                    return high_value
            
            # VARCHAR2
            else:
                # Strip quotes
                if high_value.startswith("'") and high_value.endswith("'"):
                    return high_value[1:-1]
                return high_value
        except Exception as e:
            logger.warning(f"Failed to parse high_value: {high_value}, error: {e}")
            return high_value
    
    def get_partition_list(self, table_name: str, schema: str) -> List[Dict[str, Any]]:
        """
        Get partition list for partitioned table.

        Args:
            table_name: Table name.
            schema: Schema name.

        Returns:
            List of partition info dicts (partition_name, high_value, range, ...).
        """
        try:
            # 파티션 키 컬럼 조회
            partition_key_columns = self.get_partition_key_columns(table_name, schema)
            
            sql = """
                SELECT 
                    partition_name,
                    high_value,
                    partition_position,
                    tablespace_name,
                    num_rows,
                    last_analyzed
                FROM all_tab_partitions
                WHERE table_owner = :schema
                  AND table_name = :table_name
                ORDER BY partition_position
            """
            result = self.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            
            partitions = []
            prev_high_value = None
            
            for row in result:
                partition_name = row[0]
                high_value_str = row[1]
                partition_position = row[2]
                tablespace_name = row[3]
                num_rows = row[4]
                last_analyzed = row[5]
                
                # Parse HIGH_VALUE
                high_value = self._parse_high_value(high_value_str)
                # Range: previous partition's high_value is current partition start
                range_start = prev_high_value
                range_end = high_value
                
                partitions.append({
                    'partition_name': partition_name,
                    'high_value': high_value_str,  # Keep original string
                    'high_value_parsed': high_value,
                    'range': [range_start, range_end] if range_start else [None, range_end],
                    'partition_position': partition_position,
                    'tablespace_name': tablespace_name,
                    'num_rows': num_rows,
                    'last_analyzed': last_analyzed,
                    'partition_key_columns': partition_key_columns
                })
                
                prev_high_value = high_value
            
            return partitions
        except Exception as e:
            logger.error(f"Failed to get partition list: {e}")
            return []
    
    def get_partition_info(self, table_name: str, schema: str) -> Optional[Dict[str, Any]]:
        """
        Get partition info for table. Returns None if not partitioned.

        Args:
            table_name: Table name.
            schema: Schema name.
        """
        if not self.is_partitioned_table(table_name, schema):
            return None
        
        partitions = self.get_partition_list(table_name, schema)
        return {
            'is_partitioned': True,
            'partition_count': len(partitions),
            'partitions': partitions
        }
    
    def create_partition_validation_tasks(self, table_name: str, schema: str,
                                         base_where_clause: str = "") -> List[Dict[str, Any]]:
        """
        Create validation tasks per partition.

        Args:
            table_name: Table name.
            schema: Schema name.
            base_where_clause: Base WHERE clause.

        Returns:
            List of validation task dicts.
        """
        partition_info = self.get_partition_info(table_name, schema)
        
        if not partition_info or not partition_info['is_partitioned']:
            # Non-partitioned: single task for full table
            return [{
                'table_name': table_name,
                'schema': schema,
                'partition_name': None,
                'where_clause': base_where_clause,
                'is_partitioned': False
            }]
        
        # One task per partition
        tasks = []
        for partition in partition_info['partitions']:
            partition_name = partition['partition_name']
            where_clause = base_where_clause
            if partition.get('high_value'):
                # Could add conditions from high_value; depends on partition key
                pass
            
            tasks.append({
                'table_name': table_name,
                'schema': schema,
                'partition_name': partition_name,
                'where_clause': where_clause,
                'is_partitioned': True,
                'partition_info': partition
            })
        
        return tasks
    
    def get_primary_key_columns(self, table_name: str, schema: str) -> List[str]:
        """
        Get Primary Key column list (ordered). Falls back to ALL_* then USER_* if DBA_* fails.

        Args:
            table_name: Table name.
            schema: Schema name.

        Returns:
            Ordered list of PK column names.
        """
        # Try DBA_* first (when DBA privileges available)
        try:
            sql = """
                SELECT ccc.COLUMN_NAME
                FROM DBA_CONSTRAINTS cc
                JOIN DBA_CONS_COLUMNS ccc 
                    ON cc.OWNER = ccc.OWNER 
                    AND cc.CONSTRAINT_NAME = ccc.CONSTRAINT_NAME
                WHERE cc.CONSTRAINT_TYPE = 'P'
                  AND cc.OWNER = :schema
                  AND cc.TABLE_NAME = :table_name
                ORDER BY ccc.POSITION
            """
            result = self.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            
            pk_columns = [row[0].lower() for row in result]
            if pk_columns:
                logger.info(f"Found PK columns for {schema}.{table_name}: {pk_columns}")
                return pk_columns
        except Exception as e:
            logger.debug(f"DBA_* views not accessible, trying ALL_* views: {e}")
        # Fallback to ALL_*
        try:
            sql = """
                SELECT ccc.COLUMN_NAME
                FROM ALL_CONSTRAINTS cc
                JOIN ALL_CONS_COLUMNS ccc 
                    ON cc.OWNER = ccc.OWNER 
                    AND cc.CONSTRAINT_NAME = ccc.CONSTRAINT_NAME
                WHERE cc.CONSTRAINT_TYPE = 'P'
                  AND cc.OWNER = :schema
                  AND cc.TABLE_NAME = :table_name
                ORDER BY ccc.POSITION
            """
            result = self.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            
            pk_columns = [row[0].lower() for row in result]
            if pk_columns:
                logger.info(f"Found PK columns for {schema}.{table_name}: {pk_columns}")
                return pk_columns
        except Exception as e:
            logger.debug(f"ALL_* views failed, trying USER_* views: {e}")
        
        # Fallback to USER_* (when current user owns the table)
        try:
            sql = """
                SELECT ccc.COLUMN_NAME
                FROM USER_CONSTRAINTS cc
                JOIN USER_CONS_COLUMNS ccc 
                    ON cc.CONSTRAINT_NAME = ccc.CONSTRAINT_NAME
                WHERE cc.CONSTRAINT_TYPE = 'P'
                  AND cc.TABLE_NAME = :table_name
                ORDER BY ccc.POSITION
            """
            result = self.execute_query(sql, {
                'table_name': table_name.upper()
            })
            
            pk_columns = [row[0].lower() for row in result]
            if pk_columns:
                logger.info(f"Found PK columns for {schema}.{table_name}: {pk_columns}")
                return pk_columns
        except Exception as e:
            logger.debug(f"USER_* views also failed: {e}")
        
        logger.warning(f"No PK columns found for {schema}.{table_name}")
        return []
    
    def is_composite_primary_key(self, table_name: str, schema: str) -> bool:
        """
        Check if table has composite Primary Key.

        Args:
            table_name: Table name.
            schema: Schema name.
        """
        pk_columns = self.get_primary_key_columns(table_name, schema)
        return len(pk_columns) > 1
    
    def get_column_metadata(self, table_name: str, schema: str,
                           columns: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        """
        Get column metadata (precision, scale, etc.). Based on Oracle_to_PostgreSQL_DDL_Migration_Script_2.sql. Falls back to ALL_TAB_COLUMNS if DBA_* fails.

        Args:
            table_name: Table name.
            schema: Schema name.
            columns: Column list (None = all).

        Returns:
            Dict of column name -> metadata.
        """
        where_clause = ""
        if columns:
            col_list = "', '".join([col.upper() for col in columns])
            where_clause = f"AND tc.COLUMN_NAME IN ('{col_list}')"
        # Try DBA_TAB_COLUMNS first
        try:
            sql = f"""
                SELECT 
                    tc.COLUMN_NAME,
                    tc.DATA_TYPE,
                    tc.DATA_LENGTH,
                    tc.DATA_PRECISION,
                    tc.DATA_SCALE,
                    tc.NULLABLE
                FROM DBA_TAB_COLUMNS tc
                WHERE tc.OWNER = :schema
                  AND tc.TABLE_NAME = :table_name
                  {where_clause}
                ORDER BY tc.COLUMN_ID
            """
            
            result = self.execute_query(sql, {
                'schema': schema.upper(),
                'table_name': table_name.upper()
            })
            
            metadata = {}
            for row in result:
                column_name = row[0].lower()
                metadata[column_name] = {
                    'data_type': row[1],
                    'data_length': row[2],
                    'data_precision': row[3],
                    'data_scale': row[4],
                    'nullable': row[5]
                }
            
            return metadata
        except Exception as e:
            logger.debug(f"DBA_TAB_COLUMNS not accessible, trying ALL_TAB_COLUMNS: {e}")
            try:
                # Fallback to ALL_TAB_COLUMNS
                sql = f"""
                    SELECT 
                        tc.COLUMN_NAME,
                        tc.DATA_TYPE,
                        tc.DATA_LENGTH,
                        tc.DATA_PRECISION,
                        tc.DATA_SCALE,
                        tc.NULLABLE
                    FROM ALL_TAB_COLUMNS tc
                    WHERE tc.OWNER = :schema
                      AND tc.TABLE_NAME = :table_name
                      {where_clause}
                    ORDER BY tc.COLUMN_ID
                """
                
                result = self.execute_query(sql, {
                    'schema': schema.upper(),
                    'table_name': table_name.upper()
                })
                
                metadata = {}
                for row in result:
                    column_name = row[0].lower()
                    metadata[column_name] = {
                        'data_type': row[1],
                        'data_length': row[2],
                        'data_precision': row[3],
                        'data_scale': row[4],
                        'nullable': row[5]
                    }
                
                return metadata
            except Exception as e2:
                logger.error(f"Failed to get column metadata: {e2}")
                return {}
