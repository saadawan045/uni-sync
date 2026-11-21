"""Start the background service (`uni-sync run`) automatically at login, on Windows, macOS or Linux.
No admin rights needed: it only touches the current user's startup settings."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .paths import MAC, WINDOWS
from .runner import ROOT

LABEL = "pk.unisync.runner"


def _pythonw() -> str:
    exe = Path(sys.executable)
    if WINDOWS:
        w = exe.with_name("pythonw.exe")
        return str(w if w.exists() else exe)
    return str(exe)


def _win_startup_file() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/uni-sync.vbs"


def _mac_plist() -> Path:
    return Path.home() / f"Library/LaunchAgents/{LABEL}.plist"


def _linux_unit() -> Path:
    return Path.home() / ".config/systemd/user/uni-sync.service"


def _stop_running(cfg) -> None:
    try:
        pid = json.loads((cfg.data_dir / "runner-status.json").read_text(encoding="utf-8"))["pid"]
    except (OSError, ValueError, KeyError):
        return
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    (cfg.data_dir / "runner-status.json").unlink(missing_ok=True)


def _start_now() -> None:
    args = [_pythonw(), "-P", "-X", "utf8", "-m", "unisync", "run"]
    if WINDOWS:
        flags = 0x00000008 | 0x08000000 | 0x00000200  # DETACHED_PROCESS | CREATE_NO_WINDOW | NEW_PROCESS_GROUP
        subprocess.Popen(args, cwd=ROOT, creationflags=flags, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(args, cwd=ROOT, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def on(cfg) -> None:
    if WINDOWS:
        f = _win_startup_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(  # write_text turns \n into \r\n on Windows
            'Set sh = CreateObject("WScript.Shell")\n'
            f'sh.CurrentDirectory = "{ROOT}"\n'
            f'sh.Run """{_pythonw()}"" -P -X utf8 -m unisync run", 0, False\n', encoding="utf-8")
        _stop_running(cfg)
        _start_now()
    elif MAC:
        f = _mac_plist()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{sys.executable}</string><string>-P</string><string>-X</string><string>utf8</string><string>-m</string><string>unisync</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
""", encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(f)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(f)], check=True)
    else:
        f = _linux_unit()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"""[Unit]
Description=uni-sync (calendar sync, runs while you're logged in)

[Service]
WorkingDirectory={ROOT}
ExecStart={sys.executable} -P -X utf8 -m unisync run
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
""", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "uni-sync.service"], check=True)
    time.sleep(3)
    print("✔ uni-sync now starts automatically when you log in, and it's running now.\n"
          "  It works while your laptop is on; anything missed while it was off or asleep is caught up.")


def off(cfg) -> None:
    if WINDOWS:
        _win_startup_file().unlink(missing_ok=True)
        _stop_running(cfg)
    elif MAC:
        f = _mac_plist()
        subprocess.run(["launchctl", "unload", "-w", str(f)], capture_output=True)
        f.unlink(missing_ok=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", "uni-sync.service"], capture_output=True)
        _linux_unit().unlink(missing_ok=True)
    print("uni-sync stopped and won't start at login. Turn it back on: uni-sync autostart on")


def enabled() -> bool:
    return (_win_startup_file() if WINDOWS else _mac_plist() if MAC else _linux_unit()).exists()
