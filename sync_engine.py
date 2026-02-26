"""Core sync logic for rdrive."""

import hashlib
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, NamedTuple
from enum import Enum, auto

from sync_state import SyncStateDB, FileState
from drive_client import DriveClient


class Action(Enum):
    """Sync action types."""
    UPLOAD = auto()
    DOWNLOAD = auto()
    DELETE_LOCAL = auto()
    DELETE_REMOTE = auto()
    CONFLICT = auto()
    REMOVE_TRACKING = auto()
    NONE = auto()


class SyncAction(NamedTuple):
    """Represents a sync action to be performed."""
    action: Action
    path: str
    local_path: Optional[Path]
    remote_id: Optional[str]
    local_md5: Optional[str]
    remote_md5: Optional[str]


class SyncEngine:
    """Handles the sync logic between local filesystem and Google Drive."""

    IGNORED_PATTERNS = {'.rdrive.db', '.rdrive.db-journal', '.DS_Store', '.git'}

    def __init__(self, sync_root: Path, db: SyncStateDB, drive: DriveClient):
        self.sync_root = sync_root
        self.db = db
        self.drive = drive

    def _should_ignore(self, path: str) -> bool:
        """Check if a path should be ignored."""
        parts = Path(path).parts
        for part in parts:
            if part in self.IGNORED_PATTERNS or part.startswith('.'):
                return True
        return False

    def compute_md5(self, file_path: Path) -> str:
        """Compute MD5 hash of a local file."""
        hash_md5 = hashlib.md5()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def scan_local(self) -> Dict[str, Dict]:
        """
        Scan local directory for all files.
        Returns dict mapping relative paths to file info.
        """
        files = {}
        for file_path in self.sync_root.rglob('*'):
            if file_path.is_file():
                rel_path = str(file_path.relative_to(self.sync_root))
                if not self._should_ignore(rel_path):
                    files[rel_path] = {
                        'path': file_path,
                        'md5': self.compute_md5(file_path)
                    }
        return files

    def scan_remote(self, folder_id: str) -> Dict[str, Dict]:
        """
        Scan remote Drive folder for all files.
        Returns dict mapping relative paths to file info.
        """
        return self.drive.list_files(folder_id)

    def compute_actions(self, local_files: Dict, remote_files: Dict) -> List[SyncAction]:
        """
        Compare local and remote states to determine sync actions.
        """
        actions = []
        tracked_paths = self.db.get_tracked_paths()
        all_paths = set(local_files.keys()) | set(remote_files.keys()) | tracked_paths

        for path in all_paths:
            if self._should_ignore(path):
                continue

            local_info = local_files.get(path)
            remote_info = remote_files.get(path)
            tracked = self.db.get_state(path)

            action = self._determine_action(path, local_info, remote_info, tracked)
            if action.action != Action.NONE:
                actions.append(action)

        return actions

    def _determine_action(self, path: str, local_info: Optional[Dict],
                          remote_info: Optional[Dict],
                          tracked: Optional[FileState]) -> SyncAction:
        """Determine the sync action for a single file."""
        local_exists = local_info is not None
        remote_exists = remote_info is not None
        was_tracked = tracked is not None

        local_md5 = local_info['md5'] if local_info else None
        remote_md5 = remote_info['md5'] if remote_info else None
        local_path = local_info['path'] if local_info else self.sync_root / path
        remote_id = remote_info['id'] if remote_info else (tracked.remote_id if tracked else None)

        # New file scenarios
        if not was_tracked:
            if local_exists and not remote_exists:
                # New local file -> upload
                return SyncAction(Action.UPLOAD, path, local_path, None, local_md5, None)
            elif remote_exists and not local_exists:
                # New remote file -> download
                return SyncAction(Action.DOWNLOAD, path, local_path, remote_id, None, remote_md5)
            elif local_exists and remote_exists:
                # Both exist but not tracked - check if same
                if local_md5 == remote_md5:
                    # Same content, just track it
                    return SyncAction(Action.NONE, path, local_path, remote_id, local_md5, remote_md5)
                else:
                    # Different content - conflict
                    return SyncAction(Action.CONFLICT, path, local_path, remote_id, local_md5, remote_md5)
            else:
                return SyncAction(Action.NONE, path, local_path, remote_id, local_md5, remote_md5)

        # File was previously tracked
        local_changed = local_md5 != tracked.local_md5 if local_exists else False
        remote_changed = remote_md5 != tracked.remote_md5 if remote_exists else False

        # Deletion scenarios
        if not local_exists and not remote_exists:
            # Both deleted -> remove tracking
            return SyncAction(Action.REMOVE_TRACKING, path, local_path, remote_id, None, None)
        elif not local_exists and remote_exists:
            if remote_changed:
                # Local deleted but remote changed -> download (remote wins)
                return SyncAction(Action.DOWNLOAD, path, local_path, remote_id, None, remote_md5)
            else:
                # Local deleted, remote unchanged -> delete remote
                return SyncAction(Action.DELETE_REMOTE, path, local_path, remote_id, None, remote_md5)
        elif local_exists and not remote_exists:
            if local_changed:
                # Remote deleted but local changed -> upload (local wins)
                return SyncAction(Action.UPLOAD, path, local_path, None, local_md5, None)
            else:
                # Remote deleted, local unchanged -> delete local
                return SyncAction(Action.DELETE_LOCAL, path, local_path, remote_id, local_md5, None)

        # Both exist - check for changes
        if local_changed and remote_changed:
            if local_md5 == remote_md5:
                # Same change on both sides
                return SyncAction(Action.NONE, path, local_path, remote_id, local_md5, remote_md5)
            else:
                # Conflict!
                return SyncAction(Action.CONFLICT, path, local_path, remote_id, local_md5, remote_md5)
        elif local_changed:
            # Only local changed -> upload
            return SyncAction(Action.UPLOAD, path, local_path, remote_id, local_md5, remote_md5)
        elif remote_changed:
            # Only remote changed -> download
            return SyncAction(Action.DOWNLOAD, path, local_path, remote_id, local_md5, remote_md5)
        else:
            # No changes
            return SyncAction(Action.NONE, path, local_path, remote_id, local_md5, remote_md5)

    def resolve_conflict(self, action: SyncAction) -> SyncAction:
        """Prompt user to resolve a conflict."""
        print(f"\nConflict detected for: {action.path}")
        print(f"  Local MD5:  {action.local_md5}")
        print(f"  Remote MD5: {action.remote_md5}")
        print("\nOptions:")
        print("  [l] Keep local version (upload)")
        print("  [r] Keep remote version (download)")
        print("  [s] Skip this file")

        while True:
            choice = input("Your choice [l/r/s]: ").strip().lower()
            if choice == 'l':
                return SyncAction(
                    Action.UPLOAD, action.path, action.local_path,
                    action.remote_id, action.local_md5, action.remote_md5
                )
            elif choice == 'r':
                return SyncAction(
                    Action.DOWNLOAD, action.path, action.local_path,
                    action.remote_id, action.local_md5, action.remote_md5
                )
            elif choice == 's':
                return SyncAction(
                    Action.NONE, action.path, action.local_path,
                    action.remote_id, action.local_md5, action.remote_md5
                )
            else:
                print("Invalid choice. Please enter 'l', 'r', or 's'.")

    def execute_action(self, action: SyncAction, root_folder_id: str) -> bool:
        """Execute a single sync action. Returns True if successful."""
        try:
            if action.action == Action.UPLOAD:
                print(f"  Uploading: {action.path}")
                result = self.drive.upload_file(
                    action.local_path, action.path,
                    root_folder_id, action.remote_id
                )
                self.db.set_state(
                    action.path, action.local_md5, result['md5'],
                    result['id'], int(time.time())
                )

            elif action.action == Action.DOWNLOAD:
                print(f"  Downloading: {action.path}")
                self.drive.download_file(action.remote_id, action.local_path)
                local_md5 = self.compute_md5(action.local_path)
                self.db.set_state(
                    action.path, local_md5, action.remote_md5,
                    action.remote_id, int(time.time())
                )

            elif action.action == Action.DELETE_LOCAL:
                print(f"  Deleting local: {action.path}")
                if action.local_path.exists():
                    action.local_path.unlink()
                    # Clean up empty parent directories
                    parent = action.local_path.parent
                    while parent != self.sync_root:
                        if not any(parent.iterdir()):
                            parent.rmdir()
                            parent = parent.parent
                        else:
                            break
                self.db.remove_state(action.path)

            elif action.action == Action.DELETE_REMOTE:
                print(f"  Deleting remote: {action.path}")
                if action.remote_id:
                    self.drive.delete_file(action.remote_id)
                self.db.remove_state(action.path)

            elif action.action == Action.REMOVE_TRACKING:
                print(f"  Removing tracking: {action.path}")
                self.db.remove_state(action.path)

            return True

        except Exception as e:
            print(f"  Error processing {action.path}: {e}")
            return False

    def execute_sync(self, actions: List[SyncAction], root_folder_id: str) -> Tuple[int, int]:
        """
        Execute all sync actions.
        Returns (success_count, error_count).
        """
        # First, resolve all conflicts
        resolved_actions = []
        for action in actions:
            if action.action == Action.CONFLICT:
                resolved = self.resolve_conflict(action)
                if resolved.action != Action.NONE:
                    resolved_actions.append(resolved)
            elif action.action != Action.NONE:
                resolved_actions.append(action)

        if not resolved_actions:
            print("Nothing to sync.")
            return 0, 0

        print(f"\nExecuting {len(resolved_actions)} sync actions...")
        success = 0
        errors = 0

        for action in resolved_actions:
            if self.execute_action(action, root_folder_id):
                success += 1
            else:
                errors += 1

        return success, errors

    def get_status(self, local_files: Dict, remote_files: Dict) -> Dict[str, List[str]]:
        """Get status of pending changes without syncing."""
        actions = self.compute_actions(local_files, remote_files)

        status = {
            'upload': [],
            'download': [],
            'delete_local': [],
            'delete_remote': [],
            'conflict': []
        }

        for action in actions:
            if action.action == Action.UPLOAD:
                status['upload'].append(action.path)
            elif action.action == Action.DOWNLOAD:
                status['download'].append(action.path)
            elif action.action == Action.DELETE_LOCAL:
                status['delete_local'].append(action.path)
            elif action.action == Action.DELETE_REMOTE:
                status['delete_remote'].append(action.path)
            elif action.action == Action.CONFLICT:
                status['conflict'].append(action.path)

        return status
