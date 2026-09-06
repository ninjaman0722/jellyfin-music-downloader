#!/usr/bin/env python3
"""WebSocket Companion Client for Quickshell QML.

Streams JSON event frames line-by-line to stdout for consumption by
Quickshell.Io.SplitParser, bypassing missing QML QtWebSockets plugin.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

try:
    import websockets
except ImportError:
    venv_py = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python3"
    if venv_py.is_file() and str(venv_py) != sys.executable:
        os.execv(str(venv_py), [str(venv_py)] + sys.argv)
    import websockets


async def listen(url: str):
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=15) as ws:
                # Connected handshake
                sys.stdout.write(json.dumps({"event": "ws_connected", "url": url}) + "\n")
                sys.stdout.flush()

                async for message in ws:
                    sys.stdout.write(message.strip() + "\n")
                    sys.stdout.flush()
        except (websockets.ConnectionClosed, OSError) as exc:
            err_frame = json.dumps({"event": "ws_disconnected", "error": str(exc)})
            sys.stdout.write(err_frame + "\n")
            sys.stdout.flush()
            await asyncio.sleep(2.0)
        except Exception:
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    target_url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8095/ws/events"
    try:
        asyncio.run(listen(target_url))
    except KeyboardInterrupt:
        pass
