"""Small shared helpers."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("pxlsqueeze")
log.addHandler(logging.NullHandler())
log.propagate = False

WORK_DIRNAME = ".pxlsqueeze"


def user_cache_dir() -> Path:
    """Per-user cache directory for downloaded models (not per video folder)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    d = base / "pxlsqueeze"
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_dir(root: Path) -> Path:
    d = root / WORK_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir(root: Path, *parts: str) -> Path:
    d = work_dir(root) / "cache"
    for p in parts:
        d = d / p
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_logging(root: Path, verbose: bool = False) -> None:
    logfile = work_dir(root) / "pxlsqueeze.log"
    handler = logging.FileHandler(logfile, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers.clear()
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def human_bytes(n: float | int | None) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def human_seconds(s: float | None) -> str:
    if s is None:
        return "–"
    s = max(0.0, float(s))
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:04.1f}"


def even(n: float) -> int:
    v = int(round(n))
    return v - (v % 2)
