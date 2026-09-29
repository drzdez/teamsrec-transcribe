"""Provider registry. Providers are imported lazily so that a missing optional dependency only fails when used."""

from __future__ import annotations

from .base import ProviderError, ProviderResult, Segment, TranscriptionProvider, Word

__all__ = ["get_provider", "ProviderError", "ProviderResult", "Segment", "TranscriptionProvider", "Word"]


PROVIDERS = ("whisperx", "openai", "elevenlabs")
CLOUD = ("openai", "elevenlabs")  # these send the audio to a third party (docs/privacy.md)


def get_provider(name: str) -> TranscriptionProvider:
    if name == "whisperx":
        from .whisperx_provider import WhisperXProvider
        return WhisperXProvider()
    if name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider()
    if name == "elevenlabs":
        from .elevenlabs_provider import ElevenLabsProvider
        return ElevenLabsProvider()
    raise ProviderError(f"unknown transcription provider: {name!r} (available: {', '.join(PROVIDERS)})")
