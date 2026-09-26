"""Per-item stage runners and a background job queue for the review UI."""

from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Any, Callable

from . import ff, orientation, speech
from .encode import EncodeError, encode_item
from .manifest import Manifest, needs_encode
from .util import log
from .verify import VerifyError, verify_item

_detector: orientation.FaceDetector | None = None


def detector() -> orientation.FaceDetector:
    global _detector
    if _detector is None:
        _detector = orientation.FaceDetector()
    return _detector


def analyze_item(m: Manifest, item_id: str, use_clip: bool = True) -> dict[str, Any]:
    it = m.get(item_id)
    src = m.src_path(it)
    try:
        probe = ff.probe(src)
        warnings = [w for w in it.get("warnings", []) if "HDR10+" not in w and "extra stream" not in w]
        if probe.get("hdr10plus"):
            warnings.append("HDR10+ dynamic metadata will not be carried over (the static HDR look is kept).")
        if probe.get("extra_streams"):
            kinds = ", ".join(sorted({str(s["codec"]) for s in probe["extra_streams"]}))
            warnings.append(f"Dropping extra stream(s) not needed for playback: {kinds}.")
        rot = orientation.analyze(src, probe, detector(), use_clip=use_clip)
        trim = speech.analyze(src, probe, rot["face_presence"])
        prev = it["rotation"].get("override"), it["trim"].get("override")
        m.update(item_id, save=False, rotation=None, trim=None)
        return m.update(
            item_id,
            probe=probe,
            rotation={**rot, "override": prev[0]},
            trim={**trim, "override": prev[1]},
            warnings=warnings,
            status="analyzed",
            error=None,
        )
    except Exception as e:  # noqa: BLE001 - keep the batch going
        log.error("analyze %s failed: %s\n%s", item_id, e, traceback.format_exc())
        return m.update(item_id, status="error", error=f"Analysis failed: {e}")


def encode_and_verify(m: Manifest, item_id: str, progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    try:
        it = encode_item(m, item_id, progress)
        if it["status"] == "encoded":
            it = verify_item(m, item_id)
        return it
    except (EncodeError, VerifyError, ff.FFmpegError) as e:
        log.error("encode/verify %s failed: %s", item_id, e)
        return m.update(item_id, status="error", error=str(e))
    except Exception as e:  # noqa: BLE001
        log.error("encode/verify %s crashed: %s\n%s", item_id, e, traceback.format_exc())
        return m.update(item_id, status="error", error=f"Unexpected error: {e}")


FALSE_POSITIVE = "non monotonically increasing dts"


def recover_false_positives(m: Manifest) -> list[str]:
    """Videos that failed only because of the pre-0.2 decode check go back to
    'encoded' so they're checked again with the fixed check, without re-encoding."""
    fixed = []
    for it in m:
        err = it.get("error") or ""
        if (it["status"] == "error" and FALSE_POSITIVE in err and it.get("encoded_with")
                and it.get("encode") and m.out_path(it).exists()):
            m.update(it["id"], save=False, status="encoded", error=None)
            fixed.append(it["id"])
    return fixed


def verify_only(m: Manifest, item_id: str) -> dict[str, Any]:
    try:
        return verify_item(m, item_id)
    except (VerifyError, ff.FFmpegError) as e:
        log.error("verify %s failed: %s", item_id, e)
        return m.update(item_id, status="error", error=str(e))


def pending_analysis(m: Manifest) -> list[str]:
    return [it["id"] for it in m if it["status"] == "pending"
            or (it["status"] == "error" and it.get("probe") is None and m.src_path(it).exists())]


def pending_encode(m: Manifest) -> list[str]:
    return [it["id"] for it in m if needs_encode(it, m)]


def pending_verify(m: Manifest) -> list[str]:
    return [it["id"] for it in m if it["status"] == "encoded"]


class JobQueue:
    """Single background worker so the UI can queue re-encodes without blocking."""

    def __init__(self, m: Manifest):
        self.m = m
        self.q: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.queued: list[tuple[str, str]] = []
        self.current: dict[str, Any] | None = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        for i in pending_verify(m):  # e.g. re-checks queued by recover_false_positives
            self.submit("verify", i)

    def submit(self, kind: str, item_id: str) -> None:
        with self.lock:
            if (kind, item_id) in self.queued or (self.current and self.current["id"] == item_id
                                                  and self.current["kind"] == kind):
                return
            self.queued.append((kind, item_id))
        self.q.put((kind, item_id))

    def state(self) -> dict[str, Any]:
        with self.lock:
            return {"current": dict(self.current) if self.current else None,
                    "queued": [{"kind": k, "id": i} for k, i in self.queued]}

    def _run(self) -> None:
        while True:
            kind, item_id = self.q.get()
            with self.lock:
                if (kind, item_id) in self.queued:
                    self.queued.remove((kind, item_id))
                self.current = {"kind": kind, "id": item_id, "progress": 0.0, "started": time.time()}

            def prog(p: float) -> None:
                with self.lock:
                    if self.current:
                        self.current["progress"] = round(p, 3)

            try:
                if kind == "analyze":
                    analyze_item(self.m, item_id)
                elif kind == "verify":
                    if self.m.get(item_id)["status"] == "encoded":
                        verify_only(self.m, item_id)
                elif kind == "encode":
                    it = self.m.get(item_id)
                    if needs_encode(it, self.m):
                        encode_and_verify(self.m, item_id, prog)
            finally:
                with self.lock:
                    self.current = None
