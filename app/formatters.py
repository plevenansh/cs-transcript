from __future__ import annotations

import html
import json
from app.schemas import TranscriptResponse


def format_text(transcript: TranscriptResponse) -> str:
    return transcript.text


def format_json(transcript: TranscriptResponse) -> str:
    return json.dumps(transcript.model_dump(), ensure_ascii=False, indent=2)


def _timestamp(seconds: float, separator: str) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def format_srt(transcript: TranscriptResponse) -> str:
    blocks: list[str] = []
    for index, segment in enumerate(transcript.segments, start=1):
        start = _timestamp(segment.start, ",")
        end = _timestamp(segment.start + segment.duration, ",")
        blocks.append(f"{index}\n{start} --> {end}\n{segment.text}")
    return "\n\n".join(blocks) + "\n"


def format_vtt(transcript: TranscriptResponse) -> str:
    blocks = ["WEBVTT\n"]
    for segment in transcript.segments:
        start = _timestamp(segment.start, ".")
        end = _timestamp(segment.start + segment.duration, ".")
        blocks.append(f"{start} --> {end}\n{html.escape(segment.text)}")
    return "\n\n".join(blocks) + "\n"


FORMATTERS = {
    "json": ("application/json", format_json),
    "text": ("text/plain; charset=utf-8", format_text),
    "srt": ("application/x-subrip; charset=utf-8", format_srt),
    "vtt": ("text/vtt; charset=utf-8", format_vtt),
}
