# Daily Drive — automated motivational clip channel

Turns long talks, lectures and podcasts into captioned vertical Shorts and posts them to YouTube:

```
speaker's long videos → transcript with word timings → Claude picks the best 20–58 s moments
  → cut → reframe to 9:16 (follows the face) → word-by-word captions + headline → YouTube
```

Each run posts one clip. Every source video yields up to five clips, so a channel with ten long uploads keeps you posting for weeks.

## Read this first: permission

Reposting someone else's footage without permission is copyright infringement. YouTube handles it with automatic Content ID claims and copyright strikes, and three strikes delete the channel. Fully automated reposts with no added commentary also get demonetized under YouTube's "reused content" rule.

The safe sources are:
- speakers and podcasts that run clipping programs or say in their channel description that clips are allowed;
- creators who gave you permission (keep the email);
- public-domain footage, such as NASA and other US government video.

Every upload credits the speaker and links the full video, but credit is not permission. You choose what goes in `sources.yaml`.

## What a clip looks like

- 20–58 s, 1080×1920, 30 fps, audio normalized to YouTube's playback level (-14 LUFS).
- **One speaker on screen**: the frame follows their face, re-centering only at camera cuts, like a human editor would.
- **Two people, slides, or wide shots**: the full frame sits in the middle over a blurred copy of itself.
- Captions three words at a time with the spoken word highlighted, placed under the face and clear of the Shorts title and buttons.
- A boxed headline at the top ("Why discipline beats motivation") and the speaker's name under the captions.
- Description: what the clip is about, the speaker, a link to the full video, and the credit line.

## Where it runs: your own computer

YouTube blocks video downloads from cloud servers, including GitHub Actions ("Sign in to confirm you're not a bot"). Clip mode therefore runs on a computer with a normal home connection: a laptop, desktop or a small always-on box such as a Raspberry Pi 5 or an old PC.

## Setup (about 20 minutes)

1. **Install ffmpeg and Python 3.10+**
   ```bash
   sudo apt install ffmpeg python3-venv      # macOS: brew install ffmpeg python
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
   On Windows, install Python from python.org and ffmpeg with `winget install ffmpeg`, then run the same `pip install`.

2. **Choose your speakers** in `sources.yaml`:
   ```yaml
   - channel: https://www.youtube.com/@CHANNEL_HANDLE/videos
     speaker: Speaker Name
     credit: Speaker Name (youtube.com/@CHANNEL_HANDLE)
     max_videos: 10
   - url: https://www.youtube.com/watch?v=VIDEO_ID       # or a single video
     speaker: Speaker Name
   - file: assets/sources/interview.mp4                   # or a file you were given
     speaker: Speaker Name
     source_url: https://where-the-full-video-is
   ```
   Runs rotate between entries so speakers alternate. `python -m motivate sources` shows what was found and how many clips are left.

3. **Add a Claude API key** (recommended). Claude reads each transcript and picks moments that make sense on their own, then writes the title, headline and description. It costs a few cents for a short talk and up to about 35 cents for a 3-hour podcast, which covers that video's five clips. Without a key, a free keyword-based picker is used; it cuts on sentence boundaries but its picks and titles are noticeably weaker.
   ```bash
   cp .env.example .env     # then put your key in ANTHROPIC_API_KEY=
   set -a; source .env; set +a   # load it into this terminal (run_daily.sh does this itself)
   ```

4. **Connect YouTube** (one time): follow [Connect YouTube](#connect-youtube) below, then run `python -m motivate authorize`.

5. **Make a test clip without uploading**:
   ```bash
   python -m motivate run --no-upload
   # -> out/<timestamp>-<title>/short.mp4, thumb.jpg, metadata.json
   ```
   The first run per source video downloads its audio and transcribes it: about 8 minutes for a 3-hour podcast on a 4-core laptop (a 44-minute interview took just over 2). Later clips from the same video take about a minute.

6. **Upload a private test**, check it on YouTube, then set `youtube.privacy: public` in `config.yaml`:
   ```bash
   python -m motivate run
   ```

7. **Schedule it.** macOS / Linux, daily at 9:17:
   ```bash
   crontab -e
   17 9 * * *  /path/to/repo/scripts/run_daily.sh
   ```
   Windows (PowerShell, once):
   ```powershell
   schtasks /Create /TN "Daily Drive" /SC DAILY /ST 09:17 /TR "C:\path\to\repo\scripts\run_daily.bat"
   ```
   The script updates yt-dlp before each run, because YouTube changes break old versions, and appends output to `logs/run.log`. The computer must be awake at that time.

## Making one clip by hand

```bash
# exact moment
python -m motivate clip "https://www.youtube.com/watch?v=VIDEO_ID" --speaker "Speaker Name" \
    --start 12:30 --end 13:15 --title "Speaker Name on starting before you are ready" --hook "Stop waiting to feel ready"

# let it find the best moment in that video
python -m motivate clip "https://www.youtube.com/watch?v=VIDEO_ID" --speaker "Speaker Name" --no-upload
```

## Connect YouTube

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and **enable "YouTube Data API v3"**.
2. *APIs & Services → OAuth consent screen*: External, add your email and the scope `.../auth/youtube.upload`, and add your Google account as a test user.
3. **Publish the app** (Publishing status → "In production"). In "Testing" status, tokens expire after 7 days. Google shows an "unverified app" screen when you authorize, which is fine for your own channel.
4. *Credentials → Create credentials → OAuth client ID → Desktop app*. Download the JSON as `client_secret.json` in this folder.
5. Run `python -m motivate authorize` and sign in with the account that owns the channel. This writes `token.json`, which is git-ignored.

The default API quota allows about six uploads a day.

## Clip settings (`config.yaml`)

| Key | What it does |
|---|---|
| `mode` | `clips` (this README) or `quotes` (see the end) |
| `clips.min_seconds`, `max_seconds` | clip length bounds |
| `clips.candidates_per_video` | moments picked per source video; each run posts the next unused one |
| `clips.picker` | `claude` or `heuristic` |
| `clips.whisper_model` | speech-to-text model: `tiny.en` fast, `base.en` default, `small.en` more accurate and about 3× slower; use `base`/`small` without `.en` for non-English speakers |
| `clips.layout` | `auto`, `crop` (always follow the face) or `fit` (always full frame over blur) |
| `clips.hook_overlay`, `show_speaker` | headline at the top, speaker name under the captions |
| `clips.caption_y`, `font_size` | caption height on screen and size |
| `clips.max_videos_per_channel` | newest uploads considered per channel |
| `clips.music` | quiet music bed from `assets/music`, ducked under the voice (off by default) |
| `clips.cookies_file` | browser cookies for yt-dlp; see Troubleshooting |
| `youtube.privacy`, `tags`, `hashtags` | upload settings (the speaker's name is added as a tag and hashtag) |

## Troubleshooting

- **"Sign in to confirm you're not a bot"**: you are on a server or VPN. Run from a home connection. As a last resort, export cookies from a browser signed in to a throwaway Google account with a "cookies.txt" extension and set `clips.cookies_file`. Google can flag accounts used this way, so never use your channel's account.
- **Download errors after weeks of working**: update yt-dlp with `pip install -U yt-dlp`. The daily script does this automatically.
- **"No unused clips left"**: every picked moment has been posted. Add videos or channels to `sources.yaml`, or raise `max_videos`.
- **The wrong person is framed**: set `clips.layout: fit` for that kind of footage.
- **Picks start mid-thought**: use the Claude picker. The free picker cannot tell an interviewer from the speaker.

## Commands

```
python -m motivate run [--no-upload] [--record] [--privacy private|unlisted|public] [--count N] [--mode clips|quotes]
python -m motivate clip URL_OR_FILE --speaker NAME [--start T --end T] [--title ..] [--hook ..] [--no-upload]
python -m motivate sources
python -m motivate authorize
python -m motivate stats
python -m pytest            # 42 tests, no network needed (includes full offline clip and quote renders)
```

`--no-upload` leaves `state/history.json` untouched, so test renders never use up clips. Add `--record` to count it anyway.

## Project layout

```
motivate/
  clips.py        clip mode: pick next moment, cut, reframe, caption, upload
  sources.py      sources.yaml, yt-dlp listing/downloads, local files
  transcript.py   faster-whisper transcription with word timings (cached)
  highlights.py   Claude moment picker + keyword fallback
  reframe.py      face tracking (OpenCV YuNet) and 9:16 layouts
  captions.py     ASS captions: highlighted words, headline, speaker, watermark
  render.py       ffmpeg composition for clips and quote videos
  youtube.py      OAuth + resumable upload
  pipeline.py, script.py, quotes.py, tts.py, background.py, llm.py   quote mode
sources.yaml      what to clip
scripts/          run_daily.sh / run_daily.bat for scheduling
assets/models/    YuNet face detector (MIT licence)
```

## Quote mode (optional)

`mode: quotes` (or `--mode quotes`) makes narrated quote videos instead: a verified public-domain quote, a free neural voice, Pexels stock footage (needs `PEXELS_API_KEY`) or an animated gradient, word-by-word captions and optional ducked music from `assets/music`. It needs no downloads from YouTube, so it can also run on GitHub Actions: add `YOUTUBE_TOKEN_JSON` (contents of `token.json`), `PEXELS_API_KEY` and optionally `ANTHROPIC_API_KEY` as repository secrets, and uncomment the schedule in `.github/workflows/publish.yml`. The quote bank in `assets/quotes.json` was fact-checked against Wikiquote and Quote Investigator, and every description cites the source work.

## Licences

Inter font: SIL OFL 1.1. YuNet face model: MIT. Code dependencies keep their own licences.
