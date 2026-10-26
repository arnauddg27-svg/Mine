# Daily Drive — automated motivational Shorts channel

A hands-off pipeline that produces and publishes one vertical motivational short per run:

```
quote / AI script  →  neural voiceover  →  word-by-word captions  →  stock footage + music  →  1080×1920 MP4  →  YouTube
```

Everything is original or licensed: public-domain quotes (or scripts Claude writes), a free Microsoft neural voice, Pexels stock video (free for commercial use), your own royalty-free music, and the open-licensed Inter font. Nothing is clipped from other creators, which keeps the channel clear of copyright strikes and YouTube's reused-content rule.

## What a video looks like

- 15–30 s, 9:16, 30 fps, H.264 + AAC, ready for Shorts.
- Spoken: a short hook ("Read this before you give up.") → the quote → a call to action.
- Captions: three words per page, the spoken word highlighted in yellow, author shown under the quote, channel handle as a small watermark.
- Background: a portrait stock clip matching a random search term (mountain sunrise, city rain, runner at dawn…), darkened and vignetted for legibility. Offline fallback is a slowly moving dark gradient, so the pipeline never fails for lack of footage.

## Quick start (local)

```bash
# 1. system + python deps (ffmpeg must be on PATH)
sudo apt-get install ffmpeg          # or: brew install ffmpeg
pip install -r requirements.txt

# 2. render a test video without touching YouTube
python -m motivate run --no-upload
# -> out/<timestamp>-<slug>/short.mp4, thumb.jpg, metadata.json, captions.ass
```

Add a Pexels key for real footage (free at https://www.pexels.com/api/):

```bash
export PEXELS_API_KEY=...
python -m motivate run --no-upload
```

Drop any royalty-free `.mp3` files into `assets/music/` and they are mixed under the voice automatically.

## Connect YouTube (one-time, ~10 minutes)

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and **enable "YouTube Data API v3"**.
2. *APIs & Services → OAuth consent screen*: External, fill in the app name and your email, add the scope `.../auth/youtube.upload`, add your Google account as a test user.
3. **Publish the app** (Publishing status → "In production"). While an app is in "Testing", refresh tokens expire after 7 days and the scheduler would die within a week. Google shows an "unverified app" screen when you authorize; that is fine for your own channel.
4. *Credentials → Create credentials → OAuth client ID → Desktop app*. Download the JSON and save it as `client_secret.json` in this folder.
5. Authorize once on a machine with a browser, signed in to the Google account that owns the channel:
   ```bash
   python -m motivate authorize
   ```
   This writes `token.json` (git-ignored).
6. Test an upload (the default privacy is `private`):
   ```bash
   python -m motivate run
   ```
7. When you are happy with a few test uploads, set `youtube.privacy: public` in `config.yaml` and set `channel.handle` to your real handle. Until then the scheduler keeps uploading private videos.

Quota: uploading costs 1,600 of the default 10,000 daily units, so up to 6 uploads per day fit without a quota increase.

## Run it on a schedule (GitHub Actions)

The included workflow `.github/workflows/publish.yml` renders and uploads daily at 13:00 UTC and commits `state/history.json` back so quotes are never repeated.

Add these repository secrets (*Settings → Secrets and variables → Actions*):

| Secret | Required | Value |
|---|---|---|
| `YOUTUBE_TOKEN_JSON` | yes | full contents of your local `token.json` |
| `PEXELS_API_KEY` | recommended | Pexels API key (gradient fallback without it) |
| `ANTHROPIC_API_KEY` | optional | enables `script.source: claude` or `mixed` |

Then push, and use *Actions → Publish motivational short → Run workflow* for a manual test (untick **upload** to only render; the MP4 appears under the run's artifacts). Change the `cron` lines to post more often; two to three Shorts a day is a common cadence.

Any machine with cron works too:

```
0 13 * * *  cd /path/to/repo && /usr/bin/python3 -m motivate run >> publish.log 2>&1
```

## Configuration (`config.yaml`)

| Key | What it does |
|---|---|
| `channel.handle` | watermark text and "Follow @handle" line in descriptions (`""` to hide) |
| `script.source` | `bank` (public-domain quotes), `claude` (original AI-written scripts), `mixed` |
| `script.hook` / `script.cta` | spoken opener (rotates through 16 lines, never the same as the last 3 videos) / closer (string or list, one picked per video) |
| `voice.name`, `rate`, `pitch` | any edge-tts voice; list with `python -m motivate voices` |
| `voice.names` | optional list; a different voice is picked for each video |
| `voice.pause_after` | silence after each segment, in seconds |
| `captions.*` | font, size, words per page, colours, `karaoke` or `block` style |
| `captions.time_offset` | shift captions in seconds (TTS timestamps run about 0.1 s ahead of the audio; `0.1` delays them to match exactly) |
| `background.sources` | order to try: `pexels`, `local` (`assets/backgrounds`), `gradient` |
| `background.queries` | Pexels search terms; one is picked per video |
| `background.tag_queries` | search terms per quote theme (resilience, courage, …) so footage matches the message; clips used in the last 30 videos are avoided |
| `background.pexels_pages` | pick a random Pexels result page from 1..N (more variety per search term) |
| `background.local_dir`, `music.dir` | override the folders scanned for your own clips and music |
| `music.enabled`, `music.volume` | background music mix |
| `video.max_seconds` | hard cap (default 59 s, raise up to 180 for longer Shorts) |
| `youtube.privacy` | `private` while testing, then `public` |
| `youtube.tags`, `hashtags`, `category_id` | upload metadata |
| `youtube.ai_disclosure` | appends a one-line AI-voiceover note to every description |
| `youtube.synthetic_media_label` | also sets YouTube's "altered or synthetic content" flag on the upload (off by default: a TTS voice reading a quote is not the realistic-deception case the label is for; turn it on if you prefer to over-disclose) |

Environment overrides: `MOTIVATE_PRIVACY`, `MOTIVATE_VOICE`, `MOTIVATE_SCRIPT_SOURCE`, `YOUTUBE_TOKEN_FILE`, `YOUTUBE_CLIENT_SECRET_FILE`, `EDGE_TTS_PROXY`.

## Original scripts with Claude (optional)

Set `script.source: claude` (or `mixed`) and `ANTHROPIC_API_KEY`. Each run asks Claude Opus 5.5 for a 45–75 word original monologue on a rotating theme (discipline, starting before you feel ready, comparison, …), plus a title, description and tags, returned as validated JSON. Recent titles are passed in so topics do not repeat. Server-side refusal fallback is enabled by default; if the request is declined or the API is unreachable the run silently uses the quote bank instead, so the schedule never breaks. Expect roughly one to three cents per video (thinking tokens are billed as output).

## Commands

```
python -m motivate run [--no-upload] [--record] [--privacy private|unlisted|public] [--count N] [--seed N] [--out DIR]
python -m motivate authorize [--client-secret PATH] [--token PATH] [--no-browser]
python -m motivate voices [--locale en-GB]
python -m motivate stats
python -m pytest            # unit tests (no network needed)
```

`--no-upload` renders without touching `state/history.json`, so previews never use up quotes. Add `--record` when you render for manual upload and want the quote counted.

## Project layout

```
motivate/
  cli.py          command line
  pipeline.py     orchestrates one run
  quotes.py       quote bank + no-repeat picker
  script.py       hook/quote/CTA segments, title, description, tags
  llm.py          optional Claude script writer
  tts.py          edge-tts voiceover with word timestamps
  captions.py     ASS subtitle generator (highlighted words, author, watermark)
  background.py   Pexels / local / gradient backgrounds
  render.py       ffmpeg composition
  youtube.py      OAuth + resumable upload
  state.py        state/history.json
assets/
  quotes.json     79 public-domain quotes (edit freely)
  fonts/          Inter (OFL)
  music/          your royalty-free tracks
  backgrounds/    your own vertical clips (optional)
.github/workflows/publish.yml   daily scheduler
```

## What happens when something breaks

- No Pexels key or Pexels down → gradient background, run continues.
- Microsoft's TTS endpoint refuses the runner (happens occasionally for datacenter IPs) → four attempts with growing backoff, then the run fails; the next scheduled run retries.
- Claude unavailable or declines → quote bank, run continues.
- Dropped connection during upload → the resumable upload retries the chunk up to 6 times.
- ffmpeg missing → the run stops immediately with an install hint instead of failing mid-render.
- YouTube token expired or quota exhausted → run fails with a message saying exactly what to do; the video stays in the workflow artifacts.

## Things to know before going public

- **Music**: only use tracks you may publish on YouTube (YouTube Audio Library, Pixabay Music, a paid library). Content ID claims are automatic.
- **Pexels**: free for commercial use, attribution not required; the pipeline still credits the videographer in each description.
- **Quotes**: all authors died before 1930 or the line is a proverb, so the words are public domain. Attribution follows common usage; review `assets/quotes.json` and remove anything you are not comfortable with.
- **AI voice**: a text-to-speech reading of a quote is not deceptive synthetic media, but the description discloses it anyway (`youtube.ai_disclosure`), and `youtube.synthetic_media_label` can set YouTube's official flag too.
- **Token lifetime**: if the Google OAuth app stays in "Testing" status, uploads stop after 7 days with a clear error in the Actions log. Publishing the app fixes it permanently.
- **Quota**: a run that hits the daily YouTube quota fails with an explicit message; the rendered video is still saved as a workflow artifact so nothing is lost.
- **Monetization**: YouTube's "repetitious content" rule targets channels that are mass-produced with little variation. Original Claude scripts, varied footage and voices, and your own hooks are the levers to stay on the right side of it.
