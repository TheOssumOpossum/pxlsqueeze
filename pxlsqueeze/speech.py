"""Speech detection and trim-point decisions for talking-to-camera clips."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import ff
from .models import model_path
from .util import log

SR = 16000
CHUNK = 512                  # Silero v5 window at 16 kHz (32 ms)
CONTEXT = 64                 # Silero v5 context samples at 16 kHz
THRESHOLD = 0.5
MIN_SPEECH = 0.25
MIN_SILENCE = 0.5
MERGE_GAP = 1.5
MIN_ISOLATED = 0.4
PAD = 1.0

TALK_MIN_SPEECH = 3.0
TALK_MIN_RATIO = 0.25
TALK_MIN_FACE_PRESENCE = 0.5
MIN_CLIP_FOR_TRIM = 3.0
MIN_TRIM_GAIN = 0.5


def extract_audio(path: Path) -> np.ndarray:
    res = ff.run([ff.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                  "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], text=False)
    if res.returncode != 0:
        raise ff.FFmpegError(f"audio extraction failed: {res.stderr.decode(errors='replace')[:300]}")
    return np.frombuffer(res.stdout, dtype=np.float32).copy()


class SileroVAD:
    def __init__(self, path: Path):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.sess = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])

    def probs(self, audio: np.ndarray) -> np.ndarray:
        state = np.zeros((2, 1, 128), dtype=np.float32)
        context = np.zeros((1, CONTEXT), dtype=np.float32)
        sr = np.array(SR, dtype=np.int64)
        n = len(audio) // CHUNK
        out = np.zeros(n, dtype=np.float32)
        for i in range(n):
            x = audio[i * CHUNK:(i + 1) * CHUNK][None, :]
            inp = np.concatenate([context, x], axis=1)
            p, state = self.sess.run(None, {"input": inp, "state": state, "sr": sr})
            out[i] = float(p[0][0])
            context = inp[:, -CONTEXT:]
        return out


def energy_probs(audio: np.ndarray) -> np.ndarray:
    """Crude fallback: per-chunk loudness mapped to a 0..1 'speech' score."""
    n = len(audio) // CHUNK
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = audio[: n * CHUNK].reshape(n, CHUNK)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    floor = np.percentile(db, 10)
    thr = max(floor + 15, -45.0)
    return np.clip((db - thr) / 6 + 0.5, 0, 1).astype(np.float32)


def segments_from_probs(probs: np.ndarray, threshold: float = THRESHOLD) -> list[list[float]]:
    """Hysteresis segmentation in the style of Silero's get_speech_timestamps."""
    step = CHUNK / SR
    neg = threshold - 0.15
    segs: list[list[float]] = []
    start: float | None = None
    silence_since: float | None = None
    for i, p in enumerate(probs):
        t = i * step
        if p >= threshold:
            silence_since = None
            if start is None:
                start = t
        elif start is not None and p < neg:
            if silence_since is None:
                silence_since = t
            if t + step - silence_since >= MIN_SILENCE:
                if silence_since - start >= MIN_SPEECH:
                    segs.append([start, silence_since])
                start, silence_since = None, None
    if start is not None:
        end = len(probs) * step
        if end - start >= MIN_SPEECH:
            segs.append([start, end])
    return segs


def clean_segments(segs: list[list[float]]) -> list[list[float]]:
    merged: list[list[float]] = []
    for s, e in segs:
        if merged and s - merged[-1][1] < MERGE_GAP:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [[round(s, 2), round(e, 2)] for s, e in merged if e - s >= MIN_ISOLATED]


def enough_speech(segs: list[list[float]], duration: float) -> bool:
    total = sum(e - s for s, e in segs)
    return duration >= MIN_CLIP_FOR_TRIM and total >= TALK_MIN_SPEECH and total >= TALK_MIN_RATIO * duration


def talk_window(segs: list[list[float]], duration: float) -> tuple[float, float]:
    """The part of the clip that would be kept: first to last speech, padded."""
    return max(0.0, segs[0][0] - PAD), min(duration, segs[-1][1] + PAD)


def decide_trim(segs: list[list[float]], duration: float, face_presence: float) -> dict[str, Any]:
    """face_presence should be measured over talk_window(), not the whole clip."""
    total = sum(e - s for s, e in segs)
    talking = enough_speech(segs, duration) and face_presence >= TALK_MIN_FACE_PRESENCE
    result: dict[str, Any] = {"is_talking": bool(talking), "start": None, "end": None,
                              "speech_total": round(total, 2)}
    if talking:
        start, end = talk_window(segs, duration)
        if start + (duration - end) >= MIN_TRIM_GAIN:
            result["start"], result["end"] = round(start, 2), round(end, 2)
        else:
            result["is_talking"] = True  # talking, but nothing worth trimming
    return result


_vad: SileroVAD | None = None
_vad_failed = False


def get_vad() -> SileroVAD | None:
    global _vad, _vad_failed
    if _vad is None and not _vad_failed:
        p = model_path("silero_vad")
        if p is not None:
            try:
                _vad = SileroVAD(p)
            except Exception as e:  # noqa: BLE001
                log.warning("Silero VAD failed to load: %s", e)
        if _vad is None:
            _vad_failed = True
    return _vad


def analyze(path: Path, probe: dict[str, Any],
            presence_in: Callable[[float, float], float]) -> dict[str, Any]:
    """presence_in(start, end) gives the face presence over that stretch of the clip."""
    duration = float(probe.get("duration") or 0.0)
    if not probe.get("has_audio"):
        return {"speech": [], "vad": None, "is_talking": False, "start": None, "end": None,
                "speech_total": 0.0, "face_presence": None}
    audio = extract_audio(path)
    vad = get_vad()
    if vad is not None:
        probs, method = vad.probs(audio), "silero"
    else:
        probs, method = energy_probs(audio), "energy"
    segs = clean_segments(segments_from_probs(probs))
    # Only look for faces where the speech is, and only if there's enough of it to matter.
    presence = presence_in(*talk_window(segs, duration)) if enough_speech(segs, duration) else None
    return {"speech": segs, "vad": method, "face_presence": None if presence is None else round(presence, 3),
            **decide_trim(segs, duration, presence or 0.0)}
