"""Build an ASS subtitle file with word-by-word highlighted captions."""
from __future__ import annotations

import re

from .config import get
from .script import Script
from .tts import VoiceTrack, Word

SENTENCE_END = re.compile(r"[.!?;:]$")


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_color(hex_rgb: str | int, alpha: int = 0) -> str:
    """'RRGGBB' -> ASS '&HAABBGGRR' string. Accepts ints too (YAML reads 000000 as 0)."""
    if isinstance(hex_rgb, int):
        hex_rgb = f"{hex_rgb:06X}"
    hex_rgb = str(hex_rgb).strip().lstrip("#").zfill(6)
    r, g, b = hex_rgb[0:2], hex_rgb[2:4], hex_rgb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_override_color(hex_rgb: str | int) -> str:
    """'RRGGBB' -> '&HBBGGRR&' for inline \\1c overrides (no alpha byte, trailing &)."""
    return "&H" + ass_color(hex_rgb)[4:] + "&"


def _clean(text: str) -> str:
    text = text.replace("{", "").replace("}", "").replace("\\", "")
    return text.strip()


def _display(text: str) -> str:
    """Caption-friendly word: keep ? and !, drop trailing . , ; : and wrapping quotes."""
    text = _clean(text).strip('"\u201c\u201d')
    return text.rstrip(".,;:")


def paginate(words: list[Word], per_page: int) -> list[list[Word]]:
    pages: list[list[Word]] = []
    page: list[Word] = []
    for w in words:
        page.append(w)
        if len(page) >= per_page or SENTENCE_END.search(w.text):
            pages.append(page)
            page = []
    if page:
        pages.append(page)
    # Avoid lonely one-word pages: fold them into the previous page when it has room.
    merged: list[list[Word]] = []
    for pg in pages:
        if merged and len(pg) == 1 and len(merged[-1]) <= per_page:
            merged[-1].extend(pg)
        else:
            merged.append(pg)
    return merged


def build_ass(track: VoiceTrack, script: Script, cfg: dict, total_duration: float | None = None) -> str:
    w = int(get(cfg, "video.width", 1080))
    h = int(get(cfg, "video.height", 1920))
    font = get(cfg, "captions.font", "Inter")
    size = int(get(cfg, "captions.font_size", 92))
    per_page = int(get(cfg, "captions.words_per_page", 3))
    upper = bool(get(cfg, "captions.uppercase", True))
    style = get(cfg, "captions.style", "karaoke")  # karaoke | block
    highlight = ass_override_color(get(cfg, "captions.highlight_color", "FFD600"))
    offset = float(get(cfg, "captions.time_offset", 0.0))  # +0.1 shows captions 100 ms later
    base = ass_color(get(cfg, "captions.text_color", "FFFFFF"))
    outline = ass_color(get(cfg, "captions.outline_color", "000000"))
    shadow = ass_color("000000", alpha=0x80)
    pop = int(get(cfg, "captions.pop_scale", 108))
    total = total_duration or track.duration
    handle = get(cfg, "channel.handle", "")
    show_author = bool(get(cfg, "captions.show_author", True))
    show_brand = bool(get(cfg, "captions.show_brand", True))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{font},{size},{base},{base},{outline},{shadow},-1,0,0,0,100,100,1,0,1,7,3,5,70,70,0,1
Style: Author,{font},{int(size * 0.5)},&H30FFFFFF,&H30FFFFFF,{outline},{shadow},0,-1,0,0,100,100,0,0,1,3,1,2,90,90,330,1
Style: Brand,{font},{int(size * 0.42)},&H55FFFFFF,&H55FFFFFF,{outline},{shadow},-1,0,0,0,100,100,2,0,1,3,0,8,90,90,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    def add(start: float, end: float, style_name: str, text: str, layer: int = 0) -> None:
        if end <= start:
            return
        if style_name == "Word":
            start, end = max(0.0, start + offset), max(0.0, end + offset)
        events.append(f"Dialogue: {layer},{_ts(start)},{_ts(end)},{style_name},,0,0,0,,{text}")

    segments = track.segments
    for si, seg in enumerate(segments):
        next_start = segments[si + 1].words[0].start if si + 1 < len(segments) and segments[si + 1].words else total
        pages = paginate(seg.words, per_page)
        for pi, page in enumerate(pages):
            page_end_limit = pages[pi + 1][0].start if pi + 1 < len(pages) else next_start
            page_end = min(page[-1].end + 0.35, page_end_limit) if page_end_limit - page[-1].end > 0.6 else page_end_limit
            texts = [_display(x.text.upper() if upper else x.text) for x in page]
            if style == "block":
                add(page[0].start, page_end, "Word", " ".join(texts))
                continue
            for wi, word in enumerate(page):
                start = word.start
                end = page[wi + 1].start if wi + 1 < len(page) else page_end
                rendered = []
                for ti, t in enumerate(texts):
                    if ti == wi:
                        rendered.append(f"{{\\1c{highlight}\\fscx{pop}\\fscy{pop}}}{t}{{\\r}}")
                    else:
                        rendered.append(t)
                add(start, end, "Word", " ".join(rendered))

        if show_author and script.author and seg.role == "quote":
            add(seg.words[0].start if seg.words else seg.start, next_start, "Author", f"— {_clean(script.author)}")

    if show_brand and handle:
        add(0.0, total, "Brand", _clean(handle), layer=1)

    return header + "\n".join(events) + "\n"
