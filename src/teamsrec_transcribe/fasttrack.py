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

Voice post-processing (`add_voices`, only after a fast-track transcript, started from the page when wanted): the
standard local diarization runs on the recording – one GPU pass, no transcription – and each speaker group of the
cloud transcript gets the voice embedding of the local voice that overlaps it most. Then the standard voice
recognition runs (`pipeline.recognize_voices`), and names confirmed later store prints as usual. The cloud
diarization keeps the groups; the local one only lends its voices. A group that overlaps two local voices about
equally is reported (it may mix two people).

Nothing here changes the configuration on disk: `cloud_only` returns a copy for one job (`run-job --cloud`).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace

from . import settings
from .config import Config
from .recording import Recording, RecordingError
from .voiceprints import _unit

log = logging.getLogger(__name__)

CLOUD_PROVIDERS = ("elevenlabs", "openai")
MIX_SHARE = 0.3  # a cloud group whose second local voice covers this much of its speech may mix two people


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


def needs_voices(data: dict) -> bool:
    """A transcript made by the fast track (a cloud provider) that has no voice embeddings yet."""
    return data.get("provider") in CLOUD_PROVIDERS and not data.get("speaker_embeddings")


def group_voices(segments: list[dict], turns: list[tuple[float, float, str]], embeddings: dict[str, list[float]]
                 ) -> tuple[dict[str, list[float]], dict[str, dict]]:
    """For each speaker group of the transcript, the embedding of the local voice that overlaps its replies most,
    and the groups whose speech is split between two local voices ({group: {"voices", "share"}})."""
    overlap: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s in segments:
        group = s.get("speaker")
        if not group:
            continue
        a, b = float(s["start"]), float(s["end"])
        for start, end, voice in turns:
            o = min(b, end) - max(a, start)
            if o > 0:
                overlap[group][voice] += o
    chosen: dict[str, list[float]] = {}
    mixed: dict[str, dict] = {}
    for group, row in overlap.items():
        ranked = sorted(row.items(), key=lambda kv: -kv[1])
        total = sum(row.values())
        if ranked and ranked[0][0] in embeddings:
            u = _unit(list(embeddings[ranked[0][0]]))  # stored as unit vectors (cosine = dot product)
            if u:
                chosen[group] = [round(x, 6) for x in u]
        if len(ranked) > 1 and total and ranked[1][1] / total >= MIX_SHARE:
            mixed[group] = {"voices": [v for v, _ in ranked[:2]], "share": round(ranked[1][1] / total, 2)}
    return chosen, mixed


def add_voices(cfg: Config, rec: Recording) -> dict:
    """The voice post-processing of a fast-track transcript (see the module docstring). Returns
    {"groups": n with a voice, "matches": voice recognitions, "mixed": groups that may mix two people}."""
    from .pipeline import recognize_voices
    from .providers.whisperx_provider import diarize_audio
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data = rec.read_json(rec.transcript_path)
    if data.get("provider") not in CLOUD_PROVIDERS:
        raise RecordingError(f"{rec.stem}: voices are added only to a fast-track (cloud) transcript")
    import whisperx
    audio = rec.mix_path if rec.mix_path and rec.mix_path.exists() else None
    if audio is None:
        raise RecordingError(f"{rec.stem}: no audio to take the voices from")
    from .timings import step
    with step("lokální rozlišení mluvčích"):
        dia, embeddings = diarize_audio(whisperx.load_audio(str(audio)), cfg.transcribe.diarize_model,
                                        cfg.transcribe.device)
    turns = [(float(r.start), float(r.end), str(r.speaker)) for r in dia.itertuples()]
    voices, mixed = group_voices(data.get("segments") or [], turns, embeddings or {})
    data["speaker_embeddings"] = voices
    data["voices_from"] = {"model": cfg.transcribe.diarize_model, "mixed": mixed}
    data["diarize_model"] = data.get("diarize_model") or cfg.transcribe.diarize_model
    rec.write_json(rec.transcript_path, data)
    log.info("%s: voices added to %d cloud speaker groups%s", rec.stem, len(voices),
             f", possibly mixed: {', '.join(mixed)}" if mixed else "")
    with step("poznání po hlase"):
        matches = recognize_voices(cfg, rec) if cfg.voiceprints.enabled and voices else {}
    return {"groups": len(voices), "matches": len(matches), "mixed": sorted(mixed)}
