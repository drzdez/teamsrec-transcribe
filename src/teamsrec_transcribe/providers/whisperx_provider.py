"""Local WhisperX provider: faster-whisper ASR + wav2vec2 alignment + pyannote diarization on CUDA.

Validated config (lab/FINDINGS.md): large-v3, float16, batch 16, beam 5, language auto, prompt from title +
participants + glossary, diarization pyannote/speaker-diarization-community-1.
"""

from __future__ import annotations

import gc
import re
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
    return [_detect(model, wav[s:s + N_SAMPLES]) for s in starts]


def _detect(model, chunk) -> dict[str, float]:
    """Whisper's language probabilities for one piece of audio (up to 30 s)."""
    from whisperx.audio import N_SAMPLES, log_mel_spectrogram
    n_mels = model.model.feat_kwargs.get("feature_size") if hasattr(model.model, "feat_kwargs") else None
    chunk = chunk[:N_SAMPLES]
    seg = log_mel_spectrogram(chunk, n_mels=n_mels if n_mels is not None else 80,
                              padding=0 if chunk.shape[0] >= N_SAMPLES else N_SAMPLES - chunk.shape[0])
    results = model.model.model.detect_language(model.model.encode(seg))
    return {tok[2:-2]: float(p) for tok, p in results[0]}


# what Whisper writes into silence, clicks and music (subtitle credits from its training data); a reply that is
# nothing but one of these is not speech
HALLUCINATION = re.compile(
    r"^\W*(?:(?:ďakujem|dakujem|děkuji|dekuji|díky|diky|vďaka)\s+(?:vám\s+|vam\s+)?za\s+pozornos[tť]"
    r"|konec|koniec|the end"
    r"|(?:www\.|https?://)\S+"
    r"|.*\btitulky\b.*|.*\bpřeložil\b.*|.*\bpreložil\b.*"
    r"|thank(?:s| you) for watching.*|subtitles by.*|.*\bodoberajte\b.*|.*\bodběr\b.*)\W*$",
    re.I)
QUIET_DB_BELOW = 8.0   # a short reply this far below the recording's median loudness is noise, not speech
QUIET_MAX_WORDS = 8


def is_hallucination(text: str, level_db: float | None = None, median_db: float | None = None) -> bool:
    """A known invented phrase, or a short reply much quieter than the recording. Pure, testable."""
    t = (text or "").strip()
    if not t or HALLUCINATION.match(t):
        return True
    if level_db is not None and median_db is not None and len(t.split()) < QUIET_MAX_WORDS:
        return level_db < median_db - QUIET_DB_BELOW
    return False


def _levels(wav, segments: list[dict]) -> tuple[list[float], float]:
    """Loudness of each segment (90th percentile of 100 ms frames, dBFS) and the recording's median."""
    import numpy as np
    from whisperx.audio import SAMPLE_RATE
    hop = SAMPLE_RATE // 10
    n = len(wav) // hop
    frames = np.asarray(wav[:n * hop], dtype=np.float32).reshape(n, hop) if n else np.zeros((1, hop), np.float32)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1) + 1e-12) + 1e-9)
    out = []
    for s in segments:
        a, b = int(float(s["start"]) * 10), max(int(float(s["end"]) * 10), int(float(s["start"]) * 10) + 1)
        v = db[a:b]
        out.append(float(np.percentile(v, 90)) if len(v) else -120.0)
    return out, float(np.median(db))


def drop_hallucinations(wav, segments: list[dict]) -> tuple[list[dict], list[str]]:
    levels, median = _levels(wav, segments)
    keep, dropped = [], []
    for s, lvl in zip(segments, levels):
        (dropped if is_hallucination(s.get("text", ""), lvl, median) else keep).append(s)
    return keep, [(s.get("text") or "").strip() for s in dropped]


SPEAKER_LANG_MIN_S = 20.0   # a speaker needs this much speech for a language of their own
SPEAKER_LANG_MARGIN = 0.15  # ... and their language this far ahead of the meeting's


def choose_speaker_languages(votes: dict[str, list[dict[str, float]]], meeting: str, allowed: tuple[str, ...],
                             margin: float = SPEAKER_LANG_MARGIN) -> dict[str, str]:
    """Speakers whose own speech is clearly in another allowed language than the meeting's. Pure, testable."""
    out: dict[str, str] = {}
    for speaker, vs in votes.items():
        if not vs:
            continue
        avg = {lang: sum(float(v.get(lang, 0.0)) for v in vs) / len(vs) for lang in allowed}
        best = max(avg, key=lambda k: avg[k])
        if best != meeting and avg[best] - avg.get(meeting, 0.0) >= margin:
            out[speaker] = best
    return out


def _speaker_votes(model, wav, segments: list[dict], min_s: float = SPEAKER_LANG_MIN_S,
                   windows: int = 3) -> dict[str, list[dict[str, float]]]:
    """Language probabilities per speaker, from 30 s windows made only of that speaker's speech (longest
    replies first - short ones are mostly "mhm")."""
    import numpy as np
    from whisperx.audio import N_SAMPLES, SAMPLE_RATE
    by: dict[str, list[dict]] = {}
    for s in segments:
        if s.get("speaker"):
            by.setdefault(s["speaker"], []).append(s)
    out: dict[str, list[dict[str, float]]] = {}
    for speaker, segs in by.items():
        if sum(s["end"] - s["start"] for s in segs) < min_s:
            continue
        longest = sorted(segs, key=lambda s: -(s["end"] - s["start"]))
        joined = np.concatenate([wav[int(s["start"] * SAMPLE_RATE):int(s["end"] * SAMPLE_RATE)] for s in longest])
        pieces = [joined[i:i + N_SAMPLES] for i in range(0, min(len(joined), N_SAMPLES * windows), N_SAMPLES)]
        out[speaker] = [_detect(model, p) for p in pieces if len(p) >= SAMPLE_RATE * 5]
    return out


def _retranscribe(model, wav, segments: list[dict], language: str, batch_size: int) -> list[dict]:
    """One speaker's replies again, in their own language. A reply in which the right-language model hears no
    speech at all is dropped: that is where the wrong-language pass invented a sentence."""
    from whisperx.audio import SAMPLE_RATE
    out = []
    for s in segments:
        chunk = wav[int(s["start"] * SAMPLE_RATE):int(s["end"] * SAMPLE_RATE)]
        if len(chunk) < SAMPLE_RATE // 2:
            continue
        r = model.transcribe(chunk, batch_size=batch_size, language=language)
        text = " ".join((x.get("text") or "").strip() for x in r.get("segments", [])).strip()
        if text:
            out.append({"start": s["start"], "end": s["end"], "text": text, "speaker": s["speaker"]})
    return out


def _pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def _load_align_model(whisperx, language: str, device: str):
    """The word-alignment model, from the Hugging Face cache when the hub cannot give it. 2026-10-05: with an invalid
    stored hub token the hub answered 401 and whisperx reported the Slovak model as "could not be found", while the
    cached copy works – alignment was skipped, replies stayed ~25 s long and mixed several voices. Without alignment
    there are no speaker turns inside a reply, so it is worth the second try."""
    try:
        return whisperx.load_align_model(language_code=language, device=device)
    except ValueError as first:
        try:
            got = whisperx.load_align_model(language_code=language, device=device, model_cache_only=True)
        except Exception:
            raise first
        log.warning("alignment model for %s loaded from the local cache (the hub failed: %s)", language,
                    str(first)[:120])
        return got


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
        auto_language = language is None
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
                align_model, meta = _load_align_model(whisperx, detected, device)
                result = whisperx.align(result["segments"], align_model, meta, wav, device,
                                        return_char_alignments=False)
                del align_model
                gc.collect()
                if device == "cuda":
                    torch.cuda.empty_cache()
                timings["align_s"] = round(time.time() - t, 1)
            except ValueError as e:  # no alignment model for this language
                log.warning("alignment skipped: %s", e)
                timings["align_skipped"] = str(e)[:200]  # replies stay ~30 s long and mix voices: the page says so

        embeddings = None
        if diarize:
            t = time.time()
            from whisperx.diarize import DiarizationPipeline
            pipeline = DiarizationPipeline(model_name=settings.diarize_model, device=device)
            dia, embeddings = pipeline(wav, return_embeddings=True)  # one embedding per diarization label
            result = whisperx.assign_word_speakers(dia, result)
            timings["diarize_s"] = round(time.time() - t, 1)
            del pipeline
            gc.collect()

        speaker_languages: dict[str, str] = {}
        if diarize and auto_language and settings.per_speaker_language and len(settings.languages) > 1:
            t = time.time()
            try:
                result, speaker_languages = self._per_speaker_language(whisperx, wav, result, detected, settings,
                                                                       device, asr_options)
            except Exception as e:  # never lose the transcript over the refinement
                log.warning("per-speaker language skipped: %s", e)
            timings["speaker_language_s"] = round(time.time() - t, 1)

        kept, dropped = drop_hallucinations(wav, result["segments"])
        if dropped:
            log.info("dropped %d invented replies: %s", len(dropped), "; ".join(d[:40] for d in dropped[:8]))
            result = {**result, "segments": kept}
        timings["dropped_replies"] = len(dropped)
        segments = []
        for s in result["segments"]:
            text = (s.get("text") or "").strip()
            if not text:
                continue
            words = [Word(start=float(w["start"]), end=float(w["end"]), word=w["word"], score=w.get("score"))
                     for w in s.get("words", []) if "start" in w and "end" in w]
            segments.append(Segment(start=float(s["start"]), end=float(s["end"]), text=text,
                                    speaker=s.get("speaker"), track="mix", words=words,
                                    language=s.get("language")))
        timings["total_s"] = round(sum(timings.values()), 1)
        return ProviderResult(segments=segments, language=detected, provider=self.name,
                              provider_version=_pkg_version("whisperx"), model=settings.model, timings=timings,
                              speaker_embeddings=embeddings, diarize_model=settings.diarize_model if diarize else "",
                              speaker_languages=speaker_languages)

    @staticmethod
    def _per_speaker_language(whisperx, wav, result, meeting: str, settings: TranscribeSettings, device: str,
                              asr_options: dict) -> tuple[dict, dict[str, str]]:
        """Detect each speaker's language from their own speech; transcribe the speakers who speak another
        allowed language again in it, re-align their replies, and put them back in place."""
        import torch
        model = whisperx.load_model(settings.model, device, compute_type=settings.compute_type,
                                    language=None, asr_options=asr_options)
        try:
            votes = _speaker_votes(model, wav, result["segments"])
            switch = choose_speaker_languages(votes, meeting, tuple(settings.languages))
            for speaker, vs in votes.items():
                avg = {l: round(sum(v.get(l, 0.0) for v in vs) / max(len(vs), 1), 2) for l in settings.languages}
                log.info("speaker %s language %s%s", speaker, avg, f" -> {switch[speaker]}" if speaker in switch else "")
            if not switch:
                return result, {}
            redo: dict[str, list[dict]] = {}
            for speaker, lang in switch.items():
                mine = [s for s in result["segments"] if s.get("speaker") == speaker]
                redo[speaker] = _retranscribe(model, wav, mine, lang, settings.batch_size)
                log.info("speaker %s: %d replies transcribed again in %s (%d kept)", speaker, len(mine), lang,
                         len(redo[speaker]))
        finally:
            del model
            gc.collect()
            if device == "cuda":
                torch.cuda.empty_cache()
        segments = [s for s in result["segments"] if s.get("speaker") not in switch]
        for speaker, new in redo.items():
            lang = switch[speaker]
            if settings.align and new:
                try:
                    align_model, meta = _load_align_model(whisperx, lang, device)
                    aligned = whisperx.align(new, align_model, meta, wav, device, return_char_alignments=False)
                    del align_model
                    new = aligned["segments"]
                except Exception as e:  # keep the text without word times
                    log.warning("alignment for %s (%s) skipped: %s", speaker, lang, e)
            for s in new:
                s["speaker"], s["language"] = speaker, lang
            segments += new
        segments.sort(key=lambda s: float(s["start"]))
        return {**result, "segments": segments}, switch
