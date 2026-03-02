"""Google Drive API wrapper for rdrive."""

import os
import json
import time
import logging
from pathlib import Path
from typing import Optional, Dict, List, Any

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# OAuth scopes required for Drive access
SCOPES = ['https://www.googleapis.com/auth/drive']

# Default paths for credentials
CREDENTIALS_DIR = Path.home() / '.rdrive'
TOKEN_PATH = CREDENTIALS_DIR / 'token.json'
CLIENT_SECRETS_PATH = CREDENTIALS_DIR / 'client_secrets.json'


class DriveClient:
    """Google Drive API client with OAuth authentication."""

    # Retry settings
    MAX_RETRIES = 3
    RETRY_DELAY = 1  # seconds, will be exponentially increased

    def __init__(self, continue_on_error: bool = False):
        self.service = None
        self.credentials = None
        self.continue_on_error = continue_on_error
        self.errors: List[Dict] = []  # Track errors during operations

    def authenticate(self) -> bool:
        """
        Authenticate with Google Drive using OAuth.
        Returns True if authentication was successful.
        """
        CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)

        # Check for existing valid credentials
        if TOKEN_PATH.exists():
            self.credentials = Credentials.from_authorized_user_file(
                str(TOKEN_PATH), SCOPES
            )

        # Refresh or get new credentials if needed
        if not self.credentials or not self.credentials.valid:
            if self.credentials and self.credentials.expired and self.credentials.refresh_token:
                try:
                    self.credentials.refresh(Request())
                except RefreshError:
                    print("Token expired or revoked. Re-authenticating...")
                    TOKEN_PATH.unlink(missing_ok=True)
                    self.credentials = None

            if not self.credentials or not self.credentials.valid:
                if not CLIENT_SECRETS_PATH.exists():
                    print(f"Error: Client secrets file not found at {CLIENT_SECRETS_PATH}")
                    print("\nTo set up Google Drive API access:")
                    print("1. Go to https://console.cloud.google.com/")
                    print("2. Create a new project or select existing one")
                    print("3. Enable the Google Drive API")
                    print("4. Create OAuth 2.0 credentials (Desktop application)")
                    print(f"5. Download and save as {CLIENT_SECRETS_PATH}")
                    return False

                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CLIENT_SECRETS_PATH), SCOPES
                )
                self.credentials = flow.run_local_server(port=0)

            # Save credentials for next run
            with open(TOKEN_PATH, 'w') as f:
                f.write(self.credentials.to_json())

        self.service = build('drive', 'v3', credentials=self.credentials)
        return True

    def is_authenticated(self) -> bool:
        """Check if client is authenticated."""
        return self.service is not None

    def list_files(self, folder_id: str, recursive: bool = True,
                    should_skip: callable = None) -> Dict[str, Dict]:
        """
        List files in a folder, optionally recursively.
        Returns dict mapping relative paths to file metadata.

        Args:
            should_skip: Optional callable(path) -> bool to skip directories/files.
        """
        self.errors = []  # Reset errors for this operation
        self.skipped_dirs: List[str] = []  # Track skipped directories
        files = {}
        self._list_files_recursive(folder_id, '', files, recursive, should_skip)
        return files

    def _execute_with_retry(self, request, operation_desc: str = "API call"):
        """Execute a Drive API request with retry logic for transient errors."""
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                return request.execute()
            except HttpError as e:
                last_error = e
                # Retry on server errors (5xx) and rate limiting (403, 429)
                if e.resp.status in (500, 502, 503, 504, 403, 429):
                    delay = self.RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"{operation_desc} failed (attempt {attempt + 1}/{self.MAX_RETRIES}): "
                        f"HTTP {e.resp.status}. Retrying in {delay}s..."
                    )
                    time.sleep(delay)
                else:
                    raise  # Don't retry client errors
        # All retries exhausted
        raise last_error

    def _list_files_recursive(self, folder_id: str, path_prefix: str,
                               files: Dict, recursive: bool,
                               should_skip: callable = None):
        """Recursively list files."""
        query = f"'{folder_id}' in parents and trashed = false"
        page_token = None

        try:
            while True:
                request = self.service.files().list(
                    q=query,
                    spaces='drive',
                    fields='nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime)',
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True
                )
                response = self._execute_with_retry(
                    request, f"Listing folder '{path_prefix or 'root'}'"
                )

                for item in response.get('files', []):
                    name = item['name']
                    rel_path = f"{path_prefix}{name}" if path_prefix else name

                    # Check if this path should be skipped
                    if should_skip and should_skip(rel_path):
                        if item['mimeType'] == 'application/vnd.google-apps.folder':
                            self.skipped_dirs.append(rel_path)
                            logger.info(f"Skipping ignored directory: {rel_path}")
                        continue

                    if item['mimeType'] == 'application/vnd.google-apps.folder':
                        if recursive:
                            self._list_files_recursive(
                                item['id'], f"{rel_path}/", files, recursive, should_skip
                            )
                    else:
                        files[rel_path] = {
                            'id': item['id'],
                            'name': item['name'],
                            'md5': item.get('md5Checksum'),
                            'modified': item.get('modifiedTime'),
                            'mimeType': item['mimeType']
                        }

                page_token = response.get('nextPageToken')
                if not page_token:
                    break

        except HttpError as e:
            error_info = {
                'path': path_prefix or '/',
                'folder_id': folder_id,
                'error': str(e),
                'status': e.resp.status
            }
            self.errors.append(error_info)
            logger.error(f"Error scanning '{path_prefix or '/'}': {e}")

            if not self.continue_on_error:
                raise

    def get_or_create_folder(self, name: str, parent_id: Optional[str] = None) -> str:
        """Get or create a folder by name. Returns folder ID."""
        # Search for existing folder
        query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        else:
            query += " and 'root' in parents"

        response = self.service.files().list(
            q=query,
            spaces='drive',
            fields='files(id, name)',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()

        files = response.get('files', [])
        if files:
            return files[0]['id']

        # Create new folder
        return self.create_folder(name, parent_id)

    def create_folder(self, name: str, parent_id: Optional[str] = None) -> str:
        """Create a folder and return its ID."""
        metadata = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if parent_id:
            metadata['parents'] = [parent_id]

        folder = self.service.files().create(
            body=metadata,
            fields='id',
            supportsAllDrives=True
        ).execute()

        return folder['id']

    def _ensure_parent_folders(self, rel_path: str, root_folder_id: str) -> str:
        """Ensure parent folders exist and return the parent ID for the file."""
        parts = Path(rel_path).parts
        if len(parts) <= 1:
            return root_folder_id

        current_parent = root_folder_id
        for folder_name in parts[:-1]:
            current_parent = self.get_or_create_folder(folder_name, current_parent)

        return current_parent

    def upload_file(self, local_path: Path, rel_path: str,
                    root_folder_id: str, existing_file_id: Optional[str] = None) -> Dict:
        """
        Upload a file to Drive.
        Returns file metadata including id and md5Checksum.
        """
        parent_id = self._ensure_parent_folders(rel_path, root_folder_id)

        metadata = {'name': local_path.name}

        media = MediaFileUpload(str(local_path), resumable=True)

        if existing_file_id:
            # Update existing file
            file = self.service.files().update(
                fileId=existing_file_id,
                media_body=media,
                fields='id, md5Checksum, modifiedTime',
                supportsAllDrives=True
            ).execute()
        else:
            # Create new file
            metadata['parents'] = [parent_id]
            file = self.service.files().create(
                body=metadata,
                media_body=media,
                fields='id, md5Checksum, modifiedTime',
                supportsAllDrives=True
            ).execute()

        return {
            'id': file['id'],
            'md5': file.get('md5Checksum'),
            'modified': file.get('modifiedTime')
        }

    def download_file(self, file_id: str, local_path: Path):
        """Download a file from Drive."""
        local_path.parent.mkdir(parents=True, exist_ok=True)

        request = self.service.files().get_media(fileId=file_id)

        with open(local_path, 'wb') as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()

    def delete_file(self, file_id: str):
        """Delete a file from Drive (moves to trash)."""
        self.service.files().update(
            fileId=file_id,
            body={'trashed': True},
            supportsAllDrives=True
        ).execute()

    def list_folders(self, parent_id: Optional[str] = None) -> List[Dict]:
        """
        List folders in a given parent folder.
        If parent_id is None, lists folders in root (My Drive).
        Returns list of folder metadata dicts with 'id' and 'name'.
        """
        if parent_id:
            query = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        else:
            query = "'root' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"

        return self._query_folders(query)

    def list_shared_folders(self) -> List[Dict]:
        """
        List folders shared with the user.
        Returns list of folder metadata dicts with 'id', 'name', and 'owner'.
        """
        query = "sharedWithMe = true and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        return self._query_folders(query, include_owner=True)

    def _query_folders(self, query: str, include_owner: bool = False) -> List[Dict]:
        """Execute a folder query and return results."""
        folders = []
        page_token = None
        fields = 'nextPageToken, files(id, name)'
        if include_owner:
            fields = 'nextPageToken, files(id, name, owners)'

        while True:
            response = self.service.files().list(
                q=query,
                spaces='drive',
                fields=fields,
                pageToken=page_token,
                orderBy='name',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()

            for item in response.get('files', []):
                folder = {
                    'id': item['id'],
                    'name': item['name']
                }
                if include_owner and item.get('owners'):
                    folder['owner'] = item['owners'][0].get('displayName', 'Unknown')
                folders.append(folder)

            page_token = response.get('nextPageToken')
            if not page_token:
                break

        return folders

    def get_file_metadata(self, file_id: str) -> Optional[Dict]:
        """Get file metadata including MD5 hash."""
        try:
            file = self.service.files().get(
                fileId=file_id,
                fields='id, name, md5Checksum, modifiedTime, mimeType, trashed',
                supportsAllDrives=True
            ).execute()

            if file.get('trashed'):
                return None

            return {
                'id': file['id'],
                'name': file['name'],
                'md5': file.get('md5Checksum'),
                'modified': file.get('modifiedTime'),
                'mimeType': file['mimeType']
            }
        except Exception as e:
            print(f"  Debug: {e}")
            return None
