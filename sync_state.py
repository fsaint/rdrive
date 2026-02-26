"""SQLite-based sync state tracking for rdrive."""

import sqlite3
from pathlib import Path
from typing import Optional, Dict, List, NamedTuple


class FileState(NamedTuple):
    """Represents the sync state of a file."""
    path: str
    local_md5: Optional[str]
    remote_md5: Optional[str]
    remote_id: Optional[str]
    last_sync: int


class SyncStateDB:
    """Manages the SQLite database for tracking sync state."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self._init_schema()

    def _init_schema(self):
        """Initialize the database schema."""
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                path TEXT PRIMARY KEY,
                local_md5 TEXT,
                remote_md5 TEXT,
                remote_id TEXT,
                last_sync INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    def get_state(self, path: str) -> Optional[FileState]:
        """Get the sync state for a file path."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT path, local_md5, remote_md5, remote_id, last_sync "
            "FROM sync_state WHERE path = ?",
            (path,)
        )
        row = cursor.fetchone()
        if row:
            return FileState(*row)
        return None

    def set_state(self, path: str, local_md5: Optional[str],
                  remote_md5: Optional[str], remote_id: Optional[str],
                  last_sync: int):
        """Set or update the sync state for a file."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO sync_state
            (path, local_md5, remote_md5, remote_id, last_sync)
            VALUES (?, ?, ?, ?, ?)
        """, (path, local_md5, remote_md5, remote_id, last_sync))
        self.conn.commit()

    def remove_state(self, path: str):
        """Remove tracking for a file."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM sync_state WHERE path = ?", (path,))
        self.conn.commit()

    def get_all_tracked(self) -> List[FileState]:
        """Get all tracked files."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT path, local_md5, remote_md5, remote_id, last_sync "
            "FROM sync_state"
        )
        return [FileState(*row) for row in cursor.fetchall()]

    def get_tracked_paths(self) -> set:
        """Get set of all tracked file paths."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT path FROM sync_state")
        return {row[0] for row in cursor.fetchall()}

    def get_files_since(self, since_timestamp: int) -> List[FileState]:
        """Get all files synced since the given timestamp."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT path, local_md5, remote_md5, remote_id, last_sync "
            "FROM sync_state WHERE last_sync >= ? ORDER BY last_sync DESC",
            (since_timestamp,)
        )
        return [FileState(*row) for row in cursor.fetchall()]

    def get_config(self, key: str) -> Optional[str]:
        """Get a config value."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None

    def set_config(self, key: str, value: str):
        """Set a config value."""
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value)
        )
        self.conn.commit()

    def get_remote_folder_id(self) -> Optional[str]:
        """Get the remote folder ID for this sync root."""
        return self.get_config('remote_folder_id')

    def set_remote_folder_id(self, folder_id: str):
        """Set the remote folder ID for this sync root."""
        self.set_config('remote_folder_id', folder_id)

    def close(self):
        """Close the database connection."""
        self.conn.close()
