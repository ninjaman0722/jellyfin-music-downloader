import sys, os, subprocess, json, urllib.request, urllib.parse, re, sqlite3, time, signal, html

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

def sanitize_title(t):
    t = re.sub(r'[\\/:*?"<>|]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def match_filename_title(f_name, short_title, clean_title):
    fn_lower = f_name.lower()
    st_lower = short_title.lower()
    ct_lower = clean_title.lower()
    if st_lower in fn_lower or ct_lower in fn_lower:
        return True
    words = [w.lower() for w in re.findall(r'\w+', ct_lower) if len(w) > 2]
    if words and all(w in fn_lower for w in words):
        return True
    return False

def artist_matches(d_name, artist, clean_artist):
    d_clean = sanitize_title(d_name).rstrip('.').lower()
    art_lower = artist.lower()
    cl_art_lower = clean_artist.lower()
    if d_name.lower() == art_lower or d_clean == cl_art_lower:
        return True
    if d_name.lower().replace(' ', '') == art_lower.replace(' ', ''):
        return True
    if d_clean.replace(' ', '') == cl_art_lower.replace(' ', ''):
        return True
    return False

def resolve_song_file(sn):
    """
    Given a song string from spotDL (e.g. 'Cannons - Fire for You' or 'Hayden James - NUMB'
    or 'Isaac Dunbar - scorton\'s creek - re-imagined by filous'),
    find the actual audio file on disk and return its Jellyfin path '/media/music/...'.
    """
    clean_sn = re.sub(r'\(feat\.[^\)]+\)', '', sn, flags=re.IGNORECASE)
    clean_sn = re.sub(r'\(with[^\)]+\)', '', clean_sn, flags=re.IGNORECASE).strip()
    parts = clean_sn.split(' - ')
    artist = parts[0].strip() if len(parts) >= 2 else ''
    title = ' - '.join(parts[1:]).strip() if len(parts) >= 2 else clean_sn
    short_title = title.split(' - ')[0].strip()
    clean_title = sanitize_title(short_title)
    clean_artist = sanitize_title(artist).rstrip('.')

    # 1. Search disk directly under artist folder
    if artist and os.path.exists(MUSIC_DIR):
        try:
            for d in os.listdir(MUSIC_DIR):
                if artist_matches(d, artist, clean_artist):
                    artist_dir = os.path.join(MUSIC_DIR, d)
                    for root, _, files in os.walk(artist_dir):
                        for f in files:
                            if f.endswith(('.mp3', '.flac', '.m4a', '.opus')) and match_filename_title(f, short_title, clean_title):
                                full_p = os.path.join(root, f)
                                return full_p.replace(MUSIC_DIR, '/media/music')
        except Exception:
            pass

    # 2. Search entire MUSIC_DIR if not found in artist folder
    if short_title and os.path.exists(MUSIC_DIR):
        try:
            for root, _, files in os.walk(MUSIC_DIR):
                for f in files:
                    if f.endswith(('.mp3', '.flac', '.m4a', '.opus')) and match_filename_title(f, short_title, clean_title):
                        if not artist or artist.lower() in root.lower() or clean_artist.lower() in root.lower():
                            full_p = os.path.join(root, f)
                            return full_p.replace(MUSIC_DIR, '/media/music')
        except Exception:
            pass

    return None

def sync_playlist_to_jellyfin(playlist_name, owner_id, downloaded_song_names, is_shared=False):
    log(f"LOG: 🔗 Synchronizing playlist '{playlist_name}' ({len(downloaded_song_names)} tracks) to Jellyfin...")
    
    # 1. Trigger Jellyfin library scan so newly downloaded tracks are registered
    try:
        req = urllib.request.Request(f"{JELLYFIN_URL}/Items/{MUSIC_FOLDER_ID}/Refresh?api_key={JELLYFIN_TOKEN}", method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

    # 2. Resolve every song to its exact Jellyfin path on disk
    resolved_paths = []
    for sn in downloaded_song_names:
        p = resolve_song_file(sn)
        if p and p not in resolved_paths:
            resolved_paths.append(p)
        elif not p:
            log(f"LOG: ⚠️ Track not yet matched on disk: {sn}")

    log(f"LOG: 📁 Matched {len(resolved_paths)} / {len(downloaded_song_names)} audio files on storage disk.")

    # 3. Resolve Jellyfin BaseItem IDs from Path with retry polling (waiting for background scanner)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    resolved_pairs = [] # (path, cid)
    missing_paths = list(resolved_paths)

    for attempt in range(6):
        still_missing = []
        for p in missing_paths:
            cur.execute("SELECT Id FROM BaseItems WHERE MediaType = 'Audio' AND Path = ?", (p,))
            row = cur.fetchone()
            if row:
                cid = row[0].replace('-', '').lower()
                resolved_pairs.append((p, cid))
            else:
                still_missing.append(p)

        if not still_missing:
            break

        if attempt < 5:
            time.sleep(2)
            conn.close()
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            missing_paths = still_missing

    item_ids = [pair[1] for pair in resolved_pairs]
    log(f"LOG: 🆔 Resolved {len(item_ids)} / {len(resolved_paths)} Jellyfin item IDs.")

    if not item_ids and not resolved_paths:
        log("LOG: ⚠️ No track paths or item IDs resolved for playlist linking.")
        return

    target_owner = '00000000000000000000000000000000' if is_shared else (owner_id or '')

    # 4. Check if playlist exists in Jellyfin
    existing_playlist_id = None
    try:
        cur.execute("SELECT Id FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Playlists.Playlist' AND Name = ?", (playlist_name,))
        row = cur.fetchone()
        if row:
            existing_playlist_id = row[0].replace('-', '').lower()
    except Exception:
        pass

    # Read existing paths in playlist.xml on disk
    existing_paths_in_xml = set()
    pl_dir = os.path.join(PLAYLISTS_DIR, playlist_name)
    xml_file = os.path.join(pl_dir, 'playlist.xml')
    if os.path.exists(xml_file):
        try:
            with open(xml_file, 'r', encoding='utf-8') as f:
                existing_paths_in_xml = {html.unescape(x).strip() for x in re.findall(r'<Path>([^<]+)</Path>', f.read())}
        except Exception:
            pass

    # Filter items that are not already present in the playlist
    new_ids = []
    for p, cid in resolved_pairs:
        if p not in existing_paths_in_xml and cid not in new_ids:
            new_ids.append(cid)

    if existing_playlist_id:
        if new_ids:
            log(f"LOG: 📌 Appending {len(new_ids)} new tracks to existing playlist '{playlist_name}' ({existing_playlist_id})...")
            ids_param = ','.join(new_ids)
            user_arg = f"&userId={owner_id}" if owner_id and owner_id != '00000000000000000000000000000000' else ""
            url = f"{JELLYFIN_URL}/Playlists/{existing_playlist_id}/Items?ids={ids_param}{user_arg}"
            req = urllib.request.Request(url, method="POST")
            req.add_header('X-Emby-Token', JELLYFIN_TOKEN)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    log(f"LOG: 🔒 Appended {len(new_ids)} tracks to '{playlist_name}' successfully!")
            except Exception as e:
                log(f"LOG: ⚠️ Playlist append note: {e}")
        else:
            log(f"LOG: ✅ Playlist '{playlist_name}' is already up to date ({len(resolved_paths)} tracks).")
    elif item_ids:
        log(f"LOG: 🆕 Creating fresh playlist '{playlist_name}' with {len(item_ids)} tracks...")
        ids_param = ','.join(item_ids)
        user_arg = f"&userId={owner_id}" if owner_id and owner_id != '00000000000000000000000000000000' else ""
        url = f"{JELLYFIN_URL}/Playlists?name={urllib.parse.quote(playlist_name)}&ids={ids_param}{user_arg}&mediaType=Audio"
        req = urllib.request.Request(url, method="POST")
        req.add_header('X-Emby-Token', JELLYFIN_TOKEN)
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read())
                log(f"LOG: 🔒 Created playlist '{playlist_name}' (ID: {res.get('Id')})!")
        except Exception as e:
            log(f"LOG: ⚠️ Playlist creation note: {e}")

    # 5. Direct verification and repair of playlist.xml on disk
    try:
        pl_dir = os.path.join(PLAYLISTS_DIR, playlist_name)
        xml_file = os.path.join(pl_dir, 'playlist.xml')
        if os.path.exists(xml_file):
            with open(xml_file, 'r', encoding='utf-8') as f:
                xml_content = f.read()

            # De-duplicate existing items in xml_content if any
            seen_p = set()
            def dedup_filter(m):
                p_val = html.unescape(m.group(1)).strip()
                if p_val in seen_p:
                    return ""
                seen_p.add(p_val)
                return m.group(0)
            xml_content = re.sub(r'\s*<PlaylistItem>\s*<Path>([^<]+)</Path>\s*</PlaylistItem>', dedup_filter, xml_content)

            existing_paths_in_xml = {html.unescape(x).strip() for x in re.findall(r'<Path>([^<]+)</Path>', xml_content)}
            paths_to_add = [p for p in resolved_paths if p not in existing_paths_in_xml]

            if paths_to_add:
                new_items_xml = "".join(f"\n    <PlaylistItem>\n      <Path>{html.escape(p)}</Path>\n    </PlaylistItem>" for p in paths_to_add)
                if '<PlaylistItems>' in xml_content:
                    xml_content = xml_content.replace('</PlaylistItems>', f"{new_items_xml}\n  </PlaylistItems>")
                else:
                    xml_content = xml_content.replace('</Item>', f"  <PlaylistItems>{new_items_xml}\n  </PlaylistItems>\n</Item>")

            if is_shared:
                xml_content = re.sub(r'<OwnerUserId>[^<]+</OwnerUserId>', '<OwnerUserId>00000000000000000000000000000000</OwnerUserId>', xml_content)

            with open(xml_file, 'w', encoding='utf-8') as f:
                f.write(xml_content)
            log(f"LOG: 📄 Playlist file synchronized with {len(resolved_paths)} total tracks on disk.")
    except Exception as e:
        log(f"LOG: ⚠️ Playlist XML update note: {e}")

    # Clean display names in BaseItems
    try:
        cur.execute("UPDATE BaseItems SET Name = SUBSTR(Name, 6) WHERE MediaType = 'Audio' AND Name GLOB '[0-9][0-9] - *'")
        conn.commit()
    except Exception:
        pass

    # Refresh playlist library in Jellyfin
    try:
        req = urllib.request.Request(f"{JELLYFIN_URL}/Items/{PLAYLISTS_FOLDER_ID}/Refresh?api_key={JELLYFIN_TOKEN}", method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def run_download(url, user_name, owner_id, song_action='none', song_playlist_name='', is_shared=False, bitrate='auto'):
    log("STAGE: discovery")
    log("STATUS: 🔍 Resolving Spotify metadata & scanning catalog...")
    log(f"LOG: 🌐 Contacting Spotify metadata API for {url}...")
    update_state(running=True, user=user_name, title=song_playlist_name or url, total=0, current=0, skipped=0, downloaded=0, track="Resolving catalog...", status="🔍 Resolving Spotify metadata & scanning catalog...", pct=0)

    cmd = [
        'docker', 'run', '--rm',
        '-e', 'HOME=/tmp',
        '-e', 'COLUMNS=1000',
        '-v', '/mnt/media/music:/music',
        '-u', '1000:1000',
        'spotdl-custom:latest',
        'download', url,
        '--threads', '2',
        '--max-retries', '3',
        '--sponsor-block',
        '--bitrate', bitrate or 'auto',
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

        skip_m = re.search(r'^Skipping\s+"?(.+?)(?:\s+\(file already|\s+\(duplicate|\s*$)', line_str, re.IGNORECASE)
        if skip_m:
            raw_s = skip_m.group(1).strip()
            raw_s = re.sub(r'\s+\(file.*$', '', raw_s)
            raw_s = raw_s.rstrip('")').strip()
            if raw_s:
                current_idx += 1
                skipped_count += 1
                song_name = raw_s
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
        user_id = payload.get('user_id', '').strip()
        user_name = payload.get('user_name', '').strip()
        items = payload.get('items', [])
        if not items and 'urls' in payload:
            items = [{'url': u, 'type': 'url'} for u in payload['urls']]
        song_action = payload.get('song_action', 'none')
        song_playlist_name = (payload.get('song_playlist_name') or payload.get('playlist_name') or '').strip()
        bitrate = payload.get('bitrate', 'auto')
        is_shared = payload.get('is_shared', False) or user_id == '00000000000000000000000000000000'

        if (not user_id or user_name == 'No User Selected') and not is_shared:
            try:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("SELECT Id, Username FROM Users WHERE LOWER(Username) != 'user' LIMIT 1")
                u_row = cur.fetchone()
                if u_row:
                    user_id = u_row[0].replace('-', '').lower()
                    if not user_name or user_name == 'No User Selected':
                        user_name = u_row[1]
            except Exception:
                pass

        if not user_name or user_name == 'No User Selected':
            user_name = "Household (Shared)" if is_shared else "User"

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
            run_download(url, user_name, user_id, song_action, song_playlist_name, is_shared=is_shared, bitrate=bitrate)

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
