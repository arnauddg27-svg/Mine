"""Speech-to-text with word timestamps (faster-whisper), cached per source video."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import get
from .tts import Word

log = logging.getLogger(__name__)
SENTENCE_END = re.compile(r"[.!?]['\")\]]*$")


@dataclass
class Sentence:
    index: int
    text: str
    start: float
    end: float
    first_word: int
    last_word: int
    clean_start: bool = True  # previous sentence ended with . ! or ? (not a pause or length split)


def _load_audio(path: Path):
    """Decode to 16 kHz mono float32 with ffmpeg (avoids PyAV version issues)."""
    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:"],
        capture_output=True, check=True, timeout=3600,
    ).stdout
    return np.frombuffer(raw, np.float32)


def transcribe(audio_path: Path, cfg: dict, cache_path: Path | None = None) -> list[Word]:
    if cache_path and cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return [Word(w[2], w[0], w[1]) for w in data["words"]]

    from faster_whisper import WhisperModel

    model_name = get(cfg, "clips.whisper_model", "base.en")
    model_dir = get(cfg, "clips.whisper_dir", None)
    t0 = time.time()
    model = WhisperModel(model_name, device="cpu", compute_type="int8", download_root=model_dir)
    audio = _load_audio(audio_path)
    segments, _info = model.transcribe(audio, word_timestamps=True, vad_filter=True, beam_size=int(get(cfg, "clips.whisper_beam", 1)))
    words = [Word(w.word.strip(), round(w.start, 3), round(w.end, 3)) for s in segments for w in (s.words or []) if w.word.strip()]
    log.info("transcribed %.0f min of audio with %s in %.0fs (%d words)", len(audio) / 16000 / 60, model_name, time.time() - t0, len(words))
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"model": model_name, "words": [[w.start, w.end, w.text] for w in words]}), encoding="utf-8")
    return words


def sentences(words: list[Word], max_gap: float = 1.2, max_words: int = 45) -> list[Sentence]:
    """Group words into sentences: break on terminal punctuation, long pauses, or very long runs."""
    out: list[Sentence] = []
    start_i = 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        gap = (words[i + 1].start - w.end) if not last else 0.0
        if last or SENTENCE_END.search(w.text) or gap > max_gap or (i - start_i + 1) >= max_words:
            chunk = words[start_i : i + 1]
            clean = start_i == 0 or bool(SENTENCE_END.search(words[start_i - 1].text))
            out.append(Sentence(len(out), " ".join(x.text for x in chunk), chunk[0].start, chunk[-1].end, start_i, i, clean))
            start_i = i + 1
    return out


def words_between(words: list[Word], start: float, end: float) -> list[Word]:
    """Words fully inside [start, end], re-timed so `start` becomes 0."""
    return [Word(w.text, round(w.start - start, 3), round(w.end - start, 3)) for w in words if w.start >= start - 0.05 and w.end <= end + 0.05]


def for_llm(sents: list[Sentence]) -> str:
    def mmss(t: float) -> str:
        return f"{int(t // 60)}:{int(t % 60):02d}"

    return "\n".join(f"[{s.index}] ({mmss(s.start)}) {s.text}" for s in sents)
