from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TranscriptCache
from app.schemas import TranscriptResponse
from app.youtube import FetchedTranscript, fetch_transcript, language_cache_key


def _response_from_cache(row: TranscriptCache) -> TranscriptResponse:
    return TranscriptResponse(
        video_id=row.video_id,
        language=row.language,
        language_code=row.language_code,
        is_generated=row.is_generated,
        source="cache",
        segments=row.segments,
        text=row.text,
    )


def _response_from_fetch(fetched: FetchedTranscript) -> TranscriptResponse:
    return TranscriptResponse(
        video_id=fetched.video_id,
        language=fetched.language,
        language_code=fetched.language_code,
        is_generated=fetched.is_generated,
        source="youtube",
        segments=fetched.segments,
        text=fetched.text,
    )


def get_or_fetch_transcript(
    session: Session,
    video_id: str,
    languages: list[str],
    allow_any_language: bool = False,
) -> TranscriptResponse:
    cache_key = language_cache_key(languages)
    if allow_any_language:
        cache_key = f"auto:{cache_key}"
    row = session.scalar(
        select(TranscriptCache).where(
            TranscriptCache.video_id == video_id,
            TranscriptCache.language_request == cache_key,
        )
    )
    if row:
        return _response_from_cache(row)

    fetched = fetch_transcript(video_id, languages, allow_any_language=allow_any_language)
    row = TranscriptCache(
        video_id=video_id,
        language_request=cache_key,
        language=fetched.language,
        language_code=fetched.language_code,
        is_generated=fetched.is_generated,
        segments=fetched.segments,
        text=fetched.text,
        source_status="ok",
    )
    session.add(row)
    session.flush()
    return _response_from_fetch(fetched)
