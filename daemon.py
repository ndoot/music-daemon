import os
import time
import shutil
import logging
import asyncio
import httpx
import json
import html
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import spotipy
import re
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

# Globals for interactive state
PENDING_DOWNLOADS = {}
TRANSFER_MAP = {} # {short_id: (peer, filename)}
MESSAGE_MAP = {} # {filename: (chat_id, message_id, playlist_name)}
ACTIVE_HUB = None
SYNC_METADATA_FILE = "sync_metadata.json"

# Load environment variables
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ALLOWED_USER_ID = os.getenv('TELEGRAM_ALLOWED_USER_ID')
SLSKD_URL = os.getenv('SLSKD_URL', 'http://localhost:5030')
SLSKD_API_KEY = os.getenv('SLSKD_API_KEY')
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', './downloads')
ICLOUD_MUSIC_DIR = os.getenv('ICLOUD_MUSIC_DIR')

# Spotify Sync Globals
SPOTIPY_CLIENT_ID = (os.getenv('SPOTIPY_CLIENT_ID') or os.getenv('SPOTIFY_CLIENT_ID', '')).strip()
SPOTIPY_CLIENT_SECRET = (os.getenv('SPOTIPY_CLIENT_SECRET') or os.getenv('SPOTIFY_CLIENT_SECRET', '')).strip()
SPOTIFY_USER_ID = os.getenv("SPOTIFY_USER_ID", "").strip()
SPOTIFY_PLAYLIST_IDS = os.getenv("SPOTIFY_PLAYLIST_IDS", "").split(",")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "https://github.com/ndoot/music-daemon").strip()
SPOTIFY_QUALITY_PREFERENCE = os.getenv("SPOTIFY_QUALITY_PREFERENCE", "High").capitalize()
SYNC_HISTORY_FILE = "sync_history.json"

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- Logic Components ---

def load_history():
    """Loads sync history (Spotify IDs), snapshots, and watched playlists."""
    default = {"history": set(), "snapshots": {}, "watched": set()}
    if os.path.exists(SYNC_METADATA_FILE):
        try:
            with open(SYNC_METADATA_FILE, 'r') as f:
                data = json.load(f)
                return {
                    "history": set(data.get("history", [])),
                    "snapshots": data.get("snapshots", {}),
                    "watched": set(data.get("watched", []))
                }
        except Exception as e:
            logging.error(f"Error loading metadata: {e}")
    return default

def save_history(metadata):
    """Saves sync history, snapshots, and watched playlists."""
    data = {
        "history": list(metadata["history"]),
        "snapshots": metadata["snapshots"],
        "watched": list(metadata["watched"])
    }
    try:
        with open(SYNC_METADATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving metadata: {e}")

def file_guard(source_dir, targets, subfolder=None, target_file=None):
    """Moves/Copies only safe audio files from source to multiple targets with optional subfolder."""
    SAFE_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg'}
    moved_files = []
    
    if not os.path.exists(source_dir):
        return []

    for target_dir in targets:
        full_target = os.path.join(target_dir, subfolder) if subfolder else target_dir
        if not os.path.exists(full_target):
            os.makedirs(full_target, exist_ok=True)
    
    for root, dirs, files in os.walk(source_dir):
        # Prevent walking into slskd's incomplete folder
        if "incomplete" in dirs:
            dirs.remove("incomplete")
            
        for file in files:
            if target_file and file != target_file:
                continue
            
            ext = os.path.splitext(file)[1].lower()
            if ext in SAFE_EXTENSIONS:
                source_path = os.path.join(root, file)
                # Quick stability check to ensure slskd has finished writing
                try:
                    s1 = os.path.getsize(source_path)
                    time.sleep(1)
                    s2 = os.path.getsize(source_path)
                    if s1 != s2 or s1 == 0:
                        continue # File still growing or empty
                except:
                    continue

                # Resolve subfolder (playlist name) from MESSAGE_MAP
                final_subfolder = subfolder
                if not final_subfolder and file in MESSAGE_MAP:
                    final_subfolder = MESSAGE_MAP[file][2]
                
                for i, target_dir in enumerate(targets):
                    dest_dir = os.path.join(target_dir, final_subfolder) if final_subfolder else target_dir
                    os.makedirs(dest_dir, exist_ok=True)
                    target_path = os.path.join(dest_dir, file)
                    
                    if i < len(targets) - 1:
                        shutil.copy2(source_path, target_path)
                    else:
                        shutil.move(source_path, target_path)
                moved_files.append(file)
            else:
                if file not in [".DS_Store"]:
                    logging.warning(f"Ignoring unsafe file: {file}")
    return moved_files

async def search_and_download(client, query, playlist_name=None, application=None, update=None, automatic=False, sync_hub=None, track_idx=None):
    """Searches slskd and triggers a download. Falls back to yt-dlp."""
    if not SLSKD_API_KEY:
        logging.error("SLSKD_API_KEY not set")
        return
    headers = {'X-API-Key': SLSKD_API_KEY}
    
    # 1. Start Search
    try:
        logging.info(f"TRACE: POST {SLSKD_URL}/api/v0/searches | Data: {{'searchText': {query}}}")
        search_req = await client.post(f"{SLSKD_URL}/api/v0/searches", json={'searchText': query})
        search_req.raise_for_status()
        search_id = search_req.json()['id']
    except Exception as e:
        logging.error(f"Failed to start search: {e}")
        return "ERROR", f"API Error: {e}"

    anim_task = None
    msg = None
    if update and update.message:
        msg = await update.message.reply_text(f"🔍 Searching for: <b>{html.escape(query)}</b>...", parse_mode='HTML')

    try:
        for sec in range(60):
            if sync_hub:
                sync_hub.current_track_sec = sec
                await sync_hub.render()
            
            if application and msg:
                progress = int((sec / 60) * 100)
                filled = int(progress // 10)
                bar = "⣿" * filled + "⣀" * (10 - filled)
                status_text = f"🔍 Searching for: <b>{html.escape(query)}</b>...\n[{bar}] {progress}%"
                
                # Edit every second for smooth ticking
                try: await application.bot.edit_message_text(status_text, chat_id=msg.chat_id, message_id=msg.message_id, parse_mode='HTML')
                except: pass
                
                if sec % 5 == 0: # Action every 5s
                    await application.bot.send_chat_action(chat_id=msg.chat_id, action="typing")
            
            # Poll Slskd every 10 seconds
            if sec % 10 == 0:
                logging.info(f"TRACE: Polling Slskd (Second {sec})")
                try:
                    results_req = await client.get(f"{SLSKD_URL}/api/v0/searches/{search_id}/responses")
                    responses = results_req.json()
                    
                    if responses:
                        candidates = {"flac": None, "wav": None, "high": None, "efficiency": None}
                        for resp in responses:
                            peer = resp.get('username')
                            for file in resp.get('files', []):
                                f_lower = file['filename'].lower()
                                if any(f_lower.endswith(ext) for ext in ['.mp3', '.flac', '.m4a', '.wav', '.ogg']):
                                    bitrate = file.get('bitRate', 0)
                                    if '.wav' in f_lower and not candidates["wav"]:
                                        candidates["wav"] = {'file': file, 'peer': peer, 'type': 'WAV', 'bitrate': bitrate}
                                    elif '.flac' in f_lower and not candidates["flac"]:
                                        candidates["flac"] = {'file': file, 'peer': peer, 'type': 'FLAC', 'bitrate': bitrate}
                                    elif ('.mp3' in f_lower and (bitrate >= 256 or '320' in f_lower)) and not candidates["high"]:
                                        candidates["high"] = {'file': file, 'peer': peer, 'type': 'High', 'bitrate': bitrate}
                                    elif '.mp3' in f_lower and not candidates["efficiency"]:
                                        candidates["efficiency"] = {'file': file, 'peer': peer, 'type': 'Efficiency', 'bitrate': bitrate}

                        options = [candidates[k] for k in ["flac", "wav", "high", "efficiency"] if candidates[k]]
                        if options:
                            if automatic:
                                # Pick based on preference (High, FLAC, etc)
                                selection = candidates.get(SPOTIFY_QUALITY_PREFERENCE.lower()) or options[0]
                                if sync_hub: 
                                    sync_hub.current_track_sec = sec
                                    await sync_hub.update_track(track_idx, "found")
                                elif msg:
                                    try: await msg.edit_text(f"🎯 <b>Found:</b> <code>{html.escape(query)}</code>\n🚀 Preparing download...")
                                    except: pass
                                
                                await asyncio.sleep(1) # Give user a moment to see finding
                                
                                success = await start_slskd_download(
                                    client,
                                    selection['peer'], 
                                    selection['file'], 
                                    application,
                                    update, 
                                    existing_msg_id=msg.message_id if msg else None,
                                    playlist_name=playlist_name,
                                    chat_id=sync_hub.chat_id if sync_hub else None,
                                    sync_hub=sync_hub,
                                    track_idx=track_idx
                                )
                                return "SUCCESS" if success else "ERROR", "Auto-download triggered"
                            
                            return "MENU", {'options': options, 'playlist': playlist_name, 'msg': msg}
                except Exception as e:
                    logging.error(f"Polling error at sec {sec}: {e}")

            await asyncio.sleep(1)

        if msg:
            try: await msg.edit_text("❌ No results found after 60s.")
            except: pass
        return "ERROR", "No results found after 60s."
    finally:
        if anim_task and not anim_task.done():
            anim_task.cancel()

async def start_slskd_download(client, peer, file, application, update=None, existing_msg_id=None, playlist_name=None, chat_id=None, sync_hub=None, track_idx=None):
    """Triggers the download and transitions the UI bubble."""
    if not SLSKD_API_KEY: return False
    headers = {'X-API-Key': SLSKD_API_KEY}
    
    filename = file['filename']
    clean_name = filename.replace('\\', '/').split('/')[-1]
    
    if not chat_id and update and update.effective_chat:
        chat_id = update.effective_chat.id
    
    try:
        # 1. Show "Found" state first
        found_text = (
            f"<b>🎯 Track Found!</b>\n"
            f"🎵 <code>{clean_name}</code>\n"
            f"👤 Peer: <code>{html.escape(peer)}</code>\n"
            f"💿 Quality: <code>{file.get('bitrate', '?')}kbps / {file.get('extension', 'audio')}</code>"
        )
        if existing_msg_id:
            msg = await application.bot.edit_message_text(chat_id=chat_id, message_id=existing_msg_id, text=found_text, parse_mode='HTML')
        elif update and update.message:
            msg = await update.message.reply_text(found_text, parse_mode='HTML')
        
        await asyncio.sleep(1.5) # Give user a moment to see finding

        # 2. Trigger Slskd
        payload = [{'filename': filename, 'size': file['size']}]
        logging.info(f"TRACE: Downloading {clean_name} from {peer}")
        d_req = await client.post(f"{SLSKD_URL}/api/v0/transfers/downloads/{peer}", json=payload)
        d_req.raise_for_status()

        # 2. UI Transition (Skip if no chat/update)
        if not chat_id: return True
        
        msg_text = (
            f"<b>📥 Starting Download...</b>\n"
            f"🎵 <code>{html.escape(clean_name)}</code>\n"
            f"👤 Peer: <code>{html.escape(peer)}</code>"
        )
        
        # 3. Store in MESSAGE_MAP for file_guard to pick up later
        if playlist_name:
            MESSAGE_MAP[clean_name] = (chat_id, existing_msg_id, playlist_name)
            logging.info(f"DEBUG: Mapped {clean_name} to playlist {playlist_name}")

        msg_id = existing_msg_id
        if msg_id:
            try:
                await application.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=msg_text, parse_mode='HTML')
            except:
                if update and update.message:
                    msg = await update.message.reply_text(msg_text, parse_mode='HTML')
                    msg_id = msg.message_id
        elif update and update.message:
            msg = await update.message.reply_text(msg_text, parse_mode='HTML')
            msg_id = msg.message_id

        # 3. Register for final delivery update
        MESSAGE_MAP[clean_name] = (chat_id, msg_id, playlist_name)
        
        # 4. Start background polling
        asyncio.create_task(track_download_progress(chat_id, msg_id, peer, filename, application, sync_hub=sync_hub, track_idx=track_idx))
        return True
    except Exception as e:
        logging.error(f"Download trigger error: {e}")
        return False

async def track_download_progress(chat_id, message_id, peer, filename, application, sync_hub=None, track_idx=None):
    """Background task to poll slskd and update a Telegram message with progress."""
    headers = {'X-API-Key': SLSKD_API_KEY}
    clean_name = html.escape(os.path.basename(filename).replace('\\', '/').split('/')[-1])
    
    logging.info(f"TRACE: Starting progress tracker for {clean_name}")
    
    last_text = ""
    async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
        retry_count = 0
        while True:
            try:
                await asyncio.sleep(5)
                try:
                    req = await client.get(f"{SLSKD_URL}/api/v0/transfers/downloads", timeout=15.0)
                    req.raise_for_status()
                    downloads = req.json()
                    retry_count = 0
                except (httpx.RequestError, httpx.HTTPStatusError) as e:
                    logging.warning(f"TRACE: Temporary Slskd API error for {clean_name}: {e}")
                    retry_count += 1
                    if retry_count > 10: break # Stop if really dead
                    continue

                # Find our specific download
                target = None
                for user_entry in downloads:
                    if user_entry.get('username') != peer: continue
                    for directory in user_entry.get('directories', []):
                        for f_obj in directory.get('files', []):
                            d_file = f_obj.get('filename', '').replace('\\', '/')
                            if d_file == filename.replace('\\', '/') or d_file.endswith(filename.replace('\\', '/')):
                                target = f_obj
                                break
                        if target: break
                    if target: break
                
                if not target:
                    # If it vanished, it's likely finished and cleared by slskd.
                    logging.info(f"TRACE: {clean_name} vanished. Assuming success.")
                    if sync_hub and track_idx is not None:
                        await sync_hub.update_track(track_idx, "success", percent=100)
                    break 
                
                state = target.get('state')
                percent = round(target.get('percentComplete', 0), 1)
                speed = target.get('speed', 0)
                speed_kb = round(speed / 1024, 1) if speed else 0
                
                # Sync Hub Integration
                if sync_hub and track_idx is not None:
                    if state == "InProgress":
                        await sync_hub.update_track(track_idx, "downloading", percent=percent)
                    elif state == "Completed":
                        await sync_hub.update_track(track_idx, "success", percent=100)
                    elif state in ["Errored", "Cancelled", "TimedOut"]:
                        await sync_hub.update_track(track_idx, "failed")
                
                # Update Manual Message
                filled = int(percent // 10)
                bar = "⣿" * filled + "⣀" * (10 - filled)
                status_emoji = "⚡" if state == "InProgress" else "🕒" if state == "Queued" else "✅" if state == "Completed" else "❌"
                
                new_text = (
                    f"<b>{status_emoji} {state}</b>\n"
                    f"🎵 <code>{clean_name}</code>\n"
                    f"👤 Peer: <code>{html.escape(peer)}</code>\n\n"
                    f"[{bar}] {percent}%\n"
                    f"⚡ {speed_kb} KB/s"
                )

                if new_text != last_text:
                    try:
                        await application.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=new_text,
                            parse_mode='HTML'
                        )
                        last_text = new_text
                    except: pass
                
                if state == "Completed":
                    logging.info(f"TRACE: Download {clean_name} marked as Completed.")
                    await asyncio.sleep(2)
                    asyncio.create_task(file_guard_task(application))
                    break
                    
            except Exception as e:
                logging.error(f"TRACE: Error in progress tracker loop: {e}")
                break
                
    logging.info(f"TRACE: Progress tracker finished for {clean_name}")

# --- Telegram Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_USER_ID:
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text="🎵 <b>Music Daemon Online!</b>\n\n"
             "Commands:\n"
             "🔍 Send any artist/song to search\n"
             "📊 /status - Check active downloads\n"
             "🔄 /sync - Force Spotify check",
        parse_mode='HTML'
    )

async def get_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the status of active slskd downloads."""
    if str(update.effective_user.id) != ALLOWED_USER_ID: return
    
    headers = {'X-API-Key': SLSKD_API_KEY}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            resp = await client.get(f"{SLSKD_URL}/api/v0/transfers/downloads")
            resp.raise_for_status()
            downloads = resp.json()
        
        active_files = []
        for user_entry in downloads:
            for directory in user_entry.get('directories', []):
                for f_obj in directory.get('files', []):
                    if f_obj.get('state') not in ['Completed', 'Cancelled', 'Errored', 'RemotelyCancelled']:
                        active_files.append(f_obj)
        
        if not active_files:
            await update.message.reply_text("🏖️ No active downloads at the moment.")
            return

        text = "📊 <b>Active Downloads:</b>\n\n"
        for d in active_files:
            raw_fname = d['filename'].replace('\\', '/').split('/')[-1]
            fname = html.escape(raw_fname)
            percent = round(d.get('percentComplete', 0), 1)
            speed = round(d.get('speed', 0) / 1024, 1) # KB/s
            
            # Prettify state
            state = d.get('state', 'Unknown')
            if state == "InProgress": state = "In Progress"
            
            filled = int(percent // 10)
            bar = "⣿" * filled + "⣀" * (10 - filled)
            
            text += f"🎵 <b>{fname}</b>\n"
            text += f"[{bar}] {percent}%"
            if speed > 0:
                text += f" | ⚡ {speed} KB/s"
            text += f"\n👤 Status: <b>{state}</b> | Peer: <code>{html.escape(d['username'])}</code>\n\n"
            
        await update.message.reply_text(text, parse_mode='HTML')
    except Exception as e:
        logging.error(f"Status error: {e}")
        await update.message.reply_text(f"❌ Error fetching status: {e}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching status: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    logging.info(f"Received message from user ID: {user_id}")
    logging.info(f"Allowed user ID: {ALLOWED_USER_ID}")

    if user_id != ALLOWED_USER_ID:
        logging.warning(f"Unauthorized access attempt by ID: {user_id}")
        return
    
    text = update.message.text.strip()
    logging.info(f"DEBUG: Handle Message received: '{text}'")

    # Detect Spotify Callback URL (for OAuth)
    if '?code=' in text or 'localhost:8888' in text:
        try:
            scope = "playlist-read-private playlist-read-collaborative user-library-read user-read-private user-read-email"
            auth_manager = SpotifyOAuth(client_id=SPOTIPY_CLIENT_ID, client_secret=SPOTIPY_CLIENT_SECRET, redirect_uri=SPOTIFY_REDIRECT_URI, scope=scope)
            
            # Extract code from URL if needed
            code = auth_manager.parse_response_code(text)
            logging.info(f"DEBUG: Extracted OAuth code: {code[:10]}...")
            
            token_info = auth_manager.get_access_token(code, as_dict=False)
            if token_info:
                await update.message.reply_text("✅ <b>Login Successful!</b>\nYou are now authorized to sync playlists.", parse_mode='HTML')
                return
        except Exception as e:
            await update.message.reply_text(f"❌ <b>Login Failed:</b> <code>{e}</code>", parse_mode='HTML')
            return

    # Detect Spotify Playlist Link (Force absolute priority)
    if 'open.spotify.com/playlist/' in text:
        try:
            pl_id = text.split('/playlist/')[1].split('?')[0]
            await update.message.reply_text(f"🚀 <b>One-Off Sync Started!</b>\n🆔 ID: <code>{pl_id}</code>\n\nStarting Pro Sync Hub...", parse_mode='HTML')
            asyncio.create_task(spotify_sync_task(context.application, pl_id=pl_id))
            return
        except Exception as e:
            logging.error(f"Link parsing failed: {e}")
            await update.message.reply_text(f"❌ Failed to parse link: {e}")
            return

    playlist = None
    if 'playlist:' in text:
        parts = text.split('playlist:')
        text = parts[0].strip()
        playlist = parts[1].strip()

    # Moved searching message trigger into search_and_download for animation
    async with httpx.AsyncClient(headers={'X-API-Key': SLSKD_API_KEY}, timeout=30.0) as client:
        status, detail = await search_and_download(client, text, playlist, context.application, update)
    
    if status == "MENU":
        options = detail['options']
        msg = detail['msg']
        chat_id = update.effective_chat.id
        PENDING_DOWNLOADS[chat_id] = detail
        
        text = "🔍 <b>Top Results:</b>\n\n"
        keyboard_btns = []
        tier_icons = {
            "FLAC": "💎 FLAC",
            "WAV": "🌊 WAV",
            "High": "⚡ High MP3",
            "Efficiency": "📦 Standard MP3"
        }

        for i, opt in enumerate(options):
            f_info = opt['file']
            raw_fname = f_info['filename'].replace('\\', '/').split('/')[-1]
            size_mb = round(f_info['size'] / (1024*1024), 1)
            ext = os.path.splitext(raw_fname)[1].upper()[1:]
            bitrate = f_info.get('bitRate', 0)
            tier_label = tier_icons.get(opt['type'], "🎵 Other")
            
            text += f"{i+1}️⃣ <b>{tier_label}</b> ({ext})\n"
            text += f"   📄 <code>{html.escape(raw_fname)}</code>\n"
            if bitrate > 0: text += f"   ⚡ {bitrate}kbps | 📦 {size_mb} MB\n\n"
            else: text += f"   📦 {size_mb} MB\n\n"
            keyboard_btns.append(InlineKeyboardButton(f"{i+1}", callback_data=f"dl_{i}"))
        
        reply_markup = InlineKeyboardMarkup([keyboard_btns, [InlineKeyboardButton("❌ Skip", callback_data="dl_no")]])
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
    elif status == "SUCCESS":
        target_path = os.path.join(ICLOUD_MUSIC_DIR, playlist) if (playlist and ICLOUD_MUSIC_DIR) else ICLOUD_MUSIC_DIR
        await update.message.reply_text(f"✅ Success! File triggered.\n📂 Final folder: {target_path}")
    else:
        await update.message.reply_text(f"❌ Failed: {detail}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    logging.info(f"TRACE: Button clicked by {chat_id}. Data: {query.data}")
    
    if chat_id not in PENDING_DOWNLOADS:
        logging.warning(f"TRACE: chat_id {chat_id} not in PENDING_DOWNLOADS keys: {list(PENDING_DOWNLOADS.keys())}")
        await query.edit_message_text("Request expired or already processed.")
        return

    detail = PENDING_DOWNLOADS.pop(chat_id)
    
    if query.data.startswith("dl_"):
        if query.data == "dl_no":
            await query.edit_message_text("❌ Download canceled.")
            return
            
        idx = int(query.data.split("_")[1])
        selection = detail['options'][idx]
        file_info = selection['file']
        peer = selection['peer']
        playlist = detail['playlist']
        
        # Prettify the notification name
        clean_name = html.escape(os.path.basename(file_info['filename']).replace('\\', '/').split('/')[-1])
        
        await query.edit_message_text(
            f"🚀 <b>Download Started...</b>\n"
            f"🎵 <code>{clean_name}</code>", 
            parse_mode='HTML'
        )
        success = await start_slskd_download(peer, file_info, update, context, existing_msg_id=query.message.message_id)
    elif query.data.startswith("watch_toggle:"):
        pl_id = query.data.split(":")[1]
        metadata = load_history()
        if pl_id in metadata["watched"]:
            metadata["watched"].remove(pl_id)
        else:
            metadata["watched"].add(pl_id)
        save_history(metadata)
        
        # Re-render the menu
        await watch_command(update, context) # This will send a new message, slightly messy but easy
        # Better: edit the existing one
        try:
            auth_manager = SpotifyClientCredentials(client_id=SPOTIPY_CLIENT_ID, client_secret=SPOTIPY_CLIENT_SECRET)
            sp = spotipy.Spotify(auth_manager=auth_manager)
            user_playlists = sp.user_playlists(SPOTIFY_USER_ID)
            watched = metadata["watched"]
            keyboard = []
            for pl in user_playlists['items']:
                status_icon = "🟢" if pl['id'] in watched else "🔴"
                keyboard.append([InlineKeyboardButton(f"{status_icon} {pl['name']}", callback_data=f"watch_toggle:{pl['id']}")])
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        except: pass

# --- Spotify Pro Sync Hub ---

class SyncHub:
    def __init__(self, application, chat_id):
        self.application = application
        self.chat_id = chat_id
        self.message_id = None
        self.queue = [] # list of dicts: {"query": q, "status": s, "p_name": p, "percent": 0}
        self.current_idx = 0
        self.current_track_sec = 0
    
    async def initialize(self):
        text = "🔄 <b>Spotify Sync Hub Active</b>\n\n🔍 Initializing scan..."
        msg = await self.application.bot.send_message(chat_id=self.chat_id, text=text, parse_mode='HTML')
        self.message_id = msg.message_id

    async def set_queue(self, tracks):
        self.queue = [{"query": t["query"], "status": "searching", "p_name": t["playlist"], "percent": 0} for t in tracks]
        await self.render()

    async def update_track(self, idx, status, percent=None):
        if 0 <= idx < len(self.queue):
            self.queue[idx]["status"] = status
            if percent is not None:
                self.queue[idx]["percent"] = percent
            await self.render()

    async def render(self):
        if not self.message_id: return
        text = "🔄 <b>Spotify Sync Hub</b>\n\n"
        for i, item in enumerate(self.queue):
            status = item["status"]
            query = item["query"]
            percent = item["percent"]
            
            icon = "⏳"
            bar_text = ""
            if status == "success": icon = "📦"
            elif status == "found": icon = "🎯"
            elif status == "searching": 
                icon = "🔍"
                p = int((self.current_track_sec / 60) * 100)
                fb = "▓" * (p // 20) + "░" * (5 - (p // 20))
                bar_text = f" <i>{fb} {p}%</i>"
            elif status == "failed": icon = "❌"
            elif status == "downloading" or status == "success": 
                icon = "📥" if status == "downloading" else "📦"
                p_val = int(percent) if percent else 100
                fb = "▓" * (p_val // 20) + "░" * (5 - (p_val // 20))
                bar_text = f" <i>{fb} {p_val}%</i>"
            elif status == "delivered": icon = "✅"
            
            text += f"{icon} <code>{html.escape(query)}</code>{bar_text}\n"
        
        text += f"\n📦 <i>Status: Processing {self.current_idx + 1} of {len(self.queue)}...</i>"
        try:
            await self.application.bot.edit_message_text(
                chat_id=self.chat_id, 
                message_id=self.message_id, 
                text=text, 
                parse_mode='HTML'
            )
        except: pass

    async def finalize(self):
        if not self.message_id: return
        success_count = sum(1 for item in self.queue if item["status"] == "delivered")
        text = "🏁 <b>Spotify Sync Complete</b>\n\n"
        for i, item in enumerate(self.queue):
            status = item["status"]
            # Summarize: delivered is ✅, in Slskd but not library is 📦, else ❌
            icon = "✅" if status == "delivered" else "📦" if status == "success" else "❌"
            text += f"{icon} <code>{html.escape(item['query'])}</code>\n"
        
        text += f"\n📊 Summary: <b>{success_count}</b> tracks processed."
        try:
            await self.application.bot.edit_message_text(
                chat_id=self.chat_id, 
                message_id=self.message_id, 
                text=text, 
                parse_mode='HTML'
            )
        except: pass

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows a list of public playlists to watch."""
    if str(update.effective_user.id) != ALLOWED_USER_ID: return
    if not (SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET and SPOTIFY_USER_ID):
        await update.message.reply_text(f"❌ Credentials missing.\nID: {'✅' if SPOTIPY_CLIENT_ID else '❌'}\nSecret: {'✅' if SPOTIPY_CLIENT_SECRET else '❌'}\nUser: {'✅' if SPOTIFY_USER_ID else '❌'}")
        return

    try:
        auth_manager = SpotifyClientCredentials(client_id=SPOTIPY_CLIENT_ID, client_secret=SPOTIPY_CLIENT_SECRET)
        sp = spotipy.Spotify(auth_manager=auth_manager)
        user_playlists = sp.user_playlists(SPOTIFY_USER_ID)
        
        metadata = load_history()
        watched = metadata["watched"]
        
        text = "📱 <b>Spotify Sync Manager</b>\nSelect playlists to track:\n"
        keyboard = []
        
        for pl in user_playlists['items']:
            is_watched = pl['id'] in watched
            status_icon = "🟢" if is_watched else "🔴"
            keyboard.append([InlineKeyboardButton(f"{status_icon} {pl['name']}", callback_data=f"watch_toggle:{pl['id']}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(
            f"❌ <b>Discovery Blocked by Spotify</b>\n\n"
            f"Spotify returned a 403 Forbidden error. This is common for auto-discovery.\n\n"
            f"💡 <b>Workaround:</b> Just send me a <b>Spotify Playlist Link</b> directly (e.g. <code>https://open.spotify.com/playlist/...</code>) and I will add it to your watchlist!",
            parse_mode='HTML'
        )

async def spotify_sync_task(application=None, pl_id=None):
    """Polls a specific Spotify playlist using OAuth."""
    global ACTIVE_HUB
    if not (SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET): return

    try:
        scope = "playlist-read-private playlist-read-collaborative user-library-read user-read-private user-read-email"
        auth_manager = SpotifyOAuth(client_id=SPOTIPY_CLIENT_ID, client_secret=SPOTIPY_CLIENT_SECRET, redirect_uri=SPOTIFY_REDIRECT_URI, scope=scope)
        sp = spotipy.Spotify(auth_manager=auth_manager)
        
        me = sp.me()
        logging.info(f"DEBUG: Authorized as {me['id']} ({me.get('country', 'Unknown market')})")
        
        # Check if we have a valid token
        if not auth_manager.validate_token(auth_manager.get_cached_token()):
            if application:
                auth_url = auth_manager.get_authorize_url()
                await application.bot.send_message(
                    chat_id=ALLOWED_USER_ID, 
                    text=f"🔑 <b>Spotify Authorization Required</b>\n\n1. <a href='{auth_url}'>Click here to Login</a>\n2. Authorize the app\n3. Copy the <b>URL of the broken page</b> you land on\n4. <b>Paste it here</b>",
                    parse_mode='HTML'
                )
            return

        metadata = load_history()
        
        # 1. Identify Target Playlists
        targets = [] # list of (id, name, snapshot_id)
        if pl_id:
            try:
                pl = sp.playlist(pl_id)
                targets.append((pl['id'], pl['name'], pl['snapshot_id']))
            except Exception as e:
                logging.error(f"Failed to fetch provided playlist {pl_id}: {e}")
                return
        else:
            # Fallback to watched list (optional)
            for wid in metadata["watched"]:
                try: 
                    pl = sp.playlist(wid)
                    targets.append((pl['id'], pl['name'], pl['snapshot_id']))
                except: pass

        if not targets: return

        # 2. Collect tracks
        sync_queue = []
        for pl_id, pl_name, snap_id in targets:
            logging.info(f"DEBUG: Processing tracks for {pl_name}")
            try:
                # Use authorized market to avoid 'track: null'
                user_market = me.get('country')
                results = sp.playlist_items(pl_id, market=user_market, additional_types=['track', 'episode'])
                items = results.get('items', [])
                
                if items:
                    logging.info(f"DEBUG: Raw structure of first item: {json.dumps(items[0])[:500]}...")
                
                # Check if we need more pages
                while results.get('next'):
                    results = sp.next(results)
                    items.extend(results.get('items', []))

                logging.info(f"DEBUG: Successfully collected {len(items)} tracks")
                for item in items:
                    # Support both legacy 'track' and new 'item' keys
                    track = item.get('track') or item.get('item')
                    if not track:
                        logging.warning(f"DEBUG: Item has no track/item data: {json.dumps(item)[:200]}")
                        continue
                    
                    track_id = track.get('id')
                    if not track_id:
                        # Some items might have it in 'uri' or 'id' depending on type
                        logging.warning(f"DEBUG: Item '{track.get('name')}' has no ID.")
                        continue

                    if track_id not in metadata["history"]:
                        artist_name = "Unknown Artist"
                        if track.get('artists'):
                            artist_name = track['artists'][0]['name']
                        elif track.get('show'): # for episodes
                            artist_name = track['show']['name']
                            
                        query = f"{artist_name} - {track['name']}"
                        sync_queue.append({'id': track_id, 'query': query, 'playlist': pl_name})
                        logging.info(f"DEBUG: Added to sync queue: {query}")
                    else:
                        logging.info(f"DEBUG: Already in history: {track.get('name')} ({track_id})")
            except Exception as e:
                logging.error(f"DEBUG: Failed to get items for {pl_id}: {e}")
                if application:
                    await application.bot.send_message(chat_id=ALLOWED_USER_ID, text=f"❌ <b>Spotify Error:</b> <code>{e}</code>")
        
        logging.info(f"DEBUG: Final sync_queue size: {len(sync_queue)}")
        if not sync_queue:
            if application:
                await application.bot.send_message(chat_id=ALLOWED_USER_ID, text="🏖️ <b>Sync Complete:</b> No new tracks found in this playlist.")
            return

        # 4. Hub & Parallel Search
        ACTIVE_HUB = SyncHub(application, ALLOWED_USER_ID)
        hub = ACTIVE_HUB
        await hub.initialize()
        await hub.set_queue(sync_queue)

        # Semaphore to limit parallel searches (3 at a time)
        sem = asyncio.Semaphore(3)
        
        async with httpx.AsyncClient(headers={'X-API-Key': SLSKD_API_KEY}, timeout=30.0) as client:
            async def bounded_search(idx, item):
                async with sem:
                    await hub.update_track(idx, "searching")
                    status, _ = await search_and_download(
                        client=client,
                        query=item['query'],
                        playlist_name=item['playlist'],
                        application=application,
                        update=None,
                        automatic=True,
                        sync_hub=hub,
                        track_idx=idx
                    )
                    
                    # Only update Hub if it failed. Success is handled by the background tracker.
                    if status != "SUCCESS":
                        await hub.update_track(idx, "failed")
                    
                    if status == "SUCCESS":
                        metadata["history"].add(item['id'])
                        save_history(metadata)

            # Trigger all searches in parallel
            tasks = [bounded_search(i, t) for i, t in enumerate(sync_queue)]
            await asyncio.gather(*tasks)

        # 5. Hybrid Wait Cycle (Allow downloads to finish delivering)
        wait_start = time.time()
        logging.info("DEBUG: Entering Hub Wait Cycle (120s max)")
        while time.time() - wait_start < 120:
            any_active = any(status in ["searching", "downloading"] for _, status, _ in hub.queue)
            if not any_active:
                break
            await asyncio.sleep(5)

        await hub.finalize()
        ACTIVE_HUB = None
        
    except Exception as e:
        logging.error(f"Spotify Sync Hub failed: {e}")

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates the Spotify OAuth login link."""
    if str(update.effective_user.id) != ALLOWED_USER_ID: return
    
    scope = "playlist-read-private playlist-read-collaborative user-library-read user-read-private user-read-email"
    auth_manager = SpotifyOAuth(client_id=SPOTIPY_CLIENT_ID, client_secret=SPOTIPY_CLIENT_SECRET, redirect_uri=SPOTIFY_REDIRECT_URI, scope=scope)
    auth_url = auth_manager.get_authorize_url()
    
    text = (
        "🔑 <b>Connect Spotify Hub</b>\n\n"
        "To sync playlists, I need your permission to see them.\n\n"
        f"1. <a href='{auth_url}'>Login to Spotify</a>\n"
        "2. Copy the URL of the page you are redirected to (even if it looks broken)\n"
        "3. <b>Paste that URL here</b> in the chat."
    )
    await update.message.reply_text(text, parse_mode='HTML')

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually triggers the Spotify sync."""
    if str(update.effective_user.id) != ALLOWED_USER_ID: return
    
    await update.message.reply_text("🔄 <b>Starting Spotify Sync...</b>", parse_mode='HTML')
    await spotify_sync_task(context.application)
    await update.message.reply_text("✅ <b>Sync Scan Complete.</b>", parse_mode='HTML')

# --- Scheduled Tasks ---

async def file_guard_task(application):
    """Periodic task to move finished downloads to target folders."""
    # Check both potential slskd download paths
    sources = ["./slskd-data/downloads", "./slskd-data/data/downloads"]
    local_downloads = os.path.expanduser("~/Downloads/Music-Daemon")
    
    targets = []
    if ICLOUD_MUSIC_DIR: 
        targets.append(ICLOUD_MUSIC_DIR)
    targets.append(local_downloads)
    
    # First, collect all unique subfolders (playlists) we need to check
    # Or more simply, iterate files in source and look them up in MESSAGE_MAP
    # Actually, we should just run the guard and for each file, check if we have a subfolder mapping
    
    all_moved = []
    for source in sources:
        if not os.path.exists(source): continue
        
        # We need to peek at the files first to see which ones have subfolders
        for root, dirs, files in os.walk(source):
            if "incomplete" in dirs: dirs.remove("incomplete")
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in {'.mp3', '.flac', '.m4a', '.wav', '.ogg'}:
                    # Look up playlist name
                    _, _, playlist_name = MESSAGE_MAP.get(f, (None, None, None))
                    moved = file_guard(source, targets, subfolder=playlist_name, target_file=f)
                    if moved:
                        for mf in moved:
                            # Fuzzy lookup for MESSAGE_MAP
                            found_key = None
                            if mf in MESSAGE_MAP:
                                found_key = mf
                            else:
                                for k in MESSAGE_MAP.keys():
                                    k_base = os.path.splitext(k)[0].lower()
                                    mf_base = os.path.splitext(mf)[0].lower()
                                    if k_base in mf_base or mf_base in k_base:
                                        found_key = k
                                        break
                            
                            target_chat, target_msg, p_name = MESSAGE_MAP.pop(found_key, (ALLOWED_USER_ID, None, None)) if found_key else (ALLOWED_USER_ID, None, None)
                            
                            global ACTIVE_HUB
                            if ACTIVE_HUB:
                                for i, item in enumerate(ACTIVE_HUB.queue):
                                    q = item["query"].lower()
                                    mf_clean = re.sub(r'^\d+[-\s]+', '', mf.lower()) # Remove track num
                                    mf_clean = os.path.splitext(mf_clean)[0]
                                    
                                    # Very fuzzy matching
                                    if (found_key and found_key.lower() in q) or (q in mf.lower()) or (mf_clean in q) or (q in mf_clean):
                                        asyncio.create_task(ACTIVE_HUB.update_track(i, "delivered"))
                                        break
 
                            folder_info = f"\n📂 <b>Folder</b>: <code>{p_name}</code>" if p_name else ""
                            delivery_msg = f"<b>✅ Downloads Delivered!</b>\n🎵 <code>{html.escape(mf)}</code>{folder_info}\n\n📍 iCloud & Local"
                            
                            if target_msg:
                                try: await application.bot.edit_message_text(chat_id=target_chat, message_id=target_msg, text=delivery_msg, parse_mode='HTML')
                                except: pass
                            else:
                                await application.bot.send_message(chat_id=ALLOWED_USER_ID, text=delivery_msg, parse_mode='HTML')
                        all_moved.extend(moved)
        # Re-run a general guard for anything else (rare)
        file_guard(source, targets)

async def post_init(application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(file_guard_task, 'interval', seconds=15, args=[application]) # Pass application
    scheduler.start()
    logging.info("Scheduler started.")

if __name__ == '__main__':
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in .env. See .env.example")
        exit(1)

    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('status', get_status))
    application.add_handler(CommandHandler('sync', sync_command))
    application.add_handler(CommandHandler('login', login_command))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    print("Daemon is running...")
    application.run_polling()
