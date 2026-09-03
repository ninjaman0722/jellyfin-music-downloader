import sys, os, subprocess, json, urllib.request, urllib.parse, re, sqlite3, time, signal

# Load server configuration from external JSON or environment variables
CONFIG_PATH = os.path.expanduser('~/spotdl/server_config.json')
server_cfg = {}
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as cf:
            server_cfg = json.load(cf)
    except Exception:
        pass

DB_PATH = os.environ.get('JELLYFIN_DB_PATH', server_cfg.get('db_path', '/var/lib/jellyfin/data/jellyfin.db'))
PLAYLISTS_DIR = os.environ.get('JELLYFIN_PLAYLISTS_DIR', server_cfg.get('playlists_dir', '/var/lib/jellyfin/data/playlists'))
MUSIC_DIR = os.environ.get('MUSIC_DIR', server_cfg.get('music_dir', '/mnt/media/music'))
LOG_FILE = os.path.expanduser('~/spotdl/ingest.log')
JELLYFIN_TOKEN = os.environ.get('JELLYFIN_TOKEN', server_cfg.get('jellyfin_token', ''))
JELLYFIN_URL = os.environ.get('JELLYFIN_URL', server_cfg.get('jellyfin_url', 'http://127.0.0.1:8096'))
MUSIC_FOLDER_ID = os.environ.get('JELLYFIN_MUSIC_FOLDER_ID', server_cfg.get('music_folder_id', ''))
PLAYLISTS_FOLDER_ID = os.environ.get('JELLYFIN_PLAYLISTS_FOLDER_ID', server_cfg.get('playlists_folder_id', ''))

active_proc = None

def signal_handler(sig, frame):
    log("LOG: 🛑 Stop signal received. Cancelling download and cleaning up...")
    update_state(running=False, status="Cancelled")
    global active_proc
    if active_proc:
        try:
            active_proc.terminate()
            active_proc.wait(timeout=2)
        except Exception:
            pass
    try:
        subprocess.run("docker stop $(docker ps -q --filter ancestor=spotdl-custom:latest) 2>/dev/null", shell=True)
    except Exception:
        pass
    sys.exit(0)

STATE_FILE = os.path.expanduser('~/spotdl/active_state.json')

def update_state(running=True, user="", title="", total=0, current=0, skipped=0, downloaded=0, track="", status="", pct=0):
    try:
        data = {
            "running": running,
            "user": user,
            "title": title,
            "total": total,
            "current": current,
            "skipped": skipped,
            "downloaded": downloaded,
            "track": track,
            "status": status,
            "pct": pct,
            "updated_at": int(time.time())
        }
        with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception:
        pass

try:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
except Exception:
    pass

LRCLIB_HEADERS = {
    'User-Agent': 'JellyfinMusicIngest/2.0 (https://github.com/omarchy/jellyfin-music-downloader)'
}

def log(msg):
    print(msg, flush=True)
    try:
        # Only write human-readable lines to the log file (strip GUI protocol prefixes)
        if not re.match(r"^(PROGRESS|TRACK|COUNTS|STATUS|STAGE|TOTAL|BATCH_ITEM):", msg):
            clean_msg = msg[5:] if msg.startswith("LOG: ") else msg
            t_str = time.strftime('%Y-%m-%d %H:%M:%S')
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(f"[{t_str}] {clean_msg}\n")
    except Exception:
        pass

def extract_cover_from_mp3(mp3_path, out_cover_path):
    """Extracts embedded APIC JPEG/PNG image from MP3 and writes cover.jpg for Jellyfin."""
    try:
        with open(mp3_path, 'rb') as f:
            data = f.read(4 * 1024 * 1024)
        idx = data.find(b'APIC')
        if idx != -1:
            jpg_idx = data.find(b'\xff\xd8\xff', idx)
            png_idx = data.find(b'\x89PNG\r\n\x1a\n', idx)
            if jpg_idx != -1 and (png_idx == -1 or jpg_idx < png_idx):
                start = jpg_idx
                end = data.find(b'\xff\xd9', start) + 2
            elif png_idx != -1:
                start = png_idx
                end = data.find(b'IEND', start) + 8
            else:
                start, end = -1, -1
            if start != -1 and end > start:
                with open(out_cover_path, 'wb') as out:
                    out.write(data[start:end])
                return True
    except Exception:
        pass
    return False

def fetch_and_save_lrc(song_name):
    # song_name usually in format 'Artist - Title'
    parts = song_name.split(' - ', 1)
    if len(parts) == 2:
        artist = parts[0].strip()
        title = parts[1].strip()
    else:
        artist = ''
        title = song_name.strip()

    title_clean = re.sub(r'\(feat\.[^\)]+\)', '', title, flags=re.IGNORECASE).strip()

    params = {'artist_name': artist, 'track_name': title_clean}
    url = 'https://lrclib.net/api/get?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=LRCLIB_HEADERS)

    lyrics = None
    is_synced = False

    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data.get('syncedLyrics'):
                lyrics = data['syncedLyrics']
                is_synced = True
            elif data.get('plainLyrics'):
                lyrics = data['plainLyrics']
    except Exception:
        pass

    if not lyrics:
        # Fallback search
        search_q = (artist + ' ' + title_clean).strip()
        search_url = 'https://lrclib.net/api/search?' + urllib.parse.urlencode({'q': search_q})
        req_s = urllib.request.Request(search_url, headers=LRCLIB_HEADERS)
        try:
            with urllib.request.urlopen(req_s, timeout=4) as resp:
                results = json.loads(resp.read().decode('utf-8'))
                if isinstance(results, list) and len(results) > 0:
                    for item in results:
                        if item.get('syncedLyrics'):
                            lyrics = item['syncedLyrics']
                            is_synced = True
                            break
                    if not lyrics:
                        for item in results:
                            if item.get('plainLyrics'):
                                lyrics = item['plainLyrics']
                                break
        except Exception:
            pass

    if lyrics:
        # Locate the mp3 file in MUSIC_DIR
        safe_title = re.sub(r'[^\w\s]', '', title).lower()
        for root, dirs, files in os.walk(MUSIC_DIR):
            for f in files:
                if f.endswith('.mp3'):
                    f_clean = re.sub(r'[^\w\s]', '', f).lower()
                    if safe_title in f_clean or (artist.lower() in root.lower() and title_clean.lower() in f.lower()):
                        mp3_file = os.path.join(root, f)
                        c_jpg = os.path.join(root, "cover.jpg")
                        f_jpg = os.path.join(root, "folder.jpg")
                        if not os.path.exists(c_jpg) and not os.path.exists(f_jpg):
                            extract_cover_from_mp3(mp3_file, c_jpg)
                        lrc_path = os.path.splitext(mp3_file)[0] + '.lrc'
                        try:
                            with open(lrc_path, 'w', encoding='utf-8') as out:
                                out.write(lyrics)
                            os.chmod(lrc_path, 0o664)
                            return True, is_synced
                        except Exception:
                            pass
    return False, False

def list_users():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    users_dict = {}
    for row in cur.execute('SELECT Id, Username FROM Users'):
        raw_id = row[0]
        name = row[1]
        if name.lower() == 'user':
            continue
        uid = raw_id.replace('-', '').lower()
        users_dict[uid] = {
            'id': uid,
            'raw_id': raw_id,
            'name': name,
            'is_shared': False,
            'playlists': []
        }

    for uid, udata in users_dict.items():
        try:
            req = urllib.request.Request(f"{JELLYFIN_URL}/Users/{uid}/Items?includeItemTypes=Playlist&recursive=true&api_key={JELLYFIN_TOKEN}")
            res = json.loads(urllib.request.urlopen(req, timeout=5).read())
            udata['playlists'] = [item['Name'] for item in res.get('Items', []) if item.get('Name')]
        except Exception:
            pass

    user_list = list(users_dict.values())
    user_list.append({
        'id': '00000000000000000000000000000000',
        'raw_id': '00000000-0000-0000-0000-000000000000',
        'name': 'Household (Shared)',
        'is_shared': True,
        'playlists': []
    })

    print(json.dumps({'users': user_list}))

def sync_playlist_to_jellyfin(playlist_name, owner_id, downloaded_song_names, is_shared=False):
    log(f"LOG: 🔗 Synchronizing playlist '{playlist_name}' to Jellyfin API...")
    
    try:
        req = urllib.request.Request(f"{JELLYFIN_URL}/Items/{MUSIC_FOLDER_ID}/Refresh?api_key={JELLYFIN_TOKEN}", method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    item_ids = []
    if downloaded_song_names:
        for sn in downloaded_song_names:
            clean_sn = os.path.basename(sn).replace('.mp3', '')
            parts = clean_sn.split(' - ')
            search_title = parts[-1].strip() if parts else clean_sn

            cur.execute("SELECT Id FROM BaseItems WHERE MediaType = 'Audio' AND Name LIKE ? ORDER BY DateCreated DESC LIMIT 1", ('%' + search_title + '%',))
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT Id FROM BaseItems WHERE MediaType = 'Audio' AND Path LIKE ? ORDER BY DateCreated DESC LIMIT 1", ('%' + search_title + '%',))
                row = cur.fetchone()

            if row:
                cid = row[0].replace('-', '').lower()
                if cid not in item_ids:
                    item_ids.append(cid)

    if not item_ids:
        log("LOG: ⚠️ No new track item IDs resolved for playlist linking.")
        return

    cur.execute("SELECT Id FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Playlists.Playlist' AND Name = ?", (playlist_name,))
    existing = cur.fetchone()

    target_owner = '00000000000000000000000000000000' if is_shared else (owner_id or '')

    if existing:
        playlist_id = existing[0].replace('-', '').lower()
        log(f"LOG: 📌 Appending {len(item_ids)} tracks to existing playlist '{playlist_name}' ({playlist_id})...")
        ids_param = ','.join(item_ids)
        url = f"{JELLYFIN_URL}/Playlists/{playlist_id}/Items?ids={ids_param}&userId={target_owner}"
        req = urllib.request.Request(url, method="POST")
        req.add_header('X-Emby-Token', JELLYFIN_TOKEN)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                log(f"LOG: 🔒 Appended {len(item_ids)} tracks to '{playlist_name}' successfully!")
        except Exception as e:
            log(f"LOG: ⚠️ Playlist append note: {e}")
    else:
        log(f"LOG: 🆕 Creating fresh playlist '{playlist_name}' with {len(item_ids)} tracks...")
        ids_param = ','.join(item_ids)
        url = f"{JELLYFIN_URL}/Playlists?name={urllib.parse.quote(playlist_name)}&ids={ids_param}&userId={target_owner}&mediaType=Audio"
        req = urllib.request.Request(url, method="POST")
        req.add_header('X-Emby-Token', JELLYFIN_TOKEN)
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read())
                log(f"LOG: 🔒 Created playlist '{playlist_name}' (ID: {res.get('Id')})!")
        except Exception as e:
            log(f"LOG: ⚠️ Playlist creation note: {e}")

    if is_shared:
        try:
            xml_file = os.path.join(PLAYLISTS_DIR, playlist_name, 'playlist.xml')
            if os.path.exists(xml_file):
                with open(xml_file, 'r', encoding='utf-8') as f:
                    xml_content = f.read()
                xml_content = re.sub(r'<OwnerUserId>[^<]+</OwnerUserId>', '<OwnerUserId>00000000000000000000000000000000</OwnerUserId>', xml_content)
                with open(xml_file, 'w', encoding='utf-8') as f:
                    f.write(xml_content)
                log(f"LOG: 👥 Playlist '{playlist_name}' unlocked for all household users!")
        except Exception as e:
            log(f"LOG: ⚠️ Shared playlist config note: {e}")

    try:
        cur.execute("UPDATE BaseItems SET Name = SUBSTR(Name, 6) WHERE MediaType = 'Audio' AND Name GLOB '[0-9][0-9] - *'")
        conn.commit()
    except Exception:
        pass

    try:
        req = urllib.request.Request(f"{JELLYFIN_URL}/Items/{PLAYLISTS_FOLDER_ID}/Refresh?api_key={JELLYFIN_TOKEN}", method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def run_download(url, user_name, owner_id, song_action='none', song_playlist_name='', is_shared=False):
    log("STAGE: discovery")
    log("STATUS: 🔍 Resolving Spotify metadata & scanning catalog...")
    log(f"LOG: 🌐 Contacting Spotify metadata API for {url}...")
    update_state(running=True, user=user_name, title=song_playlist_name or url, total=0, current=0, skipped=0, downloaded=0, track="Resolving catalog...", status="🔍 Resolving Spotify metadata & scanning catalog...", pct=0)

    cmd = [
        'docker', 'run', '--rm',
        '-e', 'HOME=/tmp',
        '-v', '/mnt/media/music:/music',
        '-u', '1000:1000',
        'spotdl-custom:latest',
        'download', url,
        '--threads', '2',
        '--max-retries', '3',
        '--sponsor-block',
        '--bitrate', 'auto',
        '--dont-filter-results',
        '--output', '/music/{artist}/{album}/{track-number} - {title}.{output-ext}'
    ]

    global active_proc
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_proc = proc
    
    total_tracks = 0
    current_idx = 0
    skipped_count = 0
    downloaded_count = 0
    downloaded_tracks = []
    detected_playlist_title = ""

    for line in proc.stdout:
        line_str = line.strip()
        if not line_str:
            continue
        
        if "Processing query:" in line_str:
            log(f"LOG: 🔍 Scanning query: {url}")
            log("STATUS: 🔍 Scraping albums & tracklists from Spotify...")
            continue

        found_m = re.search(r'Found (\d+) songs in (.+)', line_str, re.IGNORECASE)
        if found_m:
            total_tracks = int(found_m.group(1))
            collection_name = found_m.group(2)
            detected_playlist_title = collection_name
            log(f"TOTAL: {total_tracks}")
            log("STAGE: downloading")
            log(f"STATUS: ⚡ Found {total_tracks} tracks in {collection_name}")
            log(f"LOG: ⚡ Discovered {total_tracks} songs in {collection_name}")
            update_state(running=True, user=user_name, title=collection_name, total=total_tracks, current=current_idx, skipped=skipped_count, downloaded=downloaded_count, track=f"Found {total_tracks} tracks", status=f"⚡ Found {total_tracks} tracks in {collection_name}", pct=0)
            continue

        found_gen = re.search(r'Found (\d+) songs', line_str, re.IGNORECASE)
        if found_gen and not total_tracks:
            total_tracks = int(found_gen.group(1))
            log(f"TOTAL: {total_tracks}")
            log("STAGE: downloading")
            log(f"STATUS: ⚡ Found {total_tracks} tracks to download")
            log(f"LOG: ⚡ Discovered {total_tracks} tracks")
            update_state(running=True, user=user_name, title=detected_playlist_title or url, total=total_tracks, current=current_idx, skipped=skipped_count, downloaded=downloaded_count, track=f"Found {total_tracks} tracks", status=f"⚡ Found {total_tracks} tracks to download", pct=0)
            continue

        dl_m = re.search(r'Downloaded "([^"]+)"', line_str)
        if dl_m:
            current_idx += 1
            downloaded_count += 1
            song_name = dl_m.group(1).strip()
            downloaded_tracks.append(song_name)
            prog_pct = int((current_idx / max(total_tracks, 1)) * 100) if total_tracks else 50
            log(f"PROGRESS: {prog_pct}|{current_idx}|{total_tracks}")
            log(f"COUNTS: {skipped_count}|{downloaded_count}|{total_tracks}")
            log(f"TRACK: {song_name}")
            log(f"STATUS: ⬇ Downloading New [{current_idx}/{total_tracks or '?'}] (New #{downloaded_count}) {song_name}")
            update_state(running=True, user=user_name, title=detected_playlist_title or song_playlist_name or url, total=total_tracks, current=current_idx, skipped=skipped_count, downloaded=downloaded_count, track=song_name, status=f"⬇ Downloading [{current_idx}/{total_tracks or '?'}] (New #{downloaded_count}) {song_name}", pct=prog_pct)
            
            # Fetch synced lyrics from LRCLIB
            has_lrc, is_synced = fetch_and_save_lrc(song_name)
            if has_lrc and is_synced:
                log(f"LOG: 🎤 [{current_idx}/{total_tracks or '?'}] {song_name} (New #{downloaded_count} • Synced Lyrics)")
            elif has_lrc:
                log(f"LOG: 📄 [{current_idx}/{total_tracks or '?'}] {song_name} (New #{downloaded_count} • Plain Lyrics)")
            else:
                log(f"LOG: 🎵 [{current_idx}/{total_tracks or '?'}] {song_name} (New #{downloaded_count})")
            continue

        skip_m = re.search(r'Skipping\s+"?([^"(]+?)"?(?:\s+\(file already exists|\s+\(duplicate|\s*$)', line_str, re.IGNORECASE)
        if skip_m:
            current_idx += 1
            skipped_count += 1
            song_name = skip_m.group(1).strip()
            downloaded_tracks.append(song_name)
            prog_pct = int((current_idx / max(total_tracks, 1)) * 100) if total_tracks else 50
            log(f"PROGRESS: {prog_pct}|{current_idx}|{total_tracks}")
            log(f"COUNTS: {skipped_count}|{downloaded_count}|{total_tracks}")
            log(f"STATUS: ⏩ Skipped (In Library) [{current_idx}/{total_tracks or '?'}] {song_name}")
            log(f"LOG: ⏩ [{current_idx}/{total_tracks or '?'}] {song_name} (Already in Library)")
            update_state(running=True, user=user_name, title=detected_playlist_title or song_playlist_name or url, total=total_tracks, current=current_idx, skipped=skipped_count, downloaded=downloaded_count, track=song_name, status=f"⏩ Skipped (In Library) [{current_idx}/{total_tracks or '?'}] {song_name}", pct=prog_pct)
            continue

        search_m = re.search(r'Searching for "([^"]+)"', line_str)
        if search_m:
            song_name = search_m.group(1)
            log(f"STATUS: 🔎 Matching stream: {song_name}")
            continue

        # Log any sponsorblock actions or warnings
        if 'sponsor segments' in line_str:
            log(f"LOG: ✂ {line_str}")
        elif 'ERROR:' in line_str or 'Traceback' in line_str:
            log(f"LOG: ⚠️ {line_str}")
        else:
            log(f"LOG: {line_str}")

    proc.wait()
    active_proc = None

    target_pl = ""
    if "/playlist" in url or "playlist?list" in url:
        target_pl = song_playlist_name or detected_playlist_title.replace('(Playlist)', '').replace('(Album)', '').strip() or "Playlist"
    elif song_action in ['new_playlist', 'existing_playlist'] and song_playlist_name:
        target_pl = song_playlist_name

    if target_pl and downloaded_tracks:
        sync_playlist_to_jellyfin(target_pl, owner_id, downloaded_tracks, is_shared=is_shared)

    return proc.returncode

def main():
    if len(sys.argv) < 2:
        log("ERROR: Invalid arguments.")
        sys.exit(1)

    if sys.argv[1] == '--list-users':
        list_users()
        return

    if sys.argv[1] == '--batch':
        raw_payload = sys.argv[2]
        payload = json.loads(raw_payload)
        user_id = payload.get('user_id', '')
        user_name = payload.get('user_name', 'Kendon')
        items = payload.get('items', [])
        song_action = payload.get('song_action', 'none')
        song_playlist_name = payload.get('song_playlist_name', '').strip()
        is_shared = payload.get('is_shared', False) or user_id == '00000000000000000000000000000000'

        log("==================================================")
        log(f"▶ BATCH JOB: Initializing batch ingest ({len(items)} items) for {user_name}...")
        log(f"USER: {user_name}")
        update_state(running=True, user=user_name, title="Starting batch...", total=len(items), current=0, skipped=0, downloaded=0, track="Initializing...", status=f"▶ Starting batch ingest ({len(items)} items)...", pct=0)

        total_items = len(items)
        for idx, item in enumerate(items, 1):
            url = item.get('url', '').strip()
            item_type = item.get('type', 'url')
            if not url:
                continue

            log(f"BATCH_ITEM: {idx}|{total_items}|{item_type}|{url}")
            log(f"STATUS: 🚀 Processing [{idx}/{total_items}] {item_type.capitalize()}...")
            run_download(url, user_name, user_id, song_action, song_playlist_name, is_shared=is_shared)

        log("STATUS: 🔄 Finalizing Jellyfin & Finamp music libraries...")
        try:
            req = urllib.request.Request(f"{JELLYFIN_URL}/Items/{MUSIC_FOLDER_ID}/Refresh?api_key={JELLYFIN_TOKEN}", method="POST")
            urllib.request.urlopen(req, timeout=5)
            req_pl = urllib.request.Request(f"{JELLYFIN_URL}/Items/{PLAYLISTS_FOLDER_ID}/Refresh?api_key={JELLYFIN_TOKEN}", method="POST")
            urllib.request.urlopen(req_pl, timeout=5)
        except Exception:
            pass

        log(f"COMPLETE: Finished batch of {total_items} items for {user_name}!")
        update_state(running=False, user=user_name, title="Complete", total=total_items, current=total_items, skipped=0, downloaded=0, track="", status=f"Finished batch of {total_items} items for {user_name}!", pct=100)
        return

    url = sys.argv[1]
    target_user = sys.argv[2] if len(sys.argv) > 2 else 'shared'
    is_shared = target_user.lower() in ['shared', 'household', 'all']
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    owner_id = '00000000000000000000000000000000' if is_shared else ''
    if not is_shared:
        for row in cur.execute('SELECT Id, Username FROM Users'):
            if row[1].lower() == target_user.lower():
                owner_id = row[0].replace('-', '').lower()
                break

    log("==================================================")
    log(f"▶ SINGLE JOB: Ingesting {url} for {target_user}...")
    run_download(url, target_user, owner_id, is_shared=is_shared)
    try:
        req = urllib.request.Request(f"{JELLYFIN_URL}/Items/{MUSIC_FOLDER_ID}/Refresh?api_key={JELLYFIN_TOKEN}", method="POST")
        urllib.request.urlopen(req, timeout=5)
        req_pl = urllib.request.Request(f"{JELLYFIN_URL}/Items/{PLAYLISTS_FOLDER_ID}/Refresh?api_key={JELLYFIN_TOKEN}", method="POST")
        urllib.request.urlopen(req_pl, timeout=5)
    except Exception:
        pass
    log(f"COMPLETE: Successfully imported for {target_user}!")

if __name__ == '__main__':
    main()
