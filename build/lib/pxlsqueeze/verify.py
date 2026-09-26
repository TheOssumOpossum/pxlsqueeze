"""Post-encode checks: full decode, duration, and perceptual quality."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from . import ff
from .manifest import Manifest
from .util import even, log

VMAF_MEAN_MIN = 95.0
VMAF_P1_MIN = 90.0
SSIM_MEAN_MIN = 0.97
SSIM_P1_MIN = 0.94


class VerifyError(RuntimeError):
    pass


def decode_ok(path: Path) -> tuple[bool, str]:
    """Decode every video frame. `-enc_time_base demux` keeps the check from rounding
    variable-frame-rate timestamps to 1/fps, which made two frames collide and ffmpeg
    report bogus "non monotonically increasing dts" errors on perfectly good files."""
    res = ff.run([ff.ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
                  "-map", "0:v:0", "-enc_time_base", "demux", "-f", "null", "-"])
    err = (res.stderr or "").strip()
    return res.returncode == 0 and not err, err[:500]


def video_duration(path: Path) -> float:
    raw = ff.probe_raw(path)
    v = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), {})
    d = v.get("duration") or raw.get("format", {}).get("duration")
    return float(d or 0)


def metric_name() -> str:
    return "vmaf" if ff.has_filter("libvmaf") else "ssim"


def _compare_size(w: int, h: int) -> tuple[int, int]:
    """Fit within 1920x1080 (either orientation); VMAF's default model targets 1080p."""
    f = min(1.0, 1920 / max(w, h), 1080 / min(w, h))
    return even(w * f), even(h * f)


def quality(src: Path, out: Path, params: dict[str, Any], out_w: int, out_h: int,
            bit_depth: int, duration: float) -> dict[str, Any]:
    metric = metric_name()
    W, H = _compare_size(out_w, out_h)
    fmt = "yuv420p10le" if bit_depth >= 10 else "yuv420p"
    rot = ff.rotation_filter(params["rotation"])
    ref = ",".join(x for x in (rot, f"scale={W}:{H}:flags=bicubic", f"format={fmt}", "setpts=PTS-STARTPTS") if x)
    dist = f"scale={W}:{H}:flags=bicubic,format={fmt},setpts=PTS-STARTPTS"
    start, end = params["start"], params["end"]
    trim = start > 0.001 or end < duration - 0.001

    with tempfile.TemporaryDirectory(prefix="pxlsq-") as tmp:
        if metric == "vmaf":
            threads = max(1, (os.cpu_count() or 2) - 1)
            cmp = f"libvmaf=n_subsample=5:n_threads={threads}:log_fmt=json:log_path=q.json"
        else:
            cmp = "ssim=stats_file=q.log"
        graph = f"[0:v]{ref}[ref];[1:v]{dist}[dist];[dist][ref]{cmp}"
        cmd = [ff.ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error"]
        if trim:
            cmd += ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}"]
        cmd += ["-i", str(src), "-i", str(out), "-lavfi", graph, "-f", "null", "-"]
        # Relative log paths avoid filter-argument escaping of Windows drive letters.
        res = ff.run(cmd, cwd=tmp)
        if res.returncode != 0:
            raise VerifyError(f"{metric} failed: {(res.stderr or '').strip()[:400]}")
        if metric == "vmaf":
            data = json.loads(Path(tmp, "q.json").read_text(encoding="utf-8"))
            vals = np.array([f["metrics"]["vmaf"] for f in data.get("frames", [])], dtype=float)
            mean = float(data["pooled_metrics"]["vmaf"]["mean"])
        else:
            text = Path(tmp, "q.log").read_text(encoding="utf-8")
            vals = np.array([float(x) for x in re.findall(r"All:([0-9.]+)", text)], dtype=float)
            mean = float(vals.mean()) if len(vals) else 0.0
    if len(vals) == 0:
        raise VerifyError(f"{metric} produced no frames")
    p1 = float(np.percentile(vals, 1))
    if metric == "vmaf":
        flagged = mean < VMAF_MEAN_MIN or p1 < VMAF_P1_MIN
        return {"metric": "vmaf", "mean": round(mean, 2), "p1": round(p1, 2), "flag": flagged}
    flagged = mean < SSIM_MEAN_MIN or p1 < SSIM_P1_MIN
    return {"metric": "ssim", "mean": round(mean, 4), "p1": round(p1, 4), "flag": flagged}


def verify_item(m: Manifest, item_id: str) -> dict[str, Any]:
    it = m.get(item_id)
    if it["status"] != "encoded":
        return it
    src, out = m.src_path(it), m.out_path(it)
    params = it["encoded_with"]
    ok, err = decode_ok(out)
    if not ok:
        raise VerifyError(f"Output does not decode cleanly: {err}")

    expected = params["end"] - params["start"]
    got = video_duration(out)
    fps = float(it["probe"].get("fps_avg") or 30)
    tol = 0.1 + 1.0 / max(fps, 1)
    if abs(got - expected) > tol:
        raise VerifyError(f"Output is {got:.2f}s long, expected {expected:.2f}s.")

    oprobe = ff.probe(out)
    q = quality(src, out, params, oprobe["w"], oprobe["h"], int(it["probe"].get("bit_depth") or 8),
                float(it["probe"]["duration"]))
    log.info("verify %s: %s", item_id, q)
    enc = dict(it["encode"] or {})
    enc.update({"quality": q, "out_w": oprobe["w"], "out_h": oprobe["h"], "out_duration": round(got, 3)})
    return m.update(item_id, status="verified", encode=enc, error=None)
