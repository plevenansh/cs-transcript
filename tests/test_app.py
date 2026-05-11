from __future__ import annotations

import app.service
from app.database import session_scope
from app.models import TranscriptCache
import app.main
from app.youtube import AvailableTranscript, FetchedTranscript


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_auth_rejects_missing_token(client):
    response = client.get("/api/transcripts/dQw4w9WgXcQ")
    assert response.status_code == 401


def test_auth_rejects_invalid_token(client):
    response = client.get(
        "/api/transcripts/dQw4w9WgXcQ",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_api_usage_logs_client_name(client):
    _seed_transcript(client)

    response = client.get(
        "/api/transcripts/dQw4w9WgXcQ",
        headers={
            "Authorization": "Bearer test-token",
            "X-Client-Name": "marketing-site",
        },
    )
    assert response.status_code == 200

    usage = client.get(
        "/api/usage",
        headers={"Authorization": "Bearer test-token"},
    )

    assert usage.status_code == 200
    logs = usage.json()["logs"]
    assert any(log["client_name"] == "marketing-site" for log in logs)
    assert all("test-token" not in str(log) for log in logs)


def test_cache_hit_returns_transcript(client):
    with session_scope(client.app.state.session_factory) as session:
        session.add(
            TranscriptCache(
                video_id="dQw4w9WgXcQ",
                language_request="auto:en",
                language="English",
                language_code="en",
                is_generated=False,
                segments=[{"text": "Hello", "start": 0.0, "duration": 1.5}],
                text="Hello",
                source_status="ok",
            )
        )

    response = client.get(
        "/api/transcripts/dQw4w9WgXcQ",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "cache"
    assert body["text"] == "Hello"
    assert body["segments"][0]["start"] == 0.0


def test_cache_miss_fetches_and_persists_transcript(client, monkeypatch):
    calls = []

    def fake_fetch(video_id, languages, allow_any_language=False):
        calls.append((video_id, languages, allow_any_language))
        return FetchedTranscript(
            video_id=video_id,
            language="English",
            language_code="en",
            is_generated=True,
            segments=[{"text": "Fetched", "start": 2.0, "duration": 3.0}],
            text="Fetched",
        )

    monkeypatch.setattr(app.service, "fetch_transcript", fake_fetch)
    response = client.get(
        "/api/transcripts/dQw4w9WgXcQ?languages=en,hi",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "youtube"
    assert calls == [("dQw4w9WgXcQ", ["en", "hi"], False)]

    with session_scope(client.app.state.session_factory) as session:
        row = session.query(TranscriptCache).filter_by(video_id="dQw4w9WgXcQ").one()
        assert row.language_request == "en,hi"
        assert row.text == "Fetched"


def test_blank_language_request_allows_any_available_transcript(client, monkeypatch):
    calls = []

    def fake_fetch(video_id, languages, allow_any_language=False):
        calls.append((video_id, languages, allow_any_language))
        return FetchedTranscript(
            video_id=video_id,
            language="Hindi",
            language_code="hi",
            is_generated=False,
            segments=[{"text": "Namaste", "start": 0.0, "duration": 1.0}],
            text="Namaste",
        )

    monkeypatch.setattr(app.service, "fetch_transcript", fake_fetch)
    response = client.get(
        "/api/transcripts/dQw4w9WgXcQ",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json()["language_code"] == "hi"
    assert calls == [("dQw4w9WgXcQ", ["en"], True)]


def test_available_languages_endpoint(client, monkeypatch):
    def fake_languages(video_id):
        return [
            AvailableTranscript(
                language="Hindi",
                language_code="hi",
                is_generated=False,
                is_translatable=True,
            )
        ]

    monkeypatch.setattr(app.main, "list_available_transcripts", fake_languages)
    response = client.get(
        "/api/transcripts/dQw4w9WgXcQ/languages",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json()["languages"][0]["language_code"] == "hi"


def test_json_format(client):
    _seed_transcript(client)
    response = client.get(
        "/api/transcripts/dQw4w9WgXcQ/formats/json",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json()["video_id"] == "dQw4w9WgXcQ"


def test_text_format(client):
    _seed_transcript(client)
    response = client.get(
        "/api/transcripts/dQw4w9WgXcQ/formats/text",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.text == "Hello"


def test_srt_format(client):
    _seed_transcript(client)
    response = client.get(
        "/api/transcripts/dQw4w9WgXcQ/formats/srt",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert "00:00:00,000 --> 00:00:01,500" in response.text


def test_vtt_format(client):
    _seed_transcript(client)
    response = client.get(
        "/api/transcripts/dQw4w9WgXcQ/formats/vtt",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.text.startswith("WEBVTT")


def test_web_login_sets_cookie(client):
    response = client.post("/login", data={"token": " test-token\n"}, follow_redirects=False)
    assert response.status_code == 303
    assert "transcript_token" in response.headers["set-cookie"]


def test_web_login_shows_error_for_wrong_token(client):
    response = client.post("/login", data={"token": "wrong"}, follow_redirects=True)
    assert response.status_code == 200
    assert "Access token did not match" in response.text


def test_config_status_shows_token_configured(client):
    response = client.get("/api/config/status")
    assert response.status_code == 200
    assert response.json()["api_token_configured"] is True


def test_web_transcript_error_renders_html(client, monkeypatch):
    def fake_fetch(video_id, languages, allow_any_language=False):
        from app.youtube import TranscriptUnavailable

        raise TranscriptUnavailable(video_id)

    monkeypatch.setattr(app.service, "fetch_transcript", fake_fetch)
    response = client.post(
        "/web/transcripts",
        data={"video": "dQw4w9WgXcQ", "languages": "en"},
        cookies={"transcript_token": "test-token"},
    )

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "No captions were available" in response.text


def test_web_transcript_renders_timestamped_segments(client, monkeypatch):
    def fake_fetch(video_id, languages, allow_any_language=False):
        return FetchedTranscript(
            video_id=video_id,
            language="Hindi",
            language_code="hi",
            is_generated=True,
            segments=[
                {"text": "First sentence", "start": 5.2, "duration": 2.0},
                {"text": "Second sentence", "start": 65.0, "duration": 3.0},
            ],
            text="First sentence Second sentence",
        )

    monkeypatch.setattr(app.service, "fetch_transcript", fake_fetch)
    response = client.post(
        "/web/transcripts",
        data={"video": "dQw4w9WgXcQ", "languages": "hi"},
        cookies={"transcript_token": "test-token"},
    )

    assert response.status_code == 200
    assert "transcript-row" in response.text
    assert "0:05" in response.text
    assert "1:05" in response.text
    assert "First sentence" in response.text


def test_web_languages_renders_fetch_buttons(client, monkeypatch):
    def fake_languages(video_id):
        return [
            AvailableTranscript(
                language="Hindi",
                language_code="hi",
                is_generated=False,
                is_translatable=True,
            )
        ]

    monkeypatch.setattr(app.main, "list_available_transcripts", fake_languages)
    response = client.post(
        "/web/languages",
        data={"video": "dQw4w9WgXcQ"},
        cookies={"transcript_token": "test-token"},
    )

    assert response.status_code == 200
    assert "Available transcript languages" in response.text
    assert 'value="hi"' in response.text


def _seed_transcript(client):
    with session_scope(client.app.state.session_factory) as session:
        exists = session.query(TranscriptCache).filter_by(video_id="dQw4w9WgXcQ").first()
        if exists:
            return
        session.add(
            TranscriptCache(
                video_id="dQw4w9WgXcQ",
                language_request="auto:en",
                language="English",
                language_code="en",
                is_generated=False,
                segments=[{"text": "Hello", "start": 0.0, "duration": 1.5}],
                text="Hello",
                source_status="ok",
            )
        )
