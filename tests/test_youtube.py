from __future__ import annotations

import pytest
from requests.exceptions import RetryError

from app.youtube import (
    FetchedTranscript,
    YouTubeBlocked,
    _caption_segments_from_json3,
    _proxy_config_from_env,
    extract_video_id,
    fetch_transcript,
    list_available_transcripts,
    normalize_languages,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=10", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/live/dQw4w9WgXcQ?feature=share", "dQw4w9WgXcQ"),
    ],
)
def test_extract_video_id(value, expected):
    assert extract_video_id(value) == expected


def test_extract_video_id_rejects_invalid_value():
    with pytest.raises(ValueError):
        extract_video_id("https://example.com/nope")


def test_normalize_languages_uses_priority_order():
    assert normalize_languages("en, hi,es", ["en"]) == ["en", "hi", "es"]
    assert normalize_languages(None, ["en"]) == ["en"]
    assert normalize_languages("Auto", ["en"]) == ["en"]


def test_proxy_url_env_creates_proxy_config(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "http://user:pass@example.com:8080")
    proxy_config = _proxy_config_from_env()
    assert proxy_config is not None
    assert proxy_config.to_requests_dict()["http"] == "http://user:pass@example.com:8080"


def test_placeholder_proxy_url_is_ignored(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "http://username:password@proxy-host:port")
    monkeypatch.delenv("WEBSHARE_PROXY_USERNAME", raising=False)
    monkeypatch.delenv("WEBSHARE_PROXY_PASSWORD", raising=False)
    assert _proxy_config_from_env() is None


def test_list_available_transcripts_maps_retry_error_to_blocked(monkeypatch):
    class FakeApi:
        def list(self, video_id):
            raise RetryError("too many 429 error responses")

    monkeypatch.setattr("app.youtube._youtube_api", lambda: FakeApi())

    with pytest.raises(YouTubeBlocked):
        list_available_transcripts("dQw4w9WgXcQ")


def test_fetch_transcript_falls_back_to_ytdlp_when_primary_backend_is_blocked(monkeypatch):
    def fake_primary(video_id, languages, allow_any_language):
        raise YouTubeBlocked(video_id)

    def fake_fallback(video_id, languages, allow_any_language):
        return FetchedTranscript(
            video_id=video_id,
            language="English",
            language_code="en",
            is_generated=False,
            segments=[{"text": "Fallback", "start": 0.0, "duration": 1.0}],
            text="Fallback",
        )

    monkeypatch.setattr("app.youtube._fetch_via_transcript_api", fake_primary)
    monkeypatch.setattr("app.youtube._fetch_via_yt_dlp", fake_fallback)

    fetched = fetch_transcript("dQw4w9WgXcQ", ["en"])

    assert fetched.text == "Fallback"
    assert fetched.language_code == "en"


def test_caption_segments_from_json3_normalizes_text():
    payload = {
        "events": [
            {
                "tStartMs": 1250,
                "dDurationMs": 2750,
                "segs": [{"utf8": "Hello\n"}, {"utf8": "world"}],
            },
            {
                "tStartMs": 5000,
                "dDurationMs": 0,
                "segs": [{"utf8": "   "}],
            },
        ]
    }

    assert _caption_segments_from_json3(payload) == [
        {"text": "Hello world", "start": 1.25, "duration": 2.75}
    ]
