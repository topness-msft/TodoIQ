import sys
import time
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# Add repo root to path
repo_root = Path(__file__).parent
sys.path.insert(0, str(repo_root))

# Import the refresh trigger function - now as absolute import
from src.handlers.sync_api import run_sync
from src.services.claude_runner import is_running

logger.info("Starting TodoNess manual refresh...")

# Trigger the refresh
result = run_sync()
logger.info(f"Sync trigger result: {result}")

if not result.get("ok"):
    if "already running" in result.get("message", "").lower():
        logger.info("Sync already running, waiting for completion...")
    else:
        logger.error(f"Failed to start sync: {result['message']}")
        sys.exit(1)

# Poll until the sync completes (max 10 minutes = 600 seconds)
max_wait = 600
start = time.time()
poll_count = 0
while is_running("sync"):
    elapsed = time.time() - start
    if elapsed > max_wait:
        logger.error(f"Sync timed out after {max_wait}s")
        sys.exit(1)
    poll_count += 1
    if poll_count % 6 == 1:  # Log every 30 seconds
        logger.info(f"Refresh still running... ({elapsed:.0f}s elapsed)")
    time.sleep(5)

elapsed = time.time() - start
logger.info(f"Refresh completed! (took {elapsed:.1f}s)")

# Now read the database and report results
db_path = repo_root / "data" / "claudetodo.db"
if not db_path.exists():
    logger.error(f"Database not found at {db_path}")
    sys.exit(1)

conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row
try:
    # Get the latest full_scan sync_log entry
    cursor = conn.execute("""
        SELECT * FROM sync_log 
        WHERE sync_type = 'full_scan' 
        ORDER BY sync_start DESC 
        LIMIT 1
    """)
    last_sync = cursor.fetchone()
    
    if last_sync:
        print("\n" + "="*60)
        print("SYNC LOG - Latest full_scan:")
        print("="*60)
        print(f"  Sync Start:    {last_sync['sync_start']}")
        print(f"  Sync End:      {last_sync['sync_end']}")
        print(f"  Status:        {last_sync['sync_status']}")
        print(f"  Created Count: {last_sync['created_count']}")
        print(f"  Updated Count: {last_sync['updated_count']}")
        if last_sync.get('error_message'):
            print(f"  Error:         {last_sync['error_message']}")
        
        print("\n" + "="*60)
        print(f"RESULT: {'SUCCESS' if last_sync['sync_status'] == 'complete' else 'INCOMPLETE'}")
        print("="*60)
    else:
        print("\nNo sync_log entries found for 'full_scan'")
        
finally:
    conn.close()
