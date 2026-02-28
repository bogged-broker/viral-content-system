"""
metadata_store.py — INFRASTRUCTURE LAYER (BLUEPRINT COMPLIANT)

PURPOSE:
Handle ALL storage concerns - database, object storage, file persistence.
This file does NOT parse metadata - it only stores what metadata_parser.py produces.

SCALE TARGET: 10k-50k items/day
LATENCY TARGET: <50ms local, <200ms distributed
"""

import json
import sqlite3
import os
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Any, List
from dataclasses import asdict
import logging
from threading import Lock
from contextlib import contextmanager
from collections import defaultdict

logger = logging.getLogger(__name__)

class MetadataStore:
    """
    Infrastructure layer for metadata persistence.
    
    Responsibilities:
    1. Database operations
    2. Object storage operations  
    3. File system operations
    4. Version management
    5. Checksum validation
    
    This file NEVER parses or validates metadata - only stores it.
    """
    
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        
        # Storage paths
        self.processed_metadata_dir = self.data_root / "processed" / "metadata"
        self.processed_metadata_dir.mkdir(parents=True, exist_ok=True)
        
        # Database configuration
        self.ENABLE_DATABASE = os.getenv('ENABLE_DATABASE', 'false').lower() == 'true'
        self.db_path = self.data_root / "metadata.db"
        self.db_connection = None
        
        # Object storage configuration
        self.ENABLE_OBJECT_STORAGE = os.getenv('ENABLE_OBJECT_STORAGE', 'false').lower() == 'true'
        self.OBJECT_STORAGE_PREFIX = os.getenv('OBJECT_STORAGE_PREFIX', 'metadata')
        self.object_storage_client = None
        
        # Thread safety
        self._locks = defaultdict(Lock)
        
        # Initialize storage backends
        if self.ENABLE_DATABASE:
            self._init_database()
        
        if self.ENABLE_OBJECT_STORAGE:
            self._init_object_storage()
    
    def _init_database(self):
        """Initialize SQLite database with proper schema"""
        self.db_connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        
        # Create tables
        cursor = self.db_connection.cursor()
        
        # Metadata table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                video_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(platform, video_id, version)
            )
        """)
        
        # Lineage table for version tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lineage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                video_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                checksum TEXT NOT NULL,
                parent_checksum TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        self.db_connection.commit()
        logger.info("Database initialized successfully")
    
    def _init_object_storage(self):
        """Initialize object storage client (placeholder for S3/GCS)"""
        # TODO: Implement actual object storage client
        # For now, use local directory as object storage simulation
        object_storage_dir = self.data_root / "object_storage"
        object_storage_dir.mkdir(parents=True, exist_ok=True)
        
        self.object_storage_client = {
            'type': 'local_simulation',
            'base_path': object_storage_dir,
            'exists': lambda key: (object_storage_dir / key).exists(),
            'put': lambda key, data: self._put_object_simulation(key, data),
            'get': lambda key: self._get_object_simulation(key),
            'list': lambda prefix: self._list_objects_simulation(prefix)
        }
        
        logger.info("Object storage initialized (simulation mode)")
    
    def _put_object_simulation(self, key: str, data: str) -> bool:
        """Simulate object storage put operation"""
        try:
            object_path = self.object_storage_client['base_path'] / key
            object_path.parent.mkdir(parents=True, exist_ok=True)
            with open(object_path, 'w', encoding='utf-8') as f:
                f.write(data)
            return True
        except Exception as e:
            logger.error(f"Object storage put failed: {e}")
            return False
    
    def _get_object_simulation(self, key: str) -> Optional[str]:
        """Simulate object storage get operation"""
        try:
            object_path = self.object_storage_client['base_path'] / key
            if object_path.exists():
                with open(object_path, 'r', encoding='utf-8') as f:
                    return f.read()
            return None
        except Exception as e:
            logger.error(f"Object storage get failed: {e}")
            return None
    
    def _list_objects_simulation(self, prefix: str) -> List[str]:
        """Simulate object storage list operation"""
        try:
            base_path = self.object_storage_client['base_path']
            prefix_path = base_path / prefix
            if not prefix_path.exists():
                return []
            
            objects = []
            for obj_path in prefix_path.rglob("*"):
                if obj_path.is_file():
                    rel_path = obj_path.relative_to(base_path)
                    objects.append(str(rel_path))
            return objects
        except Exception as e:
            logger.error(f"Object storage list failed: {e}")
            return []
    
    def store_metadata(self, platform: str, video_id: str, metadata: Dict[str, Any], 
                      checksum: str, version: int) -> bool:
        """
        Store metadata with all available backends.
        
        Args:
            platform: Platform identifier
            video_id: Video identifier
            metadata: Canonical metadata as dict
            checksum: Metadata checksum
            version: Version number
            
        Returns:
            True if stored successfully in any backend
        """
        success = False
        
        # Store to database
        if self.ENABLE_DATABASE:
            db_success = self._store_to_database(platform, video_id, metadata, checksum, version)
            success = success or db_success
        
        # Store to object storage
        if self.ENABLE_OBJECT_STORAGE:
            obj_success = self._store_to_object_storage(platform, video_id, metadata, checksum, version)
            success = success or obj_success
        
        # Always store to local files (fallback)
        file_success = self._store_to_local_file(platform, video_id, metadata, checksum, version)
        success = success or file_success
        
        return success
    
    def _store_to_database(self, platform: str, video_id: str, metadata: Dict[str, Any], 
                          checksum: str, version: int) -> bool:
        """Store metadata to database"""
        try:
            cursor = self.db_connection.cursor()
            
            # Insert metadata
            cursor.execute("""
                INSERT OR REPLACE INTO metadata 
                (platform, video_id, version, checksum, metadata_json)
                VALUES (?, ?, ?, ?, ?)
            """, (platform, video_id, version, checksum, json.dumps(metadata, indent=2)))
            
            # Update lineage
            cursor.execute("""
                INSERT INTO lineage 
                (platform, video_id, version, checksum)
                VALUES (?, ?, ?, ?)
            """, (platform, video_id, version, checksum))
            
            self.db_connection.commit()
            logger.info(f"Stored to database: {platform}/{video_id} v{version}")
            return True
            
        except Exception as e:
            logger.error(f"Database storage failed: {e}")
            return False
    
    def _store_to_object_storage(self, platform: str, video_id: str, metadata: Dict[str, Any], 
                                checksum: str, version: int) -> bool:
        """Store metadata to object storage"""
        try:
            key = f"{self.OBJECT_STORAGE_PREFIX}/{platform}/{video_id}/metadata_v{version}.json"
            data = json.dumps(metadata, indent=2, ensure_ascii=False)
            
            success = self.object_storage_client['put'](key, data)
            if success:
                logger.info(f"Stored to object storage: {key}")
            return success
            
        except Exception as e:
            logger.error(f"Object storage storage failed: {e}")
            return False
    
    def _store_to_local_file(self, platform: str, video_id: str, metadata: Dict[str, Any], 
                            checksum: str, version: int) -> bool:
        """Store metadata to local file system"""
        try:
            output_dir = self.processed_metadata_dir / platform / video_id
            output_dir.mkdir(parents=True, exist_ok=True)
            
            metadata_path = output_dir / "metadata.json"
            
            # Atomic write
            temp_path = output_dir / "metadata.tmp"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            
            # Atomic move
            temp_path.replace(metadata_path)
            
            logger.info(f"Stored to local file: {metadata_path}")
            return True
            
        except Exception as e:
            logger.error(f"Local file storage failed: {e}")
            return False
    
    def load_metadata(self, platform: str, video_id: str, version: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Load metadata from available backends.
        
        Args:
            platform: Platform identifier
            video_id: Video identifier
            version: Specific version (None for latest)
            
        Returns:
            Metadata dict or None if not found
        """
        # Try database first
        if self.ENABLE_DATABASE:
            metadata = self._load_from_database(platform, video_id, version)
            if metadata:
                return metadata
        
        # Try object storage
        if self.ENABLE_OBJECT_STORAGE:
            metadata = self._load_from_object_storage(platform, video_id, version)
            if metadata:
                return metadata
        
        # Fallback to local files
        return self._load_from_local_file(platform, video_id, version)
    
    def _load_from_database(self, platform: str, video_id: str, version: Optional[int]) -> Optional[Dict[str, Any]]:
        """Load metadata from database"""
        try:
            cursor = self.db_connection.cursor()
            
            if version:
                cursor.execute("""
                    SELECT metadata_json FROM metadata 
                    WHERE platform = ? AND video_id = ? AND version = ?
                """, (platform, video_id, version))
            else:
                cursor.execute("""
                    SELECT metadata_json FROM metadata 
                    WHERE platform = ? AND video_id = ?
                    ORDER BY version DESC LIMIT 1
                """, (platform, video_id))
            
            result = cursor.fetchone()
            if result:
                return json.loads(result[0])
            return None
            
        except Exception as e:
            logger.error(f"Database load failed: {e}")
            return None
    
    def _load_from_object_storage(self, platform: str, video_id: str, version: Optional[int]) -> Optional[Dict[str, Any]]:
        """Load metadata from object storage"""
        try:
            if version:
                key = f"{self.OBJECT_STORAGE_PREFIX}/{platform}/{video_id}/metadata_v{version}.json"
            else:
                # Find latest version
                prefix = f"{self.OBJECT_STORAGE_PREFIX}/{platform}/{video_id}/"
                objects = self.object_storage_client['list'](prefix)
                
                metadata_objects = [obj for obj in objects if obj.startswith(prefix) and obj.endswith('.json')]
                if not metadata_objects:
                    return None
                
                # Get latest version
                latest_object = sorted(metadata_objects)[-1]
                key = latest_object
            
            data = self.object_storage_client['get'](key)
            if data:
                return json.loads(data)
            return None
            
        except Exception as e:
            logger.error(f"Object storage load failed: {e}")
            return None
    
    def _load_from_local_file(self, platform: str, video_id: str, version: Optional[int]) -> Optional[Dict[str, Any]]:
        """Load metadata from local file"""
        try:
            metadata_path = self.processed_metadata_dir / platform / video_id / "metadata.json"
            
            if metadata_path.exists():
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return None
            
        except Exception as e:
            logger.error(f"Local file load failed: {e}")
            return None
    
    def get_next_version(self, platform: str, video_id: str) -> int:
        """Get next version number for metadata"""
        version_lock_key = f"version:{platform}:{video_id}"
        with self._locks[version_lock_key]:
            # Try database first
            if self.ENABLE_DATABASE:
                version = self._get_next_version_database(platform, video_id)
                if version > 0:
                    return version
            
            # Try object storage
            if self.ENABLE_OBJECT_STORAGE:
                version = self._get_next_version_object_storage(platform, video_id)
                if version > 0:
                    return version
            
            # Fallback to local files
            return self._get_next_version_local(platform, video_id)
    
    def _get_next_version_database(self, platform: str, video_id: str) -> int:
        """Get next version from database"""
        try:
            cursor = self.db_connection.cursor()
            cursor.execute(
                "SELECT MAX(version) FROM metadata WHERE video_id = ? AND platform = ?",
                (video_id, platform)
            )
            result = cursor.fetchone()
            return (result[0] or 0) + 1
        except Exception as e:
            logger.error(f"Database version check failed: {e}")
            return 0
    
    def _get_next_version_object_storage(self, platform: str, video_id: str) -> int:
        """Get next version from object storage"""
        try:
            prefix = f"{self.OBJECT_STORAGE_PREFIX}/{platform}/{video_id}/metadata_v"
            objects = self.object_storage_client['list'](prefix)
            
            versions = []
            for obj in objects:
                if obj.startswith(prefix) and obj.endswith('.json'):
                    try:
                        version_str = obj.split('_v')[1].replace('.json', '')
                        versions.append(int(version_str))
                    except:
                        pass
            
            return (max(versions) if versions else 0) + 1
        except Exception as e:
            logger.error(f"Object storage version check failed: {e}")
            return 0
    
    def _get_next_version_local(self, platform: str, video_id: str) -> int:
        """Get next version from local files"""
        try:
            metadata_dir = self.processed_metadata_dir / platform / video_id
            versions = []
            
            for file_path in metadata_dir.glob("metadata_v*.json"):
                try:
                    version = int(file_path.stem.split('_v')[1])
                    versions.append(version)
                except:
                    pass
            
            return (max(versions) if versions else 0) + 1
        except Exception as e:
            logger.error(f"Local file version check failed: {e}")
            return 1
    
    def cleanup(self):
        """Clean up resources"""
        if self.db_connection:
            self.db_connection.close()
        
        logger.info("Metadata store cleaned up")
    
    @contextmanager
    def get_connection(self):
        """Context manager for database connections"""
        if self.db_connection:
            yield self.db_connection
        else:
            raise RuntimeError("Database not initialized")
