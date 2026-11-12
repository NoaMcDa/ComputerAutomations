from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "price-watcher"


def data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs"
    elif sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) / "Logs" if local else Path.home() / "AppData" / "Local" / "Logs"
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_file() -> Path:
    return data_dir() / "state.json"


def watchlist_file() -> Path:
    return data_dir() / "watchlist.json"


def lock_file() -> Path:
    return data_dir() / "price-watcher.lock"


def raw_response_dir() -> Path:
    path = data_dir() / "raw-responses"
    path.mkdir(parents=True, exist_ok=True)
    return path
