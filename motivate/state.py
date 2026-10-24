"""Persistent run history so quotes are not repeated and uploads are logged."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {"used_quote_ids": [], "uploads": []}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass

    @property
    def used_quote_ids(self) -> set[str]:
        return set(self.data.get("used_quote_ids", []))

    def mark_quote(self, quote_id: str) -> None:
        if quote_id not in self.data["used_quote_ids"]:
            self.data["used_quote_ids"].append(quote_id)

    def reset_quotes(self) -> None:
        self.data["used_quote_ids"] = []

    def record_upload(self, **entry) -> None:
        entry.setdefault("at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.data["uploads"].append(entry)

    def recent_titles(self, n: int = 25) -> list[str]:
        return [u.get("title", "") for u in self.data.get("uploads", [])[-n:] if u.get("title")]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
