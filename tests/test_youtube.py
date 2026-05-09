from __future__ import annotations

import pytest

from app.youtube import extract_video_id, normalize_languages


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
