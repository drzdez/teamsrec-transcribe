"""Emergency fast track: process a recording on cloud services only.

The normal path is local (WhisperX + pyannote on the GPU, minutes by the local Ollama) and stays the priority. This
module is the separate, opt-in way around it, chosen per job on the review page ("⚡ … rychle přes cloud"), for when
speed or a quiet PC matter more than keeping the meeting on the PC:

- transcription *and* diarization in one request by the cloud service ([transcribe] cloud_provider: ElevenLabs Scribe,
  or OpenAI gpt-4o-transcribe-diarize) instead of WhisperX + pyannote,
- minutes by Claude ([summarize] cloud_model) instead of Ollama, no comparison minutes on the local Ollama,
- no analysis of the Teams window videos (local OCR).

The meeting's audio and transcript leave the PC (docs/privacy.md). The cloud returns no voice embeddings, so no voice
prints are matched or stored for such a recording; the microphone track still names the user.

Nothing here changes the configuration on disk: `cloud_only` returns a copy for one job (`run-job --cloud`).
"""

from __future__ import annotations

from dataclasses import replace

from . import settings
from .config import Config


def cloud_only(cfg: Config) -> Config:
    """The same configuration for one fast-track job (see the module docstring)."""
    return replace(cfg,
                   transcribe=replace(cfg.transcribe, provider=cfg.transcribe.cloud_provider, diarize=True),
                   summarize=replace(cfg.summarize, provider="anthropic", model=cfg.summarize.cloud_model,
                                     compare=tuple(c for c in cfg.summarize.compare if not c.startswith("ollama:"))),
                   video=replace(cfg.video, enabled=False))


def cloud_ready(cfg: Config) -> dict:
    """Whether the fast track can run: a key for the cloud transcription and for Claude. Names only, never keys."""
    need = {cfg.transcribe.cloud_provider: f"přepis ({cfg.transcribe.cloud_provider})", "anthropic": "zápis (Claude)"}
    missing = [what for name, what in need.items() if not settings.get_secret(name)]
    return {"ok": not missing, "missing": ", ".join(missing), "provider": cfg.transcribe.cloud_provider,
            "model": cfg.summarize.cloud_model}
