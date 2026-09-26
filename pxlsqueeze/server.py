"""Local review server. Binds to 127.0.0.1 only."""

from __future__ import annotations

import re
import socket
import subprocess
import threading
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from . import __version__, ff
from .finalize import finalize as do_finalize
from .finalize import summary as finalize_summary
from .manifest import (Manifest, effective_rotation, effective_trim, is_short, is_stale, is_trimmed,
                       needs_encode, project_summary)
from .orientation import LOW_CONFIDENCE
from .pipeline import JobQueue, pending_encode
from .util import cache_dir, log
from .verify import metric_name

STATIC = resources.files("pxlsqueeze") / "static"
CHUNK = 1 << 20


# --------------------------------------------------------------------------- helpers

def serialize(it: dict[str, Any], m: Manifest) -> dict[str, Any]:
    probe = it.get("probe") or {}
    enc = it.get("encode") or {}
    q = enc.get("quality") or {}
    s, e = effective_trim(it) if probe else (0.0, 0.0)
    out_exists = m.out_path(it).exists() and it["status"] in ("encoded", "verified", "finalized")
    flags = []
    rot = it["rotation"]
    if probe and rot.get("override") is None and (rot.get("method") in (None, "none")
                                                  or float(rot.get("confidence") or 0) < LOW_CONFIDENCE):
        flags.append("rotation-unsure")
    if q.get("flag"):
        flags.append("quality")
    if it["status"] == "error":
        flags.append("error")
    if probe.get("hdr10plus"):
        flags.append("hdr10plus")
    short = is_short(it, m)
    if short and not it.get("decision") and it["status"] != "finalized":
        flags.append("short")
    stale = bool(probe) and is_stale(it, m) and it["status"] not in ("pending", "finalized")
    return {
        **it,
        "effective": {"rotation": effective_rotation(it), "start": s, "end": e, "trimmed": is_trimmed(it) if probe else False},
        # stale: encoded before, then rotation/trim/settings changed
        "stale": stale and it.get("encoded_with") is not None,
        "needs_encode": bool(probe) and needs_encode(it, m),
        "has_output": out_exists,
        "source_exists": m.src_path(it).exists(),
        "flags": flags,
        "short": short,
        "rev": int(enc.get("encoded_at") or 0),
    }


def ranged(path: Path, request: Request, media_type: str) -> Response:
    if not path.exists():
        raise HTTPException(404, "file not found")
    size = path.stat().st_size
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    rng = request.headers.get("range")
    mt = re.match(r"bytes=(\d*)-(\d*)", rng or "")
    if not mt or (not mt.group(1) and not mt.group(2)):
        return FileResponse(path, media_type=media_type, headers=headers)
    if mt.group(1):
        start = int(mt.group(1))
        end = int(mt.group(2)) if mt.group(2) else size - 1
    else:  # suffix range: last N bytes
        start, end = max(0, size - int(mt.group(2))), size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    def body() -> Iterator[bytes]:
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)})
    return StreamingResponse(body(), status_code=206, media_type=media_type, headers=headers)


# --------------------------------------------------------------------------- app

def create_app(m: Manifest, jobs: JobQueue | None = None) -> FastAPI:
    app = FastAPI(title="pxlsqueeze", docs_url=None, redoc_url=None)
    jobs = jobs or JobQueue(m)
    proxy_locks: dict[str, threading.Lock] = {}
    proxy_guard = threading.Lock()

    def item_or_404(item_id: str) -> dict[str, Any]:
        try:
            return m.get(item_id)
        except KeyError:
            raise HTTPException(404, f"unknown video {item_id}") from None

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/static/{name}")
    def static(name: str) -> Response:
        if name not in ("app.js", "app.css"):
            raise HTTPException(404)
        mt = "text/javascript" if name.endswith(".js") else "text/css"
        return Response((STATIC / name).read_text(encoding="utf-8"), media_type=mt,
                        headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- state
    @app.get("/api/state")
    def state() -> dict[str, Any]:
        with m.lock:
            items = [serialize(it, m) for it in m]
        with_out = [i for i in items if i["status"] in ("verified", "finalized") and (i.get("encode") or {}).get("size")]
        return {
            "version": __version__,
            "root": str(m.root),
            "items": items,
            "settings": m.settings,
            "jobs": jobs.state(),
            "metric": metric_name(),
            "summary": project_summary(m),
            "totals": {
                "videos": len(items),
                "source_bytes": sum(i["probe"]["size"] for i in with_out),
                "output_bytes": sum(i["encode"]["size"] for i in with_out),
                "pending_encode": len(pending_encode(m)),
            },
            "finalize": finalize_summary(m),
        }

    # ---------------------------------------------------------------- edits
    def changed(item_id: str, **fields: Any) -> dict[str, Any]:
        it = m.update(item_id, **fields)
        if it.get("decision") == "approved" and is_stale(it, m):
            it = m.update(item_id, decision=None)
        return serialize(it, m)

    @app.post("/api/items/{item_id}/rotation")
    def set_rotation(item_id: str, body: dict = Body(...)) -> dict[str, Any]:
        it = item_or_404(item_id)
        r = body.get("rotation")
        if r is not None and int(r) % 90:
            raise HTTPException(400, "rotation must be a multiple of 90")
        rot = dict(it["rotation"])
        rot["override"] = None if r is None else int(r) % 360
        return changed(item_id, rotation=rot)

    @app.post("/api/items/{item_id}/trim")
    def set_trim(item_id: str, body: dict = Body(...)) -> dict[str, Any]:
        it = item_or_404(item_id)
        dur = float(it["probe"]["duration"])
        trim = dict(it["trim"])
        if body.get("reset"):
            trim["override"] = None
        elif body.get("none"):
            trim["override"] = {"start": 0.0, "end": dur}
        else:
            s = max(0.0, min(float(body["start"]), dur))
            e = max(0.0, min(float(body["end"]), dur))
            if e - s < 0.5:
                raise HTTPException(400, "The kept part must be at least half a second long.")
            trim["override"] = {"start": round(s, 3), "end": round(e, 3)}
        return changed(item_id, trim=trim)

    @app.post("/api/items/{item_id}/decision")
    def set_decision(item_id: str, body: dict = Body(...)) -> dict[str, Any]:
        it = item_or_404(item_id)
        d = body.get("decision")
        if it["status"] == "finalized":
            raise HTTPException(409, "This video is already finalized.")
        if d == "approved":
            if it["status"] != "verified" or is_stale(it, m):
                raise HTTPException(409, "Encode this video with its current settings before approving it.")
            return serialize(m.update(item_id, decision="approved"), m)
        if d == "rejected":
            out = m.out_path(it)
            if out.exists() and it["status"] in ("encoded", "verified"):
                out.unlink()
            status = "analyzed" if it.get("probe") else it["status"]
            return serialize(m.update(item_id, decision="rejected", status=status,
                                      encoded_with=None, encode=None), m)
        if d == "trash":
            if not m.src_path(it).exists():
                raise HTTPException(409, "The original is no longer in this folder.")
            return serialize(m.update(item_id, decision="trash"), m)
        if d is None:
            return serialize(m.update(item_id, decision=None), m)
        raise HTTPException(400, "decision must be approved, rejected, trash or null")

    @app.post("/api/items/{item_id}/encode")
    def enqueue_encode(item_id: str, body: dict = Body(default={})) -> dict[str, Any]:
        """force: encode even if nothing changed (first encode, "Encode anyway", "Re-encode").
        keep_small: keep the new file even if it's barely smaller than the original."""
        it = item_or_404(item_id)
        if it["status"] == "finalized":
            raise HTTPException(409, "This video is already finalized.")
        if not it.get("probe") or not m.src_path(it).exists():
            raise HTTPException(409, "This video can't be encoded until it has been analyzed.")
        fields: dict[str, Any] = {}
        if it.get("decision") in ("rejected", "trash"):
            fields["decision"] = None
        if body.get("force"):
            fields["force_encode"] = True
        if body.get("keep_small"):
            fields["keep_output"] = True
        if fields:
            m.update(item_id, **fields)
        jobs.submit("encode", item_id)
        return jobs.state()

    @app.post("/api/items/{item_id}/verify")
    def enqueue_verify(item_id: str) -> dict[str, Any]:
        it = item_or_404(item_id)
        if it["status"] != "encoded":
            raise HTTPException(409, "Only videos waiting for their check can be checked again.")
        jobs.submit("verify", item_id)
        return jobs.state()

    @app.post("/api/encode-pending")
    def enqueue_all() -> dict[str, Any]:
        for i in pending_encode(m):
            jobs.submit("encode", i)
        return jobs.state()

    @app.post("/api/trash-short")
    def trash_short() -> dict[str, Any]:
        """Mark every undecided short clip for the Trash (reversible until finalize)."""
        marked = []
        with m.lock:
            for it in m:
                if (is_short(it, m) and not it.get("decision") and it["status"] != "finalized"
                        and m.src_path(it).exists()):
                    m.update(it["id"], save=False, decision="trash")
                    marked.append(it["id"])
            m.save()
        return {"marked": marked}

    @app.post("/api/finalize")
    def finalize_endpoint(body: dict = Body(...)) -> dict[str, Any]:
        s = finalize_summary(m)
        if int(body.get("count", -1)) != s["count"]:
            raise HTTPException(409, "The list of approved videos changed. Review the count and try again.")
        if jobs.state()["current"]:
            raise HTTPException(409, "Wait for the current encode to finish first.")
        return do_finalize(m, permanent=False)

    # ---------------------------------------------------------------- media
    @app.get("/media/{item_id}/{which}")
    def media(item_id: str, which: str, request: Request) -> Response:
        it = item_or_404(item_id)
        path = m.src_path(it) if which == "source" else m.out_path(it) if which == "output" else None
        if path is None:
            raise HTTPException(404)
        return ranged(path, request, "video/mp4")

    @app.get("/proxy/{item_id}/{which}")
    def proxy(item_id: str, which: str, request: Request) -> Response:
        it = item_or_404(item_id)
        src = m.src_path(it) if which == "source" else m.out_path(it)
        rev = int((it.get("encode") or {}).get("encoded_at") or 0) if which == "output" else 0
        dest = cache_dir(m.root, "proxies") / f"{item_id}.{which}.{rev}.mp4"
        with proxy_guard:
            lock = proxy_locks.setdefault(str(dest), threading.Lock())
        with lock:
            if not dest.exists():
                tmp = dest.with_suffix(".tmp.mp4")
                cmd = [ff.ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(src),
                       "-map", "0:v:0", "-map", "0:a?",
                       "-vf", "scale=w=1280:h=1280:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "128k",
                       "-movflags", "+faststart", str(tmp)]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(500, f"Preview could not be made: {res.stderr.strip()[-200:]}")
                tmp.replace(dest)
        return ranged(dest, request, "video/mp4")

    @app.get("/frame/{item_id}/{which}")
    def frame(item_id: str, which: str, t: float) -> Response:
        """Full-resolution PNG. `t` is in source seconds for both panes."""
        it = item_or_404(item_id)
        ew = it.get("encoded_with") or {}
        if which == "source":
            rot = ew.get("rotation", effective_rotation(it)) if it.get("encoded_with") else effective_rotation(it)
            path, ts = m.src_path(it), t
        elif which == "output":
            if not it.get("encoded_with"):
                raise HTTPException(404, "not encoded yet")
            rot, path, ts = 0, m.out_path(it), max(0.0, t - float(ew.get("start", 0)))
        else:
            raise HTTPException(404)
        rev = int((it.get("encode") or {}).get("encoded_at") or 0)
        dest = cache_dir(m.root, "frames") / f"{item_id}.{which}.{rot}.{rev}.{int(ts * 1000)}.png"
        if not dest.exists():
            try:
                dest.write_bytes(ff.extract_frame(path, ts, rotation=rot, fmt="png"))
            except ff.FFmpegError as e:
                raise HTTPException(500, str(e)) from e
        return FileResponse(dest, media_type="image/png")

    @app.get("/thumb/{item_id}")
    def thumb(item_id: str) -> Response:
        it = item_or_404(item_id)
        if not it.get("probe") or not m.src_path(it).exists():
            raise HTTPException(404)
        rot = effective_rotation(it)
        dest = cache_dir(m.root, "thumbs") / f"{item_id}.{rot}.jpg"
        if not dest.exists():
            try:
                dest.write_bytes(ff.extract_frame(m.src_path(it), float(it["probe"]["duration"]) * 0.3,
                                                  rotation=rot, max_edge=240, fmt="jpg", quality=5))
            except ff.FFmpegError as e:
                raise HTTPException(500, str(e)) from e
        return FileResponse(dest, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    @app.exception_handler(Exception)
    async def on_error(_: Request, exc: Exception) -> JSONResponse:
        log.exception("server error: %s", exc)
        return JSONResponse({"detail": str(exc)}, status_code=500)

    return app


def free_port(preferred: int = 8765) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 40)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(m: Manifest, port: int | None = None, open_browser: bool = True) -> None:
    import webbrowser

    import uvicorn

    port = free_port(port or 8765)
    app = create_app(m)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Review UI running at {url}  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
