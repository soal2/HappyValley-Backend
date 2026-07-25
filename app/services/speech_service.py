"""Canonical server-side speech chunks for painting activity messages."""

import re
import uuid
from typing import cast

from app.config import get_settings
from app.services.tts_service import TtsServiceError, get_tts_service

_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？；!?])")
_TRAILING_PUNCTUATION = "。！？；!?，、：:…"


def split_speech(text: str, capacity: int = 28) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_BOUNDARY.split(normalized):
        sentence = sentence.strip()
        if not sentence:
            continue
        if current and len(current) + len(sentence) <= capacity:
            current += sentence
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(sentence) > capacity:
            split_at = capacity
            while split_at < len(sentence) and sentence[split_at] in _TRAILING_PUNCTUATION:
                split_at += 1
            chunks.append(sentence[:split_at])
            sentence = sentence[split_at:]
        current = sentence
    if current:
        if chunks and all(char in _TRAILING_PUNCTUATION for char in current):
            chunks[-1] += current
        else:
            chunks.append(current)
    return chunks


def estimated_duration_ms(text: str) -> int:
    return max(1_200, min(20_000, len(text) * 230))


def build_speech_meta(text: str, voice_id: str, limit_ms: int) -> dict[str, object]:
    chunks = split_speech(text)
    durations = [estimated_duration_ms(chunk) for chunk in chunks]
    return {
        "voice_id": voice_id,
        "chunks": [
            {"index": index, "text": chunk, "duration_ms": durations[index]}
            for index, chunk in enumerate(chunks)
        ],
        "total_duration_ms": sum(durations),
        "limit_ms": limit_ms,
        "measurement": "estimated",
    }


async def prepare_speech_meta(text: str, voice_id: str, limit_ms: int) -> dict[str, object]:
    meta = build_speech_meta(text, voice_id, limit_ms)
    if get_settings().MODE != "production":
        return meta
    measured_chunks: list[dict[str, object]] = []
    try:
        chunks = cast(list[dict[str, object]], meta["chunks"])
        for chunk in chunks:
            audio = await get_tts_service().synthesize(
                request_id=f"speech-preflight-{uuid.uuid4()}",
                text=str(chunk["text"]),
                speaker_id=voice_id,
                language="zh-CN",
            )
            if audio.duration_ms is None:
                return meta
            measured_chunks.append(
                {
                    "index": cast(int, chunk["index"]),
                    "text": str(chunk["text"]),
                    "duration_ms": audio.duration_ms,
                }
            )
    except TtsServiceError:
        return meta
    return {
        **meta,
        "chunks": measured_chunks,
        "total_duration_ms": sum(cast(int, chunk["duration_ms"]) for chunk in measured_chunks),
        "measurement": "measured",
    }
