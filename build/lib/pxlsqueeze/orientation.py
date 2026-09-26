"""Decide how much extra clockwise rotation makes a video's content upright.

Rotations are always relative to the frame as ffmpeg auto-rotates it using the
file's own rotation metadata, so the same number is used for analysis frames,
the review preview and the encode filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import ff
from .models import model_path
from .util import log

ROTATIONS = (0, 90, 180, 270)
N_FRAMES = 8
ANALYSIS_EDGE = 640
FACE_SCORE_MIN = 0.8
FACE_AREA_MIN = 0.01        # for orientation voting
TALK_FACE_AREA_MIN = 0.03   # for "talking to camera" presence
LOW_CONFIDENCE = 0.75


def rotate_img(img: np.ndarray, r: int) -> np.ndarray:
    r %= 360
    if r == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if r == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if r == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


@dataclass
class Face:
    score: float
    area: float  # fraction of the frame


class FaceDetector:
    """YuNet (ONNX, via OpenCV) when available, else OpenCV's bundled Haar cascade."""

    def __init__(self) -> None:
        self.kind = "haar"
        self._yunet = None
        path = model_path("yunet")
        if path is not None and hasattr(cv2, "FaceDetectorYN"):
            try:
                self._yunet = cv2.FaceDetectorYN.create(str(path), "", (320, 320), FACE_SCORE_MIN, 0.3, 50)
                self.kind = "yunet"
            except cv2.error as e:
                log.warning("YuNet failed to load (%s); using Haar cascade", e)
        if self._yunet is None:
            cascade = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            self._haar = cv2.CascadeClassifier(str(cascade))

    def detect(self, img: np.ndarray) -> list[Face]:
        h, w = img.shape[:2]
        if self._yunet is not None:
            self._yunet.setInputSize((w, h))
            _, faces = self._yunet.detect(img)
            if faces is None:
                return []
            return [Face(float(f[14]), float(f[2] * f[3]) / (w * h)) for f in faces if f[14] >= FACE_SCORE_MIN]
        gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        m = max(24, int(min(w, h) * 0.06))
        boxes = self._haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(m, m))
        return [Face(1.0, float(bw * bh) / (w * h)) for (_, _, bw, bh) in boxes]


class ClipScorer:
    """Optional zero-shot 'is this upright?' scorer (pip install pxlsqueeze[orient-ml])."""

    _instance: "ClipScorer | None" = None
    _unavailable = False

    @classmethod
    def get(cls) -> "ClipScorer | None":
        if cls._instance is None and not cls._unavailable:
            try:
                cls._instance = cls()
            except Exception as e:  # noqa: BLE001 - optional dependency
                log.info("CLIP orientation fallback unavailable: %s", e)
                cls._unavailable = True
        return cls._instance

    def __init__(self) -> None:
        import open_clip  # type: ignore
        import torch  # type: ignore
        from PIL import Image  # type: ignore

        self.torch, self.Image = torch, Image
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k")
        self.model.eval()
        tok = open_clip.get_tokenizer("ViT-B-32")
        prompts = ["an upright photo", "a photo rotated sideways", "an upside-down photo"]
        with torch.no_grad():
            t = self.model.encode_text(tok(prompts))
            self.text = t / t.norm(dim=-1, keepdim=True)

    def upright_prob(self, imgs_bgr: list[np.ndarray]) -> float:
        torch = self.torch
        batch = torch.stack([self.preprocess(self.Image.fromarray(cv2.cvtColor(i, cv2.COLOR_BGR2RGB)))
                             for i in imgs_bgr])
        with torch.no_grad():
            f = self.model.encode_image(batch)
            f = f / f.norm(dim=-1, keepdim=True)
            probs = (100.0 * f @ self.text.T).softmax(dim=-1)
        return float(probs[:, 0].mean())


def sample_times(duration: float, n: int = N_FRAMES) -> list[float]:
    if duration <= 0:
        return [0.0]
    lo, hi = duration * 0.05, duration * 0.95
    if n == 1 or hi <= lo:
        return [duration / 2]
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def load_frames(path: Path, duration: float) -> list[np.ndarray]:
    frames = []
    for t in sample_times(duration):
        try:
            data = ff.extract_frame(path, t, max_edge=ANALYSIS_EDGE, fmt="png")
        except ff.FFmpegError as e:
            log.warning("%s", e)
            continue
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            frames.append(img)
    return frames


def decide_from_faces(per_rot: dict[int, list[list[Face]]]) -> tuple[int | None, float, dict[int, float]]:
    scores = {r: sum(f.score for fr in frames for f in fr if f.area >= FACE_AREA_MIN)
              for r, frames in per_rot.items()}
    frames_with = {r: sum(1 for fr in frames if any(f.area >= FACE_AREA_MIN for f in fr))
                   for r, frames in per_rot.items()}
    ranked = sorted(scores, key=scores.get, reverse=True)  # type: ignore[arg-type]
    best, second = ranked[0], ranked[1]
    if frames_with[best] >= 2 and scores[best] >= 2 * scores[second] and scores[best] > 0:
        return best, scores[best] / (scores[best] + scores[second]), scores
    return None, 0.0, scores


def analyze(path: Path, probe: dict[str, Any], detector: FaceDetector, use_clip: bool = True) -> dict[str, Any]:
    frames = load_frames(path, float(probe.get("duration") or 0))
    if not frames:
        return {"auto": 0, "confidence": 0.0, "method": "none", "face_presence": 0.0,
                "scores": {}, "note": "No frames could be read."}

    per_rot: dict[int, list[list[Face]]] = {r: [detector.detect(rotate_img(f, r)) for f in frames]
                                             for r in ROTATIONS}
    rot, conf, scores = decide_from_faces(per_rot)
    method = f"faces ({detector.kind})"

    if rot is None and use_clip:
        clip = ClipScorer.get()
        if clip is not None:
            s = {r: clip.upright_prob([rotate_img(f, r) for f in frames]) for r in ROTATIONS}
            ranked = sorted(s, key=s.get, reverse=True)  # type: ignore[arg-type]
            rot = ranked[0]
            conf = s[ranked[0]] / (s[ranked[0]] + s[ranked[1]])
            scores = s
            method = "scene (CLIP)"

    if rot is None:
        rot, conf, method = 0, 0.0, "none"

    presence = sum(1 for fr in per_rot[rot] if any(f.area >= TALK_FACE_AREA_MIN for f in fr)) / len(frames)
    return {
        "auto": int(rot),
        "confidence": round(float(conf), 3),
        "method": method,
        "face_presence": round(presence, 3),
        "scores": {str(k): round(float(v), 3) for k, v in scores.items()},
    }
