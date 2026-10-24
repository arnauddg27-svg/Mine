"""Automated motivational YouTube Shorts pipeline.

Stages: script -> voiceover (edge-tts) -> captions (ASS) -> background
(Pexels / local / procedural) -> render (ffmpeg) -> upload (YouTube Data API).
"""

__version__ = "0.1.0"
