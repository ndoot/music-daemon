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

# Globals for interactive state
PENDING_DOWNLOADS = {}
TRANSFER_MAP = {} # {id: (peer, filename)}
MESSAGE_MAP = {} # {filename: (chat_id, message_id)}

# Load environment variables
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ALLOWED_USER_ID = os.getenv('TELEGRAM_ALLOWED_USER_ID')
SLSKD_URL = os.getenv('SLSKD_URL', 'http://localhost:5030')
SLSKD_API_KEY = os.getenv('SLSKD_API_KEY')
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', './downloads')
ICLOUD_MUSIC_DIR = os.getenv('ICLOUD_MUSIC_DIR')

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- Logic Components ---

def file_guard(source_dir, targets):
    """Moves/Copies only safe audio files from source to multiple targets."""
    SAFE_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.wav', '.ogg'}
    moved_files = []
    
    if not os.path.exists(source_dir):
        return []

    for target_dir in targets:
        if not os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)
    
    for root, dirs, files in os.walk(source_dir):
        # Prevent walking into slskd's incomplete folder
        if "incomplete" in dirs:
            dirs.remove("incomplete")
            
        for file in files:
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

                for i, target_dir in enumerate(targets):
                    target_path = os.path.join(target_dir, file)
                    if i < len(targets) - 1:
                        shutil.copy2(source_path, target_path)
                    else:
                        shutil.move(source_path, target_path) # Move on the last one
                moved_files.append(file)
            else:
                if file not in [".DS_Store"]:
                    logging.warning(f"Ignoring unsafe file: {file}")
    return moved_files

async def search_and_download(query, playlist_name=None, context=None, update=None):
    """Searches slskd and triggers a download. Falls back to yt-dlp."""
    if not SLSKD_API_KEY:
        logging.error("SLSKD_API_KEY not set")
        return
    headers = {'X-API-Key': SLSKD_API_KEY}
    
    # 1. Start Search
    try:
        logging.info(f"TRACE: POST {SLSKD_URL}/api/v0/searches | Data: {{'searchText': {query}}}")
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
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
            if context and msg:
                progress = int((sec / 60) * 100)
                filled = int(progress // 10)
                bar = "⣿" * filled + "⣀" * (10 - filled)
                status_text = f"🔍 Searching for: <b>{html.escape(query)}</b>...\n[{bar}] {progress}%"
                
                # Edit every second for smooth ticking
                try: await msg.edit_text(status_text, parse_mode='HTML')
                except: pass
                
                if sec % 5 == 0: # Action every 5s
                    await context.bot.send_chat_action(chat_id=msg.chat_id, action="typing")
            
            # Poll Slskd every 10 seconds
            if sec % 10 == 0:
                logging.info(f"TRACE: Polling Slskd (Second {sec})")
                try:
                    async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
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

                        # Filter and order: FLAC, WAV, High, Efficiency
                        options = [candidates[k] for k in ["flac", "wav", "high", "efficiency"] if candidates[k]]
                        if options:
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

async def start_slskd_download(peer, file, update, context, existing_msg_id=None):
    """Triggers the download and transitions the UI bubble."""
    if not SLSKD_API_KEY: return False
    headers = {'X-API-Key': SLSKD_API_KEY}
    
    filename = file['filename']
    clean_name = filename.replace('\\', '/').split('/')[-1]
    
    try:
        # 1. Trigger Slskd
        payload = [{'filename': filename, 'size': file['size']}]
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            logging.info(f"TRACE: Downloading {clean_name} from {peer}")
            d_req = await client.post(f"{SLSKD_URL}/api/v0/transfers/downloads/{peer}", json=payload)
            d_req.raise_for_status()

        # 2. UI Transition
        chat_id = update.effective_chat.id
        msg_text = f"⏳ <b>Starting Download...</b>\n🎵 <code>{html.escape(clean_name)}</code>"
        
        if existing_msg_id:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=existing_msg_id, text=msg_text, parse_mode='HTML')
                msg_id = existing_msg_id
            except:
                msg = await update.message.reply_text(msg_text, parse_mode='HTML')
                msg_id = msg.message_id
        else:
            msg = await update.message.reply_text(msg_text, parse_mode='HTML')
            msg_id = msg.message_id

        # 3. Register for final delivery update
        MESSAGE_MAP[clean_name] = (chat_id, msg_id)
        
        # 4. Start background polling
        asyncio.create_task(track_download_progress(chat_id, msg_id, peer, filename, context.application))
        return True
    except Exception as e:
        logging.error(f"Download trigger error: {e}")
        return False

async def track_download_progress(chat_id, message_id, peer, filename, application: ApplicationBuilder):
    """Background task to poll slskd and update a Telegram message with progress."""
    headers = {'X-API-Key': SLSKD_API_KEY}
    clean_name = html.escape(os.path.basename(filename).replace('\\', '/').split('/')[-1])
    
    logging.info(f"TRACE: Starting progress tracker for {clean_name}")
    
    last_text = ""
    for _ in range(120): # Track for up to 10 minutes (120 * 5s)
        await asyncio.sleep(5)
        try:
            logging.info(f"TRACE: GET {SLSKD_URL}/api/v0/transfers/downloads")
            async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
                req = await client.get(f"{SLSKD_URL}/api/v0/transfers/downloads")
                req.raise_for_status()
                downloads = req.json()
                logging.info(f"TRACE: Slskd reporting {len(downloads)} active transfers.")
                if downloads:
                    logging.info(f"TRACE: Full structure of first transfer: {json.dumps(downloads[0])}")
                for d in downloads:
                    logging.info(f"   -> {d.get('username')} | {d.get('state')} | {d.get('filename')}")
            
            # Find our specific download - traverse nested directories and files
            target = None
            for user_entry in downloads:
                if user_entry.get('username') != peer:
                    continue
                
                for directory in user_entry.get('directories', []):
                    for f_obj in directory.get('files', []):
                        d_file = f_obj.get('filename', '').replace('\\', '/')
                        target_file = filename.replace('\\', '/')
                        
                        if d_file == target_file or d_file.endswith(target_file) or target_file.endswith(d_file):
                            target = f_obj
                            break
                    if target: break
                if target: break
            
            if not target:
                # Debug mismatch
                logging.info(f"TRACE: No match in nested list for {peer} | {filename}")
                break
            
            state = target.get('state')
            bytes_tx = target.get('bytesTransferred', 0)
            size = target.get('size', 0)
            speed = target.get('speed', 0) 
            percent = round(target.get('percentComplete', 0), 1)
            
            speed_kb = round(speed / 1024, 1) if speed else 0
            
            # Progress bar
            filled = int(percent // 10)
            bar = "⣿" * filled + "⣀" * (10 - filled)
            
            # Prettify state & emoji
            status_emoji = "⏳"
            if state == "InProgress": 
                status_emoji = "⚡"
                display_state = "In Progress"
            elif state == "Queued": 
                status_emoji = "🕒"
            elif state == "Completed": 
                status_emoji = "✅"
            elif state in ["Errored", "Cancelled", "TimedOut"]:
                status_emoji = "❌"
            
            new_text = (
                f"<b>{status_emoji} {display_state}</b>\n"
                f"🎵 <code>{clean_name}</code>\n"
                f"👤 Peer: <code>{html.escape(peer)}</code>\n\n"
                f"[{bar}] {percent}%"
            )
            
            if speed_kb > 0 and percent < 100:
                new_text += f"\n⚡ {speed_kb} KB/s"
            
            # Add Cancel button while in progress and not finished
            reply_markup = None
            if state not in ["Completed", "Errored", "Cancelled", "TimedOut"] and percent < 100:
                # Use a short ID to avoid Telegram's 64-character limit for callback_data
                transfer_id = str(hash(f"{peer}:{filename}"))[-10:]
                TRANSFER_MAP[transfer_id] = (peer, filename)
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏹ Cancel Download", callback_data=f"cancel:{transfer_id}")
                ]])

            if new_text != last_text:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=new_text,
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
                last_text = new_text
            
            if state == "Completed":
                logging.info(f"TRACE: Download {clean_name} marked as Completed. Triggering immediate delivery check.")
                # Give it a 2-second head start to settle, then trigger guard
                await asyncio.sleep(2)
                asyncio.create_task(file_guard_task(application))
                break
                
        except Exception as e:
            logging.error(f"TRACE: Error in progress tracker: {e}")
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
    
    text = update.message.text
    logging.info(f"Processing text: {text}")
    
    # Parse for playlist hint e.g., "link playlist:Gym"
    playlist = None
    if 'playlist:' in text:
        parts = text.split('playlist:')
        text = parts[0].strip()
        playlist = parts[1].strip()

    # Moved searching message trigger into search_and_download for animation
    status, detail = await search_and_download(text, playlist, context, update)
    
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
        if not success:
            await query.edit_message_text("❌ Failed to trigger download.")

# --- Scheduled Tasks ---

async def spotify_sync_task():
    logging.info("Checking Spotify playlists...")
    # TODO: Implement spotipy logic here
    pass

async def file_guard_task(application):
    """Periodic task to move finished downloads to target folders."""
    # Check both potential slskd download paths
    sources = ["./slskd-data/downloads", "./slskd-data/data/downloads"]
    local_downloads = os.path.expanduser("~/Downloads/Music-Daemon")
    
    targets = []
    if ICLOUD_MUSIC_DIR: 
        targets.append(ICLOUD_MUSIC_DIR)
    targets.append(local_downloads)
    
    all_moved = []
    for source in sources:
        logging.info(f"TRACE: File Guard checking: {os.path.abspath(source)}")
        if os.path.exists(source):
            logging.info(f"TRACE: Path exists, walking...")
            moved = file_guard(source, targets)
            if moved:
                logging.info(f"TRACE: Delivering {len(moved)} files: {moved}")
                for f in moved:
                    clean_f = f # Usually just filename
                    target_chat, target_msg = MESSAGE_MAP.pop(clean_f, (ALLOWED_USER_ID, None))
                    
                    delivery_msg = f"<b>✅ Downloads Delivered!</b>\n🎵 <code>{html.escape(f)}</code>\n\n📂 <b>Folders</b>:\n☁️ iCloud\n💻 Local"
                    
                    if target_msg:
                        try:
                            await application.bot.edit_message_text(
                                chat_id=target_chat,
                                message_id=target_msg,
                                text=delivery_msg,
                                parse_mode='HTML'
                            )
                            continue # Successfully updated unified bubble
                        except Exception as e:
                            logging.error(f"Failed to edit final msg: {e}")
                    
                    # Fallback or additional file delivery
                    await application.bot.send_message(
                        chat_id=ALLOWED_USER_ID,
                        text=delivery_msg,
                        parse_mode='HTML'
                    )
            all_moved.extend(moved)
        else:
            logging.info(f"TRACE: Path not found: {source}")

async def post_init(application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(spotify_sync_task, 'interval', hours=1)
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
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    print("Daemon is running...")
    application.run_polling()
