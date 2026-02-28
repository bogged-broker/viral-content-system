"""
metadata_runner.py — ORCHESTRATION LAYER (BLUEPRINT COMPLIANT)

PURPOSE:
Handle ALL orchestration concerns - batching, parallel execution, metrics, retries.
This file does NOT parse metadata - only orchestrates the parsing pipeline.

SCALE TARGET: 10k-50k items/day
LATENCY TARGET: <50ms local, <200ms distributed
"""

import json
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Any, List, Tuple
from dataclasses import dataclass
import concurrent.futures
from threading import Lock

from metadata_loader import MetadataLoader
from metadata_parser_pure import MetadataParser, MetadataParserError
from metadata_store import MetadataStore
from metadata_monitor import ProductionMetrics

logger = logging.getLogger(__name__)

@dataclass
class ProcessingResult:
    """Result of processing a single item"""
    video_id: str
    platform: str
    success: bool
    canonical_metadata: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    processing_time_ms: float = 0.0

class MetadataRunner:
    """
    Orchestration layer for metadata processing.
    
    Responsibilities:
    1. Batching and parallel execution
    2. Metrics collection
    3. Error handling and retries
    4. Force logic and idempotency
    5. Pipeline coordination
    
    This file NEVER parses or validates metadata - only orchestrates processing.
    """
    
    def __init__(self, data_root: Path, max_workers: int = 8):
        self.data_root = Path(data_root)
        self.max_workers = max_workers
        
        # Initialize components
        self.loader = MetadataLoader(data_root)
        self.parser = MetadataParser()
        self.store = MetadataStore(data_root)
        self.metrics = ProductionMetrics()
        
        # Thread pool for parallel processing
        self.thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    
    def process_single(self, platform: str, video_id: str, force: bool = False) -> ProcessingResult:
        """
        Process a single content item.
        
        Args:
            platform: Platform identifier
            video_id: Unique video identifier
            force: Force re-parse even if metadata exists
            
        Returns:
            ProcessingResult with success/failure status
        """
        start_time = time.time()
        
        try:
            # Check idempotency with checksum validation
            if not force:
                source_checksums = self.loader.compute_checksums(platform, video_id)
                if self._is_already_processed_with_checksums(platform, video_id, source_checksums):
                    logger.info(
                        "Already processed with matching checksums",
                        extra={
                            "platform": platform,
                            "video_id": video_id
                        }
                    )
                    return ProcessingResult(
                        video_id=video_id,
                        platform=platform,
                        success=True,
                        processing_time_ms=(time.time() - start_time) * 1000
                    )
            
            # Load artifacts
            video_manifest, audio_manifest, scrape_metadata = self.loader.load(platform, video_id)
            
            # Parse metadata (pure function)
            canonical = self.parser.parse_from_artifacts(
                video_manifest, audio_manifest, scrape_metadata, platform, video_id
            )
            
            # Store metadata
            canonical_dict = asdict(canonical)
            success = self.store.store_metadata(
                platform, video_id, canonical_dict, 
                checksum=self._compute_output_checksum(canonical_dict),
                version=canonical.ingestion_metadata.version
            )
            
            if not success:
                raise RuntimeError("Failed to store metadata")
            
            # Record success
            processing_time_ms = (time.time() - start_time) * 1000
            self.metrics.record_success(platform, processing_time_ms)
            
            logger.info(
                "Processed metadata successfully",
                extra={
                    "platform": platform,
                    "video_id": video_id,
                    "processing_time_ms": processing_time_ms
                }
            )
            
            return ProcessingResult(
                video_id=video_id,
                platform=platform,
                success=True,
                canonical_metadata=canonical_dict,
                processing_time_ms=processing_time_ms
            )
            
        except Exception as e:
            processing_time_ms = (time.time() - start_time) * 1000
            self.metrics.record_failure(platform, str(e))
            
            logger.error(
                "Processing failed",
                extra={
                    "platform": platform,
                    "video_id": video_id,
                    "error": str(e),
                    "processing_time_ms": processing_time_ms
                }
            )
            
            return ProcessingResult(
                video_id=video_id,
                platform=platform,
                success=False,
                error=str(e),
                processing_time_ms=processing_time_ms
            )
    
    def process_batch(self, items: List[tuple[str, str]], force: bool = False) -> List[ProcessingResult]:
        """
        Process multiple items in parallel.
        
        Args:
            items: List of (platform, video_id) tuples
            force: Force re-parse even if metadata exists
            
        Returns:
            List of ProcessingResult objects
        """
        logger.info(f"Processing batch of {len(items)} items with {self.max_workers} workers")
        
        # Submit all jobs to thread pool
        futures = []
        for platform, video_id in items:
            future = self.thread_pool.submit(self.process_single, platform, video_id, force)
            futures.append(future)
        
        # Collect results
        results = []
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error(f"Batch processing error: {e}")
                # Create failure result for unknown items
                results.append(ProcessingResult(
                    video_id="unknown",
                    platform="unknown",
                    success=False,
                    error=str(e)
                ))
        
        # Log batch summary
        success_count = sum(1 for r in results if r.success)
        logger.info(
            f"Batch processing complete: {success_count}/{len(results)} successful",
            extra={
                "total_items": len(results),
                "successful": success_count,
                "failed": len(results) - success_count
            }
        )
        
        return results
    
    def process_directory(self, directory: Path, force: bool = False) -> List[ProcessingResult]:
        """
        Process all items in a directory structure.
        
        Args:
            directory: Directory containing platform/video_id subdirectories
            force: Force re-parse even if metadata exists
            
        Returns:
            List of ProcessingResult objects
        """
        items = []
        
        # Discover all items
        for platform_dir in directory.iterdir():
            if not platform_dir.is_dir():
                continue
            
            platform = platform_dir.name
            if platform not in ['youtube', 'tiktok', 'instagram', 'reddit', 'twitter', 'snapchat']:
                continue
            
            for video_id_dir in platform_dir.iterdir():
                if not video_id_dir.is_dir():
                    continue
                
                video_id = video_id_dir.name
                items.append((platform, video_id))
        
        logger.info(f"Discovered {len(items)} items to process")
        
        # Process in batches
        batch_size = 100
        all_results = []
        
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            batch_results = self.process_batch(batch, force)
            all_results.extend(batch_results)
        
        return all_results
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get current processing metrics"""
        return self.metrics.get_summary()
    
    def reset_metrics(self):
        """Reset all metrics"""
        self.metrics.reset()
        logger.info("Metrics reset")
    
    def cleanup(self):
        """Clean up resources"""
        self.thread_pool.shutdown(wait=True)
        self.store.cleanup()
        logger.info("Metadata runner cleaned up")
    
    # ------------------------------------------------------------------------ #
    # PRIVATE METHODS
    # ------------------------------------------------------------------------ #
    
    def _is_already_processed_with_checksums(self, platform: str, video_id: str, 
                                           source_checksums: Dict[str, Optional[str]]) -> bool:
        """Check if already processed WITH checksum validation"""
        # Load existing metadata
        try:
            existing_metadata = self.store.load_metadata(platform, video_id)
            if not existing_metadata:
                return False
            
            # Compare source checksums with stored checksums
            return self._checksums_match_existing(source_checksums, existing_metadata)
            
        except Exception as e:
            logger.warning(f"Failed to validate checksums for {platform}/{video_id}: {e}")
            return False
    
    def _checksums_match_existing(self, source_checksums: Dict[str, Optional[str]], 
                                 existing_metadata: Dict[str, Any]) -> bool:
        """Check if source checksums match existing metadata checksums"""
        # Get stored checksums from existing metadata
        stored_checksums = existing_metadata.get('ingestion_metadata', {}).get('previous_versions', {})
        
        # Compare non-None checksums
        for key, source_checksum in source_checksums.items():
            if source_checksum is not None:
                # Find the most recent checksum for this key
                latest_stored = None
                for version_key, stored_checksum in stored_checksums.items():
                    if stored_checksum == source_checksum:
                        latest_stored = stored_checksum
                        break
                
                if latest_stored != source_checksum:
                    return False
        
        return True
    
    def _compute_output_checksum(self, metadata: Dict[str, Any]) -> str:
        """Compute checksum of output metadata"""
        import hashlib
        
        # Create deterministic string representation
        metadata_str = json.dumps(metadata, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(metadata_str.encode()).hexdigest()
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.cleanup()
