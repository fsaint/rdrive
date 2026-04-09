#!/usr/bin/env python3
"""
rdrive - Google Drive sync utility

A command-line tool for bidirectional synchronization between
a local directory and Google Drive.
"""

import argparse
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from drive_client import DriveClient

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(levelname)s: %(message)s'
)
from sync_state import SyncStateDB
from sync_engine import SyncEngine


DB_NAME = '.rdrive.db'


def find_sync_root() -> Path:
    """Find the sync root by looking for .rdrive.db in current or parent directories."""
    current = Path.cwd()
    while current != current.parent:
        if (current / DB_NAME).exists():
            return current
        current = current.parent
    return None


def cmd_auth(args):
    """Authenticate with Google Drive."""
    print("Authenticating with Google Drive...")
    drive = DriveClient()
    if drive.authenticate():
        print("Authentication successful!")
        return 0
    else:
        print("Authentication failed.")
        return 1


def cmd_logout(args):
    """Clear stored authentication credentials."""
    token_path = Path.home() / '.rdrive' / 'token.json'
    if token_path.exists():
        token_path.unlink()
        print("Logged out successfully. Credentials removed.")
    else:
        print("No credentials found.")
    return 0


def cmd_init(args):
    """Initialize a new sync directory."""
    local_path = Path(args.path).resolve()

    if not local_path.exists():
        print(f"Creating directory: {local_path}")
        local_path.mkdir(parents=True)
    elif not local_path.is_dir():
        print(f"Error: {local_path} is not a directory")
        return 1

    db_path = local_path / DB_NAME
    if db_path.exists():
        print(f"Error: {local_path} is already initialized for sync")
        return 1

    # Authenticate
    drive = DriveClient()
    if not drive.authenticate():
        return 1

    # Get or create remote folder
    if args.folder_id:
        # Use provided folder ID (for shared folders)
        folder_id = args.folder_id
        print(f"Using provided folder ID: {folder_id}")
        # Verify the folder is accessible
        try:
            metadata = drive.get_file_metadata(folder_id)
            if not metadata:
                print(f"Error: Cannot access folder with ID {folder_id}")
                return 1
            folder_name = metadata['name']
            print(f"Found folder: {folder_name}")
        except Exception as e:
            print(f"Error accessing folder: {e}")
            return 1
    else:
        folder_name = args.remote_folder or local_path.name
        print(f"Setting up remote folder: {folder_name}")
        try:
            folder_id = drive.get_or_create_folder(folder_name)
            print(f"Remote folder ID: {folder_id}")
        except Exception as e:
            print(f"Error creating remote folder: {e}")
            return 1

    # Initialize database
    db = SyncStateDB(db_path)
    db.set_remote_folder_id(folder_id)
    db.close()

    print(f"\nSync initialized successfully!")
    print(f"  Local path: {local_path}")
    print(f"  Remote folder: {folder_name}")
    print(f"\nRun 'rdrive sync' to synchronize files.")
    return 0


def cmd_sync(args):
    """Perform synchronization."""
    # Enable verbose logging if requested
    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    sync_root = find_sync_root()
    if not sync_root:
        print("Error: Not in a sync directory. Run 'rdrive init' first.")
        return 1

    db_path = sync_root / DB_NAME
    db = SyncStateDB(db_path)

    folder_id = db.get_remote_folder_id()
    if not folder_id:
        print("Error: No remote folder configured. Run 'rdrive init' first.")
        db.close()
        return 1

    # Authenticate
    drive = DriveClient(continue_on_error=args.continue_on_error)
    if not drive.authenticate():
        db.close()
        return 1

    engine = SyncEngine(sync_root, db, drive)

    print("Scanning local files...")
    local_files = engine.scan_local()
    print(f"  Found {len(local_files)} local files")

    print("Scanning remote files...")
    remote_files = engine.scan_remote(folder_id)
    print(f"  Found {len(remote_files)} remote files")

    # Report skipped directories
    if drive.skipped_dirs:
        print(f"  Skipped {len(drive.skipped_dirs)} ignored directories")
        if args.verbose:
            for d in drive.skipped_dirs[:10]:  # Show first 10
                print(f"    - {d}/")
            if len(drive.skipped_dirs) > 10:
                print(f"    ... and {len(drive.skipped_dirs) - 10} more")

    # Report any errors during remote scanning
    if drive.errors:
        print(f"  Warning: {len(drive.errors)} folder(s) could not be scanned:")
        for error in drive.errors:
            print(f"    - {error['path']} (HTTP {error['status']})")
        if not args.continue_on_error:
            print("  Use --continue-on-error to skip problematic folders.")

    print("Scanning directories...")
    local_dirs = engine.scan_local_dirs()
    remote_dirs = set(drive.found_dirs)  # Collected during scan_remote above
    dir_actions = engine.compute_dir_actions(local_dirs, remote_dirs)

    print("Computing sync actions...")
    actions = engine.compute_actions(local_files, remote_files)

    if not actions and not dir_actions:
        print("Everything is in sync!")
        db.close()
        return 0

    # Prepend directory creation actions so folders exist before file uploads
    actions = dir_actions + actions

    # Summarize actions
    from sync_engine import Action
    create_remote_dir_count = sum(1 for a in actions if a.action == Action.CREATE_REMOTE_DIR)
    create_local_dir_count = sum(1 for a in actions if a.action == Action.CREATE_LOCAL_DIR)
    upload_count = sum(1 for a in actions if a.action == Action.UPLOAD)
    download_count = sum(1 for a in actions if a.action == Action.DOWNLOAD)
    delete_local_count = sum(1 for a in actions if a.action == Action.DELETE_LOCAL)
    delete_remote_count = sum(1 for a in actions if a.action == Action.DELETE_REMOTE)
    conflict_count = sum(1 for a in actions if a.action == Action.CONFLICT)

    print(f"\nPending actions:")
    if create_local_dir_count:
        print(f"  Create local dirs: {create_local_dir_count}")
    if create_remote_dir_count:
        print(f"  Create remote dirs: {create_remote_dir_count}")
    if upload_count:
        print(f"  Upload: {upload_count} files")
    if download_count:
        print(f"  Download: {download_count} files")
    if delete_local_count:
        print(f"  Delete local: {delete_local_count} files")
    if delete_remote_count:
        print(f"  Delete remote: {delete_remote_count} files")
    if conflict_count:
        print(f"  Conflicts: {conflict_count} files")

    # Dry run mode - show detailed changes without executing
    if args.dry_run:
        print("\n[Dry run] The following changes would be made:\n")
        for a in actions:
            if a.action == Action.CREATE_LOCAL_DIR:
                print(f"  + CREATE LOCAL DIR:  {a.path}/")
            elif a.action == Action.CREATE_REMOTE_DIR:
                print(f"  + CREATE REMOTE DIR: {a.path}/")
            elif a.action == Action.UPLOAD:
                print(f"  + UPLOAD:        {a.path}")
            elif a.action == Action.DOWNLOAD:
                print(f"  ↓ DOWNLOAD:      {a.path}")
            elif a.action == Action.DELETE_LOCAL:
                print(f"  - DELETE LOCAL:  {a.path}")
            elif a.action == Action.DELETE_REMOTE:
                print(f"  × DELETE REMOTE: {a.path}")
            elif a.action == Action.CONFLICT:
                print(f"  ! CONFLICT:      {a.path}")
        print("\nNo changes made (dry run).")
        db.close()
        return 0

    # Execute sync
    success, errors = engine.execute_sync(actions, folder_id)

    db.close()

    print(f"\nSync complete: {success} succeeded, {errors} failed")
    return 0 if errors == 0 else 1


def cmd_list(args):
    """List top-level folders available for sync."""
    drive = DriveClient()
    if not drive.authenticate():
        return 1

    print("Fetching folders from Google Drive...")
    my_folders = drive.list_folders()
    shared_folders = drive.list_shared_folders()

    if not my_folders and not shared_folders:
        print("\nNo folders found.")
        print("You can create a new sync with: rdrive init <local-path>")
        return 0

    if my_folders:
        print(f"\n=== My Drive ({len(my_folders)} folder(s)) ===\n")
        print(f"{'NAME':<40} {'FOLDER ID':<44} SYNC COMMAND")
        print("-" * 120)

        for folder in my_folders:
            name = folder['name']
            folder_id = folder['id']
            display_name = name[:37] + "..." if len(name) > 40 else name
            cmd = f"rdrive init <local-path> -i {folder_id}"
            print(f"{display_name:<40} {folder_id:<44} {cmd}")

    if shared_folders:
        print(f"\n=== Shared with me ({len(shared_folders)} folder(s)) ===\n")
        print(f"{'NAME':<30} {'OWNER':<20} {'FOLDER ID':<44} SYNC COMMAND")
        print("-" * 140)

        for folder in shared_folders:
            name = folder['name']
            folder_id = folder['id']
            owner = folder.get('owner', 'Unknown')
            display_name = name[:27] + "..." if len(name) > 30 else name
            display_owner = owner[:17] + "..." if len(owner) > 20 else owner
            cmd = f"rdrive init <local-path> -i {folder_id}"
            print(f"{display_name:<30} {display_owner:<20} {folder_id:<44} {cmd}")

    print(f"\nTo sync an existing folder, run the sync command shown above.")
    print(f"To create a new folder and sync, run: rdrive init <local-path>")
    return 0


def parse_duration(duration_str: str) -> int:
    """
    Parse a duration string like '3d', '24h', '5m' into seconds.
    Supported units: d (days), h (hours), m (minutes), s (seconds).
    """
    match = re.match(r'^(\d+)([dhms])$', duration_str.lower())
    if not match:
        raise ValueError(
            f"Invalid duration format: '{duration_str}'. "
            "Use format like '3d', '24h', '30m', or '60s'."
        )

    value = int(match.group(1))
    unit = match.group(2)

    multipliers = {
        's': 1,
        'm': 60,
        'h': 3600,
        'd': 86400,
    }

    return value * multipliers[unit]


def cmd_recent(args):
    """Show files changed within a time period."""
    sync_root = find_sync_root()
    if not sync_root:
        print("Error: Not in a sync directory. Run 'rdrive init' first.")
        return 1

    try:
        duration_seconds = parse_duration(args.period)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    db_path = sync_root / DB_NAME
    db = SyncStateDB(db_path)

    since_timestamp = int(time.time()) - duration_seconds
    recent_files = db.get_files_since(since_timestamp)
    db.close()

    if not recent_files:
        print(f"No files synced in the last {args.period}.")
        return 0

    print(f"Files synced in the last {args.period}:\n")
    for f in recent_files:
        sync_time = datetime.fromtimestamp(f.last_sync).strftime('%Y-%m-%d %H:%M:%S')
        print(f"  {sync_time}  {f.path}")

    print(f"\nTotal: {len(recent_files)} file(s)")
    return 0


def cmd_status(args):
    """Show sync status without making changes."""
    # Enable verbose logging if requested
    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    sync_root = find_sync_root()
    if not sync_root:
        print("Error: Not in a sync directory. Run 'rdrive init' first.")
        return 1

    db_path = sync_root / DB_NAME
    db = SyncStateDB(db_path)

    folder_id = db.get_remote_folder_id()
    if not folder_id:
        print("Error: No remote folder configured. Run 'rdrive init' first.")
        db.close()
        return 1

    # Authenticate
    drive = DriveClient(continue_on_error=args.continue_on_error)
    if not drive.authenticate():
        db.close()
        return 1

    engine = SyncEngine(sync_root, db, drive)

    print("Scanning local files...")
    local_files = engine.scan_local()

    print("Scanning remote files...")
    remote_files = engine.scan_remote(folder_id)
    print(f"  Found {len(remote_files)} remote files")

    # Report skipped directories
    if drive.skipped_dirs:
        print(f"  Skipped {len(drive.skipped_dirs)} ignored directories")

    # Report any errors during remote scanning
    if drive.errors:
        print(f"  Warning: {len(drive.errors)} folder(s) could not be scanned:")
        for error in drive.errors:
            print(f"    - {error['path']} (HTTP {error['status']})")

    status = engine.get_status(local_files, remote_files)
    db.close()

    has_changes = False

    if status['upload']:
        has_changes = True
        print("\nFiles to upload:")
        for path in status['upload']:
            print(f"  + {path}")

    if status['download']:
        has_changes = True
        print("\nFiles to download:")
        for path in status['download']:
            print(f"  ↓ {path}")

    if status['delete_local']:
        has_changes = True
        print("\nFiles to delete locally:")
        for path in status['delete_local']:
            print(f"  - {path}")

    if status['delete_remote']:
        has_changes = True
        print("\nFiles to delete remotely:")
        for path in status['delete_remote']:
            print(f"  × {path}")

    if status['conflict']:
        has_changes = True
        print("\nConflicts (requires manual resolution):")
        for path in status['conflict']:
            print(f"  ! {path}")

    if not has_changes:
        print("\nEverything is in sync!")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description='rdrive - Google Drive sync utility',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # auth command
    auth_parser = subparsers.add_parser('auth', help='Authenticate with Google Drive')
    auth_parser.set_defaults(func=cmd_auth)

    # logout command
    logout_parser = subparsers.add_parser('logout', help='Clear stored credentials')
    logout_parser.set_defaults(func=cmd_logout)

    # list command
    list_parser = subparsers.add_parser('list', help='List top-level folders available for sync')
    list_parser.set_defaults(func=cmd_list)

    # init command
    init_parser = subparsers.add_parser('init', help='Initialize sync directory')
    init_parser.add_argument('path', help='Local directory path to sync')
    init_parser.add_argument(
        '--remote-folder', '-r',
        help='Name of remote folder (defaults to directory name)'
    )
    init_parser.add_argument(
        '--folder-id', '-i',
        help='Google Drive folder ID (use for shared folders; get from folder URL)'
    )
    init_parser.set_defaults(func=cmd_init)

    # sync command
    sync_parser = subparsers.add_parser('sync', help='Synchronize files')
    sync_parser.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='Show what would be synced without making any changes'
    )
    sync_parser.add_argument(
        '--continue-on-error', '-c',
        action='store_true',
        help='Continue scanning even if some folders fail (e.g., API errors)'
    )
    sync_parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed progress including retry attempts'
    )
    sync_parser.set_defaults(func=cmd_sync)

    # status command
    status_parser = subparsers.add_parser('status', help='Show sync status')
    status_parser.add_argument(
        '--continue-on-error', '-c',
        action='store_true',
        help='Continue scanning even if some folders fail (e.g., API errors)'
    )
    status_parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed progress including retry attempts'
    )
    status_parser.set_defaults(func=cmd_status)

    # recent command
    recent_parser = subparsers.add_parser(
        'recent',
        help='Show files changed within a time period'
    )
    recent_parser.add_argument(
        'period',
        help="Time period (e.g., '3d' for 3 days, '24h' for 24 hours, '30m' for 30 minutes)"
    )
    recent_parser.set_defaults(func=cmd_recent)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
