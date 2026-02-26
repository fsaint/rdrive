# rdrive

A command-line tool for bidirectional synchronization between a local directory and Google Drive.

## Features

- **Bidirectional sync** - Upload local changes and download remote changes
- **Conflict detection** - Interactive resolution when both local and remote files change
- **MD5-based change detection** - Efficient tracking of file modifications
- **Smart folder handling** - Automatically creates folder hierarchies on Google Drive
- **Shared Drive support** - Works with both My Drive and Shared Drives

## Prerequisites

- Python 3.6+
- A Google Cloud project with the Drive API enabled

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Google Drive API credentials

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the **Google Drive API**:
   - Navigate to "APIs & Services" > "Library"
   - Search for "Google Drive API" and enable it
4. Create OAuth 2.0 credentials:
   - Go to "APIs & Services" > "Credentials"
   - Click "Create Credentials" > "OAuth client ID"
   - Select "Desktop application" as the application type
   - Download the credentials JSON file
5. Save the credentials file:
   ```bash
   mkdir -p ~/.rdrive
   mv ~/Downloads/client_secret_*.json ~/.rdrive/client_secrets.json
   ```

### 3. Authenticate

```bash
python rdrive.py auth
```

This opens a browser window for OAuth authentication. The token is stored in `~/.rdrive/token.json`.

## Usage

### Commands

| Command | Description |
|---------|-------------|
| `auth` | Authenticate with Google Drive |
| `logout` | Clear stored credentials |
| `list` | List available folders for sync |
| `init <path>` | Initialize a sync directory |
| `sync` | Synchronize files |
| `status` | Check sync status without making changes |

### Example workflow

```bash
# Initialize a directory for syncing
python rdrive.py init ~/my_documents

# Check what will be synced
python rdrive.py status

# Perform synchronization
python rdrive.py sync
```

### Init options

```bash
# Sync to a specific remote folder name
python rdrive.py init ~/my_documents -r "Work Files"

# Sync to an existing Google Drive folder by ID
python rdrive.py init ~/my_documents -i "1abc123def456"
```

## Project Structure

```
rdrive/
├── rdrive.py          # Main CLI application
├── drive_client.py    # Google Drive API wrapper
├── sync_engine.py     # Core synchronization logic
├── sync_state.py      # SQLite state management
└── requirements.txt   # Python dependencies
```

## Ignored Files

The following files are automatically ignored during sync:
- `.rdrive.db` and `.rdrive.db-journal`
- `.DS_Store`
- `.git` directory
- All hidden files (`.*`)

## Uploading to GitHub

### Create a new repository

1. Go to [GitHub](https://github.com) and sign in
2. Click the "+" icon and select "New repository"
3. Name your repository (e.g., `rdrive`)
4. Choose public or private
5. Click "Create repository"

### Push the code

```bash
# Initialize git (if not already done)
git init

# Add all files
git add .

# Create initial commit
git commit -m "Initial commit: Google Drive sync utility"

# Add the remote repository
git remote add origin https://github.com/YOUR_USERNAME/rdrive.git

# Push to GitHub
git push -u origin main
```

### Recommended .gitignore

Create a `.gitignore` file to exclude sensitive and generated files:

```
# Python
__pycache__/
*.py[cod]
*.egg-info/
venv/
.venv/

# rdrive database
.rdrive.db
.rdrive.db-journal

# Credentials (never commit these!)
token.json
client_secrets.json

# OS files
.DS_Store
Thumbs.db
```

## License

MIT License
