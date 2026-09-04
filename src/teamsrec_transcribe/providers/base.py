"""Transcription provider interface and the normalized result (contract: <stem>.transcript.json)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config import TranscribeSettings


@dataclass
class Word:
    start: float
    end: float
    word: str
    score: float | None = None


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None  # provider label (SPEAKER_00) or a name after attribution
    track: str = "mix"
    words: list[Word] = field(default_factory=list)
    language: str | None = None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"start": round(self.start, 3), "end": round(self.end, 3),
                             "speaker": self.speaker, "track": self.track, "text": self.text}
        if self.language:
            d["language"] = self.language
        if self.words:
            d["words"] = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word,
                           **({"score": round(w.score, 3)} if w.score is not None else {})} for w in self.words]
        return d


@dataclass
class ProviderResult:
    segments: list[Segment]
    language: str
    provider: str
    provider_version: str
    model: str
    timings: dict[str, float] = field(default_factory=dict)


class TranscriptionProvider(Protocol):
    name: str

    def transcribe(self, audio: Path, *, language: str | None, prompt: str | None,
                   settings: TranscribeSettings, diarize: bool) -> ProviderResult: ...


class ProviderError(RuntimeError):
    pass
