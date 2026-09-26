"""Finalize decisions: trash originals of approved videos (the new file replaces
them) and trash videos marked for the Trash entirely."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .manifest import Manifest, is_stale
from .util import human_bytes, log, work_dir
from .verify import decode_ok, video_duration


def approved_ready(m: Manifest) -> list[dict[str, Any]]:
    return [it for it in m if it.get("decision") == "approved" and it["status"] == "verified"
            and not is_stale(it, m)]


def marked_trash(m: Manifest) -> list[dict[str, Any]]:
    return [it for it in m if it.get("decision") == "trash" and it["status"] != "finalized"
            and m.src_path(it).exists()]


def _out_size(m: Manifest, it: dict) -> int:
    out = m.out_path(it)
    return out.stat().st_size if out.exists() and it.get("encoded_with") else 0


def summary(m: Manifest) -> dict[str, Any]:
    appr = approved_ready(m)
    trash = marked_trash(m)
    src_bytes = sum(it["probe"]["size"] for it in appr)
    out_bytes = sum((it.get("encode") or {}).get("size") or 0 for it in appr)
    trash_bytes = sum(m.src_path(it).stat().st_size + _out_size(m, it) for it in trash)
    return {
        "count": len(appr) + len(trash),
        "approved": len(appr),
        "trash": len(trash),
        "source_bytes": src_bytes,
        "output_bytes": out_bytes,
        "freed_bytes": src_bytes - out_bytes + trash_bytes,
    }


def _remove(path: Path, permanent: bool) -> None:
    from send2trash import send2trash

    if permanent:
        path.unlink()
    else:
        send2trash(str(path))


def finalize(m: Manifest, permanent: bool = False, full_decode: bool = True) -> dict[str, Any]:
    done, failed = [], []
    logpath = work_dir(m.root) / f"finalize-{time.strftime('%Y%m%d-%H%M%S')}.log"
    verb = "DELETED" if permanent else "TRASHED"
    lines = []

    for it in approved_ready(m):
        src, out = m.src_path(it), m.out_path(it)
        problem = None
        if not out.exists() or out.stat().st_size < 1024:
            problem = "output missing or empty"
        elif out.stat().st_size != (it.get("encode") or {}).get("size"):
            problem = "output changed since it was verified"
        else:
            exp = it["encoded_with"]["end"] - it["encoded_with"]["start"]
            if abs(video_duration(out) - exp) > 0.25:
                problem = "output duration no longer matches"
            elif full_decode:
                ok, err = decode_ok(out)
                if not ok:
                    problem = f"output no longer decodes: {err[:120]}"
        if problem is None and not src.exists():
            problem = "original already gone"
        if problem:
            failed.append({"id": it["id"], "reason": problem})
            lines.append(f"KEPT    {src.name}: {problem}")
            continue
        try:
            _remove(src, permanent)
        except Exception as e:  # noqa: BLE001
            failed.append({"id": it["id"], "reason": str(e)})
            lines.append(f"FAILED  {src.name}: {e}")
            continue
        m.update(it["id"], save=False, status="finalized", finalized_at=time.time())
        done.append(it["id"])
        lines.append(f"{verb} {src.name} ({human_bytes(it['probe']['size'])}), "
                     f"replaced by {out.name} ({human_bytes(it['encode']['size'])})")

    for it in marked_trash(m):
        src, out = m.src_path(it), m.out_path(it)
        try:
            if out.exists() and it.get("encoded_with"):
                _remove(out, permanent)
            _remove(src, permanent)
        except Exception as e:  # noqa: BLE001
            failed.append({"id": it["id"], "reason": str(e)})
            lines.append(f"FAILED  {src.name}: {e}")
            continue
        m.update(it["id"], save=False, status="finalized", finalized_at=time.time())
        done.append(it["id"])
        lines.append(f"{verb} {src.name} (marked for the Trash, no copy kept)")

    m.save()
    logpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("finalize: %d done, %d kept", len(done), len(failed))
    return {"done": done, "failed": failed, "log": str(logpath)}
