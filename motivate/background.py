"""Background footage: Pexels stock video, local clips, or a procedural gradient."""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import get

log = logging.getLogger(__name__)

PEXELS_SEARCH = "https://api.pexels.com/videos/search"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}

# Dark duotone palettes for the procedural background (hex RGB).
PALETTES = [
    ("1b1f4a", "5b2a86", "0f5c7a"),
    ("3a1c2a", "7a2a3a", "2b2b5a"),
    ("123c36", "1f7a6e", "16385c"),
    ("2a2a2a", "6b5a30", "1f3a55"),
    ("2b0f55", "6a1fa0", "1a2466"),
    ("1f3650", "2f6f91", "4a3f66"),
    ("3b2412", "8a4a1a", "2d1f3a"),
]


@dataclass
class Background:
    kind: str  # video | gradient
    path: Path | None = None
    lavfi: str | None = None
    credit: str | None = None
    query: str | None = None


def _download(url: str, dst: Path, timeout: int = 120) -> None:
    tmp = dst.with_suffix(dst.suffix + ".part")
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
    tmp.replace(dst)


def _prune_cache(cache_dir: Path, keep: int) -> None:
    files = sorted(cache_dir.glob("pexels_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def from_pexels(cfg: dict, rng: random.Random, cache_dir: Path, min_duration: float) -> Background | None:
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        log.info("PEXELS_API_KEY not set; skipping stock footage")
        return None
    queries = list(get(cfg, "background.queries", ["mountain sunrise"]))
    query = rng.choice(queries)
    try:
        r = requests.get(
            PEXELS_SEARCH,
            headers={"Authorization": key},
            params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 40},
            timeout=30,
        )
        r.raise_for_status()
        videos = [v for v in r.json().get("videos", []) if v.get("duration", 0) >= max(4, min_duration / 3)]
    except Exception as exc:
        log.warning("Pexels search failed (%s): %s", query, exc)
        return None
    rng.shuffle(videos)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for v in videos[:6]:
        files = [
            f for f in v.get("video_files", [])
            if f.get("width") and f.get("height") and f["height"] > f["width"] and f.get("file_type") == "video/mp4"
        ]
        if not files:
            continue
        tall = sorted((f for f in files if f["height"] >= 1920), key=lambda f: f["height"])
        chosen = tall[0] if tall else sorted(files, key=lambda f: -f["height"])[0]
        dst = cache_dir / f"pexels_{v['id']}_{chosen['id']}.mp4"
        try:
            if not dst.exists():
                t0 = time.time()
                _download(chosen["link"], dst)
                log.info("downloaded %s (%dx%d) in %.1fs", dst.name, chosen["width"], chosen["height"], time.time() - t0)
            _prune_cache(cache_dir, int(get(cfg, "background.cache_keep", 24)))
            user = (v.get("user") or {}).get("name", "Pexels contributor")
            return Background("video", dst, None, f"Footage: {user} via Pexels ({v.get('url', 'https://www.pexels.com')})", query)
        except Exception as exc:
            log.warning("download failed for Pexels video %s: %s", v.get("id"), exc)
    return None


def from_local(cfg: dict, rng: random.Random, assets_dir: Path) -> Background | None:
    folder = Path(get(cfg, "background.local_dir", assets_dir / "backgrounds"))
    if not folder.is_dir():
        return None
    clips = [p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXT]
    if not clips:
        return None
    clip = rng.choice(clips)
    return Background("video", clip, None, None, None)


def gradient(cfg: dict, rng: random.Random, duration: float) -> Background:
    w = int(get(cfg, "video.width", 1080))
    h = int(get(cfg, "video.height", 1920))
    fps = int(get(cfg, "video.fps", 30))
    c0, c1, c2 = rng.choice(PALETTES)
    speed = rng.uniform(0.008, 0.02)
    # Corner-to-corner so the whole frame is covered; the filter slowly rotates it.
    if rng.random() < 0.5:
        x0, y0, x1, y1 = 0, 0, w, h
    else:
        x0, y0, x1, y1 = w, 0, 0, h
    lavfi = (
        f"gradients=s={w}x{h}:c0=0x{c0}:c1=0x{c1}:c2=0x{c2}:n=3:speed={speed:.4f}"
        f":x0={x0}:y0={y0}:x1={x1}:y1={y1}:r={fps}:d={duration + 1:.2f}"
    )
    return Background("gradient", None, lavfi, None, None)


def choose(cfg: dict, rng: random.Random, duration: float, cache_dir: Path, assets_dir: Path) -> Background:
    order = get(cfg, "background.sources", ["pexels", "local", "gradient"])
    for source in order:
        bg: Background | None = None
        if source == "pexels":
            bg = from_pexels(cfg, rng, cache_dir, duration)
        elif source == "local":
            bg = from_local(cfg, rng, assets_dir)
        elif source == "gradient":
            bg = gradient(cfg, rng, duration)
        if bg:
            log.info("background: %s%s", bg.kind, f" ({bg.query})" if bg.query else "")
            return bg
    return gradient(cfg, rng, duration)
