"""
Row Canonicalize 모듈
Decimal/NULL/empty string/bytes/datetime 정규화
"""
import logging
from decimal import Decimal
from datetime import datetime, date
from typing import Any, Optional, Dict
from enum import Enum

logger = logging.getLogger(__name__)


class NullEmptyPolicy(Enum):
    """NULL과 Empty String 처리 정책"""
    DISTINCT = "distinct"  # NULL과 ''를 구분
    NULL_AS_EMPTY = "null_as_empty"  # NULL을 ''로 취급
    EMPTY_AS_NULL = "empty_as_null"  # ''를 NULL로 취급


class RowCanonicalizer:
    """Row 값 정규화 클래스"""
    
    def __init__(self, null_empty_policy: NullEmptyPolicy = NullEmptyPolicy.DISTINCT,
                 decimal_precision: Optional[int] = None):
        """
        Args:
            null_empty_policy: NULL과 empty string 처리 정책
            decimal_precision: Decimal 정밀도 (None이면 원본 유지)
        """
        self.null_empty_policy = null_empty_policy
        self.decimal_precision = decimal_precision
    
    def canonicalize_value(self, value: Any, column_type: Optional[str] = None) -> Any:
        """
        단일 값을 정규화
        
        Args:
            value: 정규화할 값
            column_type: 컬럼 타입 (옵션)
        
        Returns:
            정규화된 값
        """
        # NULL 처리
        if value is None:
            if self.null_empty_policy == NullEmptyPolicy.EMPTY_AS_NULL:
                return None
            elif self.null_empty_policy == NullEmptyPolicy.NULL_AS_EMPTY:
                return ''
            else:
                return None
        
        # Empty string 처리
        if isinstance(value, str) and value == '':
            if self.null_empty_policy == NullEmptyPolicy.EMPTY_AS_NULL:
                return None
            elif self.null_empty_policy == NullEmptyPolicy.NULL_AS_EMPTY:
                return ''
            else:
                return ''
        
        # Decimal/Numeric 정규화
        if isinstance(value, (Decimal, float, int)):
            return self._canonicalize_numeric(value)
        
        # Datetime 정규화
        if isinstance(value, (datetime, date)):
            return self._canonicalize_datetime(value)
        
        # Bytes 정규화
        if isinstance(value, bytes):
            return self._canonicalize_bytes(value)
        
        # 문자열 정규화 (공백 제거 등)
        if isinstance(value, str):
            return self._canonicalize_string(value)
        
        return value
    
    def _canonicalize_numeric(self, value: Any) -> Decimal:
        """Numeric 값 정규화"""
        try:
            decimal_val = Decimal(str(value))
            
            # 정밀도 적용
            if self.decimal_precision is not None:
                # 소수점 이하 자리수 제한
                decimal_val = decimal_val.quantize(
                    Decimal(10) ** -self.decimal_precision
                )
            
            return decimal_val
        except Exception as e:
            logger.warning(f"Failed to canonicalize numeric value {value}: {e}")
            return Decimal(str(value))
    
    def _canonicalize_datetime(self, value: Any) -> str:
        """Datetime 값 정규화 (ISO 8601 형식)"""
        try:
            if isinstance(value, datetime):
                # 마이크로초까지 포함
                return value.isoformat()
            elif isinstance(value, date):
                return value.isoformat()
            else:
                return str(value)
        except Exception as e:
            logger.warning(f"Failed to canonicalize datetime value {value}: {e}")
            return str(value)
    
    def _canonicalize_bytes(self, value: bytes) -> str:
        """Bytes 값을 hex 문자열로 정규화"""
        try:
            return value.hex()
        except Exception as e:
            logger.warning(f"Failed to canonicalize bytes value: {e}")
            return str(value)
    
    def _canonicalize_string(self, value: str) -> str:
        """문자열 정규화"""
        # 필요시 공백 제거, 대소문자 통일 등 수행
        # 현재는 원본 유지
        return value
    
    def canonicalize_row(self, row: tuple, columns: list,
                        column_types: Optional[Dict[str, str]] = None) -> tuple:
        """
        Row 전체를 정규화
        
        Args:
            row: 정규화할 행 (tuple)
            columns: 컬럼명 리스트
            column_types: 컬럼 타입 딕셔너리 (옵션)
        
        Returns:
            정규화된 행 (tuple)
        """
        canonicalized = []
        for idx, value in enumerate(row):
            col_name = columns[idx] if idx < len(columns) else f"col_{idx}"
            col_type = column_types.get(col_name) if column_types else None
            canonicalized.append(self.canonicalize_value(value, col_type))
        
        return tuple(canonicalized)
