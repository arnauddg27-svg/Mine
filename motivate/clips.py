"""Clip mode: find the best moments in long-form talks and turn them into captioned Shorts."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import highlights, reframe, render, sources, transcript, youtube
from .captions import build_ass
from .config import get
from .highlights import Candidate
from .pipeline import _slug, check_tools
from .script import Script, clean_tags, clean_text, safe_title
from .state import State
from .tts import SpokenSegment, VoiceTrack, Word

log = logging.getLogger(__name__)


def _safe_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", key)


def parse_time(value: str | float | None) -> float | None:
    """'75', '1:15', '01:02:03.5' -> seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parts = [float(p) for p in str(value).strip().split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


def load_words(video: sources.SourceVideo, cfg: dict, cache_dir: Path, workdir: Path) -> list[Word]:
    cache = cache_dir / "transcripts" / f"{_safe_key(video.key)}.json"
    if cache.exists():
        return transcript.transcribe(cache, cfg, cache)
    log.info("transcribing %s (%s)", video.title, video.key)
    audio = sources.fetch_audio(video, workdir / "audio", cfg)
    try:
        return transcript.transcribe(audio, cfg, cache)
    finally:
        audio.unlink(missing_ok=True)


def candidates_for(video: sources.SourceVideo, words: list[Word], cfg: dict, cache_dir: Path) -> list[Candidate]:
    cache = cache_dir / "candidates" / f"{_safe_key(video.key)}.json"
    if cache.exists():
        return [Candidate.from_dict(d) for d in json.loads(cache.read_text(encoding="utf-8"))]
    sents = transcript.sentences(words)
    cands = highlights.pick(sents, video.speaker, video.title, cfg)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([c.to_dict() for c in cands], indent=2, ensure_ascii=False), encoding="utf-8")
    return cands


def next_clip(cfg: dict, state: State, cache_dir: Path, workdir: Path, record: bool):
    """First unused candidate, rotating through sources so speakers alternate between runs."""
    entries = sources.load_entries(cfg)
    done = set(state.data.get("clips_done", []))
    exhausted = set(state.data.get("clip_videos_exhausted", []))
    turn = int(state.data.get("clip_turn", 0))
    errors: list[str] = []
    for offset in range(len(entries)):
        entry = entries[(turn + offset) % len(entries)]
        try:
            videos = sources.resolve(entry, cfg)
        except sources.SourceError as exc:
            log.warning("skipping source %s: %s", entry.get("url") or entry.get("channel") or entry.get("file"), exc)
            errors.append(str(exc))
            continue
        for video in videos:
            if video.key in exhausted:
                continue
            try:
                words = load_words(video, cfg, cache_dir, workdir)
            except sources.SourceError as exc:
                log.warning("skipping %s: %s", video.title, exc)
                errors.append(str(exc))
                continue
            for cand in candidates_for(video, words, cfg, cache_dir):
                if f"{video.key}|{cand.id}" not in done:
                    if record:
                        state.data["clip_turn"] = (turn + offset + 1) % len(entries)
                    return video, cand, words
            if record:
                state.data.setdefault("clip_videos_exhausted", []).append(video.key)
    detail = f" Last error: {errors[-1]}" if errors else ""
    raise RuntimeError(f"No unused clips left in sources.yaml; add more videos or channels.{detail}")


def describe(video: sources.SourceVideo, cand: Candidate, cfg: dict) -> tuple[str, str, list[str]]:
    suffix = get(cfg, "youtube.title_suffix", " #shorts")
    title = safe_title(clean_text(cand.title), suffix)
    speaker_tag = re.sub(r"[^a-z0-9]", "", video.speaker.lower())
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in [speaker_tag] + list(get(cfg, "youtube.hashtags", ["motivation", "shorts"])))
    lines = [clean_text(cand.description or cand.title), "", f"Speaker: {video.speaker}", f"Full video: {clean_text(video.title)}"]
    if video.url:
        lines.append(video.url)
    lines.append(f"Credit: {clean_text(video.credit_line)}")
    handle = get(cfg, "channel.handle", "")
    if handle:
        lines += ["", f"Follow {handle} for daily motivation."]
    lines += ["", hashtags]
    tags = clean_tags([video.speaker] + list(cand.tags) + list(get(cfg, "youtube.tags", [])))
    return title, "\n".join(lines), tags


def make_clip(
    video: sources.SourceVideo,
    cand: Candidate,
    words: list[Word] | None,
    cfg: dict,
    workdir: Path,
) -> dict:
    """Cut, reframe, caption and render one clip. Returns metadata (no upload)."""
    workdir.mkdir(parents=True, exist_ok=True)
    section = sources.fetch_section(video, cand.start, cand.end, workdir, cfg)
    if words is None:  # manual clip: transcribe just this section
        clip_words = transcript.transcribe(section, cfg)
    else:
        clip_words = transcript.words_between(words, cand.start, cand.end)
    duration = render.ffprobe_duration(section)
    plan = reframe.analyze(section, cfg)

    text = " ".join(w.text for w in clip_words)
    track = VoiceTrack(section, duration, [SpokenSegment("clip", text, 0.0, duration, clip_words)])
    title, description, tags = describe(video, cand, cfg)
    show_speaker = bool(get(cfg, "clips.show_speaker", True))
    script = Script(segments=[], title=title, description=description, tags=tags, author=video.speaker if show_speaker else None)
    hook = cand.hook if get(cfg, "clips.hook_overlay", True) else ""
    layout = {
        "caption_y": float(get(cfg, "clips.caption_y", 0.66)),
        "speaker_y": float(get(cfg, "clips.speaker_y", 0.73)),
        "hook": hook,
        # crop: just under the Shorts top bar; fit: above the video band
        "hook_y": 0.13 if plan.mode == "crop" else 0.22,
    }
    # Clip captions run smaller than quote captions so three words fit on one line;
    # the channel watermark gives way to the headline when there is one.
    ccfg = json.loads(json.dumps(cfg))
    ccfg.setdefault("captions", {})["font_size"] = int(get(cfg, "clips.font_size", 76))
    if hook:
        ccfg["captions"]["show_brand"] = False
    ass_path = workdir / "captions.ass"
    ass_path.write_text(build_ass(track, script, ccfg, total_duration=duration, layout=layout), encoding="utf-8")

    music = render.pick_music(cfg, random.Random(), Path(get(cfg, "paths.assets"))) if get(cfg, "clips.music", False) else None
    out_mp4 = workdir / "short.mp4"
    _, out_dur = render.render_clip(section, plan, ass_path, cfg, out_mp4, Path(get(cfg, "paths.assets")) / "fonts", music)
    render.thumbnail(out_mp4, min(1.0, out_dur / 2), workdir / "thumb.jpg")
    if not get(cfg, "clips.keep_section", False):
        section.unlink(missing_ok=True)
    meta = {
        "mode": "clips",
        "title": title,
        "description": description,
        "tags": tags,
        "speaker": video.speaker,
        "source_key": video.key,
        "source_title": video.title,
        "source_url": video.url,
        "clip": {"start": round(cand.start, 2), "end": round(cand.end, 2), "picker": cand.picker, "reason": cand.reason},
        "hook": layout["hook"],
        "layout": plan.mode,
        "face_coverage": round(plan.face_coverage, 2),
        "duration": round(out_dur, 2),
        "video": str(out_mp4),
    }
    (workdir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def upload_meta(meta: dict, cfg: dict, privacy: str | None) -> str:
    return youtube.upload_video(
        Path(meta["video"]),
        title=meta["title"],
        description=meta["description"],
        tags=meta["tags"],
        privacy=privacy or get(cfg, "youtube.privacy", "private"),
        category_id=str(get(cfg, "youtube.category_id", "22")),
        made_for_kids=bool(get(cfg, "youtube.made_for_kids", False)),
        default_language=get(cfg, "channel.language", "en"),
        token_path=Path(get(cfg, "paths.token")),
        notify_subscribers=bool(get(cfg, "youtube.notify_subscribers", True)),
    )


def run_clip_once(
    cfg: dict,
    *,
    upload: bool,
    privacy: str | None = None,
    out_dir: Path | None = None,
    record: bool | None = None,
) -> dict:
    t0 = time.time()
    check_tools()
    record = upload if record is None else record
    state = State(get(cfg, "paths.state"))
    cache_dir = Path(get(cfg, "paths.cache"))
    out_root = out_dir or Path(get(cfg, "paths.out"))
    scratch = cache_dir / "work"

    video, cand, words = next_clip(cfg, state, cache_dir, scratch, record)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    workdir = out_root / f"{stamp}-{_slug(cand.title)}"
    log.info("clip: %s [%s-%s] from %s", cand.title, f"{cand.start:.0f}", f"{cand.end:.0f}", video.title)
    meta = make_clip(video, cand, words, cfg, workdir)

    if upload:
        meta["video_id"] = upload_meta(meta, cfg, privacy)
        meta["url"] = f"https://youtube.com/shorts/{meta['video_id']}"
    if record:
        state.data.setdefault("clips_done", []).append(f"{video.key}|{cand.id}")
        state.record_upload(
            video_id=meta.get("video_id"), title=meta["title"], source="clip", clip_source=video.key,
            clip=cand.id, privacy=(privacy or get(cfg, "youtube.privacy", "private")) if upload else "not-uploaded",
        )
        state.save()
    else:
        log.info("dry run: state/history.json left untouched (use --record to mark this clip as used)")
    (workdir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    meta["elapsed"] = round(time.time() - t0, 1)
    log.info("done in %.1fs -> %s", meta["elapsed"], meta["video"])
    return meta


def manual_clip(
    cfg: dict,
    source: str,
    speaker: str,
    *,
    start: float | None,
    end: float | None,
    title: str | None,
    hook: str | None,
    upload: bool,
    privacy: str | None,
    out_dir: Path | None,
) -> dict:
    """Clip a specific video. With start/end, cut exactly there; otherwise pick the best moment."""
    check_tools()
    entry = {"file": source} if Path(source).exists() else {"url": source}
    entry["speaker"] = speaker
    video = sources.resolve(entry, cfg)[0]
    cache_dir = Path(get(cfg, "paths.cache"))
    out_root = out_dir or Path(get(cfg, "paths.out"))
    if start is not None and end is not None:
        if end <= start:
            raise ValueError("--end must be after --start")
        cand = Candidate(start, end, title or f"{speaker} on what matters", hook or "", picker="manual")
        words = None
    else:
        words = load_words(video, cfg, cache_dir, cache_dir / "work")
        cands = candidates_for(video, words, cfg, cache_dir)
        if not cands:
            raise RuntimeError("no suitable moment found in that video")
        cand = cands[0]
        if title:
            cand.title = title
        if hook is not None:
            cand.hook = hook
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    meta = make_clip(video, cand, words, cfg, out_root / f"{stamp}-{_slug(cand.title)}")
    if upload:
        meta["video_id"] = upload_meta(meta, cfg, privacy)
        meta["url"] = f"https://youtube.com/shorts/{meta['video_id']}"
    return meta
