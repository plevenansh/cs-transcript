from __future__ import annotations

from json import JSONDecodeError
import os
import re
import tempfile
from os import getenv
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests
from requests import exceptions as requests_exceptions
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class TranscriptServiceError(Exception):
    error = "transcript_error"
    message = "Unable to retrieve transcript."

    def __init__(self, video_id: str | None = None, message: str | None = None):
        super().__init__(message or self.message)
        self.video_id = video_id
        self.message = message or self.message


class TranscriptUnavailable(TranscriptServiceError):
    error = "transcript_unavailable"
    message = "No captions were available for this video in the requested languages."


class YouTubeBlocked(TranscriptServiceError):
    error = "youtube_blocked"
    message = "YouTube blocked transcript retrieval from this host. Cached transcripts may still be served."


@dataclass(frozen=True)
class FetchedTranscript:
    video_id: str
    language: str
    language_code: str
    is_generated: bool
    segments: list[dict]
    text: str


@dataclass(frozen=True)
class AvailableTranscript:
    language: str
    language_code: str
    is_generated: bool
    is_translatable: bool


def extract_video_id(value: str) -> str:
    candidate = value.strip()
    if VIDEO_ID_RE.match(candidate):
        return candidate

    parsed = urlparse(candidate)
    host = parsed.netloc.lower().removeprefix("www.")

    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        query_id = parse_qs(parsed.query).get("v", [None])[0]
        if query_id and VIDEO_ID_RE.match(query_id):
            return query_id
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"} and VIDEO_ID_RE.match(parts[1]):
            return parts[1]

    if host == "youtu.be":
        path_id = parsed.path.strip("/").split("/")[0]
        if VIDEO_ID_RE.match(path_id):
            return path_id

    raise ValueError("Provide a valid YouTube video ID or URL.")


def normalize_languages(languages: str | list[str] | None, default_languages: list[str]) -> list[str]:
    if isinstance(languages, str):
        values = [item.strip() for item in languages.split(",")]
    elif languages:
        values = [item.strip() for item in languages]
    else:
        values = default_languages
    normalized = [value for value in values if value and value.lower() != "auto"]
    return normalized or default_languages


def language_cache_key(languages: list[str]) -> str:
    return ",".join(languages)


def is_explicit_language_request(languages: str | list[str] | None) -> bool:
    if isinstance(languages, str):
        return bool(normalize_languages(languages, []))
    return bool(normalize_languages(languages, [])) if languages else False


def _segment_to_dict(segment) -> dict:
    if isinstance(segment, dict):
        return {
            "text": str(segment["text"]),
            "start": float(segment["start"]),
            "duration": float(segment["duration"]),
        }
    return {
        "text": str(segment.text),
        "start": float(segment.start),
        "duration": float(segment.duration),
    }


def _fetch_segments(transcript):
    if hasattr(transcript, "fetch"):
        fetched = transcript.fetch()
        if hasattr(fetched, "to_raw_data"):
            return fetched.to_raw_data()
        return fetched
    return transcript


def _configured_proxy_url() -> str:
    proxy_url = (getenv("PROXY_URL") or "").strip()
    if proxy_url in {
        "http://username:password@proxy-host:port",
        "https://username:password@proxy-host:port",
    }:
        proxy_url = ""
    return proxy_url


def _webshare_proxy_config_from_env() -> WebshareProxyConfig | None:
    webshare_username = (getenv("WEBSHARE_PROXY_USERNAME") or "").strip()
    webshare_password = (getenv("WEBSHARE_PROXY_PASSWORD") or "").strip()
    if not webshare_username or not webshare_password:
        return None

    countries = [
        country.strip()
        for country in getenv("WEBSHARE_PROXY_COUNTRIES", "").split(",")
        if country.strip()
    ]
    try:
        retries = int(getenv("WEBSHARE_PROXY_RETRIES", "10"))
    except ValueError:
        retries = 10
    return WebshareProxyConfig(
        proxy_username=webshare_username,
        proxy_password=webshare_password,
        filter_ip_locations=countries or None,
        retries_when_blocked=retries,
    )


def _proxy_url_from_env() -> str | None:
    proxy_url = _configured_proxy_url()
    if proxy_url:
        return proxy_url

    webshare_config = _webshare_proxy_config_from_env()
    if webshare_config is not None:
        return webshare_config.url

    return None


def _unfiltered_proxy_url_from_env() -> str | None:
    """Return a Webshare proxy URL without country filters for a larger IP pool."""
    proxy_url = _configured_proxy_url()
    if proxy_url:
        return proxy_url

    webshare_username = (getenv("WEBSHARE_PROXY_USERNAME") or "").strip()
    webshare_password = (getenv("WEBSHARE_PROXY_PASSWORD") or "").strip()
    if not webshare_username or not webshare_password:
        return None

    unfiltered = WebshareProxyConfig(
        proxy_username=webshare_username,
        proxy_password=webshare_password,
    )
    return unfiltered.url


def _proxy_config_from_env():
    proxy_url = _configured_proxy_url()
    if proxy_url:
        return GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)

    webshare_config = _webshare_proxy_config_from_env()
    if webshare_config is not None:
        return webshare_config

    return None


def _normalize_cookies_content(content: str) -> str:
    """Handle Railway env vars that may store newlines as literal \\n sequences."""
    if "\n" not in content and "\\n" in content:
        content = content.replace("\\n", "\n")
    return content


_cached_cookies_file: str | None = None


def _get_cookies_file() -> str | None:
    """Write YOUTUBE_COOKIES env var to a temp file once and return the path."""
    global _cached_cookies_file
    if _cached_cookies_file is not None:
        return _cached_cookies_file if os.path.exists(_cached_cookies_file) else None
    cookies_content = _normalize_cookies_content((getenv("YOUTUBE_COOKIES") or "").strip())
    if not cookies_content:
        return None
    # yt-dlp requires a Netscape HTTP Cookie File header to recognize the file format.
    if not cookies_content.startswith("# Netscape HTTP Cookie File") and not cookies_content.startswith("# HTTP Cookie File"):
        cookies_content = "# Netscape HTTP Cookie File\n" + cookies_content
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    tmp.write(cookies_content)
    tmp.close()
    _cached_cookies_file = tmp.name
    return _cached_cookies_file


def _make_cookie_session() -> requests.Session | None:
    """Build a requests.Session with YouTube cookies parsed directly from env."""
    cookies_content = _normalize_cookies_content((getenv("YOUTUBE_COOKIES") or "").strip())
    if not cookies_content:
        return None
    session = requests.Session()
    loaded = 0
    for line in cookies_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, _secure, _expiry, name, value = parts[:7]
        try:
            session.cookies.set(name, value, domain=domain, path=path)
            loaded += 1
        except Exception:
            continue
    return session if loaded > 0 else None


def _youtube_api() -> YouTubeTranscriptApi:
    session = _make_cookie_session()
    # When cookies are configured, skip the proxy — YouTube authenticates
    # via cookies directly and proxy IPs only cause 429 rate-limits.
    proxy = None if session is not None else _proxy_config_from_env()
    return YouTubeTranscriptApi(
        proxy_config=proxy,
        http_client=session,
    )


def _is_request_blocked(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "429",
            "too many requests",
            "too many 429",
            "blocked",
            "request blocked",
            "ipblocked",
            "unusual traffic",
            "sign in to confirm",
            "not a bot",
            "please sign in",
            "confirm you",
        )
    )


def _raise_transcript_error(video_id: str, exc: Exception):
    if _is_request_blocked(exc):
        raise YouTubeBlocked(video_id) from exc
    raise TranscriptUnavailable(video_id) from exc


def _build_plain_text(segments: list[dict]) -> str:
    return " ".join(segment["text"].strip() for segment in segments if segment["text"].strip())


def _fetch_via_transcript_api(video_id: str, languages: list[str], allow_any_language: bool) -> FetchedTranscript:
    transcript_list = _list_transcripts(video_id)

    try:
        transcript = transcript_list.find_manually_created_transcript(languages)
    except NoTranscriptFound:
        try:
            transcript = transcript_list.find_generated_transcript(languages)
        except NoTranscriptFound as exc:
            if not allow_any_language:
                raise TranscriptUnavailable(video_id) from exc
            transcript = _first_available_transcript(transcript_list, video_id)

    try:
        raw_segments = _fetch_segments(transcript)
        segments = [_segment_to_dict(segment) for segment in raw_segments]
    except (CouldNotRetrieveTranscript, requests_exceptions.RequestException) as exc:
        _raise_transcript_error(video_id, exc)

    return FetchedTranscript(
        video_id=video_id,
        language=getattr(transcript, "language", getattr(transcript, "language_code", languages[0])),
        language_code=getattr(transcript, "language_code", languages[0]),
        is_generated=bool(getattr(transcript, "is_generated", False)),
        segments=segments,
        text=_build_plain_text(segments),
    )


def _language_code_matches(requested: str, available: str) -> bool:
    requested_value = requested.strip().lower()
    available_value = available.strip().lower()
    if requested_value == available_value:
        return True
    return requested_value.split("-")[0] == available_value.split("-")[0]


def _pick_yt_dlp_format(formats: list[dict]) -> dict | None:
    for format_info in formats:
        if format_info.get("ext") == "json3" and format_info.get("url"):
            return format_info
    for format_info in formats:
        if format_info.get("url"):
            return format_info
    return None


def _select_yt_dlp_track(track_map: dict[str, list[dict]], languages: list[str], allow_any_language: bool):
    for requested_language in languages:
        for language_code, formats in track_map.items():
            if not _language_code_matches(requested_language, language_code):
                continue
            selected_format = _pick_yt_dlp_format(formats)
            if selected_format is not None:
                return language_code, selected_format

    if not allow_any_language:
        return None

    for language_code, formats in track_map.items():
        selected_format = _pick_yt_dlp_format(formats)
        if selected_format is not None:
            return language_code, selected_format

    return None


def _caption_segments_from_json3(payload: dict) -> list[dict]:
    segments: list[dict] = []
    for event in payload.get("events", []):
        start_ms = event.get("tStartMs")
        if start_ms is None:
            continue

        text = "".join(segment.get("utf8", "") for segment in event.get("segs", []))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue

        duration_ms = event.get("dDurationMs") or 0
        segments.append(
            {
                "text": text,
                "start": float(start_ms) / 1000,
                "duration": float(duration_ms) / 1000,
            }
        )
    return segments


def _fetch_yt_dlp_payload(video_id: str, languages: list[str], allow_any_language: bool, proxy_url: str | None) -> FetchedTranscript:
    options = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["android", "tv_embedded", "web"]}},
    }
    if proxy_url:
        options["proxy"] = proxy_url
    cookies_file = _get_cookies_file()
    if cookies_file:
        options["cookiefile"] = cookies_file

    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)

    if info is None:
        raise TranscriptUnavailable(video_id)

    manual_tracks = info.get("subtitles") or {}
    automatic_tracks = info.get("automatic_captions") or {}

    # Prefer requested language in manual, then automatic, then any-language fallback.
    is_generated = False
    selected_track = _select_yt_dlp_track(manual_tracks, languages, allow_any_language=False)
    if selected_track is None:
        selected_track = _select_yt_dlp_track(automatic_tracks, languages, allow_any_language=False)
        is_generated = True
    if selected_track is None and allow_any_language:
        selected_track = _select_yt_dlp_track(automatic_tracks, languages, allow_any_language=True)
        is_generated = True
    if selected_track is None and allow_any_language:
        selected_track = _select_yt_dlp_track(manual_tracks, languages, allow_any_language=True)
        is_generated = False
    if selected_track is None:
        raise TranscriptUnavailable(video_id)

    language_code, format_info = selected_track
    request_kwargs: dict = {"timeout": 30}
    if proxy_url:
        request_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}

    response = requests.get(format_info["url"], **request_kwargs)
    if response.status_code == 429:
        raise YouTubeBlocked(video_id)
    response.raise_for_status()

    try:
        payload = response.json()
    except JSONDecodeError as exc:
        _raise_transcript_error(video_id, exc)

    segments = _caption_segments_from_json3(payload)
    if not segments:
        raise TranscriptUnavailable(video_id)

    return FetchedTranscript(
        video_id=video_id,
        language=str(format_info.get("name") or language_code),
        language_code=language_code,
        is_generated=is_generated,
        segments=segments,
        text=_build_plain_text(segments),
    )


def _fetch_via_yt_dlp(video_id: str, languages: list[str], allow_any_language: bool) -> FetchedTranscript:
    last_error: Exception | None = None

    # When cookies are set, go direct — no proxy.
    # Cookies authenticate with YouTube; adding a proxy only causes bot-blocks.
    if _get_cookies_file():
        try:
            return _fetch_yt_dlp_payload(video_id, languages, allow_any_language, None)
        except TranscriptUnavailable as exc:
            raise
        except (DownloadError, requests_exceptions.RequestException, YouTubeBlocked) as exc:
            _raise_transcript_error(video_id, exc)

    # No cookies — retry with rotating proxy pool.
    try:
        return _fetch_yt_dlp_payload(video_id, languages, allow_any_language, None)
    except TranscriptUnavailable as exc:
        last_error = exc
    except (DownloadError, requests_exceptions.RequestException, YouTubeBlocked) as exc:
        last_error = exc

    configured_proxy = _unfiltered_proxy_url_from_env()
    if configured_proxy:
        try:
            proxy_retries = int(getenv("YTDLP_PROXY_RETRIES", "10"))
        except ValueError:
            proxy_retries = 10

        for _ in range(proxy_retries):
            try:
                return _fetch_yt_dlp_payload(video_id, languages, allow_any_language, configured_proxy)
            except TranscriptUnavailable as exc:
                last_error = exc
                break
            except (DownloadError, requests_exceptions.RequestException, YouTubeBlocked) as exc:
                last_error = exc
                continue

    if last_error is not None:
        if isinstance(last_error, TranscriptUnavailable):
            raise last_error
        _raise_transcript_error(video_id, last_error)

    raise TranscriptUnavailable(video_id)


def _list_transcripts(video_id: str):
    api = _youtube_api()
    try:
        return api.list(video_id)
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable) as exc:
        raise TranscriptUnavailable(video_id) from exc
    except (CouldNotRetrieveTranscript, requests_exceptions.RequestException) as exc:
        _raise_transcript_error(video_id, exc)


def list_available_transcripts(video_id: str) -> list[AvailableTranscript]:
    transcript_list = _list_transcripts(video_id)
    return [
        AvailableTranscript(
            language=transcript.language,
            language_code=transcript.language_code,
            is_generated=bool(transcript.is_generated),
            is_translatable=bool(transcript.is_translatable),
        )
        for transcript in transcript_list
    ]


def fetch_transcript(video_id: str, languages: list[str], allow_any_language: bool = False) -> FetchedTranscript:
    try:
        return _fetch_via_transcript_api(video_id, languages, allow_any_language)
    except YouTubeBlocked:
        try:
            return _fetch_via_yt_dlp(video_id, languages, allow_any_language)
        except TranscriptUnavailable:
            raise
        except TranscriptServiceError:
            raise


def _first_available_transcript(transcript_list, video_id: str):
    for transcript in transcript_list:
        return transcript
    raise TranscriptUnavailable(video_id)
