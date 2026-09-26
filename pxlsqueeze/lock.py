"""One pxlsqueeze per folder.

Uses an OS-level lock on DIR/.pxlsqueeze/lock (flock on macOS/Linux,
msvcrt.locking on Windows). The OS releases it automatically when the process
exits, even after a crash or a force-quit, so there are no stale locks to clean
up. A small JSON file next to it records who holds the lock, for the message
shown to a second copy.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import IO, Any

from .util import atomic_write_json, work_dir

_held: dict[str, IO[str]] = {}


class FolderBusy(RuntimeError):
    def __init__(self, root: Path, holder: dict[str, Any] | None):
        self.root, self.holder = root, holder or {}
        h = self.holder
        who = f"`pxlsqueeze {h['command']}`" if h.get("command") else "Another pxlsqueeze"
        since = ""
        if h.get("started"):
            since = f", started {time.strftime('%a %H:%M', time.localtime(h['started']))}"
        pid = f", process {h['pid']}" if h.get("pid") else ""
        super().__init__(
            f"{who} is already working on this folder{since}{pid}. Running two at once makes them "
            f"overwrite each other's work. Wait for it to finish, or stop it with Ctrl+C in its "
            f"terminal, then try again."
        )


def _try_lock(f: IO[str]) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def acquire(root: Path, command: str) -> None:
    root = Path(root).resolve()
    key = str(root)
    if key in _held:
        return
    d = work_dir(root)
    f = open(d / "lock", "a+", encoding="utf-8")  # noqa: SIM115 - held for the process lifetime
    if not _try_lock(f):
        f.close()
        holder = None
        try:
            holder = json.loads((d / "lock.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        raise FolderBusy(root, holder)
    _held[key] = f
    atomic_write_json(d / "lock.json", {
        "command": command, "pid": os.getpid(), "host": socket.gethostname(),
        "started": time.time(), "argv": sys.argv[1:],
    })


def release(root: Path) -> None:
    f = _held.pop(str(Path(root).resolve()), None)
    if f is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    f.close()
