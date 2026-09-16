"""Local WhisperX provider: faster-whisper ASR + wav2vec2 alignment + pyannote diarization on CUDA.

Validated config (lab/FINDINGS.md): large-v3, float16, batch 16, beam 5, language auto, prompt from title +
participants + glossary, diarization pyannote/speaker-diarization-community-1.
"""

from __future__ import annotations

import gc
import logging
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ..config import TranscribeSettings
from .base import ProviderError, ProviderResult, Segment, Word

log = logging.getLogger(__name__)


def choose_language(votes: list[dict[str, float]], allowed: tuple[str, ...]) -> str | None:
    """Sum the per-window probabilities of the allowed languages; the best one wins. Pure, testable."""
    if not votes:
        return None
    totals: dict[str, float] = {}
    for probs in votes:
        for lang in allowed:
            totals[lang] = totals.get(lang, 0.0) + float(probs.get(lang, 0.0))
    if not totals or max(totals.values()) <= 0:
        return None
    return max(totals.items(), key=lambda kv: kv[1])[0]


def _language_votes(model, wav, windows: int = 4) -> list[dict[str, float]]:
    """Whisper's language probabilities on several loud 30 s windows spread over the recording (the first 30 s
    are often silence or a lone greeting and mislead the detector)."""
    import numpy as np
    from whisperx.audio import N_SAMPLES, SAMPLE_RATE, log_mel_spectrogram
    total = len(wav)
    if total <= N_SAMPLES:
        starts = [0]
    else:
        # candidate starts every 15 s; keep the loudest `windows` among evenly spread quarters
        step = SAMPLE_RATE * 15
        cands = list(range(0, total - N_SAMPLES, step))
        loud = sorted(cands, key=lambda s: -float(np.abs(wav[s:s + N_SAMPLES]).mean()))
        starts = sorted(loud[:windows]) or [0]
    n_mels = model.model.feat_kwargs.get("feature_size") if hasattr(model.model, "feat_kwargs") else None
    votes = []
    for s in starts:
        chunk = wav[s:s + N_SAMPLES]
        seg = log_mel_spectrogram(chunk, n_mels=n_mels if n_mels is not None else 80,
                                  padding=0 if chunk.shape[0] >= N_SAMPLES else N_SAMPLES - chunk.shape[0])
        enc = model.model.encode(seg)
        results = model.model.model.detect_language(enc)
        votes.append({tok[2:-2]: float(p) for tok, p in results[0]})
    return votes


def _pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


class WhisperXProvider:
    name = "whisperx"

    def transcribe(self, audio: Path, *, language: str | None, prompt: str | None,
                   settings: TranscribeSettings, diarize: bool) -> ProviderResult:
        try:
            import torch
            import whisperx
        except ImportError as e:  # pragma: no cover
            raise ProviderError("whisperx is not installed; install with the [whisperx] extra") from e

        device = settings.device
        if device == "cuda" and not torch.cuda.is_available():
            raise ProviderError("CUDA is not available to torch; install the CUDA torch build or set device = \"cpu\"")
        timings: dict[str, float] = {}

        t = time.time()
        wav = whisperx.load_audio(str(audio))
        timings["load_audio_s"] = round(time.time() - t, 1)

        asr_options = {"beam_size": settings.beam_size}
        if prompt:
            asr_options["initial_prompt"] = prompt
        t = time.time()
        model = whisperx.load_model(settings.model, device, compute_type=settings.compute_type,
                                    language=language, asr_options=asr_options)
        timings["load_model_s"] = round(time.time() - t, 1)
        if language is None and settings.languages:
            try:
                votes = _language_votes(model, wav)
                chosen = choose_language(votes, tuple(settings.languages))
                best = {k: round(v, 2) for k, v in sorted(votes[0].items(), key=lambda kv: -kv[1])[:3]} if votes else {}
                log.info("language among %s: %s (window 1 top: %s)", ",".join(settings.languages), chosen, best)
                language = chosen
            except Exception as e:  # fall back to whisperx's own detection
                log.warning("restricted language detection failed (%s), using whisperx default", e)
        t = time.time()
        result = model.transcribe(wav, batch_size=settings.batch_size, language=language)
        timings["transcribe_s"] = round(time.time() - t, 1)
        detected = result.get("language") or language or "unknown"
        del model
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()

        if settings.align:
            t = time.time()
            try:
                align_model, meta = whisperx.load_align_model(language_code=detected, device=device)
                result = whisperx.align(result["segments"], align_model, meta, wav, device,
                                        return_char_alignments=False)
                del align_model
                gc.collect()
                if device == "cuda":
                    torch.cuda.empty_cache()
                timings["align_s"] = round(time.time() - t, 1)
            except ValueError as e:  # no alignment model for this language
                log.warning("alignment skipped: %s", e)

        embeddings = None
        if diarize:
            t = time.time()
            from whisperx.diarize import DiarizationPipeline
            pipeline = DiarizationPipeline(model_name=settings.diarize_model, device=device)
            dia, embeddings = pipeline(wav, return_embeddings=True)  # one embedding per diarization label
            result = whisperx.assign_word_speakers(dia, result)
            timings["diarize_s"] = round(time.time() - t, 1)

        segments = []
        for s in result["segments"]:
            text = (s.get("text") or "").strip()
            if not text:
                continue
            words = [Word(start=float(w["start"]), end=float(w["end"]), word=w["word"], score=w.get("score"))
                     for w in s.get("words", []) if "start" in w and "end" in w]
            segments.append(Segment(start=float(s["start"]), end=float(s["end"]), text=text,
                                    speaker=s.get("speaker"), track="mix", words=words))
        timings["total_s"] = round(sum(timings.values()), 1)
        return ProviderResult(segments=segments, language=detected, provider=self.name,
                              provider_version=_pkg_version("whisperx"), model=settings.model, timings=timings,
                              speaker_embeddings=embeddings, diarize_model=settings.diarize_model if diarize else "")
