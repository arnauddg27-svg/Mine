"""End-to-end run: pick content, synthesize, caption, render, upload, record."""
from __future__ import annotations

import json
import logging
import random
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import background as bg_mod
from . import captions, llm, quotes, render, script as script_mod, tts, youtube
from .config import get
from .state import State

log = logging.getLogger(__name__)


def check_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"{' and '.join(missing)} not found on PATH; install ffmpeg (apt install ffmpeg / brew install ffmpeg)")


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "short"


def choose_script(cfg: dict, rng: random.Random, state: State, assets_dir: Path) -> script_mod.Script:
    source = get(cfg, "script.source", "bank")
    use_llm = source == "claude" or (source == "mixed" and rng.random() < float(get(cfg, "llm.mixed_ratio", 0.5)))
    if use_llm:
        s = llm.generate_script(cfg, rng, state.recent_titles())
        if s:
            return s
    bank = quotes.load_bank(assets_dir / "quotes.json")
    used = state.used_quote_ids
    if len(used) >= len(bank):
        log.info("every quote has been used once; starting a new cycle")
        state.reset_quotes()
        used = set()
    q = quotes.pick_quote(bank, used, rng, int(get(cfg, "script.max_quote_words", 36)))
    return script_mod.from_quote(q, cfg, rng, avoid_hooks=state.recent_values("hook", 3))


def run_once(cfg: dict, *, upload: bool, privacy: str | None = None, seed: int | None = None, out_dir: Path | None = None) -> dict:
    t0 = time.time()
    check_tools()
    rng = random.Random(seed)
    assets_dir = Path(get(cfg, "paths.assets"))
    cache_dir = Path(get(cfg, "paths.cache"))
    out_root = out_dir or Path(get(cfg, "paths.out"))
    state = State(get(cfg, "paths.state"))

    script = choose_script(cfg, rng, state, assets_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    workdir = out_root / f"{stamp}-{_slug(script.title)}"
    workdir.mkdir(parents=True, exist_ok=True)
    log.info("script [%s]: %s", script.source, script.title)

    voices = get(cfg, "voice.names") or []
    voice = rng.choice(list(voices)) if voices else None
    track = tts.synthesize(script.spoken(), cfg, workdir, voice=voice)
    tail = float(get(cfg, "video.tail_seconds", 0.8))
    total = track.duration + tail

    ass_text = captions.build_ass(track, script, cfg, total_duration=total)
    ass_path = workdir / "captions.ass"
    ass_path.write_text(ass_text, encoding="utf-8")

    bg = bg_mod.choose(cfg, rng, total, cache_dir, assets_dir, theme=script.theme, avoid_ids=set(state.recent_values("background_id", 30)))
    music = render.pick_music(cfg, rng, assets_dir)
    if music:
        log.info("music: %s", music.name)

    out_mp4 = workdir / "short.mp4"
    fonts_dir = assets_dir / "fonts"
    _, duration = render.render(bg, track, ass_path, music, cfg, out_mp4, fonts_dir)
    render.thumbnail(out_mp4, min(1.5, duration / 2), workdir / "thumb.jpg")

    description = script.description
    if bg.credit:
        description += f"\n\n{bg.credit}"
    if get(cfg, "youtube.ai_disclosure", True):
        description += "\n\nVoiceover generated with AI text-to-speech."

    meta = {
        "title": script.title,
        "description": description,
        "tags": script.tags,
        "author": script.author,
        "source": script.source,
        "theme": script.theme,
        "duration": round(duration, 2),
        "background": bg.kind,
        "background_id": bg.id,
        "background_query": bg.query,
        "voice": voice or get(cfg, "voice.name"),
        "music": music.name if music else None,
        "video": str(out_mp4),
        "segments": [{"role": s.role, "text": s.text} for s in script.segments],
    }
    (workdir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    video_id = None
    if upload:
        privacy = privacy or get(cfg, "youtube.privacy", "private")
        video_id = youtube.upload_video(
            out_mp4,
            title=script.title,
            description=description,
            tags=script.tags,
            privacy=privacy,
            category_id=str(get(cfg, "youtube.category_id", "22")),
            made_for_kids=bool(get(cfg, "youtube.made_for_kids", False)),
            default_language=get(cfg, "channel.language", "en"),
            token_path=Path(get(cfg, "paths.token")),
            notify_subscribers=bool(get(cfg, "youtube.notify_subscribers", True)),
            contains_synthetic_media=bool(get(cfg, "youtube.synthetic_media_label", False)),
        )
        meta["video_id"] = video_id
        meta["url"] = f"https://youtube.com/shorts/{video_id}"
    hook = next((s.text for s in script.segments if s.role == "hook"), None)
    state.record_upload(
        video_id=video_id,
        title=script.title,
        source=script.source,
        quote_id=script.quote_id,
        privacy=privacy if upload else "not-uploaded",
        hook=hook,
        background_id=bg.id,
    )

    if script.quote_id:
        state.mark_quote(script.quote_id)
    state.save()
    (workdir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    meta["elapsed"] = round(time.time() - t0, 1)
    log.info("done in %.1fs -> %s", meta["elapsed"], out_mp4)
    return meta
