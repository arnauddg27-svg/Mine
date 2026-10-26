"""Turn landscape footage into 9:16.

Two layouts:
- crop: follow the speaker's face, one crop position per camera shot (changes only at cuts,
  so it looks like an editor framed it, not a wobbling auto-zoom);
- fit: the full frame centered over a blurred, zoomed copy of itself, used for two-person
  shots, slides, or footage where no single face dominates.
"""
from __future__ import annotations

import logging
import statistics
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT, get

log = logging.getLogger(__name__)
MODEL = ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLE_W, SAMPLE_H = 640, 360
FIT_BAND_TOP = 0.29  # top of the video band in fit layout, as a fraction of frame height


@dataclass
class Shot:
    start: float
    end: float
    cx: float | None  # face centre as a fraction of source width; None = no single face


@dataclass
class Plan:
    mode: str  # crop | fit
    src_w: int
    src_h: int
    shots: list[Shot] = field(default_factory=list)
    face_coverage: float = 0.0


def probe_size(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    vals = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    return int(vals["width"]), int(vals["height"]), float(vals.get("duration", 0) or 0)


def sample_frames(path: Path, fps: float):
    """Yield (t, BGR frame) at `fps`, downscaled to 640x360."""
    import numpy as np

    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps={fps},scale={SAMPLE_W}:{SAMPLE_H}",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:"],
        stdout=subprocess.PIPE,
    )
    size = SAMPLE_W * SAMPLE_H * 3
    i = 0
    try:
        while True:
            buf = proc.stdout.read(size)  # type: ignore[union-attr]
            if len(buf) < size:
                break
            yield i / fps, np.frombuffer(buf, np.uint8).reshape(SAMPLE_H, SAMPLE_W, 3)
            i += 1
    finally:
        proc.stdout.close()  # type: ignore[union-attr]
        proc.wait(timeout=60)


def detect_faces(frames, min_score: float = 0.85):
    """[(t, [(cx_frac, width_frac, score), ...], gray_thumb)] using OpenCV's YuNet model."""
    import cv2

    det = cv2.FaceDetectorYN.create(str(MODEL), "", (SAMPLE_W, SAMPLE_H), 0.6, 0.3, 20)
    out = []
    for t, img in frames:
        _, faces = det.detect(img)
        found = []
        if faces is not None:
            for f in faces:
                score = float(f[-1])
                if score >= min_score:
                    found.append(((float(f[0]) + float(f[2]) / 2) / SAMPLE_W, float(f[2]) / SAMPLE_W, score))
        found.sort(key=lambda x: x[1] * x[2], reverse=True)  # biggest confident face first
        thumb = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (32, 18)).astype("float32")
        out.append((t, found, thumb))
    return out


def split_shots(samples, duration: float, cut_threshold: float = 28.0) -> list[list]:
    """Group samples into camera shots by thumbnail difference."""
    import numpy as np

    shots: list[list] = []
    current: list = []
    prev = None
    for s in samples:
        if prev is not None and float(np.mean(np.abs(s[2] - prev))) > cut_threshold:
            shots.append(current)
            current = []
        current.append(s)
        prev = s[2]
    if current:
        shots.append(current)
    return shots


def plan_from_samples(samples, src_w: int, src_h: int, duration: float, cfg: dict) -> Plan:
    layout = get(cfg, "clips.layout", "auto")
    if src_w * 16 <= src_h * 9 + 1:  # already vertical (or narrower): nothing to reframe
        return Plan("crop", src_w, src_h, [Shot(0, duration, 0.5)], 1.0)
    groups = split_shots(samples, duration)
    shots: list[Shot] = []
    covered = 0.0
    for gi, g in enumerate(groups):
        start = g[0][0]
        end = groups[gi + 1][0][0] if gi + 1 < len(groups) else duration
        with_face = [faces for _, faces, _ in g if faces]
        two_shot = sum(1 for faces in with_face if len(faces) >= 2 and abs(faces[0][0] - faces[1][0]) > 0.3)
        if with_face and len(with_face) >= 0.5 * len(g) and two_shot < 0.5 * len(with_face):
            cx = statistics.median(faces[0][0] for faces in with_face)
            covered += end - start
        else:
            cx = None
        shots.append(Shot(start, end, cx))
    coverage = covered / duration if duration else 0.0
    if layout == "fit" or (layout == "auto" and coverage < float(get(cfg, "clips.crop_min_coverage", 0.7))):
        return Plan("fit", src_w, src_h, shots, coverage)
    # Shots without a usable face borrow the nearest framed shot's position.
    known = [s.cx for s in shots if s.cx is not None]
    fallback = statistics.median(known) if known else 0.5
    last = None
    for s in shots:
        if s.cx is None:
            s.cx = last if last is not None else fallback
        last = s.cx
    return Plan("crop", src_w, src_h, merge_shots(shots), coverage)


def merge_shots(shots: list[Shot], tol: float = 0.04) -> list[Shot]:
    merged: list[Shot] = []
    for s in shots:
        if merged and merged[-1].cx is not None and s.cx is not None and abs(merged[-1].cx - s.cx) <= tol:
            merged[-1].end = s.end
        else:
            merged.append(Shot(s.start, s.end, s.cx))
    return merged


def analyze(path: Path, cfg: dict) -> Plan:
    src_w, src_h, duration = probe_size(path)
    layout = get(cfg, "clips.layout", "auto")
    if layout == "fit":
        return Plan("fit", src_w, src_h, [Shot(0, duration, None)], 0.0)
    if not MODEL.exists():
        log.warning("face model missing (%s); using the fit layout", MODEL)
        return Plan("fit", src_w, src_h, [Shot(0, duration, None)], 0.0)
    try:
        samples = detect_faces(sample_frames(path, float(get(cfg, "clips.analysis_fps", 2))))
    except Exception as exc:  # OpenCV missing or model incompatible: never fail the run over framing
        log.warning("face analysis failed (%s); using the fit layout", exc)
        return Plan("fit", src_w, src_h, [Shot(0, duration, None)], 0.0)
    plan = plan_from_samples(samples, src_w, src_h, duration, cfg)
    log.info("reframe: %s layout, faces in %.0f%% of the clip, %d shot(s)", plan.mode, plan.face_coverage * 100, len(plan.shots))
    return plan


def video_filter(plan: Plan, out_w: int, out_h: int, fps: int) -> str:
    """ffmpeg filter chain from [0:v] to a 9:16 stream (without the [v] label)."""
    if plan.mode == "crop":
        crop_w = min(plan.src_w, int(round(plan.src_h * out_w / out_h / 2)) * 2)
        max_x = plan.src_w - crop_w

        def x_for(cx: float) -> int:
            return int(min(max(cx * plan.src_w - crop_w / 2, 0), max_x))

        expr = str(x_for(plan.shots[-1].cx if plan.shots[-1].cx is not None else 0.5))
        for s in reversed(plan.shots[:-1]):
            expr = f"if(lt(t,{s.end:.3f}),{x_for(s.cx if s.cx is not None else 0.5)},{expr})"
        return (
            f"[0:v]crop=w={crop_w}:h={plan.src_h}:x='{expr}':y=0,scale={out_w}:{out_h},setsar=1,"
            f"fps={fps},format=yuv420p"
        )
    band_top = int(out_h * FIT_BAND_TOP)
    return (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},"
        f"gblur=sigma=28,eq=brightness=-0.12:saturation=0.9[bg];"
        f"[fgsrc]scale={out_w}:-2[fg];"
        f"[bg][fg]overlay=x=(W-w)/2:y={band_top},setsar=1,fps={fps},format=yuv420p"
    )
