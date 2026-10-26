"""Where clips come from: YouTube videos/channels (via yt-dlp) or local video files.

sources.yaml lists what to clip. Each entry is one of:
    - url: https://www.youtube.com/watch?v=...      a single video
    - channel: https://www.youtube.com/@name/videos  newest uploads of a channel or playlist
    - file: path/to/video.mp4                         a local file you have rights to
plus `speaker:` (shown on screen and in the title) and optional `credit:`, `max_videos:`,
`title:` and `source_url:` (for files).
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import ROOT, get

log = logging.getLogger(__name__)


@dataclass
class SourceVideo:
    key: str                 # stable id: "yt:<id>" or "file:<hash>"
    title: str
    speaker: str
    url: str | None = None   # public link credited in the description
    channel: str | None = None
    credit: str | None = None
    duration: float | None = None
    local_path: Path | None = None

    @property
    def is_file(self) -> bool:
        return self.local_path is not None

    @property
    def credit_line(self) -> str:
        return self.credit or self.channel or self.speaker


class SourceError(RuntimeError):
    pass


BOT_CHECK_HINT = (
    "YouTube asked for a sign-in ('confirm you're not a bot'). This happens on cloud servers and CI runners. "
    "Run the clip pipeline on your own computer, or set clips.cookies_file to a cookies.txt exported from a browser."
)


def load_entries(cfg: dict) -> list[dict]:
    path = Path(get(cfg, "clips.sources_file", ROOT / "sources.yaml"))
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise SourceError(f"{path} not found; create it with the channels, videos or files to clip")
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(entries, list):
        raise SourceError(f"{path} must be a YAML list of sources")
    if not entries:
        raise SourceError(f"{path} has no sources yet; add at least one channel, video or file (see the examples in it)")
    for e in entries:
        if not any(k in e for k in ("url", "channel", "file")):
            raise SourceError(f"source entry needs url, channel or file: {e}")
        if not e.get("speaker"):
            raise SourceError(f"source entry needs a speaker name: {e}")
    return entries


def _ydl_opts(cfg: dict, **extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True}
    cookies = get(cfg, "clips.cookies_file", "")
    if cookies:
        opts["cookiefile"] = str(cookies)
    opts.update(extra)
    return opts


def _ydl(cfg: dict, **extra):
    import yt_dlp

    return yt_dlp.YoutubeDL(_ydl_opts(cfg, **extra))


def _wrap_ytdlp_error(exc: Exception) -> Exception:
    text = str(exc)
    if "confirm you" in text and "bot" in text:
        return SourceError(BOT_CHECK_HINT)
    return SourceError(text)


def _file_key(path: Path) -> str:
    st = path.stat()
    return "file:" + hashlib.sha1(f"{path.name}:{st.st_size}".encode()).hexdigest()[:12]


def _probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout.strip()
        return float(out)
    except Exception:
        return None


def resolve(entry: dict, cfg: dict) -> list[SourceVideo]:
    """Expand one sources.yaml entry into concrete videos (newest first for channels)."""
    speaker = entry["speaker"]
    credit = entry.get("credit")
    if "file" in entry:
        path = Path(entry["file"])
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            raise SourceError(f"source file not found: {path}")
        return [SourceVideo(
            key=_file_key(path), title=entry.get("title") or path.stem, speaker=speaker,
            url=entry.get("source_url"), channel=entry.get("channel_name"), credit=credit,
            duration=_probe_duration(path), local_path=path,
        )]

    try:
        if "url" in entry:
            with _ydl(cfg) as ydl:
                info = ydl.extract_info(entry["url"], download=False)
            return [_from_info(info, speaker, credit)]
        limit = int(entry.get("max_videos", get(cfg, "clips.max_videos_per_channel", 15)))
        with _ydl(cfg, extract_flat="in_playlist", playlistend=limit, noplaylist=False) as ydl:
            info = ydl.extract_info(entry["channel"], download=False)
    except Exception as exc:  # yt-dlp raises DownloadError subclasses
        raise _wrap_ytdlp_error(exc) from exc

    videos = []
    for item in (info.get("entries") or [])[:limit]:
        if not item or not item.get("id"):
            continue
        dur = item.get("duration")
        min_len = float(get(cfg, "clips.min_source_seconds", 180))
        if dur and dur < min_len:  # skip the channel's own Shorts and trailers
            continue
        videos.append(SourceVideo(
            key=f"yt:{item['id']}", title=item.get("title") or item["id"], speaker=speaker,
            url=item.get("url") if str(item.get("url", "")).startswith("http") else f"https://www.youtube.com/watch?v={item['id']}",
            channel=item.get("channel") or info.get("channel") or info.get("uploader"), credit=credit, duration=dur,
        ))
    return videos


def _from_info(info: dict, speaker: str, credit: str | None) -> SourceVideo:
    vid = info.get("id")
    return SourceVideo(
        key=f"yt:{vid}" if info.get("extractor_key", "").lower().startswith("youtube") else f"url:{vid}",
        title=info.get("title") or str(vid), speaker=speaker,
        url=info.get("webpage_url") or info.get("original_url"),
        channel=info.get("channel") or info.get("uploader"), credit=credit, duration=info.get("duration"),
    )


def fetch_audio(video: SourceVideo, workdir: Path, cfg: dict) -> Path:
    """16 kHz mono WAV of the whole video, for transcription."""
    workdir.mkdir(parents=True, exist_ok=True)
    wav = workdir / "audio16k.wav"
    src: Path
    if video.is_file:
        src = video.local_path  # type: ignore[assignment]
    else:
        try:
            with _ydl(cfg, format="bestaudio/best", outtmpl=str(workdir / "audio.%(ext)s")) as ydl:
                info = ydl.extract_info(video.url, download=True)
                src = Path(ydl.prepare_filename(info))
        except Exception as exc:
            raise _wrap_ytdlp_error(exc) from exc
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(wav)],
        check=True, timeout=3600,
    )
    if not video.is_file:
        src.unlink(missing_ok=True)
    return wav


def fetch_section(video: SourceVideo, start: float, end: float, workdir: Path, cfg: dict) -> Path:
    """Just the [start, end] part of the video as an MP4 whose t=0 is `start`."""
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "section.mp4"
    if video.is_file:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
             "-i", str(video.local_path), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
             "-c:a", "aac", "-b:a", "192k", str(out)],
            check=True, timeout=1800,
        )
        return out
    import yt_dlp.utils

    height = int(get(cfg, "clips.max_height", 1080))
    fmt = f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/bv*[height<={height}]+ba/b[height<={height}]/b"
    try:
        with _ydl(
            cfg,
            format=fmt,
            merge_output_format="mp4",
            outtmpl=str(workdir / "section_raw.%(ext)s"),
            download_ranges=yt_dlp.utils.download_range_func(None, [(start, end)]),
            force_keyframes_at_cuts=True,  # re-encode at the cut so t=0 is exactly `start`
        ) as ydl:
            info = ydl.extract_info(video.url, download=True)
            raw = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    except Exception as exc:
        raise _wrap_ytdlp_error(exc) from exc
    if not raw.exists():
        candidates = sorted(workdir.glob("section_raw.*"))
        if not candidates:
            raise SourceError("yt-dlp finished but produced no file")
        raw = candidates[0]
    raw.replace(out)
    return out
