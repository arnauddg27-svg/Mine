"""Tests for the paths that talk to outside services (mocked) and an offline end-to-end render."""
import copy
import json
import random
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from motivate import llm, youtube
from motivate.config import load_config

ROOT = Path(__file__).resolve().parent.parent
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture()
def cfg():
    return copy.deepcopy(load_config(ROOT / "config.yaml"))


# --------------------------------------------------------------------------- Claude

GOOD = {
    "hook": "Nobody is coming to do it for you.",
    "body": " ".join(["Start small today and keep the promise you make to yourself."] * 5),
    "title": "The promise that changes everything",
    "description": "A reminder about keeping small promises.",
    "tags": ["discipline", "habits", "self respect", "consistency"],
    "footage": "sunrise mountain ridge",
}


class FakeAnthropic:
    """Stands in for anthropic.Anthropic; records the request and returns a canned reply."""

    calls: list = []
    reply = None

    def __init__(self, *a, **k):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        FakeAnthropic.calls.append(kwargs)
        if isinstance(FakeAnthropic.reply, Exception):
            raise FakeAnthropic.reply
        return FakeAnthropic.reply


def _reply(text=None, stop_reason="end_turn"):
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(stop_reason=stop_reason, content=content, stop_details=SimpleNamespace(category="cyber"))


@pytest.fixture()
def fake_claude(monkeypatch):
    import anthropic

    FakeAnthropic.calls = []
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    return FakeAnthropic


def test_claude_request_shape_and_script(fake_claude, cfg):
    fake_claude.reply = _reply(json.dumps(GOOD))
    script = llm.generate_script(cfg, random.Random(0), ["Old title"])
    req = fake_claude.calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["betas"] == ["server-side-fallback-2026-07-01"] and req["fallbacks"] == "default"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert "footage" in req["output_config"]["format"]["schema"]["required"]
    assert "thinking" not in req and "temperature" not in req
    assert "Old title" in req["messages"][0]["content"]
    assert script.source == "claude" and script.footage_query == "sunrise mountain ridge"
    assert [s.role for s in script.segments][:2] == ["hook", "body"]
    assert script.title.endswith("#shorts") and len(script.title) <= 100


@pytest.mark.parametrize(
    "reply",
    [
        _reply(stop_reason="refusal"),
        _reply("not json"),
        _reply(json.dumps({**GOOD, "body": "Too short."})),
        _reply(json.dumps({**GOOD, "title": "  "})),
        RuntimeError("network down"),
    ],
    ids=["refusal", "bad-json", "short-body", "empty-title", "exception"],
)
def test_claude_failures_fall_back(fake_claude, cfg, reply):
    fake_claude.reply = reply
    assert llm.generate_script(cfg, random.Random(0), []) is None


def test_claude_bad_footage_is_dropped(fake_claude, cfg):
    fake_claude.reply = _reply(json.dumps({**GOOD, "footage": ""}))
    assert llm.generate_script(cfg, random.Random(0), []).footage_query is None


# --------------------------------------------------------------------------- YouTube

class FakeRequest:
    def __init__(self, steps):
        self.steps = list(steps)

    def next_chunk(self):
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class FakeYouTube:
    def __init__(self, steps):
        self.steps = steps
        self.insert_kwargs = None

    def videos(self):
        return self

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return FakeRequest(self.steps)


def _http_error(status, reason):
    import httplib2
    from googleapiclient.errors import HttpError

    body = {"error": {"code": status, "message": reason, "errors": [{"reason": reason, "domain": "youtube"}]}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode())


@pytest.fixture()
def fake_youtube(monkeypatch, tmp_path):
    import googleapiclient.discovery

    holder = {}

    def install(steps):
        svc = FakeYouTube(steps)
        holder["svc"] = svc
        monkeypatch.setattr(googleapiclient.discovery, "build", lambda *a, **k: svc)
        return svc

    monkeypatch.setattr(youtube, "load_credentials", lambda token_path: object())
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\0" * 1024)
    return install, video, tmp_path / "token.json"


def _upload(video, token, **kw):
    return youtube.upload_video(video, title="T", description="D", tags=["a"], token_path=token, **kw)


def test_upload_body(fake_youtube):
    install, video, token = fake_youtube
    svc = install([(None, {"id": "abc123"})])
    vid = _upload(video, token, privacy="unlisted", contains_synthetic_media=True, notify_subscribers=False)
    assert vid == "abc123"
    body = svc.insert_kwargs["body"]
    assert svc.insert_kwargs["part"] == "snippet,status"
    assert svc.insert_kwargs["notifySubscribers"] is False
    assert body["snippet"]["title"] == "T" and body["snippet"]["categoryId"] == "22"
    assert body["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}


def test_upload_retries_server_errors_and_dropped_connections(fake_youtube):
    install, video, token = fake_youtube
    install([_http_error(503, "backendError"), ConnectionResetError("reset"), (None, {"id": "ok"})])
    assert _upload(video, token) == "ok"


@pytest.mark.parametrize("status,reason,needle", [(403, "quotaExceeded", "quota"), (400, "uploadLimitExceeded", "upload limit")])
def test_upload_explains_limits(fake_youtube, status, reason, needle):
    install, video, token = fake_youtube
    install([_http_error(status, reason)])
    with pytest.raises(RuntimeError, match=needle):
        _upload(video, token)


def test_upload_does_not_retry_client_errors(fake_youtube):
    from googleapiclient.errors import HttpError

    install, video, token = fake_youtube
    install([_http_error(400, "invalidTitle"), (None, {"id": "never"})])
    with pytest.raises(HttpError):
        _upload(video, token)


# --------------------------------------------------------------------------- offline end-to-end

def _fake_synth(text, voice, rate, pitch, out_mp3, proxy, attempts=4):
    """Replace edge-tts: a soft tone per word with evenly spaced word timestamps."""
    from motivate.tts import Word

    words = text.split()
    per = 0.3
    dur = 0.1 + per * len(words) + 0.2
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=f=220:d={dur:.2f}",
         "-af", "volume=0.3", "-ar", "24000", "-ac", "1", str(out_mp3)],
        check=True,
    )
    return [Word(w.strip(".,;:!?\"").strip() or w, 0.1 + i * per, 0.1 + (i + 1) * per - 0.05) for i, w in enumerate(words)]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_end_to_end_offline(monkeypatch, tmp_path, cfg):
    from motivate import pipeline, tts

    monkeypatch.setattr(tts, "synth_segment", _fake_synth)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=3:c=pink:a=0.3",
                    "-ac", "2", str(music_dir / "bed.mp3")], check=True)
    cfg["paths"].update(state=str(tmp_path / "state.json"), cache=str(tmp_path / "cache"))
    cfg["background"]["sources"] = ["gradient"]
    cfg["music"]["dir"] = str(music_dir)
    cfg["video"].update(width=540, height=960, preset="ultrafast")
    out = tmp_path / "it's an out dir"  # apostrophe on purpose: exercises filter-path escaping

    meta = pipeline.run_once(cfg, upload=False, record=True, seed=3, out_dir=out)

    video = Path(meta["video"])
    assert video.exists()
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-show_entries", "format=duration",
         "-of", "json", str(video)], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (540, 960)
    assert any(s["codec_type"] == "audio" for s in probe["streams"])
    assert 4 < float(probe["format"]["duration"]) < 59
    assert meta["music"] == "bed.mp3"
    assert (video.parent / "thumb.jpg").exists()
    state = json.loads((tmp_path / "state.json").read_text())
    assert len(state["used_quote_ids"]) == 1 and state["uploads"][-1]["privacy"] == "not-uploaded"
    ass = (video.parent / "captions.ass").read_text()
    assert "Style: Word,Inter,46," in ass and ",3.5,1.5,5,40,85,0,1" in ass  # font, outline, margins scaled to 540 px
