"""Downloads small ONNX models once into the per-user cache.

Both models are optional: if a download fails, pxlsqueeze falls back to
OpenCV's bundled Haar face cascade and to an energy-based speech detector,
and says so in the item's analysis method.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path

from .util import log, user_cache_dir

MODELS = {
    "yunet": {
        "file": "face_detection_yunet_2023mar.onnx",
        "urls": [
            "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
            "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        ],
        "min_size": 200_000,
    },
    "silero_vad": {
        "file": "silero_vad.onnx",
        "urls": [
            "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx",
        ],
        "min_size": 1_000_000,
    },
}

_failed: set[str] = set()


def model_path(name: str) -> Path | None:
    """Return a local path to the model, downloading it if needed; None if unavailable."""
    spec = MODELS[name]
    env = os.environ.get(f"PXLSQUEEZE_{name.upper()}_MODEL")
    if env and Path(env).exists():
        return Path(env)
    dest = user_cache_dir() / spec["file"]
    if dest.exists() and dest.stat().st_size >= spec["min_size"]:
        return dest
    if name in _failed:
        return None
    for url in spec["urls"]:
        try:
            log.info("downloading %s from %s", name, url)
            tmp = dest.with_suffix(".part")
            with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 16):
                    f.write(chunk)
            # Git LFS pointer files are tiny text files: reject them.
            if tmp.stat().st_size < spec["min_size"]:
                tmp.unlink(missing_ok=True)
                continue
            os.replace(tmp, dest)
            log.info("%s saved (%s)", name, hashlib.sha256(dest.read_bytes()).hexdigest()[:12])
            return dest
        except Exception as e:  # noqa: BLE001 - any network failure means fall back
            log.warning("download of %s from %s failed: %s", name, url, e)
    _failed.add(name)
    return None
