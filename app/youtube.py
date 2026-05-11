from __future__ import annotations

import re
from os import getenv
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

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


def _proxy_config_from_env():
    proxy_url = (getenv("PROXY_URL") or "").strip()
    if proxy_url in {
        "http://username:password@proxy-host:port",
        "https://username:password@proxy-host:port",
    }:
        proxy_url = ""
    if proxy_url:
        return GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)

    webshare_username = getenv("WEBSHARE_PROXY_USERNAME")
    webshare_password = getenv("WEBSHARE_PROXY_PASSWORD")
    if webshare_username and webshare_password:
        countries = [
            country.strip()
            for country in getenv("WEBSHARE_PROXY_COUNTRIES", "").split(",")
            if country.strip()
        ]
        retries = int(getenv("WEBSHARE_PROXY_RETRIES", "10"))
        return WebshareProxyConfig(
            proxy_username=webshare_username,
            proxy_password=webshare_password,
            filter_ip_locations=countries or None,
            retries_when_blocked=retries,
        )

    return None


def _youtube_api() -> YouTubeTranscriptApi:
    return YouTubeTranscriptApi(proxy_config=_proxy_config_from_env())


def _list_transcripts(video_id: str):
    api = _youtube_api()
    try:
        return api.list(video_id)
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable) as exc:
        raise TranscriptUnavailable(video_id) from exc
    except CouldNotRetrieveTranscript as exc:
        message = str(exc).lower()
        if "blocked" in message or "ip" in message or "request blocked" in message:
            raise YouTubeBlocked(video_id) from exc
        raise TranscriptUnavailable(video_id) from exc


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
    except CouldNotRetrieveTranscript as exc:
        message = str(exc).lower()
        if "blocked" in message or "ip" in message or "request blocked" in message:
            raise YouTubeBlocked(video_id) from exc
        raise TranscriptUnavailable(video_id) from exc

    plain_text = " ".join(segment["text"].strip() for segment in segments if segment["text"].strip())
    return FetchedTranscript(
        video_id=video_id,
        language=getattr(transcript, "language", getattr(transcript, "language_code", languages[0])),
        language_code=getattr(transcript, "language_code", languages[0]),
        is_generated=bool(getattr(transcript, "is_generated", False)),
        segments=segments,
        text=plain_text,
    )


def _first_available_transcript(transcript_list, video_id: str):
    for transcript in transcript_list:
        return transcript
    raise TranscriptUnavailable(video_id)
