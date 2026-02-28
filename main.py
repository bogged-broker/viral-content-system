"""
Main Entry Point for Viral Content System

This is the primary entry point to start the system.
Run with: python main.py --mode=<mode>

Modes:
    ingest       - Start ingestion loops only
    generate     - Start content generation only
    post         - Start posting system only
    train        - Start ML training only
    stress-test  - Stress testing mode
    full-system  - Start everything (default)

Examples:
    python main.py --mode=full-system
    python main.py --mode=ingest
    python main.py --mode=generate
"""

import sys
import os
import asyncio
import argparse
import logging
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Viral Content System - Main Entry Point',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --mode=full-system    # Start everything
  python main.py --mode=ingest         # Only ingestion
  python main.py --mode=generate       # Only generation
  python main.py --mode=post           # Only posting
  python main.py --mode=train          # Only training
  python main.py --mode=stress-test    # Stress testing
        """
    )
    
    parser.add_argument(
        '--mode',
        type=str,
        default='full-system',
        choices=['ingest', 'generate', 'post', 'train', 'stress-test', 'full-system'],
        help='Execution mode (default: full-system)'
    )
    
    parser.add_argument(
        '--interactive',
        action='store_true',
        help='Run in interactive mode after startup'
    )
    
    parser.add_argument(
        '--log-level',
        type=str,
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Logging level (default: INFO)'
    )
    
    return parser.parse_args()


async def main_async():
    """Async main entry point."""
    args = parse_args()
    
    # Set log level
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    logger = logging.getLogger(__name__)
    
    print("=" * 60)
    print("VIRAL CONTENT SYSTEM - Starting...")
    print("=" * 60)
    print(f"Mode: {args.mode}")
    print("=" * 60)
    
    try:
        # Import and start orchestrator
        from orchestration.system_orchestrator import SystemOrchestrator
        
        orchestrator = SystemOrchestrator()
        
        # Start system in specified mode
        success = await orchestrator.start(mode=args.mode)
        
        if not success:
            logger.error("Failed to start system orchestrator")
            sys.exit(1)
        
        logger.info("System started successfully")
        print("\n" + "=" * 60)
        print("SYSTEM RUNNING")
        print("=" * 60)
        print("The system is now running in the background.")
        print("Execution loops are active:")
        print("  - Ingestion: every 30 seconds")
        print("  - Feature Extraction: every 15 seconds")
        print("  - Scoring: every 20 seconds")
        print("  - Generation: every 25 seconds")
        print("\nPress Ctrl+C to stop the system.")
        print("=" * 60)
        print("=" * 60)
        print(f"Mode: {args.mode}")
        print("Press Ctrl+C to shutdown gracefully")
        print("=" * 60)
        
        # Interactive mode (if requested)
        if args.interactive:
            await interactive_mode(orchestrator)
        else:
            # Wait for shutdown signal
            await orchestrator.wait_for_shutdown()
        
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Ensure graceful shutdown
        try:
            if 'orchestrator' in locals() and orchestrator is not None:
                await orchestrator.shutdown()
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")


async def interactive_mode(orchestrator):
    """Interactive command mode."""
    print("\n" + "=" * 60)
    print("INTERACTIVE MODE")
    print("=" * 60)
    print("\nCommands:")
    print("  status    - Show system status")
    print("  shutdown  - Shutdown gracefully")
    print("  quit      - Exit")
    print()
    
    while True:
        try:
            cmd = input("> ").strip().split()
            if not cmd:
                continue
            
            command = cmd[0].lower()
            
            if command == "quit" or command == "exit":
                print("Shutting down...")
                await orchestrator.shutdown()
                break
            elif command == "shutdown":
                print("Shutting down...")
                await orchestrator.shutdown()
                break
            elif command == "status":
                status = orchestrator.get_status()
                print(f"\nSystem Status:")
                print(f"  Mode: {status['mode']}")
                print(f"  Components: {', '.join(status['started_components'])}")
                print(f"  Running Tasks: {status['running_tasks']}")
                print(f"  Shutdown Requested: {status['shutdown_requested']}")
            else:
                print(f"Unknown command: {command}")
                print("Commands: status, shutdown, quit")
        
        except KeyboardInterrupt:
            print("\nShutting down...")
            await orchestrator.shutdown()
            break
        except Exception as e:
            print(f"Error: {e}")


def main():
    """Synchronous main entry point - wraps async main."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nShutdown requested")
        sys.exit(0)
    except Exception as e:
        print(f"\nFatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
