"""Command line entry point: python -m motivate <command>."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import get, load_config


def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import run_once

    cfg = load_config(args.config)
    results = []
    for i in range(args.count):
        seed = None if args.seed is None else args.seed + i
        meta = run_once(cfg, upload=not args.no_upload, privacy=args.privacy, seed=seed, out_dir=Path(args.out) if args.out else None)
        results.append(meta)
        print(json.dumps({k: meta.get(k) for k in ("title", "duration", "video", "url", "source")}, ensure_ascii=False))
    return 0


def cmd_authorize(args: argparse.Namespace) -> int:
    from .youtube import authorize

    cfg = load_config(args.config)
    client_secret = Path(args.client_secret or get(cfg, "paths.client_secret"))
    token = Path(args.token or get(cfg, "paths.token"))
    authorize(client_secret, token, open_browser=not args.no_browser, port=args.port)
    print(f"Saved {token}. For GitHub Actions, store its contents in the YOUTUBE_TOKEN_JSON secret.")
    return 0


def cmd_voices(args: argparse.Namespace) -> int:
    import edge_tts

    from .tts import _patch_tls

    _patch_tls()
    voices = asyncio.run(edge_tts.list_voices())
    for v in sorted(voices, key=lambda v: v["ShortName"]):
        if args.locale and not v["Locale"].lower().startswith(args.locale.lower()):
            continue
        print(f"{v['ShortName']:<40} {v['Gender']:<7} {', '.join(v.get('VoiceTag', {}).get('VoicePersonalities', []))}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    from .quotes import load_bank
    from .state import State

    cfg = load_config(args.config)
    state = State(get(cfg, "paths.state"))
    bank = load_bank(Path(get(cfg, "paths.assets")) / "quotes.json")
    uploads = state.data.get("uploads", [])
    print(f"quotes in bank: {len(bank)}  used: {len(state.used_quote_ids)}  remaining: {len(bank) - len(state.used_quote_ids)}")
    print(f"runs recorded: {len(uploads)}  uploaded: {sum(1 for u in uploads if u.get('video_id'))}")
    for u in uploads[-10:]:
        print(f"  {u.get('at', '')[:19]}  {u.get('privacy', ''):<13} {u.get('title', '')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="motivate", description="Automated motivational YouTube Shorts")
    p.add_argument("--config", default=None, help="path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="generate one (or more) shorts and upload them")
    r.add_argument("--no-upload", action="store_true", help="render only; skip YouTube")
    r.add_argument("--privacy", choices=["private", "unlisted", "public"], default=None)
    r.add_argument("--count", type=int, default=1)
    r.add_argument("--seed", type=int, default=None, help="deterministic choices (useful for tests)")
    r.add_argument("--out", default=None, help="output directory (default: ./out)")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("authorize", help="one-time Google OAuth; writes token.json")
    a.add_argument("--client-secret", default=None)
    a.add_argument("--token", default=None)
    a.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    a.add_argument("--port", type=int, default=8080)
    a.set_defaults(func=cmd_authorize)

    v = sub.add_parser("voices", help="list available edge-tts voices")
    v.add_argument("--locale", default="en")
    v.set_defaults(func=cmd_voices)

    s = sub.add_parser("stats", help="show quote usage and recent runs")
    s.set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    return args.func(args)
