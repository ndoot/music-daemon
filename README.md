# Music Daemon Setup Guide

### 1. Requirements
- **Docker Desktop**: Ensure it's installed and running.
- **Python 3.10+**: You already have this.
- **VPN**: (AirVPN or ProtonVPN recommended).

### 2. Configuration
Fill out the `.env` file using `.env.example` as a template. You'll need:
- **Telegram Bot Token**: Get this from `@BotFather`.
- **Spotify API Keys**: Get these from the Spotify Developer Dashboard.
- **slskd API Key**: You can find this in the slskd Web UI (`http://localhost:5030`) under Settings after starting the container.

### 3. Usage
1. **Start Docker**:
   ```bash
   docker compose up -d
   ```
2. **Start the Daemon**:
   ```bash
   source .venv/bin/activate
   python daemon.py
   ```
3. **Send Commands**:
   - Just paste a link to your Telegram bot.
   - Or use: `/download [query] playlist:Gym`

### 4. iCloud Sync
The daemon moves safe files to:
`/Users/neeldutta/Library/Mobile Documents/com~apple~CloudDocs/Music`

You can change this in the `.env` file.
