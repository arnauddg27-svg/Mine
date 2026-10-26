"""Clip mode: transcript grouping, moment picking, reframing, and an offline end-to-end run."""
import copy
import json
import random
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from motivate import clips, highlights, reframe, transcript
from motivate.config import load_config
from motivate.sources import SourceVideo
from motivate.tts import Word

ROOT = Path(__file__).resolve().parent.parent
HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture()
def cfg():
    return copy.deepcopy(load_config(ROOT / "config.yaml"))


def words_from(text: str, start: float = 0.0, per: float = 0.35) -> list[Word]:
    return [Word(t, round(start + i * per, 3), round(start + i * per + per - 0.05, 3)) for i, t in enumerate(text.split())]


FILLER = "Yeah so um that was kind of the thing you know and we went there. " * 3
GOOD = (
    "Discipline is choosing your future over your comfort. Take responsibility for one small thing today. "
    "Your life gets better when you keep the promises you make to yourself. Start before you feel ready, "
    "because courage grows from action and fear shrinks when you face it. That is how you grow."
)


def test_parse_time():
    assert clips.parse_time("75") == 75
    assert clips.parse_time("1:15") == 75
    assert clips.parse_time("01:02:03.5") == 3723.5
    assert clips.parse_time(None) is None


def test_sentences_and_clean_start():
    ws = words_from("One two three. Four five six? Seven eight") + words_from("nine ten.", start=10)
    sents = transcript.sentences(ws)
    assert [s.text for s in sents] == ["One two three.", "Four five six?", "Seven eight", "nine ten."]
    assert [s.clean_start for s in sents] == [True, True, True, False]  # last one follows a pause, not a full stop


def test_words_between_retimes():
    ws = words_from("a b c d e", start=10)
    got = transcript.words_between(ws, 10.3, 11.4)
    assert [w.text for w in got] == ["b", "c", "d"] and got[0].start == pytest.approx(0.05)


def test_span_trims_to_max_and_rejects_short():
    sents = transcript.sentences(words_from(GOOD))
    times = highlights.span(sents, 0, len(sents) - 1, 5, 12)
    assert times and times[1] - times[0] <= 12 + 0.5
    assert highlights.span(sents, 0, 0, 30, 58) is None


def test_heuristic_prefers_clean_motivational_window(cfg):
    ws = words_from(FILLER + GOOD + " " + FILLER)
    sents = transcript.sentences(ws)
    cfg["clips"].update(min_seconds=10, max_seconds=30, candidates_per_video=2)
    cands = highlights.pick_heuristic(sents, "Ada", "Talk", cfg, 2)
    assert cands, "should find the motivational stretch"
    best = cands[0]
    first = next(s for s in sents if s.start >= best.start - 0.2)
    assert first.text.startswith(("Discipline", "Take", "Your", "Start")), first.text
    assert best.title.startswith("Ada on ")


# --------------------------------------------------------------------------- Claude picker

class FakeAnthropic:
    calls: list = []
    reply = None

    def __init__(self, *a, **k):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        FakeAnthropic.calls.append(kw)
        return FakeAnthropic.reply


def _reply(obj, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, stop_details=None,
                           content=[SimpleNamespace(type="text", text=json.dumps(obj))])


def test_claude_picker_maps_sentences_to_times(monkeypatch, cfg):
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    FakeAnthropic.calls = []
    sents = transcript.sentences(words_from(FILLER + GOOD))
    good_first = next(s.index for s in sents if s.text.startswith("Discipline"))
    clip = {"title": "Ada on discipline", "hook": "The promise that matters", "description": "d", "tags": ["discipline"], "reason": "r"}
    FakeAnthropic.reply = _reply({"clips": [
        {**clip, "first": good_first, "last": len(sents) - 1},
        {**clip, "first": good_first, "last": len(sents) - 1},   # duplicate -> dropped as overlapping
        {**clip, "first": 999, "last": 1000},                      # out of range -> dropped
    ]})
    cfg["clips"].update(min_seconds=10, max_seconds=60)
    got = highlights.pick_with_claude(sents, "Ada", "Talk", cfg, 5)
    req = FakeAnthropic.calls[0]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert f"[{good_first}]" in req["messages"][0]["content"] and "Ada" in req["system"]
    assert len(got) == 1 and got[0].picker == "claude" and got[0].hook == "The promise that matters"
    assert got[0].start == pytest.approx(sents[good_first].start - 0.15)


def test_claude_picker_refusal_returns_none(monkeypatch, cfg):
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    FakeAnthropic.reply = _reply({"clips": []}, stop_reason="refusal")
    assert highlights.pick_with_claude(transcript.sentences(words_from(GOOD)), "Ada", "T", cfg, 3) is None


# --------------------------------------------------------------------------- reframing

def _samples(spec):
    """spec: list of (t, faces, scene_id) -> samples with distinct thumbnails per scene."""
    return [(t, faces, np.full((18, 32), scene * 80.0, np.float32)) for t, faces, scene in spec]


def test_plan_follows_face_per_shot(cfg):
    spec = [(t / 2, [(0.3, 0.1, 0.95)], 0) for t in range(10)] + [(5 + t / 2, [(0.7, 0.1, 0.95)], 1) for t in range(10)]
    plan = reframe.plan_from_samples(_samples(spec), 1280, 720, 10.0, cfg)
    assert plan.mode == "crop" and len(plan.shots) == 2
    assert plan.shots[0].cx == pytest.approx(0.3) and plan.shots[1].cx == pytest.approx(0.7)
    f = reframe.video_filter(plan, 1080, 1920, 30)
    assert "crop=w=404:h=720" in f and "if(lt(t,5.000)" in f


def test_plan_falls_back_to_fit_for_two_people_or_no_faces(cfg):
    two = [(t / 2, [(0.25, 0.1, 0.95), (0.75, 0.1, 0.94)], 0) for t in range(20)]
    assert reframe.plan_from_samples(_samples(two), 1280, 720, 10.0, cfg).mode == "fit"
    none = [(t / 2, [], 0) for t in range(20)]
    plan = reframe.plan_from_samples(_samples(none), 1280, 720, 10.0, cfg)
    assert plan.mode == "fit"
    f = reframe.video_filter(plan, 1080, 1920, 30)
    assert "gblur" in f and "overlay=x=(W-w)/2" in f


def test_crop_x_is_clamped_inside_frame(cfg):
    spec = [(t / 2, [(0.99, 0.1, 0.95)], 0) for t in range(20)]
    plan = reframe.plan_from_samples(_samples(spec), 1280, 720, 10.0, cfg)
    assert "x='876'" in reframe.video_filter(plan, 1080, 1920, 30)  # 1280 - 404


# --------------------------------------------------------------------------- metadata

def test_describe_credits_source(cfg):
    v = SourceVideo("yt:abc", "Full Talk", "Ada Lovelace", url="https://youtu.be/abc", channel="Ada Channel")
    c = highlights.Candidate(10, 40, "Ada Lovelace on discipline", "hook", "Why discipline beats motivation.", ["discipline"])
    title, desc, tags = clips.describe(v, c, cfg)
    assert title.endswith("#shorts") and "https://youtu.be/abc" in desc and "Credit: Ada Channel" in desc
    assert "#adalovelace" in desc and tags[0] == "ada lovelace"


# --------------------------------------------------------------------------- offline end-to-end

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_clip_mode_end_to_end(monkeypatch, tmp_path, cfg):
    src = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=60",
                    "-f", "lavfi", "-i", "sine=f=200:d=60", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(src)], check=True)
    text = (FILLER + GOOD + " " + FILLER + GOOD + " " + FILLER)
    fake_words = words_from(text, start=1.0, per=0.32)
    monkeypatch.setattr(transcript, "transcribe", lambda *a, **k: fake_words)
    (tmp_path / "sources.yaml").write_text(json.dumps([{"file": str(src), "speaker": "Ada", "source_url": "https://example.com/talk"}]))
    cfg["clips"].update(sources_file=str(tmp_path / "sources.yaml"), picker="heuristic", min_seconds=10, max_seconds=25,
                        candidates_per_video=2, heuristic_hooks=True)
    cfg["paths"].update(state=str(tmp_path / "state.json"), cache=str(tmp_path / "cache"))
    cfg["video"].update(width=540, height=960, preset="ultrafast")

    first = clips.run_clip_once(cfg, upload=False, record=True, out_dir=tmp_path / "out")
    second = clips.run_clip_once(cfg, upload=False, record=True, out_dir=tmp_path / "out")
    assert first["clip"] != second["clip"]
    with pytest.raises(RuntimeError, match="No unused clips"):
        clips.run_clip_once(cfg, upload=False, record=True, out_dir=tmp_path / "out")

    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json",
                                       first["video"]], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (540, 960) and any(s["codec_type"] == "audio" for s in probe["streams"])
    assert first["layout"] == "fit"  # synthetic footage has no faces
    ass = (Path(first["video"]).parent / "captions.ass").read_text()
    assert "\\an5\\pos(" in ass and ",Author," in ass
    assert "https://example.com/talk" in first["description"]
    state = json.loads((tmp_path / "state.json").read_text())
    assert len(state["clips_done"]) == 2
