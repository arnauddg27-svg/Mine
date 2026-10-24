"""Voiceover with Microsoft Edge neural voices (free) including word timestamps."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import get
from .script import Segment

log = logging.getLogger(__name__)


@dataclass
class Word:
    text: str
    start: float  # seconds, relative to the full track
    end: float


@dataclass
class SpokenSegment:
    role: str
    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)


@dataclass
class VoiceTrack:
    path: Path
    duration: float
    segments: list[SpokenSegment]

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]


def normalize_for_speech(text: str) -> str:
    text = text.replace("—", ", ").replace("–", ", ").replace("…", "...")
    text = re.sub(r'[“”"]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_PUNCT = re.compile(r"[^\w']+", re.UNICODE)


def _key(token: str) -> str:
    return _PUNCT.sub("", token).lower()


def align_punctuation(words: list[Word], text: str) -> list[Word]:
    """edge-tts word boundaries drop punctuation; copy it back from the source text.

    Walks both sequences in order and swaps in the original token whenever the
    letters match, so sentence ends survive into the captions. Unmatched words
    are left as they are.
    """
    tokens = text.split()
    ti = 0
    out: list[Word] = []
    for w in words:
        k = _key(w.text)
        j = ti
        matched = None
        while j < len(tokens) and j < ti + 3:  # small look-ahead tolerates merged/split tokens
            if _key(tokens[j]) == k and k:
                matched = j
                break
            j += 1
        if matched is not None:
            out.append(Word(tokens[matched], w.start, w.end))
            ti = matched + 1
        else:
            out.append(w)
    return out


def _patch_tls() -> None:
    """edge-tts pins certifi; add a corporate/proxy CA bundle when one is configured."""
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if not cafile or not os.path.exists(cafile):
        return
    try:
        import edge_tts.communicate as ec  # type: ignore

        ec._SSL_CTX.load_verify_locations(cafile=cafile)
    except Exception as exc:  # pragma: no cover - best effort
        log.debug("could not add CA bundle to edge-tts: %s", exc)


async def _synth_once(text: str, voice: str, rate: str, pitch: str, out_mp3: Path, proxy: str | None) -> list[Word]:
    import edge_tts  # imported lazily so the rest of the package works without it

    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary", proxy=proxy)
    words: list[Word] = []
    with open(out_mp3, "wb") as fh:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append(Word(str(chunk["text"]), start, start + chunk["duration"] / 1e7))
    if out_mp3.stat().st_size == 0:
        raise RuntimeError("edge-tts returned no audio")
    return words


def synth_segment(text: str, voice: str, rate: str, pitch: str, out_mp3: Path, proxy: str | None, attempts: int = 3) -> list[Word]:
    _patch_tls()
    last: Exception | None = None
    for i in range(attempts):
        try:
            return asyncio.run(_synth_once(text, voice, rate, pitch, out_mp3, proxy))
        except Exception as exc:  # network hiccups / transient 403s
            last = exc
            log.warning("edge-tts attempt %d/%d failed: %s", i + 1, attempts, exc)
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"edge-tts failed after {attempts} attempts: {last}")


def ffprobe_duration(path: str | Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def _to_wav(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )


def synthesize(segments: list[Segment], cfg: dict, workdir: Path) -> VoiceTrack:
    """Synthesize each segment separately, then join them with configurable pauses."""
    workdir.mkdir(parents=True, exist_ok=True)
    voice = get(cfg, "voice.name", "en-US-GuyNeural")
    rate = get(cfg, "voice.rate", "+0%")
    pitch = get(cfg, "voice.pitch", "+0Hz")
    pauses = get(cfg, "voice.pause_after", {}) or {}
    default_pause = float(get(cfg, "voice.default_pause", 0.5))
    proxy = os.environ.get("EDGE_TTS_PROXY") or None

    wavs: list[Path] = []
    spoken: list[SpokenSegment] = []
    cursor = 0.0
    for i, seg in enumerate(segments):
        text = normalize_for_speech(seg.text)
        if not text:
            continue
        mp3 = workdir / f"seg{i}_{seg.role}.mp3"
        wav = workdir / f"seg{i}_{seg.role}.wav"
        words = align_punctuation(synth_segment(text, voice, rate, pitch, mp3, proxy), text)
        _to_wav(mp3, wav)
        dur = ffprobe_duration(wav)
        shifted = [Word(w.text, cursor + w.start, cursor + min(w.end, dur)) for w in words]
        spoken.append(SpokenSegment(seg.role, text, cursor, cursor + dur, shifted))
        wavs.append(wav)
        pause = float(pauses.get(seg.role, default_pause)) if i < len(segments) - 1 else 0.0
        cursor += dur + pause

    if not wavs:
        raise ValueError("nothing to synthesize")

    # Join: pad each segment with its pause, then concat.
    out = workdir / "voice.wav"
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for w in wavs:
        args += ["-i", str(w)]
    parts = []
    for i, seg in enumerate(spoken):
        pause = (spoken[i + 1].start - seg.end) if i + 1 < len(spoken) else 0.0
        parts.append(f"[{i}:a]apad=pad_dur={pause:.3f}[a{i}]")
    chain = "".join(f"[a{i}]" for i in range(len(spoken)))
    filt = ";".join(parts) + f";{chain}concat=n={len(spoken)}:v=0:a=1[out]"
    args += ["-filter_complex", filt, "-map", "[out]", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(out)]
    subprocess.run(args, check=True)
    duration = ffprobe_duration(out)
    log.info("voiceover: %.1fs, %d words, voice=%s", duration, sum(len(s.words) for s in spoken), voice)
    return VoiceTrack(out, duration, spoken)
