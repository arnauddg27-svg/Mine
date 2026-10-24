"""YouTube Data API v3: one-time OAuth and resumable video upload."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
RETRIABLE = {500, 502, 503, 504}


def ensure_token_file(token_path: Path) -> None:
    """In CI the token arrives as the YOUTUBE_TOKEN_JSON secret; materialize it."""
    if token_path.exists():
        return
    blob = os.environ.get("YOUTUBE_TOKEN_JSON")
    if blob:
        token_path.write_text(blob, encoding="utf-8")
        log.info("wrote %s from YOUTUBE_TOKEN_JSON", token_path)


def authorize(client_secret: Path, token_path: Path, open_browser: bool = True, port: int = 8080) -> Path:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_secret.exists():
        raise FileNotFoundError(
            f"{client_secret} not found. Create an OAuth client (Desktop app) in Google Cloud Console, "
            "enable the YouTube Data API v3, download the JSON and save it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=port, open_browser=open_browser, prompt="consent", access_type="offline")
    token_path.write_text(creds.to_json(), encoding="utf-8")
    log.info("saved credentials to %s", token_path)
    return token_path


def load_credentials(token_path: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    ensure_token_file(token_path)
    if not token_path.exists():
        raise FileNotFoundError(f"{token_path} missing; run `python -m motivate authorize` first")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("stored YouTube credentials are invalid; re-run `python -m motivate authorize`")
    return creds


def upload_video(
    video_path: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    privacy: str = "private",
    category_id: str = "22",
    made_for_kids: bool = False,
    publish_at: str | None = None,
    default_language: str = "en",
    token_path: Path,
    notify_subscribers: bool = True,
) -> str:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = load_credentials(token_path)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    status: dict = {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids}
    if publish_at:
        # Scheduled publishing requires the video to start private.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags,
            "categoryId": str(category_id),
            "defaultLanguage": default_language,
            "defaultAudioLanguage": default_language,
        },
        "status": status,
    }
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=notify_subscribers)

    response = None
    attempt = 0
    while response is None:
        try:
            progress, response = request.next_chunk()
            if progress:
                log.info("upload %.0f%%", progress.progress() * 100)
        except HttpError as exc:
            if exc.resp.status in RETRIABLE and attempt < 6:
                attempt += 1
                wait = 2 ** attempt
                log.warning("YouTube returned %s; retrying in %ss", exc.resp.status, wait)
                time.sleep(wait)
                continue
            raise
    video_id = response["id"]
    log.info("uploaded https://youtube.com/shorts/%s", video_id)
    return video_id
