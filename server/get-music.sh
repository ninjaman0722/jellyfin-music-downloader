#!/bin/bash
if [ -z "$1" ]; then
  echo "Usage: ~/spotdl/get-music.sh <spotify_or_ytmusic_url> [user_name|shared]"
  echo "Run 'python3 ~/spotdl/ingest.py --list-users' to view available accounts."
  exit 1
fi

python3 "$(dirname "$0")/ingest.py" "$@"
