"""End-to-end run: pick content, synthesize, caption, render, upload, record."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import background as bg_mod
from . import captions, llm, quotes, render, script as script_mod, tts, youtube
from .config import get
from .state import State

log = logging.getLogger(__name__)


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
    return script_mod.from_quote(q, cfg, rng)


def run_once(cfg: dict, *, upload: bool, privacy: str | None = None, seed: int | None = None, out_dir: Path | None = None) -> dict:
    t0 = time.time()
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

    track = tts.synthesize(script.spoken(), cfg, workdir)
    tail = float(get(cfg, "video.tail_seconds", 0.8))
    total = track.duration + tail

    ass_text = captions.build_ass(track, script, cfg, total_duration=total)
    ass_path = workdir / "captions.ass"
    ass_path.write_text(ass_text, encoding="utf-8")

    bg = bg_mod.choose(cfg, rng, total, cache_dir, assets_dir)
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
        "background_query": bg.query,
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
        )
        meta["video_id"] = video_id
        meta["url"] = f"https://youtube.com/shorts/{video_id}"
        state.record_upload(video_id=video_id, title=script.title, source=script.source, quote_id=script.quote_id, privacy=privacy)
    else:
        state.record_upload(video_id=None, title=script.title, source=script.source, quote_id=script.quote_id, privacy="not-uploaded")

    if script.quote_id:
        state.mark_quote(script.quote_id)
    state.save()
    (workdir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    meta["elapsed"] = round(time.time() - t0, 1)
    log.info("done in %.1fs -> %s", meta["elapsed"], out_mp4)
    return meta
