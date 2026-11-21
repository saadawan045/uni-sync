"""Where uni-sync keeps its config and data on each operating system.

Linux:   ~/.config/uni-sync          ~/.local/share/uni-sync
macOS:   ~/Library/Application Support/uni-sync   (config and data together)
Windows: %APPDATA%\\uni-sync          %LOCALAPPDATA%\\uni-sync

UNISYNC_CONFIG (a file) and UNISYNC_DATA (a folder) override these.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"


def config_dir() -> Path:
    if os.environ.get("UNISYNC_CONFIG"):
        return Path(os.environ["UNISYNC_CONFIG"]).expanduser().parent
    if WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "uni-sync"
    if MAC:
        return Path.home() / "Library/Application Support/uni-sync"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "uni-sync"


def config_file() -> Path:
    if os.environ.get("UNISYNC_CONFIG"):
        return Path(os.environ["UNISYNC_CONFIG"]).expanduser()
    return config_dir() / "config.toml"


def default_data_dir() -> Path:
    if os.environ.get("UNISYNC_DATA"):
        return Path(os.environ["UNISYNC_DATA"]).expanduser()
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "uni-sync"
    if MAC:
        return Path.home() / "Library/Application Support/uni-sync/data"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "uni-sync"


def private(p: Path) -> None:
    """Owner-only permissions. On Windows the per-user AppData folders are already private."""
    if not WINDOWS:
        try:
            p.chmod(0o700 if p.is_dir() else 0o600)
        except OSError:
            pass
