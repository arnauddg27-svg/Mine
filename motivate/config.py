"""Configuration loading: config.yaml plus a few environment overrides."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


def get(cfg: dict, dotted: str, default: Any = None) -> Any:
    """Read a nested key like ``"video.width"`` from a dict."""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def load_config(path: str | Path | None = None) -> dict:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    with open(cfg_path, encoding="utf-8") as fh:
        cfg: dict = yaml.safe_load(fh) or {}

    paths = cfg.setdefault("paths", {})
    paths.setdefault("assets", str(ROOT / "assets"))
    paths.setdefault("state", str(ROOT / "state" / "history.json"))
    paths.setdefault("cache", str(ROOT / ".cache"))
    paths.setdefault("out", str(ROOT / "out"))
    paths.setdefault("client_secret", str(ROOT / "client_secret.json"))
    paths.setdefault("token", str(ROOT / "token.json"))

    # Environment overrides (handy for CI without editing the YAML).
    env = os.environ
    if env.get("MOTIVATE_PRIVACY"):
        cfg.setdefault("youtube", {})["privacy"] = env["MOTIVATE_PRIVACY"]
    if env.get("MOTIVATE_VOICE"):
        cfg.setdefault("voice", {})["name"] = env["MOTIVATE_VOICE"]
    if env.get("MOTIVATE_SCRIPT_SOURCE"):
        cfg.setdefault("script", {})["source"] = env["MOTIVATE_SCRIPT_SOURCE"]
    if env.get("YOUTUBE_CLIENT_SECRET_FILE"):
        paths["client_secret"] = env["YOUTUBE_CLIENT_SECRET_FILE"]
    if env.get("YOUTUBE_TOKEN_FILE"):
        paths["token"] = env["YOUTUBE_TOKEN_FILE"]
    return cfg
