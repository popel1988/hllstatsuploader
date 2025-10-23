"""
CRCON Standalone Sync: ONE-WAY data export to external database
Runs independently from CRCON, accesses database directly
Docker-optimized version - Configuration via environment variables
"""

import json
import hashlib
import requests
import time
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
import psycopg2
from psycopg2.extras import RealDictCursor
import logging

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION FROM ENVIRONMENT VARIABLES
# ============================================================================

# Database configuration from environment variables (directly from docker-compose)
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', '5432')),
    'database': os.getenv('DB_NAME', 'rcon'),
    'user': os.getenv('DB_USER', 'rcon'),
    'password': os.getenv('DB_PASSWORD', '')
}

# ============================================================================
# CONFIGURATION FROM ENVIRONMENT VARIABLES (docker-compose.yml)
# ============================================================================

# External API configuration
EXTERNAL_DB_URL = os.getenv('EXTERNAL_DB_URL', 'https://dein-externer-server.com/api/sync')
EXTERNAL_DB_API_KEY = os.getenv('EXTERNAL_DB_API_KEY', '')

# Synchronization settings
SYNC_INTERVAL_MINUTES = int(os.getenv('SYNC_INTERVAL_MINUTES', '5'))
ENABLE_SYNC = os.getenv('ENABLE_SYNC', 'true').lower() == 'true'

# Table-specific batch sizes (optimized for different table sizes)
BATCH_SIZE_LOG_LINES = int(os.getenv('BATCH_SIZE_LOG_LINES', '50000'))      # Medium size, many records
BATCH_SIZE_PLAYER_STATS = int(os.getenv('BATCH_SIZE_PLAYER_STATS', '25000')) # Large size, complex data
BATCH_SIZE_MAPS = int(os.getenv('BATCH_SIZE_MAPS', '10000'))                # Small size, simple data
BATCH_SIZE_PLAYER_SESSIONS = int(os.getenv('BATCH_SIZE_PLAYER_SESSIONS', '30000')) # Medium size

# Legacy batch size (for backward compatibility)
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '100000'))

# Session export behavior
EXPORT_ONLY_CLOSED_SESSIONS = os.getenv('EXPORT_ONLY_CLOSED_SESSIONS', 'true').lower() == 'true'
CHECK_RECENTLY_CLOSED_SESSIONS = os.getenv('CHECK_RECENTLY_CLOSED_SESSIONS', 'true').lower() == 'true'
RECENT_SESSION_LOOKBACK_HOURS = int(os.getenv('RECENT_SESSION_LOOKBACK_HOURS', '24'))

# Server configuration
ENABLED_SERVERS = os.getenv('ENABLED_SERVERS', '1').split(',')
EXTERNAL_SERVER_ID = os.getenv('EXTERNAL_SERVER_ID', 'crcon_server_001')

# Server names (from JSON string in environment variable)
SERVER_NAMES_JSON = os.getenv('SERVER_NAMES', '{"1": "Server-DE-01"}')
try:
    SERVER_NAMES = json.loads(SERVER_NAMES_JSON)
except json.JSONDecodeError:
    logger.error(f"Error parsing SERVER_NAMES: {SERVER_NAMES_JSON}")
    SERVER_NAMES = {"1": "Server-DE-01"}

# Network settings
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '300'))  # 5 minutes for large batches
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))
RETRY_DELAY = int(os.getenv('RETRY_DELAY', '5'))

# Status file (in /data volume in Docker container)
STATE_DIR = Path(os.getenv('STATE_DIR', '/data'))
STATE_DIR.mkdir(parents=True, exist_ok=True)
SYNC_STATE_FILE = STATE_DIR / "external_sync_state.json"

# ============================================================================
# DATABASE CONNECTION
# ============================================================================

def get_db_connection():
    """Creates a PostgreSQL database connection"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except psycopg2.Error as e:
        logger.error(f"Database connection failed: {e}")
        raise

# ============================================================================
# HELPER FUNCTIONS (identical to plugin version)
# ============================================================================

def load_sync_state() -> Dict:
    """Loads export status (highest exported IDs per table)"""
    if SYNC_STATE_FILE.exists():
        try:
            with open(SYNC_STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading export status: {e}")
    
    return {
        'last_exported_ids': {
            'log_lines': 0,
            'player_sessions': 0,
            'player_stats': 0,
            'map_history': 0
        },
        'export_count': 0,
        'last_export_time': None,
        'last_success': None,
        'last_error': None,
        'total_exported': {
            'log_lines': 0,
            'player_sessions': 0,
            'player_stats': 0,
            'map_history': 0
        }
    }


def save_sync_state(state: Dict) -> None:
    """Saves export status (only on successful export!)"""
    try:
        state['last_export_time'] = datetime.utcnow().isoformat()
        with open(SYNC_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        logger.debug(f"Export status saved: {state.get('last_exported_ids')}")
    except Exception as e:
        logger.error(f"Error saving export status: {e}")


def create_map_external_id(server_name: str, map_id: int, start: datetime, end: Optional[datetime], map_name: str) -> str:
    """Creates unique external map ID with hash"""
    start_str = start.isoformat() if isinstance(start, datetime) else str(start)
    end_str = end.isoformat() if end and isinstance(end, datetime) else str(end) if end else "ongoing"
    
    hash_base = f"{start_str}_{end_str}_{map_name}"
    hash_short = hashlib.md5(hash_base.encode()).hexdigest()[:12]
    
    return f"{server_name}_map_{map_id}_{hash_short}"


# ============================================================================
# DATA EXPORT FUNCTIONS (adapted for direct DB access)
# ============================================================================

def get_map_id_mapping(conn, since_id: int = 0) -> tuple:
    """Loads new maps and creates ID mapping (internal_id -> map_external_id)"""
    maps_for_export = []
    id_mapping = {}
    highest_id = since_id
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            server_numbers = [int(s) for s in ENABLED_SERVERS]  # As integer, not string!
            logger.info(f"🔍 Loading maps: since_id={since_id}, server_numbers={server_numbers}")
            
            cursor.execute("""
                SELECT id, start, "end", server_number, map_name, result 
                FROM map_history 
                WHERE server_number = ANY(%s)
                AND id > %s
                ORDER BY id ASC
                LIMIT %s
            """, (server_numbers, since_id, BATCH_SIZE_MAPS))
            
            for row in cursor.fetchall():
                internal_id = row['id']
                highest_id = max(highest_id, internal_id)
                
                server_number = str(row['server_number'])
                server_name = SERVER_NAMES.get(server_number, f"Server-{server_number}")
                
                map_external_id = create_map_external_id(
                    server_name=server_name,
                    map_id=internal_id,
                    start=row['start'],
                    end=row['end'],
                    map_name=row['map_name']
                )
                
                id_mapping[internal_id] = map_external_id
                
                result_json = None
                if row['result']:
                    try:
                        result_json = json.loads(row['result']) if isinstance(row['result'], str) else row['result']
                    except (json.JSONDecodeError, TypeError):
                        pass
                
                map_entry = {
                    'map_external_id': map_external_id,
                    'start': row['start'].isoformat() if row['start'] else None,
                    'end': row['end'].isoformat() if row['end'] else None,
                    'server_name': server_name,
                    'map_name': row['map_name'],
                    'result': result_json
                }
                
                maps_for_export.append(map_entry)
            
            if maps_for_export:
                logger.info(f"New maps loaded: {len(maps_for_export)} entries (since ID {since_id}, up to ID {highest_id})")
                
    except Exception as e:
        logger.error(f"Error loading map data: {e}")
    
    return maps_for_export, id_mapping, highest_id


def get_filtered_logs(conn, since_id: int = 0) -> tuple:
    """Loads new KILL/TEAM KILL logs with Steam IDs (JOIN), only enabled servers"""
    logs = []
    highest_id = since_id
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            # CRCON DB stores only the number (not "Server 2")
            server_filters = ENABLED_SERVERS  # ['2']
            logger.info(f"🔍 Loading log lines: since_id={since_id}, server_filters={server_filters}")
            
            cursor.execute("""
                SELECT 
                    ll.id,
                    ll.event_time,
                    ll.type,
                    ll.weapon,
                    s1.steam_id_64 AS player1_steamid,
                    s2.steam_id_64 AS player2_steamid,
                    ll.server
                FROM log_lines AS ll
                LEFT JOIN steam_id_64 AS s1 ON ll.player1_steamid = s1.id
                LEFT JOIN steam_id_64 AS s2 ON ll.player2_steamid = s2.id
                WHERE ll.type IN ('KILL', 'TEAM KILL')
                AND ll.id > %s
                AND ll.server = ANY(%s)
                ORDER BY ll.id ASC
                LIMIT %s
            """, (since_id, server_filters, BATCH_SIZE_LOG_LINES))
            
            for row in cursor.fetchall():
                internal_id = row['id']
                highest_id = max(highest_id, internal_id)
                
                # Extract server_number from "Server 1" → "1"
                server_str = row['server'] if row['server'] else "Server 1"
                server_number = server_str.split()[-1] if server_str else "1"
                server_name = SERVER_NAMES.get(server_number, f"Server-{server_number}")
                
                log_entry = {
                    'event_time': row['event_time'].isoformat() if row['event_time'] else None,
                    'type': row['type'],
                    'weapon': row['weapon'],
                    'player1_steamid': row['player1_steamid'],
                    'player2_steamid': row['player2_steamid'],
                    'server_name': server_name
                }
                
                logs.append(log_entry)
            
            if logs:
                logger.info(f"New logs loaded: {len(logs)} entries (since ID {since_id}, up to ID {highest_id})")
            else:
                logger.info(f"⚠️ No new log lines found (since ID {since_id})")
                
    except Exception as e:
        logger.error(f"Error loading new logs: {e}")
    
    return logs, highest_id


def get_player_stats(conn, since_id: int = 0) -> tuple:
    """Loads new player_stats with JOINs (Steam-ID, map data, server_name)"""
    stats = []
    highest_id = since_id
    logger.info(f"🔍 Loading player stats: since_id={since_id}")
    
    def safe_json_parse(data, field_name):
        if data is None:
            return None
        try:
            if isinstance(data, str):
                return json.loads(data)
            return data
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"JSON parse error for {field_name}: {e}")
            return None
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            server_numbers = [int(s) for s in ENABLED_SERVERS]  # As integer!
            
            cursor.execute("""
                SELECT 
                    ps.id,
                    s.steam_id_64 AS steam_id_64,
                    ps.map_id,
                    mh.start,
                    mh.end,
                    mh.server_number,
                    mh.map_name,
                    ps.kills,
                    ps.kills_streak,
                    ps.deaths,
                    ps.deaths_without_kill_streak,
                    ps.teamkills,
                    ps.teamkills_streak,
                    ps.deaths_by_tk,
                    ps.deaths_by_tk_streak,
                    ps.time_seconds,
                    ps.kills_per_minute,
                    ps.deaths_per_minute,
                    ps.kill_death_ratio,
                    ps.longest_life_secs,
                    ps.shortest_life_secs,
                    ps.death_by,
                    ps.most_killed,
                    ps.name,
                    ps.weapons,
                    ps.death_by_weapons,
                    ps.combat,
                    ps.offense,
                    ps.defense,
                    ps.support
                FROM player_stats AS ps
                LEFT JOIN steam_id_64 AS s ON ps.playersteamid_id = s.id
                LEFT JOIN map_history AS mh ON ps.map_id = mh.id
                WHERE ps.id > %s
                AND mh.server_number = ANY(%s)
                ORDER BY ps.id ASC
                LIMIT %s
            """, (since_id, server_numbers, BATCH_SIZE_PLAYER_STATS))
            
            for row in cursor.fetchall():
                stat_id = row['id']
                highest_id = max(highest_id, stat_id)
                
                server_number = str(row['server_number'])
                server_name = SERVER_NAMES.get(server_number, f"Server-{server_number}")
                
                map_external_id = create_map_external_id(
                    server_name=server_name,
                    map_id=row['map_id'],
                    start=row['start'],
                    end=row['end'],
                    map_name=row['map_name']
                )
                
                stat_entry = {
                    'steam_id_64': row['steam_id_64'],
                    'map_external_id': map_external_id,
                    'server_name': server_name,
                    'kills': row['kills'],
                    'kills_streak': row['kills_streak'],
                    'deaths': row['deaths'],
                    'deaths_without_kill_streak': row['deaths_without_kill_streak'],
                    'teamkills': row['teamkills'],
                    'teamkills_streak': row['teamkills_streak'],
                    'deaths_by_tk': row['deaths_by_tk'],
                    'deaths_by_tk_streak': row['deaths_by_tk_streak'],
                    'time_seconds': row['time_seconds'],
                    'kills_per_minute': float(row['kills_per_minute']) if row['kills_per_minute'] is not None else None,
                    'deaths_per_minute': float(row['deaths_per_minute']) if row['deaths_per_minute'] is not None else None,
                    'kill_death_ratio': float(row['kill_death_ratio']) if row['kill_death_ratio'] is not None else None,
                    'longest_life_secs': row['longest_life_secs'],
                    'shortest_life_secs': row['shortest_life_secs'],
                    'death_by': safe_json_parse(row['death_by'], 'death_by'),
                    'most_killed': safe_json_parse(row['most_killed'], 'most_killed'),
                    'name': row['name'],
                    'weapons': safe_json_parse(row['weapons'], 'weapons'),
                    'death_by_weapons': safe_json_parse(row['death_by_weapons'], 'death_by_weapons'),
                    'combat': row['combat'],
                    'offense': row['offense'],
                    'defense': row['defense'],
                    'support': row['support'],
                    'match_end': row['end'].isoformat() if row['end'] else None
                }
                
                stats.append(stat_entry)
            
            if stats:
                logger.info(f"New player stats loaded: {len(stats)} entries (since ID {since_id}, up to ID {highest_id})")
                
    except Exception as e:
        logger.error(f"Error loading new player stats: {e}")
    
    return stats, highest_id


def get_player_sessions(conn, since_id: int = 0) -> tuple:
    """
    Loads player_sessions with 2-query strategy:
    1. New sessions (ID > since_id)
    2. Lookback for recently closed sessions
    """
    sessions = []
    highest_checked_id = since_id
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            server_numbers = [int(s) for s in ENABLED_SERVERS]  # As integer!
            
            cursor.execute("""
                SELECT 
                    psess.id,
                    s.steam_id_64 AS steam_id_64,
                    psess.start,
                    psess.end,
                    psess.server_number
                FROM player_sessions AS psess
                LEFT JOIN steam_id_64 AS s ON psess.playersteamid_id = s.id
                WHERE psess.id > %s
                AND psess.server_number = ANY(%s)
                ORDER BY psess.id ASC
                LIMIT %s
            """, (since_id, server_numbers, BATCH_SIZE_PLAYER_SESSIONS))
            
            result_new = cursor.fetchall()
            
            closed_count = 0
            skipped_count = 0
            recently_closed_count = 0
            
            for row in result_new:
                internal_id = row['id']
                highest_checked_id = max(highest_checked_id, internal_id)
                
                if EXPORT_ONLY_CLOSED_SESSIONS and row['end'] is None:
                    skipped_count += 1
                    continue
                
                server_number = str(row['server_number'])
                server_name = SERVER_NAMES.get(server_number, f"Server-{server_number}")
                
                session_entry = {
                    'steam_id_64': row['steam_id_64'],
                    'start': row['start'].isoformat() if row['start'] else None,
                    'end': row['end'].isoformat() if row['end'] else None,
                    'server_name': server_name
                }
                
                sessions.append(session_entry)
                closed_count += 1
            
            # Query 2: Lookback for recently closed sessions
            # DISABLED: Causes duplicates on every run
            # The first query with "since ID 0" fetched all sessions on first run
            # After that, new closed sessions are found via Query 1
            if False:  # CHECK_RECENTLY_CLOSED_SESSIONS disabled for standalone
                cutoff_time = datetime.utcnow() - timedelta(hours=RECENT_SESSION_LOOKBACK_HOURS)
                
                cursor.execute("""
                    SELECT 
                        psess.id,
                        s.steam_id_64 AS steam_id_64,
                        psess.start,
                        psess.end,
                        psess.server_number
                    FROM player_sessions AS psess
                    LEFT JOIN steam_id_64 AS s ON psess.playersteamid_id = s.id
                    WHERE psess.end IS NOT NULL
                    AND psess.end > %s
                    AND psess.id <= %s
                    AND psess.server_number = ANY(%s)
                    ORDER BY psess.id ASC
                """, (cutoff_time, since_id, server_numbers))
                
                result_recent = cursor.fetchall()
                
                for row in result_recent:
                    server_number = str(row['server_number'])
                    server_name = SERVER_NAMES.get(server_number, f"Server-{server_number}")
                    
                    session_entry = {
                        'steam_id_64': row['steam_id_64'],
                        'start': row['start'].isoformat() if row['start'] else None,
                        'end': row['end'].isoformat() if row['end'] else None,
                        'server_name': server_name
                    }
                    
                    sessions.append(session_entry)
                    recently_closed_count += 1
            
            if result_new:
                logger.info(f"Player sessions checked: {len(result_new)} new entries (since ID {since_id}, up to ID {highest_checked_id})")
                logger.info(f"  → {closed_count} new closed, {skipped_count} open skipped")
                if recently_closed_count > 0:
                    logger.info(f"  → {recently_closed_count} recently closed (lookback) caught up")
                    
    except Exception as e:
        logger.error(f"Error loading new player sessions: {e}")
    
    return sessions, highest_checked_id


def prepare_data_for_export() -> Optional[tuple]:
    """Collects new data from all tables (based on last_exported_id)"""
    if not ENABLE_SYNC:
        logger.info("Export is disabled")
        return None
    
    try:
        conn = get_db_connection()
        state = load_sync_state()
        last_ids = state.get('last_exported_ids', {})
        
        # Load new data
        maps, map_id_mapping, maps_highest_id = get_map_id_mapping(conn, last_ids.get('map_history', 0))
        logs, logs_highest_id = get_filtered_logs(conn, last_ids.get('log_lines', 0))
        sessions, sessions_highest_id = get_player_sessions(conn, last_ids.get('player_sessions', 0))
        stats, stats_highest_id = get_player_stats(conn, last_ids.get('player_stats', 0))
        
        conn.close()
        
        # Create data package (flat structure for backend API)
        data = {
            'server_id': EXTERNAL_SERVER_ID,
            'maps': maps,
            'log_lines': logs,
            'player_sessions': sessions,
            'player_stats': stats
        }
        
        # Update state
        new_state = state.copy()
        new_state['last_exported_ids'] = {
            'map_history': maps_highest_id,
            'log_lines': logs_highest_id,
            'player_sessions': sessions_highest_id,
            'player_stats': stats_highest_id
        }
        new_state['export_count'] = state.get('export_count', 0) + 1
        
        # Update total statistics
        new_state['total_exported']['log_lines'] = state.get('total_exported', {}).get('log_lines', 0) + len(logs)
        new_state['total_exported']['player_sessions'] = state.get('total_exported', {}).get('player_sessions', 0) + len(sessions)
        new_state['total_exported']['player_stats'] = state.get('total_exported', {}).get('player_stats', 0) + len(stats)
        new_state['total_exported']['map_history'] = state.get('total_exported', {}).get('map_history', 0) + len(maps)
        
        logger.info(f"📦 Data package prepared: Maps={len(maps)}, Logs={len(logs)}, Sessions={len(sessions)}, Stats={len(stats)}")
        
        return data, new_state
        
    except Exception as e:
        logger.error(f"Error during data preparation: {e}", exc_info=True)
        return None


def send_to_external_db(data: Dict, state: Dict) -> bool:
    """Sends data to external API with retry logic, saves state only on success"""
    if not data:
        logger.warning("No data to send")
        return False
    
    # Check if there are any new data available
    counts = {
        'maps': len(data.get('maps', [])),
        'log_lines': len(data.get('log_lines', [])),
        'player_sessions': len(data.get('player_sessions', [])),
        'player_stats': len(data.get('player_stats', []))
    }
    total_new = sum(counts.values())
    
    if total_new == 0:
        logger.info("No new data to export")
        return True
    
    logger.info(f"Sending {total_new} new entries: {counts}")
    
    headers = {
        'Content-Type': 'application/json'
    }
    
    if EXTERNAL_DB_API_KEY:
        headers['Authorization'] = f"Bearer {EXTERNAL_DB_API_KEY}"
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"Attempt {attempt}/{MAX_RETRIES}: Sending data to {EXTERNAL_DB_URL}")
            
            response = requests.post(
                EXTERNAL_DB_URL,
                json=data,
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )
            
            if response.status_code == 200:
                try:
                    response_data = response.json()
                    if response_data.get('success'):
                        logger.info(f"✅ Export successful: {response_data.get('message', 'OK')}")
                        state['last_success'] = datetime.utcnow().isoformat()
                        state['last_error'] = None
                        save_sync_state(state)
                        return True
                    else:
                        error_msg = response_data.get('error', 'Unknown error')
                        logger.error(f"API reports error: {error_msg}")
                        state['last_error'] = error_msg
                except json.JSONDecodeError:
                    logger.info("Data sent (no JSON response)")
                    state['last_success'] = datetime.utcnow().isoformat()
                    state['last_error'] = None
                    save_sync_state(state)
                    return True
            elif response.status_code == 429:
                logger.warning(f"Rate limit reached, waiting {RETRY_DELAY * attempt}s...")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
            else:
                logger.error(f"HTTP error {response.status_code}: {response.text[:500]}")
                state['last_error'] = f"HTTP {response.status_code}"
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
        except requests.exceptions.Timeout:
            logger.error(f"Timeout after {REQUEST_TIMEOUT}s (attempt {attempt})")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * 2)
                continue
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
    
    logger.error(f"❌ Export failed after {MAX_RETRIES} attempts")
    return False


# ============================================================================
# MAIN SYNC FUNCTION
# ============================================================================

def sync_data() -> None:
    """Main function: Exports new data to external DB"""
    if not ENABLE_SYNC:
        logger.info("Export is disabled")
        return
    
    logger.info("=" * 60)
    logger.info("🔄 Starting data export (Standalone)")
    logger.info("=" * 60)
    
    try:
        # Data preparation
        result = prepare_data_for_export()
        
        if not result:
            logger.info("No data to export or error during preparation")
            return
        
        data, new_state = result
        
        # Send to external DB
        success = send_to_external_db(data, new_state)
        
        if success:
            logger.info("✅ Export completed")
        else:
            logger.warning("⚠️  Export failed - will be retried next time")
            
    except Exception as e:
        logger.error(f"Critical error during export: {e}", exc_info=True)


# ============================================================================
# TEST AND DEBUG FUNCTIONS
# ============================================================================

def test_db_connection() -> bool:
    """Tests database connection"""
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT version();")
            version = cursor.fetchone()
            logger.info(f"✅ DB connection successful: {version[0]}")
        conn.close()
        return True
    except Exception as e:
        logger.error(f"❌ DB connection failed: {e}")
        return False


def get_export_status() -> Dict:
    """Returns current export status (monitoring/debugging)"""
    state = load_sync_state()
    return {
        'enabled': ENABLE_SYNC,
        'server_id': EXTERNAL_SERVER_ID,
        'batch_size': BATCH_SIZE,
        'last_export': state.get('last_export_time'),
        'last_success': state.get('last_success'),
        'last_error': state.get('last_error'),
        'export_count': state.get('export_count', 0),
        'last_exported_ids': state.get('last_exported_ids', {}),
        'total_exported': state.get('total_exported', {}),
        'db_config': {
            'host': DB_CONFIG['host'],
            'port': DB_CONFIG['port'],
            'database': DB_CONFIG['database'],
            'user': DB_CONFIG['user']
        }
    }


def reset_export_state() -> None:
    """Resets export status (WARNING: Next export will send EVERYTHING!)"""
    logger.warning("=" * 60)
    logger.warning("Export status will be reset!")
    logger.warning("Next export will start at ID 0 for all tables!")
    logger.warning("=" * 60)
    
    if SYNC_STATE_FILE.exists():
        SYNC_STATE_FILE.unlink()
        logger.info("✅ State file deleted")
    else:
        logger.info("ℹ️  State file does not exist")
    
    logger.info("Next export will send ALL data from the beginning")


# ============================================================================
# MAIN & SCHEDULER
# ============================================================================

def main_loop():
    """Main loop: Runs export periodically"""
    logger.info("🚀 Standalone Sync started")
    logger.info(f"DB: {DB_CONFIG['user']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    logger.info(f"Interval: {SYNC_INTERVAL_MINUTES} minutes")
    logger.info(f"External API: {EXTERNAL_DB_URL}")
    logger.info("📊 Batch sizes:")
    logger.info(f"  - Maps: {BATCH_SIZE_MAPS:,} rows (~30 sec)")
    logger.info(f"  - Log Lines: {BATCH_SIZE_LOG_LINES:,} rows (~2-3 min)")
    logger.info(f"  - Player Sessions: {BATCH_SIZE_PLAYER_SESSIONS:,} rows (~1-2 min)")
    logger.info(f"  - Player Stats: {BATCH_SIZE_PLAYER_STATS:,} rows (~5-8 min)")
    
    # Test DB connection
    if not test_db_connection():
        logger.error("No DB connection - exiting")
        return
    
    # Main loop
    try:
        while True:
            sync_data()
            logger.info(f"⏰ Next run in {SYNC_INTERVAL_MINUTES} minutes...")
            time.sleep(SYNC_INTERVAL_MINUTES * 60)
    except KeyboardInterrupt:
        logger.info("\n👋 Terminated by user (Ctrl+C)")


def main_once():
    """One-time export (for cronjobs)"""
    logger.info("🚀 Standalone Sync (one-time)")
    if test_db_connection():
        sync_data()
    else:
        logger.error("No DB connection")
        exit(1)


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1:
        if sys.argv[1] == 'loop':
            main_loop()
        elif sys.argv[1] == 'once':
            main_once()
        elif sys.argv[1] == 'status':
            import pprint
            pprint.pprint(get_export_status())
        elif sys.argv[1] == 'reset':
            reset_export_state()
        elif sys.argv[1] == 'test-db':
            test_db_connection()
        else:
            print("Usage:")
            print("  python external_sync_standalone.py loop      # Continuous execution")
            print("  python external_sync_standalone.py once      # One-time export")
            print("  python external_sync_standalone.py status    # Show status")
            print("  python external_sync_standalone.py reset     # Reset state")
            print("  python external_sync_standalone.py test-db   # Test DB connection")
    else:
        # Default: One-time export
        main_once()

