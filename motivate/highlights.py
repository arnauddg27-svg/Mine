"""Pick the best self-contained moments in a transcript.

Claude reads the numbered transcript and returns sentence ranges, so every cut lands on a
sentence boundary. Without an API key, a keyword heuristic picks the moments instead.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field

from .config import get
from .transcript import Sentence, for_llm

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    start: float
    end: float
    title: str
    hook: str = ""                 # short on-screen headline
    description: str = ""
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    picker: str = "heuristic"

    @property
    def id(self) -> str:
        return f"{self.start:.1f}-{self.end:.1f}"

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def span(sents: list[Sentence], first: int, last: int, min_s: float, max_s: float) -> tuple[float, float] | None:
    """Times for sentences first..last, trimmed from the end to fit max_s; None if too short."""
    if not (0 <= first <= last < len(sents)):
        return None
    while last > first and sents[last].end - sents[first].start > max_s:
        last -= 1
    start, end = sents[first].start, sents[last].end
    if end - start > max_s or end - start < min_s:
        return None
    return max(0.0, start - 0.15), end + 0.35


def overlaps(a: Candidate, b: Candidate) -> bool:
    return a.start < b.end and b.start < a.end


# --------------------------------------------------------------------------- Claude

SYSTEM = """You are the editor of a motivational YouTube Shorts channel that posts short clips of {speaker}.

From the numbered transcript, choose the moments that work best as standalone vertical clips.

A good clip:
- makes complete sense with no earlier context; it does not start mid-thought, with "and", "so", "but",
  or a reference like "that" or "he" whose meaning was set up earlier;
- opens strong: the first sentence earns attention on its own;
- delivers one clear idea about discipline, responsibility, meaning, courage, habits, relationships,
  purpose or facing hardship, and ends on a finished thought;
- is mostly the speaker talking, not an interviewer asking questions.

Avoid partisan politics, attacks on named people or groups, medical or financial advice, and anything that
would read as mocking the speaker. If nothing in the transcript is good enough, return fewer clips or none.

For each clip return the first and last sentence numbers (inclusive) so it runs {min_s}-{max_s} seconds,
plus:
- title: a specific YouTube title under 70 characters that names {speaker}, no clickbait, no ALL CAPS;
- hook: an on-screen headline of at most 7 words that sets up the clip (not a quote from it);
- description: 1-2 sentences, no hashtags;
- tags: 4-8 lowercase topic phrases;
- reason: one short line on why this moment works.
Rank the clips best first."""

SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first": {"type": "integer"},
                    "last": {"type": "integer"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "description": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["first", "last", "title", "hook", "description", "tags", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def pick_with_claude(sents: list[Sentence], speaker: str, video_title: str, cfg: dict, n: int) -> list[Candidate] | None:
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK not installed; using the heuristic picker")
        return None
    min_s = float(get(cfg, "clips.min_seconds", 20))
    max_s = float(get(cfg, "clips.max_seconds", 58))
    user = (
        f"Video: {video_title}\nSpeaker: {speaker}\nReturn up to {n} clips.\n\n"
        f"Transcript (sentence number, timestamp, text):\n{for_llm(sents)}"
    )
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=get(cfg, "llm.model", "claude-opus-5-5"),
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM.format(speaker=speaker, min_s=int(min_s), max_s=int(max_s)),
            output_config={"effort": get(cfg, "clips.picker_effort", "high"), "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            log.warning("Claude declined (%s); using the heuristic picker", getattr(response.stop_details, "category", None))
            return None
        if response.stop_reason == "max_tokens":
            log.warning("Claude ran out of output tokens; using the heuristic picker")
            return None
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
    except anthropic.APIStatusError as exc:
        log.warning("Claude API error %s: %s; using the heuristic picker", exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        log.warning("Claude connection error: %s; using the heuristic picker", exc)
        return None
    except Exception as exc:
        log.warning("Claude moment picking failed: %s; using the heuristic picker", exc)
        return None

    out: list[Candidate] = []
    for c in data.get("clips", []):
        times = span(sents, int(c["first"]), int(c["last"]), min_s, max_s)
        if not times or not str(c.get("title", "")).strip():
            continue
        cand = Candidate(
            times[0], times[1], c["title"].strip(), " ".join(str(c.get("hook", "")).split()[:8]),
            c.get("description", "").strip(), list(c.get("tags", []))[:8], c.get("reason", ""), "claude",
        )
        if not any(overlaps(cand, o) for o in out):
            out.append(cand)
    log.info("Claude picked %d clip(s)", len(out))
    return out[:n]


# --------------------------------------------------------------------------- heuristic

LEXICON = {
    "discipline", "responsibility", "responsible", "meaning", "meaningful", "purpose", "courage", "brave",
    "fear", "afraid", "truth", "honest", "goal", "goals", "habit", "habits", "sacrifice", "suffering", "suffer",
    "strength", "strong", "future", "better", "effort", "fail", "failure", "success", "succeed", "change",
    "decide", "decision", "choose", "choice", "aim", "character", "adversity", "struggle", "pain", "grow",
    "growth", "potential", "hard", "difficult", "work", "life", "yourself", "dream", "dreams", "commit",
    "commitment", "confidence", "resilience", "focus", "patience", "persist", "quit", "start",
}
WEAK_OPENERS = {"and", "but", "so", "because", "or", "which", "that", "it", "he", "she", "they", "this", "yeah",
                "um", "uh", "well", "like", "okay", "right"}
WEAK_OPENING_PHRASES = ("you know", "i think", "i mean", "kind of", "sort of")
FILLERS = ("you know", "kind of", "sort of", "i mean", " um ", " uh ", " like, ")

# Lexicon word -> how a title names the topic ("Speaker on facing fear").
TOPICS = {
    "responsibility": "taking responsibility", "responsible": "taking responsibility", "discipline": "discipline",
    "meaning": "finding meaning", "meaningful": "finding meaning", "purpose": "finding your purpose",
    "courage": "courage", "brave": "courage", "fear": "facing fear", "afraid": "facing fear", "truth": "telling the truth",
    "honest": "honesty", "goal": "setting goals", "goals": "setting goals", "habit": "building habits",
    "habits": "building habits", "sacrifice": "sacrifice", "suffering": "suffering", "suffer": "suffering",
    "failure": "failure", "fail": "failure", "success": "success", "succeed": "success", "change": "changing your life",
    "decide": "making decisions", "decision": "making decisions", "choose": "making choices", "choice": "making choices",
    "character": "character", "adversity": "adversity", "struggle": "struggle", "pain": "pain", "growth": "growth",
    "grow": "growth", "potential": "your potential", "work": "hard work", "dream": "your dreams", "dreams": "your dreams",
    "confidence": "confidence", "resilience": "resilience", "focus": "focus", "patience": "patience", "quit": "not quitting",
    "commitment": "commitment", "commit": "commitment", "future": "the future", "strength": "strength", "strong": "strength",
}


def score_window(sents: list[Sentence]) -> float:
    text = " ".join(s.text for s in sents)
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) < 25:
        return -1.0
    first_text = sents[0].text.strip()
    if not sents[0].clean_start or not first_text[:1].isupper():
        return -1.0  # starts mid-sentence
    hits = sum(1 for w in words if w in LEXICON)
    you = sum(1 for w in words if w in ("you", "your", "you're", "yourself"))
    questions = sum(1 for s in sents if s.text.rstrip().endswith("?"))
    padded = f" {text.lower()} "
    fillers = sum(padded.count(f) for f in FILLERS)
    score = (hits + 0.5 * you) / len(words) * 100 - 4 * questions - 25 * fillers / len(words)
    opening = first_text.lower()
    if opening.split(" ", 1)[0].strip(",.") in WEAK_OPENERS or opening.startswith(WEAK_OPENING_PHRASES):
        score -= 6
    if not sents[-1].text.rstrip().endswith((".", "!")):
        score -= 3
    return score


def topic_of(sents: list[Sentence]) -> str | None:
    counts: dict[str, int] = {}
    for w in re.findall(r"[a-z']+", " ".join(s.text for s in sents).lower()):
        if w in TOPICS:
            counts[TOPICS[w]] = counts.get(TOPICS[w], 0) + 1
    return max(counts, key=counts.get) if counts else None


def pick_heuristic(sents: list[Sentence], speaker: str, video_title: str, cfg: dict, n: int) -> list[Candidate]:
    min_s = float(get(cfg, "clips.min_seconds", 20))
    max_s = float(get(cfg, "clips.max_seconds", 58))
    scored: list[tuple[float, Candidate]] = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            dur = sents[j].end - sents[i].start
            if dur > max_s:
                break
            if dur < min_s:
                continue
            times = span(sents, i, j, min_s, max_s)
            if times:
                window = sents[i : j + 1]
                topic = topic_of(window)
                title = f"{speaker} on {topic}" if topic else f"{speaker} on what matters"
                hook = topic.upper() if topic and get(cfg, "clips.heuristic_hooks", False) else ""
                desc = re.sub(r"\s+", " ", window[0].text).strip()
                scored.append((score_window(window), Candidate(times[0], times[1], title[:90], hook, desc)))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[Candidate] = []
    for s, c in scored:
        if s <= 0:
            break
        if not any(overlaps(c, o) for o in out):
            c.reason = f"keyword score {s:.1f}"
            out.append(c)
        if len(out) >= n:
            break
    log.info("heuristic picked %d clip(s)", len(out))
    return out


def pick(sents: list[Sentence], speaker: str, video_title: str, cfg: dict) -> list[Candidate]:
    n = int(get(cfg, "clips.candidates_per_video", 5))
    if get(cfg, "clips.picker", "claude") == "claude":
        got = pick_with_claude(sents, speaker, video_title, cfg, n)
        if got is not None:
            return got
    return pick_heuristic(sents, speaker, video_title, cfg, n)
