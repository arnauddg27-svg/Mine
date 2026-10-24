import random
from pathlib import Path

import pytest

from motivate import captions, quotes, render, script as script_mod
from motivate.config import load_config
from motivate.tts import SpokenSegment, VoiceTrack, Word

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture(scope="module")
def bank():
    return quotes.load_bank(ROOT / "assets" / "quotes.json")


def test_bank_is_well_formed(bank):
    assert len(bank) >= 50
    ids = [q.id for q in bank]
    assert len(ids) == len(set(ids)), "duplicate quotes"
    for q in bank:
        assert q.author and q.text
        assert q.word_count <= 36, q.text


def test_pick_avoids_used(bank):
    rng = random.Random(1)
    used = {q.id for q in bank[:-1]}
    picked = quotes.pick_quote(bank, used, rng)
    assert picked.id == bank[-1].id


def test_pick_when_everything_used(bank):
    picked = quotes.pick_quote(bank, {q.id for q in bank}, random.Random(2))
    assert picked in bank


def test_script_metadata_limits(bank, cfg):
    for q in bank:
        s = script_mod.from_quote(q, cfg, random.Random(3))
        assert len(s.title) <= 100, s.title
        assert s.title.endswith("#shorts")
        assert "<" not in s.title and ">" not in s.title
        assert sum(len(t) + 2 for t in s.tags) <= 500
        assert [seg.role for seg in s.segments] == ["hook", "quote", "cta"]


def test_long_title_is_shortened(cfg):
    q = quotes.Quote("word " * 60, "Someone With A Long Name")
    s = script_mod.from_quote(q, cfg, random.Random(0))
    assert len(s.title) <= 100
    assert "Someone With A Long Name" in s.title


def _fake_track():
    words = [Word(t, 0.4 * i + 0.1, 0.4 * i + 0.4) for i, t in enumerate("Stop scrolling. You need this today.".split())]
    hook = SpokenSegment("hook", "Stop scrolling. You need this today.", 0.0, words[-1].end, words)
    qwords = [Word(t, 3.0 + 0.35 * i, 3.0 + 0.35 * i + 0.3) for i, t in enumerate("Fall seven times, stand up eight.".split())]
    quote = SpokenSegment("quote", "Fall seven times, stand up eight.", 3.0, qwords[-1].end, qwords)
    return VoiceTrack(Path("voice.wav"), 6.0, [hook, quote])


def test_ass_captions(cfg):
    track = _fake_track()
    s = script_mod.Script(segments=[], title="t", description="d", tags=[], author="Japanese proverb")
    ass = captions.build_ass(track, s, cfg, total_duration=6.8)
    assert "[V4+ Styles]" in ass and "Style: Word,Inter" in ass
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    word_events = [l for l in lines if ",Word," in l]
    # karaoke mode: one event per word
    assert len(word_events) == len(track.words)
    assert any(",Author," in l and "Japanese proverb" in l for l in lines)
    assert "SCROLLING." not in ass and "TODAY." not in ass, "trailing periods are stripped from captions"
    assert any(",Brand," in l for l in lines)
    # highlighted word carries the colour override and a reset
    assert "\\c&H00D6FF&" in ass or "\\c&H0000D6FF" in ass
    assert "{\\r}" in ass
    # timestamps are non-decreasing per word event and end > start
    def parse(ts):
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    for l in word_events:
        _, start, end = l.split(",")[0:3]
        assert parse(end) > parse(start)


def test_paginate_breaks_on_sentence_end():
    words = [Word(t, i, i + 0.5) for i, t in enumerate("One two. Three four five six seven".split())]
    pages = captions.paginate(words, 3)
    assert [len(p) for p in pages] == [2, 3, 2]
    # a one-word sentence tail folds into the previous page
    words = [Word(t, i, i + 0.5) for i, t in enumerate("I have not failed. I've just found".split())]
    assert [[w.text for w in p] for p in captions.paginate(words, 3)] == [["I", "have", "not", "failed."], ["I've", "just", "found"]]


def test_align_punctuation():
    from motivate.tts import align_punctuation
    raw = [Word(t, i, i + 0.4) for i, t in enumerate(["I", "have", "not", "failed", "I've", "just", "found"])]
    aligned = align_punctuation(raw, "I have not failed. I've just found")
    assert [w.text for w in aligned] == ["I", "have", "not", "failed.", "I've", "just", "found"]
    assert [w.start for w in aligned] == [w.start for w in raw]
    # letters-only comparison bridges hyphenation; unmatched words pass through untouched
    assert align_punctuation([Word("twentyfive", 0, 1)], "twenty-five.")[0].text == "twenty-five."
    assert align_punctuation([Word("hello", 0, 1)], "goodbye world.")[0].text == "hello"


def test_ass_color():
    assert captions.ass_color("FFD600") == "&H0000D6FF"
    assert captions.ass_color("#102030", alpha=0x80) == "&H80302010"


def test_filter_escape():
    assert render.esc("C:\\a'b") == "C\\:\\\\a\\'b"


def test_block_captions(cfg):
    import copy
    c = copy.deepcopy(cfg)
    c["captions"]["style"] = "block"
    track = _fake_track()
    s = script_mod.Script(segments=[], title="t", description="d", tags=[], author=None)
    ass = captions.build_ass(track, s, c, total_duration=6.8)
    word_events = [l for l in ass.splitlines() if l.startswith("Dialogue:") and ",Word," in l]
    expected = sum(len(captions.paginate(seg.words, 3)) for seg in track.segments)
    assert len(word_events) == expected
    assert "\\c&H" not in ass


def test_pexels_picks_portrait_file(monkeypatch, tmp_path, cfg):
    from motivate import background

    payload = {"videos": [{
        "id": 42, "duration": 12, "url": "https://www.pexels.com/video/42/",
        "user": {"name": "Jane Doe"},
        "video_files": [
            {"id": 1, "width": 1920, "height": 1080, "file_type": "video/mp4", "link": "https://x/landscape.mp4"},
            {"id": 2, "width": 720, "height": 1280, "file_type": "video/mp4", "link": "https://x/small.mp4"},
            {"id": 3, "width": 1080, "height": 1920, "file_type": "video/mp4", "link": "https://x/hd.mp4"},
            {"id": 4, "width": 2160, "height": 3840, "file_type": "video/mp4", "link": "https://x/uhd.mp4"},
        ],
    }]}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    downloaded = []
    monkeypatch.setenv("PEXELS_API_KEY", "test")
    monkeypatch.setattr(background.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(background, "_download", lambda url, dst: (downloaded.append(url), dst.write_bytes(b"x")))
    bg = background.from_pexels(cfg, random.Random(0), tmp_path, min_duration=15)
    assert bg is not None and bg.kind == "video"
    assert downloaded == ["https://x/hd.mp4"], "should prefer the smallest file that is at least 1920 tall"
    assert bg.path.exists() and "Jane Doe" in bg.credit
