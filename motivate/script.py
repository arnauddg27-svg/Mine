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
    "If today feels heavy, listen.",
    "Here is the truth about hard days.",
    "Send this to someone who needs it.",
    "Write this down somewhere you'll see it.",
    "Before you quit, hear this.",
    "This took me years to understand.",
    "Keep this in your pocket today.",
    "Most people never hear this in time.",
]

DEFAULT_CTAS = ["Follow for your daily dose of motivation."]

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
    footage_query: str | None = None  # stock-footage search term chosen for this script
    extra: dict = field(default_factory=dict)

    def spoken(self) -> list[Segment]:
        return [s for s in self.segments if s.text.strip()]


def tag_cost(tag: str) -> int:
    """Characters YouTube counts toward its 500-char tag limit: a separating comma,
    plus two quote marks around any tag that contains a space."""
    return len(tag) + 1 + (2 if " " in tag else 0)


def clean_tags(tags: list[str], limit: int = MAX_TAGS_CHARS) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for t in tags:
        t = re.sub(r"[#<>,\"]", "", str(t)).strip().lower()
        t = re.sub(r"\s+", " ", t)
        if not t or t in seen or len(t) > 30:
            continue
        if total + tag_cost(t) > limit:
            continue  # a shorter tag later in the list may still fit
        seen.add(t)
        out.append(t)
        total += tag_cost(t)
    return out


def clean_text(text: str) -> str:
    """YouTube rejects < and > in titles and descriptions."""
    return re.sub(r"[<>]", "", text)


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


def pick_cta(cfg: dict, rng: random.Random) -> str:
    cta = get(cfg, "script.cta", DEFAULT_CTAS)
    if isinstance(cta, list):
        return rng.choice(cta) if cta else ""
    return str(cta or "")


def pick_hook(rng: random.Random, avoid: list[str] | None = None) -> str:
    pool = [h for h in HOOKS if h not in (avoid or [])] or HOOKS
    return rng.choice(pool)


def from_quote(q: Quote, cfg: dict, rng: random.Random, avoid_hooks: list[str] | None = None) -> Script:
    segments: list[Segment] = []
    if get(cfg, "script.hook", True):
        segments.append(Segment("hook", pick_hook(rng, avoid_hooks)))
    segments.append(Segment("quote", q.text))
    cta = pick_cta(cfg, rng)
    if cta:
        segments.append(Segment("cta", cta))

    suffix = get(cfg, "youtube.title_suffix", " #shorts")
    handle = get(cfg, "channel.handle", "")
    base_tags = list(get(cfg, "youtube.tags", []))
    tags = clean_tags(base_tags + q.tags + [q.author, "quotes", "motivational quotes"])
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in get(cfg, "youtube.hashtags", ["motivation", "mindset", "shorts"]))
    attribution = f"— {q.author}, {q.source}" if q.source else f"— {q.author}"
    lines = [f'"{q.text}"', attribution, ""]
    if handle:
        lines.append(f"Follow {handle} for daily motivation.")
    lines += ["", hashtags]
    return Script(
        segments=segments,
        title=title_from_quote(q, suffix),
        description=clean_text("\n".join(lines)),
        tags=tags,
        author=q.author,
        quote_id=q.id,
        source="bank",
        theme=(q.tags[0] if q.tags else None),
    )


def from_llm(data: dict, cfg: dict, theme: str, rng: random.Random | None = None) -> Script:
    """Build a Script from the JSON the LLM returned (validated upstream)."""
    segments: list[Segment] = []
    if data.get("hook"):
        segments.append(Segment("hook", data["hook"].strip()))
    segments.append(Segment("body", data["body"].strip()))
    cta = pick_cta(cfg, rng or random.Random())
    if cta:
        segments.append(Segment("cta", cta))
    suffix = get(cfg, "youtube.title_suffix", " #shorts")
    handle = get(cfg, "channel.handle", "")
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in get(cfg, "youtube.hashtags", ["motivation", "mindset", "shorts"]))
    desc = clean_text(data.get("description", "")).strip()
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
        footage_query=(data.get("footage") or "").strip() or None,
    )
