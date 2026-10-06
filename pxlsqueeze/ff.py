"""Thin wrapper around the ffmpeg / ffprobe executables.

All subprocess calls use argument lists (never a shell) so paths with spaces
work on macOS and Windows alike.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from .util import log

_BIN: dict[str, str | None] = {"ffmpeg": None, "ffprobe": None}


class FFmpegError(RuntimeError):
    pass


def set_binaries(ffmpeg: str | None = None) -> None:
    """Resolve ffmpeg/ffprobe. `ffmpeg` may be a path to ffmpeg or its folder."""
    if ffmpeg:
        p = Path(ffmpeg)
        folder = p if p.is_dir() else p.parent
        exe = ".exe" if os.name == "nt" else ""
        _BIN["ffmpeg"] = str(p if p.is_file() else folder / f"ffmpeg{exe}")
        _BIN["ffprobe"] = str(folder / f"ffprobe{exe}")
    else:
        _BIN["ffmpeg"] = shutil.which("ffmpeg")
        _BIN["ffprobe"] = shutil.which("ffprobe")
    for k, v in _BIN.items():
        if not v or not Path(v).exists():
            raise FFmpegError(
                f"Could not find {k}. Install ffmpeg (macOS: `brew install ffmpeg`, "
                f"Windows: `winget install Gyan.FFmpeg`) or pass --ffmpeg PATH."
            )
    has_filter.cache_clear()
    has_encoder.cache_clear()


def ffmpeg_bin() -> str:
    if not _BIN["ffmpeg"]:
        set_binaries()
    return _BIN["ffmpeg"]  # type: ignore[return-value]


def ffprobe_bin() -> str:
    if not _BIN["ffprobe"]:
        set_binaries()
    return _BIN["ffprobe"]  # type: ignore[return-value]


def run(args: list[str], *, capture: bool = True, text: bool = True, **kw: Any) -> subprocess.CompletedProcess:
    log.debug("run: %s", subprocess.list2cmdline(args))
    return subprocess.run(args, capture_output=capture, text=text, **kw)


def version() -> str:
    out = run([ffmpeg_bin(), "-hide_banner", "-version"]).stdout
    return out.splitlines()[0] if out else "unknown"


MIN_VERSION = (6, 1)  # -fps_mode (5.1) and -enc_time_base demux (6.1)


def version_tuple() -> tuple[int, int]:
    m = re.search(r"version n?(\d+)\.(\d+)", version())
    if m:
        return int(m.group(1)), int(m.group(2))
    # Git/nightly builds ("version 2021-08-29-git-...") carry no release number; infer it from libavcodec.
    out = run([ffmpeg_bin(), "-hide_banner", "-version"]).stdout or ""
    m = re.search(r"libavcodec\s+(\d+)\.\s*(\d+)", out)
    if not m:
        return (0, 0)
    major, minor = int(m.group(1)), int(m.group(2))
    if major >= 61:
        return (7, 0)
    if major == 60:
        return (6, 1) if minor >= 31 else (6, 0)
    return (5, 0) if major == 59 else (4, 0)


def check_version() -> None:
    if version_tuple() < MIN_VERSION:
        raise FFmpegError(
            f"ffmpeg at {ffmpeg_bin()} is too old ({version()}); pxlsqueeze needs ffmpeg "
            f"{MIN_VERSION[0]}.{MIN_VERSION[1]} or newer. Upgrade it (macOS: `brew upgrade ffmpeg`, "
            f"Windows: `winget install Gyan.FFmpeg`) or pass --ffmpeg PATH to a newer build."
        )


@functools.lru_cache(maxsize=None)
def has_filter(name: str) -> bool:
    out = run([ffmpeg_bin(), "-hide_banner", "-filters"]).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


@functools.lru_cache(maxsize=None)
def has_encoder(name: str) -> bool:
    out = run([ffmpeg_bin(), "-hide_banner", "-encoders"]).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


# --------------------------------------------------------------------------- probe

def _ratio(s: str | None) -> float | None:
    if not s or s in ("0/0", "N/A"):
        return None
    try:
        return float(Fraction(s))
    except (ValueError, ZeroDivisionError):
        return None


def _num(s: Any) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def probe_raw(path: Path) -> dict:
    res = run([ffprobe_bin(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    if res.returncode != 0:
        raise FFmpegError(f"ffprobe failed on {path.name}: {res.stderr.strip()[:500]}")
    return json.loads(res.stdout or "{}")


def first_frame_side_data(path: Path) -> list[dict]:
    res = run([
        ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
        "-show_frames", "-show_entries", "frame=side_data_list", "-of", "json", str(path),
    ])
    try:
        frames = json.loads(res.stdout or "{}").get("frames", [])
        return frames[0].get("side_data_list", []) if frames else []
    except json.JSONDecodeError:
        return []


def _rotation_of(stream: dict) -> int:
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    rot = (stream.get("tags") or {}).get("rotate")
    try:
        return int(rot) if rot is not None else 0
    except ValueError:
        return 0


def _mastering_display(side: list[dict]) -> tuple[str | None, str | None]:
    """Build x265 master-display / max-cll strings from frame side data (PQ HDR10)."""
    md, cll = None, None
    for sd in side:
        t = sd.get("side_data_type", "")
        if t.startswith("Mastering display metadata"):
            try:
                def c(k: str) -> int:
                    return int(round(float(Fraction(sd[k])) * 50000))

                def l(k: str) -> int:
                    return int(round(float(Fraction(sd[k])) * 10000))

                md = (f"G({c('green_x')},{c('green_y')})B({c('blue_x')},{c('blue_y')})"
                      f"R({c('red_x')},{c('red_y')})WP({c('white_point_x')},{c('white_point_y')})"
                      f"L({l('max_luminance')},{l('min_luminance')})")
            except (KeyError, ValueError, ZeroDivisionError):
                md = None
        elif t.startswith("Content light level"):
            try:
                cll = f"{int(sd['max_content'])},{int(sd['max_average'])}"
            except (KeyError, ValueError):
                cll = None
    return md, cll


def probe(path: Path) -> dict:
    """Probe a file and return the normalized summary stored in the manifest."""
    raw = probe_raw(path)
    streams = raw.get("streams", [])
    fmt = raw.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    if v is None:
        raise FFmpegError(f"{path.name} has no video stream")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    extra = [
        {"type": s.get("codec_type"), "codec": s.get("codec_name") or s.get("codec_tag_string")}
        for s in streams
        if s is not v and s is not a
    ]
    pix = v.get("pix_fmt") or ""
    bits = _num(v.get("bits_per_raw_sample"))
    bit_depth = int(bits) if bits else (10 if "10" in pix else 12 if "12" in pix else 8)
    rot = _rotation_of(v)
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    dw, dh = (h, w) if rot % 180 else (w, h)
    trc = v.get("color_transfer")
    hdr = "hlg" if trc == "arib-std-b67" else "pq" if trc == "smpte2084" else None
    tags = {k.lower(): val for k, val in (fmt.get("tags") or {}).items()}
    location = (tags.get("location") or tags.get("location-eng")
                or tags.get("com.apple.quicktime.location.iso6709"))
    duration = _num(fmt.get("duration")) or _num(v.get("duration")) or 0.0

    info: dict[str, Any] = {
        "vcodec": v.get("codec_name"),
        "profile": v.get("profile"),
        "pix_fmt": pix,
        "w": w,
        "h": h,
        "display_w": dw,
        "display_h": dh,
        "rotation_meta": rot,
        "fps_avg": _ratio(v.get("avg_frame_rate")),
        "fps_r": _ratio(v.get("r_frame_rate")),
        "bit_depth": bit_depth,
        "color": {
            "primaries": v.get("color_primaries"),
            "trc": trc,
            "space": v.get("color_space"),
            "range": v.get("color_range"),
        },
        "hdr": hdr,
        "hdr10plus": False,
        "master_display": None,
        "max_cll": None,
        "has_audio": a is not None,
        "acodec": a.get("codec_name") if a else None,
        "extra_streams": extra,
        "duration": duration,
        "video_bitrate": _num(v.get("bit_rate")),
        "size": path.stat().st_size,
        "creation_time": tags.get("creation_time"),
        "location": location,
    }
    if hdr:
        side = first_frame_side_data(path)
        info["hdr10plus"] = any("2094-40" in (sd.get("side_data_type") or "") or "HDR10+" in (sd.get("side_data_type") or "")
                                for sd in side)
        info["master_display"], info["max_cll"] = _mastering_display(side)
    return info


# --------------------------------------------------------------------------- filters / frames

def rotation_filter(r: int) -> str | None:
    """Extra clockwise rotation (applied after ffmpeg's own auto-rotation)."""
    r %= 360
    return {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}.get(r)


def extract_frame(path: Path, t: float, *, rotation: int = 0, max_edge: int | None = None,
                  fmt: str = "png", quality: int = 3, _retry: bool = True) -> bytes:
    """Grab one frame at time t (seconds) as PNG/JPEG bytes, auto-rotated + extra rotation."""
    filters = []
    rf = rotation_filter(rotation)
    if rf:
        filters.append(rf)
    if max_edge:
        filters.append(f"scale=w={max_edge}:h={max_edge}:force_original_aspect_ratio=decrease")
    args = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}",
            "-i", str(path), "-frames:v", "1", "-an"]
    if filters:
        args += ["-vf", ",".join(filters)]
    if fmt == "png":
        args += ["-c:v", "png", "-pix_fmt", "rgb24", "-f", "image2pipe", "-"]
    else:
        args += ["-c:v", "mjpeg", "-q:v", str(quality), "-f", "image2pipe", "-"]
    res = run(args, text=False)
    if res.returncode != 0 or not res.stdout:
        # Seeking past the last frame of a short clip yields nothing; retry slightly earlier.
        if _retry and t > 0:
            return extract_frame(path, max(0.0, t - 1.0), rotation=rotation, max_edge=max_edge, fmt=fmt,
                                 quality=quality, _retry=False)
        raise FFmpegError(f"Frame extraction failed for {path.name} at {t:.2f}s: "
                          f"{res.stderr.decode(errors='replace')[:300]}")
    return res.stdout
