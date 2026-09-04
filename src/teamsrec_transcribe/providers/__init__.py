"""Provider registry. Providers are imported lazily so that a missing optional dependency only fails when used."""

from __future__ import annotations

from .base import ProviderError, ProviderResult, Segment, TranscriptionProvider, Word

__all__ = ["get_provider", "ProviderError", "ProviderResult", "Segment", "TranscriptionProvider", "Word"]


def get_provider(name: str) -> TranscriptionProvider:
    if name == "whisperx":
        from .whisperx_provider import WhisperXProvider
        return WhisperXProvider()
    raise ProviderError(f"unknown transcription provider: {name!r} (available: whisperx)")
