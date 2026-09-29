"""OpenAI gpt-4o-transcribe-diarize (speech to text with speaker labels).

POST https://api.openai.com/v1/audio/transcriptions, multipart: file, model, response_format=diarized_json,
chunking_strategy=auto (required above 30 s). The upload limit is 25 MB, so the audio goes as mono Opus at a bit
rate that fits (a 4-hour meeting still does at ~12 kbps). The diarize model takes neither a language nor a
prompt, and returns segments (start, end, text, speaker "A", "B", ...) without word timestamps and without a
language, so the language stays what the config says (or "und"). No voice embeddings come back: voice prints
do not work with this provider.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config import TranscribeSettings
from .base import ProviderError, ProviderResult, Segment
from .cloud import api_key, compress_for_upload, remove_upload, normalize_speakers

log = logging.getLogger(__name__)

URL = "https://api.openai.com/v1/audio/transcriptions"
MAX_UPLOAD = 24_000_000  # the API takes 25 MB


class OpenAIProvider:
    name = "openai"

    def transcribe(self, audio: Path, *, language: str | None, prompt: str | None,
                   settings: TranscribeSettings, diarize: bool) -> ProviderResult:
        import requests
        key = api_key("openai", "OPENAI_API_KEY")
        model = settings.openai_model or "gpt-4o-transcribe-diarize"
        t0 = time.monotonic()
        upload = compress_for_upload(audio, MAX_UPLOAD, kbps_max=32)
        t_enc = time.monotonic()
        data = {"model": model, "response_format": "diarized_json", "chunking_strategy": "auto"}
        try:
            with upload.open("rb") as f:
                r = requests.post(URL, headers={"Authorization": f"Bearer {key}"}, data=data,
                                  files={"file": (upload.name, f, "audio/webm")}, timeout=3600)
        except requests.RequestException as e:
            raise ProviderError(f"OpenAI request failed: {e}") from e
        finally:
            remove_upload(upload)
        if r.status_code != 200:
            raise ProviderError(f"OpenAI {r.status_code}: {r.text[:400]}")
        body = r.json()
        lang = language or "und"
        segments = [Segment(start=float(s.get("start", 0.0)), end=float(s.get("end", 0.0)),
                            text=str(s.get("text", "")).strip(),
                            speaker=str(s.get("speaker")) if diarize and s.get("speaker") is not None else None,
                            language=None if lang == "und" else lang)
                    for s in body.get("segments", []) if str(s.get("text", "")).strip()]
        normalize_speakers(segments)
        usage = body.get("usage") or {}
        log.info("openai %s: %d segments, %d speakers, usage %s", model, len(segments),
                 len({s.speaker for s in segments if s.speaker}), usage)
        return ProviderResult(segments=segments, language=lang, provider=self.name, provider_version="v1",
                              model=model, timings={"encode_s": round(t_enc - t0, 1),
                                                    "request_s": round(time.monotonic() - t_enc, 1)},
                              speaker_embeddings=None, diarize_model=f"openai:{model}" if diarize else "")
