"""
metadata_cli.py — COMMAND LINE INTERFACE (BLUEPRINT COMPLIANT)

PURPOSE:
Handle ALL CLI operations - health checks, cleanup, metrics, batch processing.
This file does NOT parse metadata - only provides user interface.

SCALE TARGET: 10k-50k items/day
LATENCY TARGET: <50ms local, <200ms distributed
"""

import json
import argparse
import sys
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime
import logging

from metadata_parser import MetadataParser, MetadataParserError

logger = logging.getLogger(__name__)

class MetadataCLI:
    """
    Command-line interface for metadata operations.
    
    Responsibilities:
    1. Parse command-line arguments
    2. Execute metadata operations
    3. Display results and errors
    4. Handle batch processing
    5. Provide health check interface
    
    This file NEVER parses or validates metadata - only orchestrates operations.
    """
    
    def __init__(self):
        self.parser = self._setup_argument_parser()
    
    def _setup_argument_parser(self) -> argparse.ArgumentParser:
        """Setup command-line argument parser"""
        parser = argparse.ArgumentParser(
            description='Production Metadata Parser - 300M+ Views Ready',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  # Parse single video
  python metadata_cli.py --data-root /data --platform youtube --video-id abc123

  # Batch process directory
  python metadata_cli.py --data-root /data --batch

  # Health check
  python metadata_cli.py --data-root /data --health-check

  # Show metrics
  python metadata_cli.py --data-root /data --metrics

  # Cleanup resources
  python metadata_cli.py --data-root /data --cleanup
            """
        )
        
        # Required arguments
        parser.add_argument(
            '--data-root',
            type=Path,
            help='Root directory for raw and processed metadata'
        )
        
        # Processing modes
        parser.add_argument(
            '--platform',
            choices=['youtube', 'tiktok', 'instagram', 'reddit', 'twitter', 'snapchat'],
            help='Platform identifier'
        )
        parser.add_argument(
            '--video-id',
            help='Unique video identifier'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force re-parse even if metadata exists'
        )
        parser.add_argument(
            '--batch',
            action='store_true',
            help='Process directory in batch mode'
        )
        
        # Production features
        parser.add_argument(
            '--enable-database',
            action='store_true',
            help='Enable database storage'
        )
        parser.add_argument(
            '--enable-object-storage',
            action='store_true',
            help='Enable object storage'
        )
        
        # Operations
        parser.add_argument(
            '--health-check',
            action='store_true',
            help='Run comprehensive system health check'
        )
        parser.add_argument(
            '--cleanup',
            action='store_true',
            help='Clean up resources and exit'
        )
        parser.add_argument(
            '--metrics',
            action='store_true',
            help='Show production metrics'
        )
        
        return parser
    
    def run(self, args: Optional[list] = None) -> int:
        """Run CLI with provided arguments"""
        try:
            parsed_args = self.parser.parse_args(args)
            
            # Initialize production features
            if parsed_args.enable_database:
                import os
                os.environ['ENABLE_DATABASE'] = 'true'
            if parsed_args.enable_object_storage:
                import os
                os.environ['ENABLE_OBJECT_STORAGE'] = 'true'
            
            # Use context manager for automatic resource cleanup
            with MetadataParser(parsed_args.data_root) as metadata_parser:
                return self._execute_command(parsed_args, metadata_parser)
                
        except KeyboardInterrupt:
            print("\nOperation interrupted by user")
            return 130
        except Exception as e:
            print(f"CLI Error: {e}")
            return 1
    
    def _execute_command(self, args, metadata_parser: MetadataParser) -> int:
        """Execute the requested command"""
        try:
            if args.health_check:
                return self._run_health_check(metadata_parser)
            
            elif args.cleanup:
                return self._run_cleanup(metadata_parser)
            
            elif args.metrics:
                return self._show_metrics(metadata_parser)
            
            elif args.batch:
                return self._run_batch_processing(args, metadata_parser)
            
            elif args.platform and args.video_id:
                return self._run_single_parse(args, metadata_parser)
            
            else:
                self.parser.print_help()
                return 0
                
        except MetadataParserError as e:
            print(f"Parse failed: {e}")
            return 1
        except Exception as e:
            print(f"Unexpected error: {e}")
            return 1
    
    def _run_health_check(self, metadata_parser: MetadataParser) -> int:
        """Run comprehensive health check"""
        print("Running comprehensive system health check...")
        health_status = metadata_parser.validate_system_health()
        
        print(f"\n=== SYSTEM HEALTH STATUS: {health_status['overall_health'].upper()} ===")
        print(f"Checks performed: {health_status['checks_performed']}")
        print(f"Checks passed: {health_status['checks_passed']}")
        print(f"Issues found: {len(health_status['issues'])}")
        
        if health_status['issues']:
            print("\n=== ISSUES ===")
            for issue in health_status['issues']:
                print(f"• {issue['type'].replace('_', ' ').title()}: {issue['severity']}")
                if 'error' in issue:
                    print(f"  Error: {issue['error']}")
                if 'message' in issue:
                    print(f"  Message: {issue['message']}")
                if 'missing_dirs' in issue:
                    print(f"  Missing directories: {issue['missing_dirs']}")
        
        # Show metrics summary
        metrics = health_status['metrics']
        print(f"\n=== PERFORMANCE METRICS ===")
        print(f"Items processed: {metrics['items_processed']}")
        print(f"Items failed: {metrics['items_failed']}")
        print(f"Success rate: {metrics['success_rate']:.2%}")
        print(f"Average processing time: {metrics['avg_processing_time_ms']:.2f}ms")
        print(f"Items per second: {metrics['items_per_second']:.2f}")
        
        # Exit with error code if health is critical
        if health_status['overall_health'] == 'critical':
            print("\n❌ CRITICAL ISSUES DETECTED - System may not function properly")
            return 1
        elif health_status['overall_health'] == 'degraded':
            print("\n⚠️  SYSTEM DEGRADED - Some functionality may be limited")
            return 2
        else:
            print("\n✅ SYSTEM HEALTHY - All checks passed")
            return 0
    
    def _run_cleanup(self, metadata_parser: MetadataParser) -> int:
        """Clean up resources"""
        print("Cleaning up resources...")
        metadata_parser.cleanup_resources()
        print("✅ Resource cleanup completed")
        return 0
    
    def _show_metrics(self, metadata_parser: MetadataParser) -> int:
        """Show current metrics"""
        metrics = metadata_parser.get_production_metrics()
        print("\n=== PRODUCTION METRICS ===")
        for key, value in metrics.items():
            print(f"{key}: {value}")
        return 0
    
    def _run_batch_processing(self, args, metadata_parser: MetadataParser) -> int:
        """Run batch processing"""
        if not args.data_root:
            print("Error: --data-root required for batch mode")
            return 1
        
        print(f"Processing directory: {args.data_root}")
        results = metadata_parser.process_directory(args.data_root)
        
        print(f"\nProcessed {len(results)} items successfully")
        
        # Show final metrics
        metrics = metadata_parser.get_production_metrics()
        print("\n=== FINAL METRICS ===")
        for key, value in metrics.items():
            print(f"{key}: {value}")
        
        return 0
    
    def _run_single_parse(self, args, metadata_parser: MetadataParser) -> int:
        """Run single item parse"""
        metadata = metadata_parser.parse(
            platform=args.platform, 
            video_id=args.video_id, 
            force=args.force
        )
        
        print(f"Video: {metadata.content_identity.video_id}")
        print(f"Author: {metadata.content_identity.author_id}")
        print(f"Duration: {metadata.media.video.duration_seconds}s")
        print(f"Resolution: {metadata.media.video.resolution}")
        print(f"Timeline valid: {metadata.timeline.validated_alignment}")
        print(f"Views: {metadata.engagement_snapshot.views:,}")
        print(f"Likes: {metadata.engagement_snapshot.likes:,}")
        print(f"Saves: {metadata.engagement_snapshot.saves if metadata.engagement_snapshot.saves is not None else 0}")
        
        # Optionally write CLI output to JSON file for debugging
        try:
            output_file = args.data_root / "last_parsed.json"
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(metadata.__dict__, f, indent=2, ensure_ascii=False, default=str)
            print(f"Debug output written to: {output_file}")
        except Exception as e:
            print(f"Warning: Failed to write debug output: {e}")
        
        return 0

def main():
    """Main entry point"""
    cli = MetadataCLI()
    exit_code = cli.run()
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
