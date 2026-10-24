"""Turn a quote (or an LLM result) into a timed, captioned script plus YouTube metadata."""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from .config import get
from .quotes import Quote

# Original hook lines (spoken before the quote). Keep them short.
HOOKS = [
    "Stop scrolling. You need this today.",
    "Read this before you give up.",
    "Save this for your hardest day.",
    "This is the reminder you needed.",
    "Remember this when it gets hard.",
    "One line that changes everything.",
    "Read this twice.",
    "Nobody told you this growing up.",
]

MAX_TITLE = 100
MAX_TAGS_CHARS = 480


@dataclass
class Segment:
    role: str  # hook | quote | body | cta
    text: str


@dataclass
class Script:
    segments: list[Segment]
    title: str
    description: str
    tags: list[str]
    author: str | None = None
    quote_id: str | None = None
    source: str = "bank"
    theme: str | None = None
    extra: dict = field(default_factory=dict)

    def spoken(self) -> list[Segment]:
        return [s for s in self.segments if s.text.strip()]


def clean_tags(tags: list[str], limit: int = MAX_TAGS_CHARS) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for t in tags:
        t = re.sub(r"[#<>]", "", str(t)).strip().lower()
        if not t or t in seen or len(t) > 30:
            continue
        if total + len(t) + 2 > limit:
            break
        seen.add(t)
        out.append(t)
        total += len(t) + 2
    return out


def safe_title(title: str, suffix: str = "") -> str:
    title = re.sub(r"[<>]", "", title).strip()
    room = MAX_TITLE - len(suffix)
    if len(title) > room:
        title = title[: room - 1].rstrip(" ,;:-") + "…"
    return title + suffix


def title_from_quote(q: Quote, suffix: str) -> str:
    text = q.text.strip().rstrip(".")
    base = f'"{text}" - {q.author}'
    if len(base) + len(suffix) <= MAX_TITLE:
        return base + suffix
    # Shorten the quote part, keep the author.
    room = MAX_TITLE - len(suffix) - len(f'"…" - {q.author}')
    short = text[: max(room, 20)].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return safe_title(f'"{short}…" - {q.author}', suffix)


def from_quote(q: Quote, cfg: dict, rng: random.Random) -> Script:
    segments: list[Segment] = []
    if get(cfg, "script.hook", True):
        segments.append(Segment("hook", rng.choice(HOOKS)))
    segments.append(Segment("quote", q.text))
    cta = get(cfg, "script.cta", "")
    if cta:
        segments.append(Segment("cta", cta))

    suffix = get(cfg, "youtube.title_suffix", " #shorts")
    handle = get(cfg, "channel.handle", "")
    base_tags = list(get(cfg, "youtube.tags", []))
    tags = clean_tags(base_tags + q.tags + [q.author, "quotes", "motivational quotes"])
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in get(cfg, "youtube.hashtags", ["motivation", "mindset", "shorts"]))
    lines = [f'"{q.text}"', f"— {q.author}", ""]
    if handle:
        lines.append(f"Follow {handle} for daily motivation.")
    lines += ["", hashtags]
    return Script(
        segments=segments,
        title=title_from_quote(q, suffix),
        description="\n".join(lines),
        tags=tags,
        author=q.author,
        quote_id=q.id,
        source="bank",
        theme=(q.tags[0] if q.tags else None),
    )


def from_llm(data: dict, cfg: dict, theme: str) -> Script:
    """Build a Script from the JSON the LLM returned (validated upstream)."""
    segments: list[Segment] = []
    if data.get("hook"):
        segments.append(Segment("hook", data["hook"].strip()))
    segments.append(Segment("body", data["body"].strip()))
    cta = get(cfg, "script.cta", "")
    if cta:
        segments.append(Segment("cta", cta))
    suffix = get(cfg, "youtube.title_suffix", " #shorts")
    handle = get(cfg, "channel.handle", "")
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in get(cfg, "youtube.hashtags", ["motivation", "mindset", "shorts"]))
    desc = data.get("description", "").strip()
    if handle:
        desc += f"\n\nFollow {handle} for daily motivation."
    desc += f"\n\n{hashtags}"
    tags = clean_tags(list(get(cfg, "youtube.tags", [])) + list(data.get("tags", [])) + [theme])
    return Script(
        segments=segments,
        title=safe_title(data["title"], suffix),
        description=desc,
        tags=tags,
        author=None,
        quote_id=None,
        source="claude",
        theme=theme,
    )
