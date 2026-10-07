"""End-to-end tests on synthetic clips. Run: pytest -q tests/

Fixture generation needs ffmpeg with libflite (for synthetic speech) and
scikit-image; tests are skipped if those aren't available.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from pxlsqueeze import ff, speech
from pxlsqueeze.manifest import Manifest, effective_trim, needs_encode, output_name
from pxlsqueeze.pipeline import analyze_item, encode_and_verify, pending_analysis, pending_encode

HERE = Path(__file__).parent


def _can_make_fixtures() -> bool:
    try:
        import skimage  # noqa: F401
    except ImportError:
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " flite " in out


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not _can_make_fixtures():
        pytest.skip("needs ffmpeg with libflite and scikit-image")
    d = tmp_path_factory.mktemp("fx")
    subprocess.run(["python3", str(HERE / "make_fixtures.py"), str(d)], check=True)
    return d


@pytest.fixture(scope="session")
def processed(fixtures: Path) -> Manifest:
    ff.set_binaries()
    m = Manifest(fixtures)
    m.settings["preset"]["hevc"] = "ultrafast"
    m.discover()
    for i in pending_analysis(m):
        analyze_item(m, i, use_clip=False)
    for i in pending_encode(m):
        encode_and_verify(m, i)
    return m


# ------------------------------------------------------------------ unit-ish

def test_output_name() -> None:
    assert output_name("PXL_20260920_142205874.mp4") == "PXL_20260920_142205874~2.mp4"


def test_segments_and_trim_rules() -> None:
    step = speech.CHUNK / speech.SR
    probs = np.zeros(int(20 / step), dtype=np.float32)
    probs[int(5 / step):int(9 / step)] = 0.9
    probs[int(9.8 / step):int(12 / step)] = 0.9    # gap < 1.5 s -> merged
    probs[int(16 / step):int(16.2 / step)] = 0.9   # too short -> dropped
    segs = speech.clean_segments(speech.segments_from_probs(probs))
    assert len(segs) == 1 and abs(segs[0][0] - 5) < 0.1 and abs(segs[0][1] - 12) < 0.1
    t = speech.decide_trim(segs, 20.0, face_presence=1.0)
    assert t["is_talking"] and abs(t["start"] - 4) < 0.1 and abs(t["end"] - 13) < 0.1
    # No face on screen -> never trimmed (background chatter)
    assert not speech.decide_trim(segs, 20.0, face_presence=0.0)["is_talking"]
    # Very short clip -> never trimmed
    assert not speech.decide_trim([[0.5, 2.0]], 2.5, 1.0)["is_talking"]


# ------------------------------------------------------------------ pipeline

def test_discovery(processed: Manifest) -> None:
    ids = {it["id"] for it in processed}
    assert ids == {f"PXL_20260920_1{h}0000000" for h in "0123467"}  # variant + IMG_ ignored


def test_rotation_detection(processed: Manifest) -> None:
    rot = {it["id"]: it["rotation"]["auto"] for it in processed}
    assert rot["PXL_20260920_110000000"] == 90
    assert rot["PXL_20260920_120000000"] == 180
    assert rot["PXL_20260920_130000000"] == 0   # correct metadata already
    assert processed.get("PXL_20260920_100000000")["rotation"]["method"] == "none"


def test_trim_detection(processed: Manifest) -> None:
    it = processed.get("PXL_20260920_110000000")
    s, e = effective_trim(it)
    assert it["trim"]["is_talking"]
    assert 2.5 < s < 4.0 and 9.5 < e < 11.5
    assert effective_trim(processed.get("PXL_20260920_100000000")) == (0.0, 6.0)


def test_trim_after_faceless_lead_in(processed: Manifest) -> None:
    # Faces in under half the whole clip, but in all of the part that's kept.
    it = processed.get("PXL_20260920_170000000")
    assert it["rotation"]["auto"] == 90
    assert it["rotation"]["face_presence"] < 0.5 <= it["trim"]["face_presence"]
    assert it["trim"]["is_talking"]
    s, e = effective_trim(it)
    assert 8.5 < s < 10.0 and e < it["probe"]["duration"]


def test_outputs(processed: Manifest) -> None:
    for it in processed:
        assert it["status"] in ("verified", "skipped"), it
        if it["status"] != "verified":
            continue
        out = processed.out_path(it)
        o = ff.probe(out)
        assert o["vcodec"] == "hevc" and o["rotation_meta"] == 0
        assert out.stat().st_mtime == pytest.approx(processed.src_path(it).stat().st_mtime, abs=1)
    # Rotated talking clip: upright landscape, trimmed, metadata kept
    o = ff.probe(processed.out_path(processed.get("PXL_20260920_110000000")))
    assert (o["w"], o["h"]) == (1280, 720)
    assert o["creation_time"].startswith("2026-09-20T14:22:05")
    assert o["location"] == "+40.7265-073.9815/"
    it = processed.get("PXL_20260920_110000000")
    assert abs(o["duration"] - (it["encoded_with"]["end"] - it["encoded_with"]["start"])) < 0.2
    # HDR clip keeps 10-bit HLG
    h = ff.probe(processed.out_path(processed.get("PXL_20260920_140000000")))
    assert h["bit_depth"] == 10 and h["hdr"] == "hlg"


def test_idempotent(processed: Manifest) -> None:
    assert pending_encode(processed) == []
    m2 = Manifest(processed.root)
    assert m2.discover() == []


# ------------------------------------------------------------------ server

def test_server_flow(processed: Manifest, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from pxlsqueeze.server import create_app

    m = processed
    client = TestClient(create_app(m))
    st = client.get("/api/state").json()
    assert st["totals"]["videos"] == 7
    short = [i["id"] for i in st["items"] if "short" in i["flags"]]
    assert short == ["PXL_20260920_160000000"]

    iid = "PXL_20260920_120000000"
    # Range request support (Safari needs it)
    r = client.get(f"/media/{iid}/output", headers={"Range": "bytes=0-1"})
    assert r.status_code == 206 and r.headers["content-range"].startswith("bytes 0-1/")

    # Frames and thumbs
    assert client.get(f"/frame/{iid}/source?t=1").headers["content-type"] == "image/png"
    assert client.get(f"/frame/{iid}/output?t=1").status_code == 200
    assert client.get(f"/thumb/{iid}").status_code == 200

    # Manual trim makes the item stale; approve is refused until re-encoded
    it = client.post(f"/api/items/{iid}/trim", json={"start": 1.0, "end": 4.0}).json()
    assert it["stale"] and it["needs_encode"]
    assert client.post(f"/api/items/{iid}/decision", json={"decision": "approved"}).status_code == 409

    client.post(f"/api/items/{iid}/encode")
    for _ in range(240):
        st = client.get("/api/state").json()
        cur = next(i for i in st["items"] if i["id"] == iid)
        if not st["jobs"]["current"] and not st["jobs"]["queued"] and not cur["stale"]:
            break
        time.sleep(0.5)
    assert cur["status"] == "verified" and not cur["stale"]
    assert abs(cur["encode"]["out_duration"] - 3.0) < 0.2

    # Approve, then finalize with a wrong count (refused), then the right one
    assert client.post(f"/api/items/{iid}/decision", json={"decision": "approved"}).status_code == 200
    assert client.post("/api/finalize", json={"count": 99}).status_code == 409
    src = m.src_path(m.get(iid))
    backup = tmp_path / src.name
    shutil.copy2(src, backup)
    r = client.post("/api/finalize", json={"count": 1}).json()
    assert r["done"] == [iid] and not src.exists() and m.out_path(m.get(iid)).exists()
    assert m.get(iid)["status"] == "finalized"

    # Reject deletes the new file but keeps the original
    rid = "PXL_20260920_130000000"
    client.post(f"/api/items/{rid}/decision", json={"decision": "rejected"})
    assert not m.out_path(m.get(rid)).exists() and m.src_path(m.get(rid)).exists()
    assert not needs_encode(m.get(rid), m)


def test_trash_short(processed: Manifest) -> None:
    from fastapi.testclient import TestClient

    from pxlsqueeze.server import create_app

    m = processed
    client = TestClient(create_app(m))
    sid = "PXL_20260920_160000000"
    # Bulk mark, then undo one, then mark via the single-video decision
    assert client.post("/api/trash-short").json()["marked"] == [sid]
    assert m.get(sid)["decision"] == "trash"
    assert client.post("/api/trash-short").json()["marked"] == []  # already decided
    client.post(f"/api/items/{sid}/decision", json={"decision": None})
    it = client.post(f"/api/items/{sid}/decision", json={"decision": "trash"}).json()
    assert it["decision"] == "trash" and "short" not in it["flags"]
    assert not needs_encode(m.get(sid), m)

    # Also mark a normal-length video for the Trash
    lid = "PXL_20260920_100000000"
    client.post(f"/api/items/{lid}/decision", json={"decision": "trash"})
    fs = client.get("/api/state").json()["finalize"]
    assert fs["trash"] == 2
    src_s, out_s = m.src_path(m.get(sid)), m.out_path(m.get(sid))
    src_l, out_l = m.src_path(m.get(lid)), m.out_path(m.get(lid))
    r = client.post("/api/finalize", json={"count": fs["count"]}).json()
    assert sid in r["done"] and lid in r["done"]
    for p in (src_s, out_s, src_l, out_l):
        assert not p.exists()
    assert m.get(sid)["status"] == "finalized"


# ------------------------------------------------------------------ 0.2 fixes

def _packet_times(path: Path) -> list[float]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "packet=pts_time", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return sorted(float(x) for x in out.split() if x.strip() not in ("", "N/A"))


@pytest.fixture(scope="session")
def vfr_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clip with uneven frame spacing, stored at a 90 kHz timescale like a Pixel."""
    d = tmp_path_factory.mktemp("vfr")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
                    "-vf", "setpts='(N/30 + if(mod(N,7),0,0.02))/TB'", "-fps_mode", "passthrough",
                    "-c:v", "libx264", "-crf", "16", "-g", "30", "-c:a", "aac", "-shortest",
                    "-video_track_timescale", "90000", str(d / "PXL_20260921_000000000.mp4")], check=True)
    return d


def test_vfr_encode_passes_check_and_keeps_timestamps(vfr_dir: Path) -> None:
    from pxlsqueeze.server import serialize
    from pxlsqueeze.verify import decode_ok

    ff.set_binaries()
    src = vfr_dir / "PXL_20260921_000000000.mp4"
    assert decode_ok(src)[0], "the check must not flag a healthy VFR source"
    m = Manifest(vfr_dir)
    m.settings["preset"]["hevc"] = "ultrafast"
    m.discover()
    iid = "PXL_20260921_000000000"
    analyze_item(m, iid, use_clip=False)
    s = serialize(m.get(iid), m)  # never encoded: needs encoding, but isn't "changed"
    assert s["needs_encode"] and not s["stale"]
    it = encode_and_verify(m, iid)
    assert it["status"] == "verified", it.get("error")
    assert not serialize(m.get(iid), m)["needs_encode"]
    a, b = _packet_times(src), _packet_times(m.out_path(it))
    assert len(a) == len(b)
    assert max(abs(x - y) for x, y in zip(a, b)) < 0.002


def test_recover_false_positive(processed: Manifest) -> None:
    from pxlsqueeze.pipeline import recover_false_positives, verify_only

    m = processed
    iid = "PXL_20260920_110000000"
    assert m.get(iid)["status"] == "verified"
    m.update(iid, status="error", error="Output does not decode cleanly: [null @ 0x1] Application provided "
                                        "invalid, non monotonically increasing dts to muxer in stream 0: 201 >= 201")
    assert recover_false_positives(m) == [iid]
    assert m.get(iid)["status"] == "encoded" and not needs_encode(m.get(iid), m)
    assert verify_only(m, iid)["status"] == "verified"


def test_failed_encode_can_be_retried(processed: Manifest) -> None:
    m = processed
    iid = "PXL_20260920_140000000"
    m.update(iid, status="error", error="ffmpeg failed: something")
    assert needs_encode(m.get(iid), m)
    assert encode_and_verify(m, iid)["status"] == "verified"


def test_folder_lock(tmp_path: Path) -> None:
    import sys

    from pxlsqueeze.lock import FolderBusy, acquire, release

    acquire(tmp_path, "encode")
    try:
        code = ("import sys; from pathlib import Path; from pxlsqueeze.lock import acquire, FolderBusy\n"
                "try:\n    acquire(Path(sys.argv[1]), 'review')\nexcept FolderBusy as e:\n"
                "    print(e); sys.exit(3)\n")
        r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
        assert r.returncode == 3 and "`pxlsqueeze encode` is already working on this folder" in r.stdout
        # The CLI refuses too, but read-only `status` still works
        (tmp_path / "PXL_20260921_000000000.mp4").write_bytes(b"")
        r = subprocess.run([sys.executable, "-m", "pxlsqueeze", "analyze", str(tmp_path)], capture_output=True, text=True)
        assert r.returncode == 3 and "already working on this folder" in r.stdout
        r = subprocess.run([sys.executable, "-m", "pxlsqueeze", "status", str(tmp_path)], capture_output=True, text=True)
        assert r.returncode == 0
    finally:
        release(tmp_path)
    # Released: a second process can take it now
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0


# ------------------------------------------------------------------ 0.4

def test_project_summary_adds_up(processed: Manifest) -> None:
    from pxlsqueeze.manifest import project_summary

    s = project_summary(processed)
    assert s["total"] == len(list(processed))
    assert s["encoded"] + s["trashed"] + s["kept"] + s["todo"] == s["total"]
    assert s["freed_bytes"] > 0  # earlier tests finalized videos


def test_encode_anyway_and_reencode(tmp_path: Path) -> None:
    """A clip that barely compresses is kept as the original; Encode anyway keeps the new file.
    Re-encode works on an up-to-date video."""
    from fastapi.testclient import TestClient

    from pxlsqueeze.manifest import project_summary
    from pxlsqueeze.server import create_app

    ff.set_binaries()
    # Already-efficient source: a flat colour clip in HEVC, re-encoding saves ~nothing.
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=gray:s=640x360:r=30:d=4", "-c:v", "libx265", "-preset", "ultrafast",
                    "-crf", "28", "-x265-params", "log-level=error", "-tag:v", "hvc1",
                    str(tmp_path / "PXL_20260922_000000000.mp4")], check=True)
    m = Manifest(tmp_path)
    m.settings["preset"]["hevc"] = "ultrafast"
    m.discover()
    iid = "PXL_20260922_000000000"
    analyze_item(m, iid, use_clip=False)
    assert project_summary(m)["todo"] == 1
    it = encode_and_verify(m, iid)
    assert it["status"] == "skipped" and not m.out_path(it).exists()
    assert project_summary(m)["kept"] == 1

    client = TestClient(create_app(m))
    client.post(f"/api/items/{iid}/encode", json={"force": True, "keep_small": True})
    for _ in range(120):
        st = client.get("/api/state").json()
        if not st["jobs"]["current"] and not st["jobs"]["queued"]:
            break
        time.sleep(0.5)
    it = m.get(iid)
    assert it["status"] == "verified" and m.out_path(it).exists() and not it["force_encode"]
    assert st["summary"]["encoded"] == 1 and st["summary"]["kept"] == 0

    rev = it["encode"]["encoded_at"]
    client.post(f"/api/items/{iid}/encode", json={"force": True})
    for _ in range(120):
        st = client.get("/api/state").json()
        if not st["jobs"]["current"] and not st["jobs"]["queued"]:
            break
        time.sleep(0.5)
    assert m.get(iid)["encode"]["encoded_at"] > rev and m.get(iid)["status"] == "verified"
