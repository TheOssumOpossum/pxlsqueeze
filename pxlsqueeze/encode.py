"""Build and run the ffmpeg encode for one manifest item."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import ff
from .manifest import Manifest, desired_params, effective_rotation, is_trimmed
from .util import cache_dir, log

ProgressCb = Callable[[float], None]


class EncodeError(RuntimeError):
    pass


def _clean(v: str | None) -> str | None:
    return None if v in (None, "", "unknown", "unspecified", "reserved") else v


def x265_params(probe: dict[str, Any]) -> str:
    params = ["log-level=error"]
    c = probe.get("color") or {}
    prim, trc, space = _clean(c.get("primaries")), _clean(c.get("trc")), _clean(c.get("space"))
    if prim:
        params.append(f"colorprim={prim}")
    if trc:
        params.append(f"transfer={trc}")
    if space:
        params.append(f"colormatrix={space}")
    if probe.get("hdr"):
        params.append("repeat-headers=1")
    if probe.get("hdr") == "pq":
        params += ["hdr10=1", "hdr10-opt=1"]
        if probe.get("master_display"):
            params.append(f"master-display={probe['master_display']}")
        if probe.get("max_cll"):
            params.append(f"max-cll={probe['max_cll']}")
    return ":".join(params)


def build_cmd(src: Path, dst: Path, probe: dict[str, Any], params: dict[str, Any]) -> list[str]:
    start, end = params["start"], params["end"]
    duration = float(probe.get("duration") or 0)
    trim = start > 0.001 or end < duration - 0.001

    cmd = [ff.ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-nostats", "-progress", "pipe:1"]
    if trim:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if trim:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-map", "0:v:0", "-map", "0:a?"]

    rf = ff.rotation_filter(params["rotation"])
    if rf:
        cmd += ["-vf", rf]

    pix = "yuv420p10le" if int(probe.get("bit_depth") or 8) >= 10 else "yuv420p"
    if params["codec"] == "hevc":
        cmd += ["-c:v", "libx265", "-preset", str(params["preset"]), "-crf", str(params["crf"]),
                "-tag:v", "hvc1", "-x265-params", x265_params(probe)]
    elif params["codec"] == "av1":
        cmd += ["-c:v", "libsvtav1", "-preset", str(params["preset"]), "-crf", str(params["crf"]),
                "-svtav1-params", "tune=0"]
    else:
        raise EncodeError(f"unknown codec {params['codec']}")
    cmd += ["-pix_fmt", pix]

    c = probe.get("color") or {}
    for opt, key in (("-color_primaries", "primaries"), ("-color_trc", "trc"),
                     ("-colorspace", "space"), ("-color_range", "range")):
        if _clean(c.get(key)):
            cmd += [opt, c[key]]

    # passthrough + the source's own timebase keeps every frame's original timestamp;
    # the default (1/fps) would nudge variable-frame-rate frames by up to one frame.
    cmd += ["-fps_mode", "passthrough", "-enc_time_base", "demux", "-c:a", "copy",
            "-map_metadata", "0", "-movflags", "+faststart+use_metadata_tags",
            "-f", "mp4", str(dst)]
    return cmd


def encode_item(m: Manifest, item_id: str, progress: ProgressCb | None = None) -> dict[str, Any]:
    """Encode one item. Updates the manifest; returns the item."""
    it = m.get(item_id)
    src, out = m.src_path(it), m.out_path(it)
    probe = it["probe"]
    params = desired_params(it, m)
    partial = out.with_name(out.stem + ".partial.mp4")

    codec_enc = {"hevc": "libx265", "av1": "libsvtav1"}[params["codec"]]
    if not ff.has_encoder(codec_enc):
        raise EncodeError(f"Your ffmpeg build lacks {codec_enc}.")

    free = shutil.disk_usage(m.root).free
    if free < probe["size"] * 1.1:
        raise EncodeError(f"Not enough free disk space ({free / 1e9:.1f} GB free).")

    cmd = build_cmd(src, partial, probe, params)
    logfile = cache_dir(m.root, "logs") / f"{item_id}.encode.log"
    total = max(0.001, params["end"] - params["start"])
    log.info("encode %s: %s", item_id, subprocess.list2cmdline(cmd))
    t0 = time.time()
    with open(logfile, "w", encoding="utf-8") as errf:
        errf.write(subprocess.list2cmdline(cmd) + "\n\n")
        errf.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            if progress and line.startswith("out_time_us="):
                try:
                    progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / total))
                except ValueError:
                    pass
        rc = proc.wait()
    if rc != 0 or not partial.exists():
        partial.unlink(missing_ok=True)
        tail = logfile.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
        raise EncodeError("ffmpeg failed: " + " | ".join(tail))

    os.replace(partial, out)
    st = src.stat()
    os.utime(out, (st.st_atime, st.st_mtime))
    size = out.stat().st_size
    ratio = size / probe["size"]
    if progress:
        progress(1.0)

    warnings = [w for w in it.get("warnings", [])
                if not w.startswith(("Output is only", "The new file is only", "The new file came out"))]
    status = "encoded"
    changed = effective_rotation(it) != 0 or is_trimmed(it)
    if ratio > 1 - float(m.settings["min_savings"]):
        size_note = (f"The new file came out {100 * (ratio - 1):.0f}% larger" if ratio >= 1
                     else f"The new file is only {100 * (1 - ratio):.0f}% smaller")
        if it.get("keep_output"):
            warnings.append(f"{size_note}; kept because you chose Encode anyway.")
        elif changed:
            warnings.append(f"{size_note}; kept because it is rotated or trimmed.")
        else:
            out.unlink(missing_ok=True)
            status = "skipped"
            warnings.append(f"{size_note}, so the original was kept and the new file discarded.")

    m.update(item_id, save=False, encode=None, encoded_with=None)
    return m.update(
        item_id,
        status=status,
        decision=None,
        error=None,
        force_encode=False,
        warnings=warnings,
        encoded_with=params,
        encode={"codec": params["codec"], "crf": params["crf"], "size": size if status == "encoded" else None,
                "attempted_size": size, "ratio": round(ratio, 4), "seconds": round(time.time() - t0, 1),
                "encoded_at": time.time()},
    )
