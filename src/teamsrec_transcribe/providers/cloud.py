"""Shared pieces of the cloud transcription providers (OpenAI, ElevenLabs).

A cloud provider sends the recording's audio to a third party - that only happens when `[transcribe] provider`
names one of them (docs/privacy.md). Keys come from the environment: `TEAMSREC_<VENDOR>_API_KEY` first, then
the vendor's usual name (`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`), then the Windows Credential Manager where the
settings page stores them (settings.get_secret).
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from ..media import _bin, probe
from .base import ProviderError, Segment, Word

log = logging.getLogger(__name__)

# ISO 639-3 / names some services answer with -> the two-letter codes the rest of teamsrec uses
LANG_2 = {"ces": "cs", "cze": "cs", "czech": "cs", "slk": "sk", "slo": "sk", "slovak": "sk", "eng": "en",
          "english": "en", "deu": "de", "ger": "de", "german": "de", "pol": "pl", "polish": "pl"}


def api_key(vendor: str, fallback: str) -> str:
    """TEAMSREC_<VENDOR>_API_KEY, else the vendor's usual variable, else the stored key. Never logged."""
    from ..settings import get_secret
    value = get_secret(vendor)
    if value:
        return value
    raise ProviderError(f"no API key for {vendor}: enter it in Settings on the review page, or set "
                        f"TEAMSREC_{vendor.upper()}_API_KEY (or {fallback}) in the environment")


def lang2(code: str | None) -> str:
    c = (code or "").strip().lower()
    if not c:
        return "und"
    return LANG_2.get(c, c if len(c) == 2 else c[:2])


def compress_for_upload(audio: Path, max_bytes: int, kbps_max: int = 48, kbps_min: int = 12) -> Path:
    """Mono Opus in WebM at a bit rate that fits `max_bytes` (speech stays clear down to ~12 kbps).
    A temporary file the caller deletes."""
    duration = max(probe(audio).duration_s, 1.0)
    kbps = int(min(kbps_max, max(kbps_min, max_bytes * 8 * 0.95 / duration / 1000)))
    fd, name = tempfile.mkstemp(prefix="teamsrec-upload-", suffix=".webm")
    os.close(fd)  # an open handle would keep Windows from deleting the file after the upload
    out = Path(name)
    cmd = [_bin("ffmpeg"), "-y", "-loglevel", "error", "-i", str(audio), "-ac", "1", "-ar", "16000",
           "-c:a", "libopus", "-b:a", f"{kbps}k", "-application", "voip", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode or not out.exists():
        out.unlink(missing_ok=True)
        raise ProviderError(f"could not compress {audio.name} for upload: {r.stderr.strip()[-300:]}")
    size = out.stat().st_size
    if size > max_bytes:
        out.unlink(missing_ok=True)
        raise ProviderError(f"{audio.name}: {duration / 3600:.1f} h does not fit the upload limit even at "
                            f"{kbps} kbps ({size / 1e6:.1f} MB > {max_bytes / 1e6:.0f} MB)")
    log.info("upload: %s -> %.1f MB (opus %d kbps, %.0f min)", audio.name, size / 1e6, kbps, duration / 60)
    return out


def normalize_speakers(segments: list[Segment]) -> None:
    """Whatever the service calls its speakers ("A", "speaker_1"), the pipeline expects SPEAKER_00,
    SPEAKER_01, ... in the order they first speak."""
    order: dict[str, str] = {}
    for seg in segments:
        if seg.speaker is None:
            continue
        if seg.speaker not in order:
            order[seg.speaker] = f"SPEAKER_{len(order):02d}"
        seg.speaker = order[seg.speaker]


def words_to_segments(words: list[dict], language: str, max_gap_s: float = 1.2,
                      max_len_s: float = 25.0) -> list[Segment]:
    """Word-level results (text, start, end, speaker) -> sentence-sized segments: a new segment on a change of
    speaker, a pause, or a long stretch that has just ended a sentence."""
    segments: list[Segment] = []
    cur: Segment | None = None
    for w in words:
        text = str(w.get("text", ""))
        if not text.strip():
            continue
        start, end = float(w.get("start", 0.0)), float(w.get("end", 0.0))
        speaker = w.get("speaker")
        if (cur is None or speaker != cur.speaker or start - cur.end > max_gap_s
                or (end - cur.start > max_len_s and cur.text.rstrip().endswith((".", "?", "!")))):
            cur = Segment(start=start, end=end, text="", speaker=speaker, language=language)
            segments.append(cur)
        cur.text = (cur.text + " " + text.strip()).strip()
        cur.end = max(cur.end, end)
        cur.words.append(Word(start, end, text.strip(), w.get("score")))
    return segments


def remove_upload(path: Path) -> None:
    """Best effort: a temporary file that cannot be deleted must never cost a finished (and paid) transcript."""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("could not delete %s: %s", path, e)
