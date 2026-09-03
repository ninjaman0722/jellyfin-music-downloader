import sys, re

p = "/usr/local/lib/python3.12/site-packages/spotdl/download/downloader.py"
with open(p, "r", encoding="utf-8") as f:
    c = f.read()

# Replace previous patch or original target line
old_patch_marker = "        # Global instant library index check: match all existing tracks across library in microseconds"
original_marker = "        # Reinitialize the song object if it's missing metadata"

new_patch = '''        # Global instant library index check: match all existing tracks across library in microseconds
        try:
            if self.settings.get("overwrite") == "skip" and song.name:
                if not hasattr(self, "_lib_index"):
                    import os, re
                    self._lib_index = {}
                    base_dir = Path(self.settings["output"].split("{", 1)[0]) if "{" in self.settings["output"] else Path("/music")
                    def _clean(s):
                        if not s: return ""
                        s = s.lower()
                        s = re.sub(r'\\b(feat|ft|featuring|remastered|remaster|radio edit|single version|deluxe|explicit|edition|bonus track|soundtrack|version|mix|remix)\\b.*', '', s)
                        s = re.sub(r'[\\[\\(].*?[\]\\)]', '', s)
                        return re.sub(r'[^a-z0-9]', '', s)
                    self._clean_fn = _clean
                    if base_dir.exists():
                        for root, _, files in os.walk(str(base_dir)):
                            for f in files:
                                if f.endswith(".mp3") or f.endswith(".flac"):
                                    fpath = Path(root) / f
                                    # Integrity check: ignore/purge truncated orphan files (< 350KB)
                                    try:
                                        if fpath.stat().st_size < 350000:
                                            fpath.unlink(missing_ok=True)
                                            continue
                                    except Exception:
                                        continue

                                    # Auto-extract cover.jpg for Jellyfin if missing in album folder
                                    try:
                                        c_jpg = Path(root) / "cover.jpg"
                                        f_jpg = Path(root) / "folder.jpg"
                                        if not c_jpg.exists() and not f_jpg.exists() and f.endswith(".mp3"):
                                            with open(str(fpath), "rb") as mf:
                                                mdata = mf.read(4 * 1024 * 1024)
                                            apic_i = mdata.find(b"APIC")
                                            if apic_i != -1:
                                                j_i = mdata.find(b"\\xff\\xd8\\xff", apic_i)
                                                if j_i != -1:
                                                    j_end = mdata.find(b"\\xff\\xd9", j_i) + 2
                                                    if j_end > j_i:
                                                        with open(str(c_jpg), "wb") as cf:
                                                            cf.write(mdata[j_i:j_end])
                                    except Exception:
                                        pass

                                    rel = os.path.relpath(root, str(base_dir)).split(os.sep)
                                    art = _clean(rel[0]) if len(rel) > 0 else ""
                                    stem = f.rsplit(".", 1)[0]
                                    stem_clean = re.sub(r'^\\d+\\s*-\\s*', '', stem)
                                    ttl = _clean(stem_clean)
                                    if ttl not in self._lib_index:
                                        self._lib_index[ttl] = []
                                    self._lib_index[ttl].append((art, fpath))
                
                c_title = self._clean_fn(song.name)
                c_artists = [self._clean_fn(a) for a in song.artists if a] if song.artists else []
                candidates = self._lib_index.get(c_title, [])
                for c_art, file_path in candidates:
                    if any(a and (a in c_art or c_art in a) for a in c_artists) or not any(c_artists):
                        logger.info("Skipping %s (file already exists) (duplicate)", song.display_name)
                        return song, file_path
        except Exception:
            pass

        # Reinitialize the song object if it's missing metadata'''

if old_patch_marker in c:
    parts = c.split(old_patch_marker, 1)
    sub_parts = parts[1].split(original_marker, 1)
    c = parts[0] + new_patch + sub_parts[1]
    print("Replaced previous library index patch with enhanced integrity & cover patch!")
elif original_marker in c:
    c = c.replace(original_marker, new_patch, 1)
    print("Applied library index patch to downloader.py!")
else:
    print("ERROR: Neither patch marker nor original marker found in downloader.py!")
    sys.exit(1)

with open(p, "w", encoding="utf-8") as f:
    f.write(c)

print("Successfully wrote updated downloader.py!")
