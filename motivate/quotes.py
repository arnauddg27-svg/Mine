"""Quote bank: public-domain quotes with stable ids and no-repeat selection."""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Quote:
    text: str
    author: str
    tags: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return hashlib.sha1(self.text.strip().lower().encode("utf-8")).hexdigest()[:10]

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def load_bank(path: str | Path) -> list[Quote]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    bank = [Quote(q["text"].strip(), q.get("author", "Unknown").strip(), list(q.get("tags", []))) for q in raw]
    if not bank:
        raise ValueError(f"quote bank {path} is empty")
    return bank


def pick_quote(bank: list[Quote], used_ids: set[str], rng: random.Random, max_words: int = 36) -> Quote:
    """Pick an unused quote; when every quote has been used, start over."""
    candidates = [q for q in bank if q.word_count <= max_words]
    if not candidates:
        candidates = list(bank)
    fresh = [q for q in candidates if q.id not in used_ids]
    pool = fresh or candidates
    return rng.choice(pool)
