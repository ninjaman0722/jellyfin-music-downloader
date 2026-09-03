#!/usr/bin/env python3
import sys
import time
import os
import re

log_path = os.path.expanduser("~/.local/state/omarchy/extensions/jellyfin-music-app/ingest.log")
filter_re = re.compile(r"^(PROGRESS|TRACK|COUNTS|STATUS|STAGE|TOTAL|BATCH_ITEM):")

with open(log_path, "a", encoding="utf-8") as f:
    for line in sys.stdin:
        # Pass every raw line through to stdout so Quickshell GUI receives full protocol
        sys.stdout.write(line)
        sys.stdout.flush()

        s = line.strip()
        # Only write human-readable lines to the desktop log file
        if s and not filter_re.match(s):
            if s.startswith("LOG: "):
                s = s[5:]
            ts = time.strftime("[%H:%M:%S]")
            f.write(f"{ts} {s}\n")
            f.flush()
