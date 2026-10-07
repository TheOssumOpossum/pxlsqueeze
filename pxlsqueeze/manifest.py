"""Persistent per-directory state in DIR/.pxlsqueeze/manifest.json.

Pipeline status (``status``):
    pending -> analyzed -> encoded -> verified
    plus: skipped (no savings / output already existed), error, finalized
User decision (``decision``): None | "approved" | "rejected" | "trash"
    approved: replace the original with the new file at finalize
    rejected: keep the original, delete the new file
    trash:    move the original (and any new file) to the Trash at finalize

An item is *stale* when the effective rotation/trim/encoder settings differ from
what its current output was encoded with; stale items need a re-encode before
they can be approved.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .util import atomic_write_json, log, work_dir

SCHEMA_VERSION = 1

PXL_RE = re.compile(r"^PXL_\d{8}_\d{9}\.mp4$", re.IGNORECASE)
PXL_VARIANT_RE = re.compile(r"^PXL_\d{8}_\d{9}(\.[A-Z0-9_]+)+\.mp4$", re.IGNORECASE)

DEFAULT_SETTINGS: dict[str, Any] = {
    "codec": "hevc",
    "crf": {"hevc": 20, "av1": 28},
    "preset": {"hevc": "slow", "av1": "5"},
    "min_savings": 0.10,  # output must be at least 10% smaller unless rotated/trimmed
    "short_clip_seconds": 3.0,  # clips this short (or shorter) are suggested for the Trash
}


def output_name(source_name: str) -> str:
    p = Path(source_name)
    return f"{p.stem}~2{p.suffix}"


def new_item(src: Path) -> dict[str, Any]:
    return {
        "id": src.stem,
        "source": src.name,
        "output": output_name(src.name),
        "status": "pending",
        "decision": None,
        "probe": None,
        "rotation": {"auto": 0, "confidence": 0.0, "method": None, "override": None, "face_presence": 0.0},
        "trim": {"speech": [], "vad": None, "is_talking": False, "start": None, "end": None,
                 "face_presence": None, "override": None},
        "encode": None,
        "encoded_with": None,
        "warnings": [],
        "error": None,
        "updated_at": time.time(),
    }


class Manifest:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = work_dir(self.root) / "manifest.json"
        self.lock = threading.RLock()
        self.items: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        with self.lock:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.items = {it["id"]: it for it in data.get("items", [])}
                saved = data.get("settings") or {}
                for k, v in saved.items():
                    if isinstance(v, dict) and isinstance(self.settings.get(k), dict):
                        self.settings[k].update(v)
                    else:
                        self.settings[k] = v

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, {
                "schema": SCHEMA_VERSION,
                "settings": self.settings,
                "items": [self.items[k] for k in sorted(self.items)],
            })

    # ------------------------------------------------------------------ access
    def __iter__(self) -> Iterator[dict[str, Any]]:
        with self.lock:
            return iter([self.items[k] for k in sorted(self.items)])

    def get(self, item_id: str) -> dict[str, Any]:
        with self.lock:
            return self.items[item_id]

    def update(self, item_id: str, save: bool = True, **fields: Any) -> dict[str, Any]:
        with self.lock:
            it = self.items[item_id]
            for k, v in fields.items():
                if isinstance(v, dict) and isinstance(it.get(k), dict):
                    it[k].update(v)
                else:
                    it[k] = v
            it["updated_at"] = time.time()
            if save:
                self.save()
            return it

    def src_path(self, it: dict) -> Path:
        return self.root / it["source"]

    def out_path(self, it: dict) -> Path:
        return self.root / it["output"]

    # ------------------------------------------------------------------ discover
    def discover(self, include_variants: bool = False) -> list[str]:
        """Add new PXL_*.mp4 files; return ids of newly discovered items."""
        new_ids = []
        with self.lock:
            for p in sorted(self.root.iterdir()):
                if not p.is_file() or "~" in p.stem:
                    continue
                if not (PXL_RE.match(p.name) or (include_variants and PXL_VARIANT_RE.match(p.name))):
                    continue
                if p.stem in self.items:
                    continue
                it = new_item(p)
                if (self.root / it["output"]).exists():
                    it["status"] = "skipped"
                    it["warnings"].append("An output file already existed before pxlsqueeze saw this video; left untouched.")
                    log.warning("skip %s: output already exists", p.name)
                self.items[it["id"]] = it
                new_ids.append(it["id"])
            # Sources that disappeared (and weren't finalized) are marked, not dropped.
            for it in self.items.values():
                if it["status"] != "finalized" and not (self.root / it["source"]).exists():
                    it["status"] = "error"
                    it["error"] = "Source file is missing."
            self.save()
        return new_ids

    def cleanup_partials(self) -> None:
        for p in self.root.glob("*.partial.mp4"):
            try:
                p.unlink()
                log.info("removed leftover partial %s", p.name)
            except OSError:
                pass

    # ------------------------------------------------------------------ effective values
    def encoder_params(self) -> dict[str, Any]:
        codec = self.settings["codec"]
        return {"codec": codec, "crf": self.settings["crf"][codec], "preset": self.settings["preset"][codec]}


def effective_rotation(it: dict) -> int:
    r = it["rotation"]
    return int(r["override"] if r.get("override") is not None else r.get("auto") or 0) % 360


def effective_trim(it: dict) -> tuple[float, float]:
    """(start, end) in source seconds."""
    dur = float((it.get("probe") or {}).get("duration") or 0.0)
    t = it["trim"]
    ov = t.get("override")
    if ov:
        s, e = ov.get("start", 0.0), ov.get("end", dur)
    elif t.get("is_talking") and t.get("start") is not None:
        s, e = t["start"], t["end"]
    else:
        s, e = 0.0, dur
    s = max(0.0, min(float(s), dur))
    e = max(s, min(float(e), dur))
    return round(s, 3), round(e, 3)


def is_trimmed(it: dict) -> bool:
    dur = float((it.get("probe") or {}).get("duration") or 0.0)
    s, e = effective_trim(it)
    return s > 0.001 or e < dur - 0.001


def desired_params(it: dict, m: Manifest) -> dict[str, Any]:
    s, e = effective_trim(it)
    return {"rotation": effective_rotation(it), "start": s, "end": e, **m.encoder_params()}


def is_short(it: dict, m: Manifest) -> bool:
    d = (it.get("probe") or {}).get("duration")
    return d is not None and float(d) <= float(m.settings.get("short_clip_seconds", 3.0))


def is_stale(it: dict, m: Manifest) -> bool:
    ew = it.get("encoded_with")
    return ew is None or ew != desired_params(it, m)


def needs_encode(it: dict, m: Manifest) -> bool:
    if it["status"] in ("finalized", "pending") or it.get("decision") in ("rejected", "trash"):
        return False
    if it.get("probe") is None or not (m.root / it["source"]).exists():
        return False
    if it["status"] == "error" or it.get("force_encode"):
        return True  # retry failures; or the user asked to (re-)encode as-is
    if it["status"] == "skipped" and it.get("encoded_with") is None:
        return False  # pre-existing output; never overwrite
    return is_stale(it, m)


def project_summary(m: Manifest) -> dict[str, Any]:
    """Every video lands in exactly one bucket, so the counts add up to the total.

    encoded: has a new file (awaiting a decision, approved, or finalized)
    trashed: marked for the Trash, or already trashed
    kept:    original kept as is (rejected, or re-encoding didn't save enough)
    todo:    still needs analysis or encoding, has unencoded changes, or failed
    """
    c = {"total": 0, "encoded": 0, "trashed": 0, "kept": 0, "todo": 0,
         "freed_bytes": 0, "pending_bytes": 0}
    for it in m:
        c["total"] += 1
        src = int((it.get("probe") or {}).get("size") or 0)
        out = int((it.get("encode") or {}).get("size") or 0)
        done = it["status"] == "finalized"
        if it.get("decision") == "trash":
            c["trashed"] += 1
            c["freed_bytes" if done else "pending_bytes"] += src
        elif done:
            c["encoded"] += 1
            c["freed_bytes"] += src - out
        elif it.get("decision") == "rejected" or (it["status"] == "skipped" and not needs_encode(it, m)):
            c["kept"] += 1
        elif it["status"] in ("verified", "encoded") and not needs_encode(it, m):
            c["encoded"] += 1
            c["pending_bytes"] += max(0, src - out)
        else:
            c["todo"] += 1
    return c
