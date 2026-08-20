"""
YouTube connection (OAuth2) and publishing for a channel — the last link in the
zero-human-input chain: once a channel is connected, an auto-generated video
that finishes rendering gets uploaded and made public on that YouTube channel
with no further action from the creator.

Uses plain HTTP calls (httpx) against Google's OAuth2 and YouTube Data API v3
endpoints directly rather than the heavy google-api-python-client, matching
the rest of this codebase's style.

IMPORTANT — Google OAuth consent screen "Testing" status: while the app is in
Testing mode (the default until Google verifies it for the youtube.upload
scope), refresh tokens for test users expire after 7 days, silently breaking
auto-publishing until the creator reconnects. For real unattended daily
publishing this app needs to be moved to "In production" and pass Google's
OAuth verification for restricted scopes.
"""
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

from src.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, YOUTUBE_OAUTH_REDIRECT_URI
from src.utils.logger import logger

OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

# youtube.upload to publish; youtube.readonly to confirm which channel got connected.
YOUTUBE_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and YOUTUBE_OAUTH_REDIRECT_URI)


def build_auth_url(channel_id: str) -> str:
    """The URL the frontend redirects the user to, to grant NicheCut upload
    access to their YouTube channel. `state` carries the NicheCut channel_id
    through the round trip so the callback knows which channel to attach the
    resulting tokens to."""
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": YOUTUBE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": YOUTUBE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # forces a refresh_token even on repeat consent
        "state": channel_id,
    }
    return f"{OAUTH_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Exchanges a one-time authorization code for an access_token + refresh_token."""
    resp = httpx.post(OAUTH_TOKEN_URL, data={
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": YOUTUBE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _refresh_access_token(refresh_token: str) -> dict:
    resp = httpx.post(OAUTH_TOKEN_URL, data={
        "refresh_token": refresh_token,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_own_channel_info(access_token: str) -> Optional[dict]:
    """Returns {"id", "title", "handle", "thumbnail_url"} for the YouTube
    channel the granted account owns, or None if the lookup fails. Used to
    replace NicheCut's placeholder channel identity with the creator's real
    YouTube name/avatar/handle once they connect."""
    try:
        resp = httpx.get(YOUTUBE_CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                          headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
        resp.raise_for_status()
        items = resp.json().get("items") or []
        if not items:
            return None
        snippet = items[0]["snippet"]
        thumbnails = snippet.get("thumbnails") or {}
        thumbnail_url = (thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}).get("url")
        return {
            "id": items[0]["id"],
            "title": snippet["title"],
            "handle": snippet.get("customUrl"),
            "thumbnail_url": thumbnail_url,
        }
    except Exception as e:
        logger.warning(f"Failed to fetch YouTube channel info: {e}")
        return None


def get_valid_access_token(channel) -> Optional[str]:
    """Returns a currently-valid access_token for this channel, refreshing it
    first if it's expired or about to expire. Returns None if the channel
    isn't connected or the refresh fails (e.g. a revoked/expired Testing-mode
    refresh token) — callers should treat that as "publish skipped this run"."""
    if not channel.youtube_refresh_token:
        return None
    expiry = channel.youtube_token_expiry
    if channel.youtube_access_token and expiry and expiry > datetime.utcnow() + timedelta(minutes=2):
        return channel.youtube_access_token

    try:
        data = _refresh_access_token(channel.youtube_refresh_token)
        access_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        channel.youtube_access_token = access_token
        channel.youtube_token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)
        return access_token
    except Exception as e:
        logger.warning(f"YouTube token refresh failed for channel {channel.id}: {e}")
        return None


def upload_video(
    access_token: str,
    video_path: Path,
    title: str,
    description: str = "",
    privacy_status: str = "public",
    category_id: str = "22",  # "People & Blogs" — reasonable default for narration/faceless channels
) -> str:
    """
    Uploads a finished video file to the account behind access_token via
    YouTube's resumable upload protocol, and returns the new video's id.
    Raises on any failure — callers should catch and record the error rather
    than letting it break the render pipeline that already succeeded.
    """
    file_size = video_path.stat().st_size
    snippet = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    init_resp = httpx.post(
        YOUTUBE_UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        },
        json=snippet,
        timeout=30,
    )
    init_resp.raise_for_status()
    upload_url = init_resp.headers.get("Location")
    if not upload_url:
        raise RuntimeError("YouTube did not return a resumable upload URL.")

    with open(video_path, "rb") as f:
        upload_resp = httpx.put(
            upload_url,
            content=f,
            headers={"Content-Type": "video/mp4", "Content-Length": str(file_size)},
            timeout=1800,  # long-form videos can take a while to upload
        )
    upload_resp.raise_for_status()
    video_id = upload_resp.json().get("id")
    if not video_id:
        raise RuntimeError(f"YouTube upload succeeded but returned no video id: {upload_resp.text[:300]}")
    return video_id


def publish_video_for_channel(channel, video_path: Path, title: str, description: str = "") -> str:
    """High-level helper: refreshes the channel's token if needed, then uploads.
    Raises RuntimeError if the channel isn't connected or the token can't be refreshed."""
    access_token = get_valid_access_token(channel)
    if not access_token:
        raise RuntimeError("Chaîne non connectée à YouTube, ou jeton d'accès expiré/révoqué.")
    return upload_video(access_token, video_path, title, description)
