from __future__ import annotations

import pytest

from app.youtube import _proxy_config_from_env, extract_video_id, normalize_languages


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
