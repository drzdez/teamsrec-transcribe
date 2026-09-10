"""The steps behind the CLI commands, working on Recording objects."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from .config import Config
from .export import write_exports
from .importer import import_file
from .media import mix_tracks, probe, utc_now_iso
from .mic_speakers import apply_mic_track
from .prompt import build_prompt
from .providers import get_provider
from .providers.base import Segment, Word
from .recording import Recording, RecordingError, is_media_file, is_sidecar, iter_recordings, resolve_recording
from .speakers import apply_manual_names, apply_video_timeline, speaker_list
from .video_speakers import VideoTimeline, analyze_video

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- import

def do_import(cfg: Config, src: Path, *, title: str | None = None, start: datetime | None = None,
              language: str | None = None, participants: list[str] | None = None,
              video: bool | None = None, force: bool = False) -> Recording:
    rec = import_file(src, cfg.out_dir, title=title, start=start, language=language,
                      participants=participants, force=force)
    want_video = cfg.video.enabled if video is None else video
    if want_video and (force or not rec.speakers_video_path.exists()):
        do_video(cfg, rec)
    return rec


def do_video(cfg: Config, rec: Recording) -> VideoTimeline | None:
    src = rec.origin_path
    if not src or not src.exists():
        log.info("%s: no origin video file, skipping video analysis", rec.stem)
        return None
    info = probe(src)
    if not info.has_video:
        return None
    log.info("%s: analysing video for active speakers", rec.stem)
    tl = analyze_video(src, fps=cfg.video.fps, names=rec.participants or None,
                       width=info.width or 1920, height=info.height or 1080)
    if tl is None:
        return None
    rec.write_json(rec.speakers_video_path, tl.to_json())
    for name in sorted(tl.speakers, key=lambda n: -tl.total_seconds(n)):
        log.info("  %-28s %5.1f min", name, tl.total_seconds(name) / 60)
    return tl


# ---------------------------------------------------------------- resolve

def resolve_target(cfg: Config, target: str, *, allow_import: bool = True) -> Recording:
    """A stem, a sidecar/any recording file, or an ad-hoc media file (implicitly imported)."""
    p = Path(target)
    if p.exists() and p.is_file() and is_media_file(p) and not is_sidecar(p) and allow_import:
        try:
            return resolve_recording(p, cfg.out_dir)  # a _mix.wav etc. of an existing recording
        except RecordingError:
            return do_import(cfg, p)
    return resolve_recording(target, cfg.out_dir)


# ---------------------------------------------------------------- transcribe

def _ensure_mix(rec: Recording) -> Path:
    mix = rec.mix_path
    if mix and mix.exists():
        return mix
    tracks = [p for p in (rec.track_path("sys"), rec.track_path("mic")) if p and p.exists()]
    if not tracks:
        raise RecordingError(f"{rec.stem}: no audio (mix and tracks missing; audio purged?)")
    mix = rec.file("_mix.wav")
    log.info("%s: building mix from %d track(s)", rec.stem, len(tracks))
    mix_tracks(tracks, mix)
    rec.sidecar["mix"] = {"file": mix.name, "sample_rate": 16000, "channels": 1}
    rec.save_sidecar()
    return mix


def do_transcribe(cfg: Config, rec: Recording, *, force: bool = False, diarize: bool | None = None) -> Path:
    if rec.transcript_path.exists() and not force:
        log.info("%s: transcript exists, skipping (use --force)", rec.stem)
        return rec.transcript_path
    audio = _ensure_mix(rec)
    if audio.stat().st_size < 16000 * 2:  # under one second of 16 kHz PCM
        raise RecordingError(f"{rec.stem}: audio is empty ({audio.name}, {audio.stat().st_size} bytes)")
    ts = cfg.transcribe
    lang = rec.sidecar.get("language") or ts.language
    language = None if lang in ("auto", "", None) else lang
    prompt = build_prompt(rec, ts.glossary)

    timeline = None
    if rec.speakers_video_path.exists():
        timeline = VideoTimeline.from_json(rec.read_json(rec.speakers_video_path))
    want_diarize = ts.diarize if diarize is None else diarize

    provider = get_provider(ts.provider)
    log.info("%s: transcribing with %s (%s, %s, language=%s, diarize=%s)", rec.stem, provider.name, ts.model,
             ts.compute_type, language or "auto", want_diarize)
    res = provider.transcribe(audio, language=language, prompt=prompt, settings=ts, diarize=want_diarize)
    log.info("%s: %d segments, language %s, timings %s", rec.stem, len(res.segments), res.language, res.timings)

    if timeline:
        apply_video_timeline(res.segments, timeline)
    mic = rec.track_path("mic")
    speaker_sources = ["video"] if timeline else []
    if cfg.user_name and mic and mic.exists():
        if apply_mic_track(res.segments, mic, cfg.user_name):
            speaker_sources.append("mic")
    elif mic and mic.exists():
        log.info("%s: mic track present but [user] name is not set, your voice stays SPEAKER_xx", rec.stem)
    speaker_sources.append("diarization")

    transcript = {
        "format": 1,
        "provider": res.provider,
        "provider_version": res.provider_version,
        "model": res.model,
        "language": res.language,
        "created": utc_now_iso(),
        "prompt": prompt,
        "timings": res.timings,
        "speaker_sources": speaker_sources,
        "speakers": speaker_list(res.segments),
        "segments": [s.to_json() for s in res.segments],
    }
    rec.write_json(rec.transcript_path, transcript)
    return rec.transcript_path


# ---------------------------------------------------------------- export

def load_segments(rec: Recording) -> tuple[dict, list[Segment]]:
    data = rec.read_json(rec.transcript_path)
    segs = [Segment(start=s["start"], end=s["end"], text=s["text"], speaker=s.get("speaker"),
                    track=s.get("track", "mix"), language=s.get("language"),
                    words=[Word(w["start"], w["end"], w["word"], w.get("score")) for w in s.get("words", [])])
            for s in data["segments"]]
    return data, segs


def do_export(cfg: Config, rec: Recording, *, txt: bool = True, srt: bool = True) -> list[Path]:
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data, segs = load_segments(rec)
    if rec.speakers_path.exists():
        apply_manual_names(segs, rec.read_json(rec.speakers_path))
    header = {"start": rec.sidecar.get("start"), "duration": f"{rec.sidecar.get('duration_s', 0) // 60} min",
              "language": data.get("language"), "speakers": ", ".join(speaker_list(segs))}
    out = []
    if txt:
        out.append(rec.file(".txt"))
    if srt:
        out.append(rec.file(".srt"))
    write_exports(segs, rec.file(".txt") if txt else None, rec.file(".srt") if srt else None,
                  title=rec.title, header=header)
    return out


# ---------------------------------------------------------------- summarize

def _summary_input(rec: Recording):
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data, segs = load_segments(rec)
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    if names:
        apply_manual_names(segs, names)
    header = {"start": rec.sidecar.get("start"), "duration": f"{rec.sidecar.get('duration_s', 0) // 60} min",
              "language": data.get("language"), "participants": ", ".join(rec.participants) or None,
              "speakers": ", ".join(speaker_list(segs)),
              "speaker labels": ", ".join(f"{k} = {v}" for k, v in names.items()) or None}
    return segs, header


def do_summarize(cfg: Config, rec: Recording, *, force: bool = False) -> Path:
    from .summarize import summarize
    if rec.summary_path.exists() and not force:
        log.info("%s: summary exists, skipping (use --force)", rec.stem)
        return rec.summary_path
    segs, header = _summary_input(rec)
    text = summarize(rec, segs, header, cfg.summarize)
    rec.summary_path.write_text(text, encoding="utf-8")
    return rec.summary_path


def do_summarize_compare(cfg: Config, rec: Recording, *, force: bool = False) -> list[Path]:
    """Extra summaries with other providers/models (config `compare`), each to <stem>.summary.<model>.md."""
    from dataclasses import replace
    from .recording import slugify
    from .summarize import summarize
    out = []
    for spec in cfg.summarize.compare:
        provider, _, model = spec.partition(":")
        if not model:
            log.error("%s: compare entry %r must be provider:model", rec.stem, spec)
            continue
        path = rec.file(f".summary.{slugify(model)}.md")
        if path.exists() and not force:
            continue
        try:
            segs, header = _summary_input(rec)
            text = summarize(rec, segs, header, replace(cfg.summarize, provider=provider, model=model))
            path.write_text(text, encoding="utf-8")
            out.append(path)
        except Exception as e:
            log.error("%s: compare summary %s failed: %s", rec.stem, spec, e)
    return out


# ---------------------------------------------------------------- process

def do_process_inbox(cfg: Config) -> list[Recording]:
    inbox = cfg.inbox_dir
    if not inbox.exists():
        return []
    done = inbox / "done"
    imported = []
    for f in sorted(p for p in inbox.iterdir() if p.is_file() and is_media_file(p)):
        try:
            rec = do_import(cfg, f)
        except Exception as e:  # keep going with the other files
            log.error("import failed for %s: %s", f.name, e)
            continue
        done.mkdir(exist_ok=True)
        shutil.move(str(f), str(done / f.name))
        rec.sidecar["origin_path"] = str(done / f.name)
        rec.save_sidecar()
        imported.append(rec)
    return imported


def do_process(cfg: Config, rec: Recording, *, force: bool = False) -> None:
    do_transcribe(cfg, rec, force=force)
    do_export(cfg, rec)
    if cfg.summarize.enabled:
        try:
            do_summarize(cfg, rec, force=force)
        except Exception as e:  # summary is optional: no model, no API key, network, refusal
            log.error("%s: summary failed: %s", rec.stem, e)
        do_summarize_compare(cfg, rec, force=force)


def pending_recordings(cfg: Config) -> list[Recording]:
    """Recordings missing a transcript, or (when summaries are on) missing a summary."""
    out = []
    for r in iter_recordings(cfg.out_dir):
        if not r.transcript_path.exists() or (cfg.summarize.enabled and not r.summary_path.exists()):
            out.append(r)
    return out
