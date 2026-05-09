from __future__ import annotations

from pydantic import BaseModel


class TranscriptSegment(BaseModel):
    text: str
    start: float
    duration: float


class TranscriptResponse(BaseModel):
    video_id: str
    language: str
    language_code: str
    is_generated: bool
    source: str
    segments: list[TranscriptSegment]
    text: str


class ErrorResponse(BaseModel):
    error: str
    message: str
    video_id: str | None = None
