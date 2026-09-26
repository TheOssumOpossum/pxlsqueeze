"""CRF sweep on short segments of a few videos, to pick a default quality level."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterator

from . import ff
from .encode import build_cmd
from .manifest import Manifest
from .verify import quality

SEGMENT = 10.0


def pick_samples(m: Manifest, n: int) -> list[dict[str, Any]]:
    items = [it for it in m if it.get("probe") and m.src_path(it).exists()]
    if not items:
        return []
    # Prefer variety: one per (codec, hdr) combo first, then spread by position.
    seen, picked = set(), []
    for it in items:
        key = (it["probe"]["vcodec"], it["probe"].get("hdr"))
        if key not in seen:
            seen.add(key)
            picked.append(it)
    step = max(1, len(items) // max(1, n))
    for it in items[::step]:
        if len(picked) >= n:
            break
        if it not in picked:
            picked.append(it)
    return picked[:n]


def run(m: Manifest, crfs: list[int], samples: int = 4) -> Iterator[dict[str, Any]]:
    codec = m.settings["codec"]
    preset = m.settings["preset"][codec]
    for it in pick_samples(m, samples):
        probe = it["probe"]
        dur = float(probe["duration"])
        seg = min(SEGMENT, dur)
        start = max(0.0, dur / 2 - seg / 2)
        src = m.src_path(it)
        src_seg_bytes = probe["size"] * seg / dur
        for crf in crfs:
            params = {"rotation": 0, "start": round(start, 3), "end": round(start + seg, 3),
                      "codec": codec, "crf": crf, "preset": preset}
            with tempfile.TemporaryDirectory(prefix="pxlsq-cal-") as tmp:
                out = Path(tmp) / "cal.mp4"
                cmd = [a for a in build_cmd(src, out, probe, params)]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    yield {"id": it["id"], "crf": crf, "error": res.stderr.strip()[-200:]}
                    continue
                o = ff.probe(out)
                q = quality(src, out, params, o["w"], o["h"], int(probe.get("bit_depth") or 8), dur)
                yield {"id": it["id"], "vcodec": probe["vcodec"], "hdr": probe.get("hdr"), "crf": crf,
                       "ratio": out.stat().st_size / src_seg_bytes, **{f"q_{k}": v for k, v in q.items()}}
