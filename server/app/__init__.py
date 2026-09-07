"""Jellyfin Music Downloader V2 - Asynchronous Daemon Application Package."""

from __future__ import annotations

import sys
import types

__version__ = "2.1.0"

# Compatibility shim: Ensure imports like 'from server.app.x import y'
# succeed whether the daemon is executed from the repo root or from within the server package/container.
if "server" not in sys.modules:
    sys.modules["server"] = types.ModuleType("server")
if "server.app" not in sys.modules:
    sys.modules["server.app"] = sys.modules[__name__]
    setattr(sys.modules["server"], "app", sys.modules[__name__])
