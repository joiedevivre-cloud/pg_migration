"""
Profile loader module. Loads profiles/tables YAML.
"""
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

logger = logging.getLogger(__name__)


class ProfileLoader:
    """Profile loader class."""

    def __init__(self, profile_path: Optional[str] = None):
        """
        Args:
            profile_path: Path to profile YAML file.
        """
        self.profile_path = profile_path or "profiles/tables.yaml"
        self.profiles: Dict[str, Any] = {}
    
    def load_profiles(self) -> Dict[str, Any]:
        """Load profile YAML file."""
        try:
            profile_file = Path(self.profile_path)
            if not profile_file.exists():
                logger.warning(f"Profile file not found: {self.profile_path}")
                return {}
            
            with open(profile_file, 'r', encoding='utf-8') as f:
                self.profiles = yaml.safe_load(f) or {}
            
            logger.info(f"Loaded profiles from {self.profile_path}")
            return self.profiles
            
        except Exception as e:
            logger.error(f"Failed to load profiles: {e}")
            return {}
    
    def get_table_config(self, schema: str, table_name: str, key_path: Optional[str] = None) -> Optional[Any]:
        """
        Get table-specific config.

        Args:
            schema: Schema name.
            table_name: Table name.
            key_path: Config key path (e.g. 'validation_mode', 'validation_mode.type').

        Returns:
            Table config dict or specific key value, or None.
        """
        if not self.profiles:
            self.load_profiles()
        
        # profiles structure: {schema: {table_name: config}}
        schema_config = self.profiles.get(schema, {})
        table_config = schema_config.get(table_name)
        
        if not table_config:
            return None
        
        # Return nested value when key_path is given
        if key_path:
            keys = key_path.split('.')
            value = table_config
            for key in keys:
                if isinstance(value, dict):
                    value = value.get(key)
                else:
                    return None
            return value
        
        return table_config
    
    def is_all_validation_required(self, schema: str, table_name: str) -> bool:
        """Whether table requires full (ALL) validation."""
        config = self.get_table_config(schema, table_name)
        if config:
            return config.get('validation_mode', 'sample') == 'all'
        return False
    
    def get_table_columns(self, schema: str, table_name: str) -> Optional[List[str]]:
        """Get list of columns to validate for the table."""
        config = self.get_table_config(schema, table_name)
        if config:
            return config.get('columns')
        return None
    
    def get_table_pk_columns(self, schema: str, table_name: str) -> Optional[List[str]]:
        """Get Primary Key column list for the table."""
        config = self.get_table_config(schema, table_name)
        if config:
            return config.get('pk_columns')
        return None
