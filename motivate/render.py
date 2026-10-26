"""Compose background + voice + music + captions into a 9:16 MP4 with ffmpeg."""
from __future__ import annotations

import logging
import random
import subprocess
from pathlib import Path

from .background import Background
from .config import get
from .tts import VoiceTrack, ffprobe_duration

log = logging.getLogger(__name__)
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def esc(value: str | Path) -> str:
    """Escape a path for use inside a single-quoted ffmpeg filter option.

    ffmpeg tokenizes a filter string twice (graph level, then option level) and
    does not process escapes inside quotes, so: backslashes and colons get one
    backslash (passed through level 1 verbatim, unescaped at level 2), and a
    single quote closes the quoted run, emits \\\\\\' (level 1 -> \\', level 2 -> ')
    and reopens the quote.
    """
    s = str(value)
    s = s.replace("\\", "\\\\").replace(":", "\\:")
    return s.replace("'", "'\\\\\\''")


def pick_music(cfg: dict, rng: random.Random, assets_dir: Path) -> Path | None:
    if not get(cfg, "music.enabled", True):
        return None
    folder = Path(get(cfg, "music.dir", assets_dir / "music"))
    if not folder.is_dir():
        return None
    tracks = [p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXT]
    return rng.choice(tracks) if tracks else None


def render(
    background: Background,
    voice: VoiceTrack,
    ass_path: Path,
    music_path: Path | None,
    cfg: dict,
    out_path: Path,
    fonts_dir: Path,
) -> tuple[Path, float]:
    w = int(get(cfg, "video.width", 1080))
    h = int(get(cfg, "video.height", 1920))
    fps = int(get(cfg, "video.fps", 30))
    tail = float(get(cfg, "video.tail_seconds", 0.8))
    crf = int(get(cfg, "video.crf", 19))
    preset = get(cfg, "video.preset", "medium")
    darken = float(get(cfg, "video.darken", 0.08))
    music_vol = float(get(cfg, "music.volume", 0.18))
    duck = bool(get(cfg, "music.duck", True))
    total = round(voice.duration + tail, 3)
    max_seconds = float(get(cfg, "video.max_seconds", 59))
    if total > max_seconds:
        raise ValueError(f"video would be {total:.1f}s, over the {max_seconds:.0f}s limit; shorten the script")

    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if background.kind == "video" and background.path:
        args += ["-stream_loop", "-1", "-i", str(background.path)]
    else:
        lavfi = background.lavfi or f"color=c=0x101020:s={w}x{h}:r={fps}"
        args += ["-f", "lavfi", "-i", lavfi]
    args += ["-i", str(voice.path)]
    if music_path:
        args += ["-stream_loop", "-1", "-i", str(music_path)]

    if background.kind == "video":
        grade = f"eq=brightness=-{darken:.3f}:contrast=1.04:saturation=1.06,vignette=angle=PI/4.5,"
    else:
        grade = "vignette=angle=PI/5.5,"
    vf = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps},format=yuv420p,"
        f"{grade}"
        f"subtitles=filename='{esc(ass_path)}':fontsdir='{esc(fonts_dir)}'[v]"
    )
    voice_chain = f"[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo,apad=pad_dur={tail:.3f}"
    if music_path:
        fade_out_start = max(total - 2.5, 0.0)
        music_chain = (
            f"[2:a]aresample=48000,aformat=channel_layouts=stereo,volume={music_vol:.3f},"
            f"afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out_start:.3f}:d=2.5"
        )
        if duck:
            # Sidechain: the voice drives a compressor on the music (about -8 dB while speaking).
            af = (
                f"{voice_chain},asplit=2[voice][key];{music_chain}[music];"
                f"[music][key]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=350[bed];"
                f"[voice][bed]amix=inputs=2:duration=first:normalize=0[a]"
            )
        else:
            af = f"{voice_chain}[voice];{music_chain}[music];[voice][music]amix=inputs=2:duration=first:normalize=0[a]"
        amap = "[a]"
    else:
        af = f"{voice_chain}[voice]"
        amap = "[voice]"

    args += [
        "-filter_complex", vf + ";" + af,
        "-map", "[v]", "-map", amap,
        "-t", f"{total:.3f}",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(out_path),
    ]
    log.info("rendering %s (%.1fs)", out_path.name, total)
    timeout = int(get(cfg, "video.render_timeout", 900))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 and background.kind != "video":
        # Older ffmpeg builds lack some `gradients` options; fall back to a flat colour.
        log.warning("gradient background failed (%s); retrying with a flat colour", proc.stderr.strip().splitlines()[-1:] or "?")
        idx = args.index("-i", args.index("lavfi"))
        args[idx + 1] = f"color=c=0x1b1f4a:s={w}x{h}:r={fps}:d={total + 1:.2f}"
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr[-4000:]}")
    return out_path, ffprobe_duration(out_path)


def thumbnail(video_path: Path, at: float, out_path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(video_path), "-frames:v", "1", "-q:v", "2", str(out_path)],
        check=True,
        timeout=120,
    )
    return out_path
