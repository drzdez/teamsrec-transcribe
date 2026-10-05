"""ElevenLabs Scribe (speech to text with diarization and word timestamps).

POST https://api.elevenlabs.io/v1/speech-to-text, multipart: model_id, file, diarize, timestamps_granularity,
optional language_code. Files up to 5 GB, so the whole meeting goes in one request and the speaker labels are
consistent across it. The answer is a list of words with start/end/speaker_id; they are grouped into segments
here. No voice embeddings come back, so voice prints do not work with this provider (names come from the
microphone track, the Teams window and by hand).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config import TranscribeSettings
from .base import ProviderError, ProviderResult
from .cloud import api_key, compress_for_upload, post_with_retry, remove_upload, request_timeout, lang2, normalize_speakers, words_to_segments

log = logging.getLogger(__name__)

URL = "https://api.elevenlabs.io/v1/speech-to-text"
MAX_UPLOAD = 500_000_000  # far below the 5 GB limit; compressed audio of a 4-hour meeting is ~20 MB


class ElevenLabsProvider:
    name = "elevenlabs"

    def transcribe(self, audio: Path, *, language: str | None, prompt: str | None,
                   settings: TranscribeSettings, diarize: bool) -> ProviderResult:
        import requests
        key = api_key("elevenlabs", "ELEVENLABS_API_KEY")
        model = settings.elevenlabs_model or "scribe_v2"
        t0 = time.monotonic()
        upload = compress_for_upload(audio, MAX_UPLOAD, kbps_max=48)
        t_enc = time.monotonic()
        data = {"model_id": model, "diarize": "true" if diarize else "false",
                "timestamps_granularity": "word", "tag_audio_events": "false"}
        if language:
            data["language_code"] = language
        try:
            r = post_with_retry(URL, headers={"xi-api-key": key}, data=data, upload=upload,
                                timeout=request_timeout(audio), vendor="ElevenLabs")
        finally:
            remove_upload(upload)
        if r.status_code != 200:
            raise ProviderError(f"ElevenLabs {r.status_code}: {r.text[:400]}")
        body = r.json()
        lang = lang2(body.get("language_code") or language)
        words = [{"text": w.get("text", ""), "start": w.get("start", 0.0), "end": w.get("end", 0.0),
                  "speaker": w.get("speaker_id") if diarize else None}
                 for w in body.get("words", []) if w.get("type", "word") == "word"]
        segments = words_to_segments(words, lang)
        normalize_speakers(segments)
        log.info("elevenlabs %s: %d words, %d segments, language %s (p=%.2f)", model, len(words), len(segments),
                 lang, float(body.get("language_probability") or 0.0))
        return ProviderResult(segments=segments, language=lang, provider=self.name, provider_version="v1",
                              model=model, timings={"encode_s": round(t_enc - t0, 1),
                                                    "request_s": round(time.monotonic() - t_enc, 1)},
                              speaker_embeddings=None, diarize_model=f"elevenlabs:{model}" if diarize else "")
