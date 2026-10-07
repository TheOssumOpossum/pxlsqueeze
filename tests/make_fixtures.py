"""Generate synthetic PXL_*.mp4 test videos.

Needs an ffmpeg with libx264/libx265 (and libflite for the speech clip),
plus scikit-image for a sample face photo. Usage:

    python tests/make_fixtures.py OUTDIR
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np


def sh(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def face_canvas(path: Path) -> None:
    from skimage import data

    face = cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)
    canvas = np.full((720, 1280, 3), (70, 110, 150), np.uint8)
    face = cv2.resize(face[10:300, 110:400], (680, 680))  # close-up, like a selfie
    canvas[20:700, 300:980] = face
    cv2.imwrite(str(path), canvas)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "_tmp"
    tmp.mkdir(exist_ok=True)
    canvas = tmp / "face.png"
    face_canvas(canvas)
    common_v = ["-c:v", "libx264", "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p", "-r", "30", "-g", "30"]
    loop = ["-loop", "1", "-framerate", "30", "-i", str(canvas)]
    meta = ["-metadata", "creation_time=2026-09-20T14:22:05Z", "-metadata", "location=+40.7265-073.9815/"]

    # 1. Landscape scene, upright, no speech (music-like tone): expect no rotation, no trim.
    sh("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6",
       "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
       *common_v, "-c:a", "aac", "-shortest", *meta, str(out / "PXL_20260920_100000000.mp4"))

    # 2. Talking head filmed sideways (content rotated 90° CCW, no metadata): expect +90 and a trim.
    speech = tmp / "speech.wav"
    sh("-f", "lavfi", "-i",
       "flite=text='Hi everyone, welcome back. Today I am going to show you my new climbing route in the park.':voice=slt",
       "-ar", "48000", "-ac", "2", str(tmp / "s.wav"))
    # 4 s silence + speech + 4 s silence
    sh("-f", "lavfi", "-t", "4", "-i", "anullsrc=r=48000:cl=stereo", "-i", str(tmp / "s.wav"),
       "-f", "lavfi", "-t", "4", "-i", "anullsrc=r=48000:cl=stereo",
       "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", str(speech))
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(speech)], capture_output=True, text=True).stdout)
    sh(*loop, "-i", str(speech), "-t", f"{dur:.2f}", "-vf", "transpose=2,noise=alls=6:allf=t",
       *common_v, "-c:a", "aac", "-b:a", "128k", *meta, str(out / "PXL_20260920_110000000.mp4"))

    # 3. Upside-down face, no speech: expect 180.
    sh(*loop, "-f", "lavfi", "-i", "anoisesrc=d=5:a=0.01", "-t", "5", "-vf", "hflip,vflip,noise=alls=6:allf=t",
       *common_v, "-c:a", "aac", str(out / "PXL_20260920_120000000.mp4"))

    # 4. Portrait stored sideways WITH correct rotation metadata (normal phone behaviour): expect 0.
    sh(*loop, "-t", "5", "-vf", "transpose=1,noise=alls=6:allf=t", *common_v, "-an", str(tmp / "p.mp4"))
    sh("-display_rotation", "90", "-i", str(tmp / "p.mp4"), "-c", "copy", str(out / "PXL_20260920_130000000.mp4"))

    # 5. 10-bit HLG clip: exercises the HDR path.
    sh("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=4",
       "-f", "lavfi", "-i", "sine=frequency=500:duration=4",
       "-c:v", "libx265", "-preset", "fast", "-crf", "14", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
       "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
       "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
       "-c:a", "aac", "-shortest", str(out / "PXL_20260920_140000000.mp4"))

    # 6. An accidental 2-second clip: suggested for the Trash.
    sh("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=2",
       "-f", "lavfi", "-i", "sine=frequency=250:duration=2",
       *common_v, "-c:a", "aac", "-shortest", str(out / "PXL_20260920_160000000.mp4"))

    # 8. Recording started in landscape, then the phone was spun round before talking: 10 s of
    #    faceless lead-in, then a sideways talking head. Expect +90 and a trim despite the lead-in.
    lead = tmp / "lead.wav"
    sh("-f", "lavfi", "-t", "10", "-i", "anullsrc=r=48000:cl=stereo", "-i", str(tmp / "s.wav"),
       "-f", "lavfi", "-t", "1", "-i", "anullsrc=r=48000:cl=stereo",
       "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", str(lead))
    talk = dur - 8 + 1  # speech plus 1 s of trailing silence
    sh("-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=10", *loop, "-i", str(lead),
       "-filter_complex",
       f"[0:v]format=yuv420p,setsar=1[a];[1:v]transpose=2,noise=alls=6:allf=t,trim=duration={talk:.2f},"
       "setpts=PTS-STARTPTS,format=yuv420p,setsar=1[b];[a][b]concat=n=2:v=1:a=0[v]",
       "-map", "[v]", "-map", "2:a", *common_v, "-c:a", "aac", "-b:a", "128k",
       str(out / "PXL_20260920_170000000.mp4"))

    # 7. A variant filename (skipped by default) and a non-Pixel file (ignored).
    sh("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=1", *common_v,
       str(out / "PXL_20260920_150000000.LS.mp4"))
    sh("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=1", *common_v, str(out / "IMG_0001.mp4"))

    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures"))
