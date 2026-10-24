"""Optional: have Claude write an original motivational script (no attribution needed).

Enabled when ``script.source`` is ``claude`` or ``mixed`` and ANTHROPIC_API_KEY is set.
Any failure falls back to the quote bank, so this path never breaks a scheduled run.
"""
from __future__ import annotations

import json
import logging
import random

from .config import get
from .script import Script, from_llm

log = logging.getLogger(__name__)

SYSTEM = """You write 20-second spoken scripts for a motivational YouTube Shorts channel.

Rules:
- Second person, direct, plain words, short sentences. One clear idea per script.
- 45 to 75 words in the body. It must read naturally out loud.
- No clichés ("unleash your potential", "grind", "hustle"), no hashtags, no emojis.
- Do not quote or attribute anything to a real person. Everything is original.
- No medical, financial, or legal advice. Nothing that shames the viewer.
- End on one concrete thing the viewer can do today.
- The title is for YouTube: specific, under 80 characters, no clickbait, no ALL CAPS.
- The description is 1-3 sentences, no hashtags (they are added later).
- Tags are 4-8 lowercase phrases without the # sign."""

SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string", "description": "Opening line, at most 10 words."},
        "body": {"type": "string", "description": "The script body, 45-75 words."},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 8},
    },
    "required": ["hook", "body", "title", "description", "tags"],
    "additionalProperties": False,
}


def generate_script(cfg: dict, rng: random.Random, recent_titles: list[str]) -> Script | None:
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK not installed; using the quote bank")
        return None

    model = get(cfg, "llm.model", "claude-opus-5-5")
    themes = list(get(cfg, "llm.themes", ["discipline", "consistency", "fear of starting"]))
    theme = rng.choice(themes)
    avoid = "\n".join(f"- {t}" for t in recent_titles[-15:]) or "- (none yet)"
    user = (
        f"Theme: {theme}.\n\n"
        f"Recently published titles (do not repeat these ideas):\n{avoid}\n\n"
        "Write one new script and return it as JSON."
    )
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=model,
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": get(cfg, "llm.effort", "medium"), "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            log.warning("Claude declined the request (%s); using the quote bank", getattr(response.stop_details, "category", None))
            return None
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
    except anthropic.RateLimitError as exc:
        log.warning("Claude rate limited: %s; using the quote bank", exc)
        return None
    except anthropic.APIStatusError as exc:
        log.warning("Claude API error %s: %s; using the quote bank", exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        log.warning("Claude connection error: %s; using the quote bank", exc)
        return None
    except Exception as exc:  # malformed JSON etc.
        log.warning("Claude script generation failed: %s; using the quote bank", exc)
        return None

    body_words = len(data.get("body", "").split())
    if not (30 <= body_words <= 95):
        log.warning("Claude body was %d words; using the quote bank", body_words)
        return None
    script = from_llm(data, cfg, theme)
    log.info("Claude wrote an original script on '%s' (%d words)", theme, body_words)
    return script
