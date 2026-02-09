"""
Chunk strategy module: BETWEEN with keyset fallback. Composite PK uses keyset pagination only.
"""
import logging
from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from db.oracle import OracleDB

logger = logging.getLogger(__name__)


class ChunkStrategy:
    """Chunk strategy class."""

    def __init__(self, oracle_db: OracleDB, chunk_size: int = 10000,
                 mismatch_threshold: int = 3):
        """
        Args:
            oracle_db: Oracle DB connection.
            chunk_size: Chunk size.
            mismatch_threshold: Switch to keyset when mismatch count reaches this.
        """
        self.oracle_db = oracle_db
        self.chunk_size = chunk_size
        self.mismatch_threshold = mismatch_threshold
    
    def create_chunks_by_between(self, table_name: str, schema: str,
                                 pk_columns: List[str], where_clause: str = "",
                                 chunk_size_override: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Create chunks by BETWEEN. Do not use for composite PK (use keyset instead).

        Args:
            table_name: Table name.
            schema: Schema name.
            pk_columns: PK column list (single PK only).
            where_clause: WHERE clause.
            chunk_size_override: Chunk size for this call (None = self.chunk_size).

        Returns:
            List of chunk dicts.
        """
        size = chunk_size_override if chunk_size_override is not None else self.chunk_size
        # BETWEEN not supported for composite PK
        if len(pk_columns) > 1:
            logger.warning(f"BETWEEN chunking is not supported for composite PK. Use keyset pagination instead.")
            return []
        
        if not pk_columns:
            logger.warning("No PK columns provided")
            return []
        
        pk_column = pk_columns[0]
        
        try:
            # Get PK range (Oracle stores uppercase)
            where_sql = f"WHERE {where_clause}" if where_clause else ""
            sql = f"""
                SELECT MIN("{pk_column.upper()}") as min_val, MAX("{pk_column.upper()}") as max_val
                FROM "{schema}"."{table_name}"
                {where_sql}
            """
            
            result = self.oracle_db.execute_query(sql)
            if not result or len(result) == 0:
                return []
            
            min_val, max_val = result[0]
            if min_val is None or max_val is None:
                return []
            
            chunks = []
            current = min_val
            
            # Build chunks by BETWEEN
            while current <= max_val:
                chunk_end = self._calculate_chunk_end(current, max_val, size)
                
                chunk_where = f'"{pk_column.upper()}" BETWEEN {self._format_value(current)} AND {self._format_value(chunk_end)}'
                if where_clause:
                    chunk_where = f"({where_clause}) AND {chunk_where}"
                
                chunks.append({
                    'table_name': table_name,
                    'schema': schema,
                    'where_clause': chunk_where,
                    'pk_start': current,
                    'pk_end': chunk_end,
                    'strategy': 'between',
                    'pk_columns': pk_columns
                })
                
                current = chunk_end + 1
            
            return chunks
            
        except Exception as e:
            logger.error(f"Failed to create chunks by BETWEEN: {e}")
            return []
    
    def create_chunks_by_keyset(self, table_name: str, schema: str,
                                pk_columns: List[str], where_clause: str = "",
                                chunk_size_override: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Create chunks by keyset (composite PK supported; no OFFSET). Use this only for composite PK tables.

        Args:
            table_name: Table name.
            schema: Schema name.
            pk_columns: PK column list (supports composite).
            where_clause: WHERE clause.
            chunk_size_override: Chunk size for this call (None = self.chunk_size).

        Returns:
            List of chunk dicts (each with keyset pagination conditions).
        """
        size = chunk_size_override if chunk_size_override is not None else self.chunk_size
        try:
            if not pk_columns:
                logger.warning("No PK columns provided for keyset pagination")
                return []
            
            # Composite PK
            if len(pk_columns) > 1:
                return self._create_chunks_for_composite_pk_keyset(
                    table_name, schema, pk_columns, where_clause, chunk_size_override=size
                )
            
            # Single PK: WHERE pk > :last_pk ORDER BY pk FETCH FIRST :n ROWS
            pk_column = pk_columns[0]
            where_sql = f"WHERE {where_clause}" if where_clause else ""
            
            # Get PK range
            sql = f"""
                SELECT MIN("{pk_column.upper()}") as min_val, MAX("{pk_column.upper()}") as max_val
                FROM "{schema}"."{table_name}"
                {where_sql}
            """
            
            result = self.oracle_db.execute_query(sql)
            if not result or len(result) == 0:
                return []
            
            min_val, max_val = result[0]
            if min_val is None or max_val is None:
                return []
            
            chunks = []
            current_pk = min_val
            
            # Build chunks by keyset pagination
            while current_pk is not None:
                if current_pk == min_val:
                    # First chunk: start with ORDER BY
                    chunk_where = f'"{pk_column.upper()}" >= {self._format_value(current_pk)}'
                else:
                    # Next chunk: WHERE pk > :last_pk
                    chunk_where = f'"{pk_column.upper()}" > {self._format_value(current_pk)}'
                
                if where_clause:
                    chunk_where = f"({where_clause}) AND {chunk_where}"
                
                # Get next PK (min value greater than current)
                next_pk_sql = f"""
                    SELECT MIN("{pk_column.upper()}")
                    FROM "{schema}"."{table_name}"
                    WHERE "{pk_column.upper()}" > {self._format_value(current_pk)}
                    {f'AND ({where_clause})' if where_clause else ''}
                """
                next_result = self.oracle_db.execute_query(next_pk_sql)
                next_pk = next_result[0][0] if next_result and next_result[0][0] else None
                
                # Chunk range: current PK up to (but not including) next PK, or chunk_size
                if next_pk is not None:
                    # Up to (but not including) next PK
                    chunk_where_end = f'"{pk_column.upper()}" < {self._format_value(next_pk)}'
                    if where_clause:
                        chunk_where = f"({where_clause}) AND {chunk_where} AND {chunk_where_end}"
                    else:
                        chunk_where = f"{chunk_where} AND {chunk_where_end}"
                
                chunks.append({
                    'table_name': table_name,
                    'schema': schema,
                    'where_clause': chunk_where,
                    'pk_start': current_pk,
                    'pk_end': next_pk,
                    'strategy': 'keyset',
                    'pk_columns': pk_columns
                })
                
                current_pk = next_pk
            
            return chunks
            
        except Exception as e:
            logger.error(f"Failed to create chunks by keyset: {e}")
            return []
    
    def _create_chunks_for_composite_pk_keyset(self, table_name: str, schema: str,
                                                pk_columns: List[str],
                                                where_clause: str = "",
                                                chunk_size_override: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Create keyset chunks for composite PK (tuple comparison; no OFFSET).
        Rules: (k1,k2,...) > (:k1,:k2,...); ORDER BY pk_cols enforced; NULL handling.

        Args:
            table_name: Table name.
            schema: Schema name.
            pk_columns: PK column list.
            where_clause: WHERE clause.
            chunk_size_override: Chunk size for this call (None = self.chunk_size).

        Returns:
            List of chunk dicts with keyset conditions.
        """
        size = chunk_size_override if chunk_size_override is not None else self.chunk_size
        try:
            where_sql = f"WHERE {where_clause}" if where_clause else ""
            
            # SELECT and ORDER BY all PK columns (Oracle uses uppercase)
            pk_col_list = ", ".join([f'"{col.upper()}"' for col in pk_columns])
            order_by_list = ", ".join([f'"{col.upper()}"' for col in pk_columns])
            
            # Check for NULLs in composite PK
            null_check_sql = f"""
                SELECT COUNT(*)
                FROM "{schema}"."{table_name}"
                {where_sql}
                WHERE {' OR '.join([f'"{col.upper()}" IS NULL' for col in pk_columns])}
            """
            null_count = self.oracle_db.execute_query(null_check_sql)
            if null_count and null_count[0][0] > 0:
                logger.warning(f"Composite PK has NULL values in {schema}.{table_name} - this may cause issues")
            
            # Find start of first chunk (MIN)
            min_sql = f"""
                SELECT {pk_col_list}
                FROM "{schema}"."{table_name}"
                {where_sql}
                ORDER BY {order_by_list}
                FETCH FIRST 1 ROWS ONLY
            """
            min_result = self.oracle_db.execute_query(min_sql)
            if not min_result:
                return []
            
            chunks = []
            last_pk_tuple = None
            
            # Build chunks by keyset pagination
            while True:
                if last_pk_tuple is None:
                    chunk_where = "1=1"  # All rows
                else:
                    # (k1,k2,...) > (:k1,:k2,...) as (k1>:k1) OR (k1=:k1 AND k2>:k2) OR ...
                    tuple_conditions = []
                    for i in range(len(pk_columns)):
                        if i == 0:
                            tuple_conditions.append(f'"{pk_columns[i].upper()}" > {self._format_value(last_pk_tuple[i])}')
                        else:
                            # Compare next column only when previous columns equal
                            prev_equals = " AND ".join([
                                f'"{pk_columns[j].upper()}" = {self._format_value(last_pk_tuple[j])}'
                                for j in range(i)
                            ])
                            tuple_conditions.append(
                                f'({prev_equals} AND "{pk_columns[i].upper()}" > {self._format_value(last_pk_tuple[i])})'
                            )
                    chunk_where = " OR ".join([f"({cond})" for cond in tuple_conditions])
                
                if where_clause:
                    chunk_where = f"({where_clause}) AND ({chunk_where})"
                
                # Get last PK tuple of current chunk (for next chunk)
                chunk_sql = f"""
                    SELECT {pk_col_list}
                    FROM "{schema}"."{table_name}"
                    WHERE {chunk_where}
                    ORDER BY {order_by_list}
                    FETCH FIRST {size} ROWS ONLY
                """
                chunk_result = self.oracle_db.execute_query(chunk_sql)
                
                if not chunk_result:
                    break
                
                # Store last row's PK tuple
                last_pk_tuple = tuple(chunk_result[-1])
                
                chunks.append({
                    'table_name': table_name,
                    'schema': schema,
                    'where_clause': chunk_where,
                    'pk_start': last_pk_tuple if len(chunks) == 0 else chunks[-1].get('pk_end'),
                    'pk_end': last_pk_tuple,
                    'strategy': 'keyset',
                    'pk_columns': pk_columns,
                    'is_composite_pk': True
                })
                
                # Last chunk if fewer rows than chunk size
                if len(chunk_result) < size:
                    break
            
            return chunks
            
        except Exception as e:
            logger.error(f"Failed to create chunks for composite PK: {e}")
            return []
    
    def _format_value(self, value: Any) -> str:
        """Format value for SQL (strings quoted and escaped)."""
        if value is None:
            return 'NULL'
        elif isinstance(value, str):
            # Escape quotes for SQL safety
            escaped = value.replace("'", "''")
            return f"'{escaped}'"
        elif isinstance(value, (int, float, Decimal)):
            return str(value)
        else:
            # Date and other types
            return f"'{str(value)}'"
    
    def _calculate_chunk_end(self, start: Any, max_val: Any, chunk_size_override: Optional[int] = None) -> Any:
        """Chunk 끝 값 계산"""
        size = chunk_size_override if chunk_size_override is not None else self.chunk_size
        try:
            if isinstance(start, (int, Decimal)):
                end = start + size - 1
                return min(end, max_val)
            else:
                # For string/date types, keyset is recommended
                return max_val
        except Exception:
            return max_val
    
    def should_fallback_to_keyset(self, mismatch_count: int) -> bool:
        """Whether to switch to keyset strategy."""
        return mismatch_count >= self.mismatch_threshold
    
    def create_chunks(self, table_name: str, schema: str,
                     pk_columns: List[str], where_clause: str = "",
                     force_keyset: bool = False,
                     chunk_size_override: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Create chunks (auto-detect composite PK).

        Args:
            table_name: Table name.
            schema: Schema name.
            pk_columns: PK column list.
            where_clause: WHERE clause.
            force_keyset: Force keyset strategy.
            chunk_size_override: Chunk size for this call (None = self.chunk_size).

        Returns:
            List of chunk dicts.
        """
        # Composite PK or force_keyset
        if len(pk_columns) > 1 or force_keyset:
            logger.info(f"Using keyset pagination for table {schema}.{table_name} (composite PK or forced)")
            return self.create_chunks_by_keyset(table_name, schema, pk_columns, where_clause,
                                                chunk_size_override=chunk_size_override)
        
        # Single PK: try BETWEEN
        try:
            chunks = self.create_chunks_by_between(table_name, schema, pk_columns, where_clause,
                                                    chunk_size_override=chunk_size_override)
            if chunks:
                return chunks
        except Exception as e:
            logger.warning(f"BETWEEN chunking failed, falling back to keyset: {e}")
        
        # Fallback to keyset if BETWEEN fails
        return self.create_chunks_by_keyset(table_name, schema, pk_columns, where_clause,
                                            chunk_size_override=chunk_size_override)
