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
from .people import People
from .prompt import build_prompt
from .providers import get_provider
from .providers.base import Segment, Word
from .recording import (Recording, RecordingError, is_media_file, is_sidecar, iter_recordings, make_stem,
                        resolve_recording, slugify)
from .speakers import apply_manual_names, apply_video_timeline, speaker_list
from .video_speakers import VideoTimeline, analyze_video
from .voiceprints import Voiceprints, enroll_from_recording, remap_embeddings, speech_seconds

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


# ---------------------------------------------------------------- rename

def _fix_heading(path: Path, title: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines[:3]):
        if line.startswith("# "):
            lines[i] = f"# {title}"
            break
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def rename_recording(cfg: Config, rec: Recording, title: str) -> Recording:
    """Give the recording a new title: the folder and every `<stem>.*` file are renamed to the new stem
    (same date/time part, new slug), the sidecar, voice prints and summary headings follow. Returns the
    renamed recording (same object when only the title text changed)."""
    from .voiceprints import Voiceprints
    title = " ".join(title.split())
    if not title:
        raise RecordingError("empty title")
    start = datetime.fromisoformat(rec.sidecar["start"]) if rec.sidecar.get("start") else None
    new_stem = make_stem(start, title) if start else f"{rec.stem[:16]}_{slugify(title)}"
    old_stem, old_dir = rec.stem, rec.dir
    if new_stem == old_stem:
        if title != rec.title:
            rec.sidecar["title"] = title
            rec.save_sidecar()
            for md in [rec.summary_path, *old_dir.glob(f"{old_stem}.summary.*.md")]:
                if md.exists():
                    _fix_heading(md, title)
        return rec
    new_dir = old_dir.with_name(new_stem)
    if new_dir.exists():
        raise RecordingError(f"{new_dir.name}: a recording with this name already exists")
    old_dir.rename(new_dir)
    for f in sorted(new_dir.iterdir()):
        if f.name.startswith(old_stem):
            f.rename(f.with_name(new_stem + f.name[len(old_stem):]))
    sc = rec.sidecar
    sc["title"], sc["slug"] = title, new_stem.split("_", 2)[2]
    for t in (sc.get("tracks") or {}).values():
        if isinstance(t, dict) and str(t.get("file", "")).startswith(old_stem):
            t["file"] = new_stem + t["file"][len(old_stem):]
    if isinstance(sc.get("mix"), dict) and str(sc["mix"].get("file", "")).startswith(old_stem):
        sc["mix"]["file"] = new_stem + sc["mix"]["file"][len(old_stem):]
    if str(sc.get("origin_path", "")).replace("\\", "/").startswith(str(old_dir).replace("\\", "/")):
        sc["origin_path"] = str(new_dir / (new_stem + Path(sc["origin_path"]).name[len(old_stem):]))
    new = Recording(stem_path=new_dir / new_stem, sidecar=sc)
    new.save_sidecar()
    for md in [new.summary_path, *new_dir.glob(f"{new_stem}.summary.*.md")]:
        if md.exists():
            _fix_heading(md, title)
    vp = Voiceprints.load(cfg.out_dir)
    touched = False
    for prints in vp.people.values():
        for pr in prints:
            if pr.get("stem") == old_stem:
                pr["stem"] = new_stem
                touched = True
    if touched:
        vp.save()
    log.info("renamed %s -> %s", old_stem, new_stem)
    return new


# ---------------------------------------------------------------- remove noise "speakers"

def remove_speaker(cfg: Config, rec: Recording, label: str) -> int:
    """Drop every segment of one speaker label from the transcript (typing, mouse clicks, breathing that the
    ASR turned into invented sentences). Recorded in `removed_speakers`; `transcribe --force` brings it back.
    Exports are regenerated. Returns the number of removed segments."""
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data = rec.read_json(rec.transcript_path)
    if label == "UNKNOWN":  # segments the diarization left without a speaker
        keep = [s for s in data["segments"] if s.get("speaker") not in (None, "", "UNKNOWN")]
    else:
        keep = [s for s in data["segments"] if s.get("speaker") != label]
    n = len(data["segments"]) - len(keep)
    if not n:
        return 0
    data["segments"] = keep
    data["speakers"] = [s for s in data.get("speakers", []) if s != label]
    for key in ("speaker_embeddings", "voice_matches"):
        if isinstance(data.get(key), dict):
            data[key].pop(label, None)
    data.setdefault("removed_speakers", []).append({"label": label, "segments": n, "at": utc_now_iso()})
    rec.write_json(rec.transcript_path, data)
    if rec.speakers_path.exists():
        names = rec.read_json(rec.speakers_path)
        if label in names:
            del names[label]
            rec.write_json(rec.speakers_path, names)
    do_export(cfg, rec)
    log.info("%s: removed %d segments of %s", rec.stem, n, label)
    return n


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

    labels_before = [s.speaker for s in res.segments]
    if timeline:
        apply_video_timeline(res.segments, timeline)
        _register_video_names(cfg, res.segments)
    mic = rec.track_path("mic")
    speaker_sources = ["video"] if timeline else []
    mic_mapping: dict[str, str] = {}
    if cfg.user_name and mic and mic.exists():
        mic_mapping = apply_mic_track(res.segments, mic, cfg.user_name)
        if mic_mapping:
            speaker_sources.append("mic")
    elif mic and mic.exists():
        log.info("%s: mic track present but [user] name is not set, your voice stays SPEAKER_xx", rec.stem)
    embeddings = remap_embeddings(res.speaker_embeddings or {}, labels_before, [s.speaker for s in res.segments])
    durations = speech_seconds([{"start": s.start, "end": s.end, "speaker": s.speaker} for s in res.segments])
    voice_matches = _voiceprints_step(cfg, rec, embeddings, durations, mic_mapping, res.diarize_model)
    if voice_matches:
        speaker_sources.append("voiceprint")
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
        "diarize_model": res.diarize_model,
        "speaker_embeddings": embeddings,  # keyed by the final speaker names, unit vectors
        "voice_matches": voice_matches,
        "segments": [s.to_json() for s in res.segments],
    }
    rec.write_json(rec.transcript_path, transcript)
    return rec.transcript_path


def _register_video_names(cfg: Config, segments: list[Segment]) -> None:
    """Names read from the Teams video are real display names: make sure each is in the people registry
    (so they show up on the People tab and can collect voice prints)."""
    people = People.load(cfg.out_dir, cfg.people_display)
    before = len(people.people)
    for name in speaker_list(segments):
        if name and not name.startswith("SPEAKER_") and name != "UNKNOWN":
            people.ensure(name)
    if len(people.people) != before:
        people.save()
        log.info("people registry: %d new from the video", len(people.people) - before)


def _voiceprints_step(cfg: Config, rec: Recording, embeddings: dict[str, list[float]], durations: dict[str, float],
                      mic_mapping: dict[str, str], model: str) -> dict:
    """Name still-unknown labels by voice (writes speakers.json like a manual assignment) and store the user's
    own print from the mic track. Returns {label: {"person", "score"}} for the transcript / review page."""
    if not cfg.voiceprints.enabled or not embeddings:
        return {}
    vp = Voiceprints.load(cfg.out_dir)
    people = People.load(cfg.out_dir, cfg.people_display)
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    matches: dict = {}
    unknown = {lab: v for lab, v in embeddings.items() if lab.startswith("SPEAKER_") and not names.get(lab)}
    vs = cfg.voiceprints
    for label, (pid, score) in vp.recognize(unknown, vs.threshold, vs.margin, durations, vs.min_seconds).items():
        person = people.get(pid)
        if person is None:  # print of a person that was deleted from the registry
            continue
        names[label] = pid
        matches[label] = {"person": pid, "score": score}
        log.info("%s: %s recognised by voice as %s (%.2f)", rec.stem, label, person.full, score)
    if matches:
        rec.write_json(rec.speakers_path, names)
    changed = 0
    if mic_mapping and cfg.user_name:
        me = people.find(cfg.user_name)
        vec = embeddings.get(cfg.user_name)
        if me and vec and durations.get(cfg.user_name, 0.0) >= cfg.voiceprints.min_seconds:
            changed += vp.enroll(me.id, vec, rec.stem, cfg.user_name, model)
    # names assigned by hand before a re-transcription (or by voice just now) are worth a print too,
    # and so are names that came from the video / the mic and resolve to a registered person
    manual = {lab: pid for lab, pid in names.items() if lab not in matches and people.get(pid)}
    for key in embeddings:
        if key not in manual and not key.startswith("SPEAKER_") and key != cfg.user_name:
            p = people.find(key)
            if p:
                manual[key] = p.id
    changed += enroll_from_recording(vp, {"speaker_embeddings": embeddings, "diarize_model": model,
                                          "segments": [{"start": 0, "end": durations.get(lab, 0.0), "speaker": lab}
                                                       for lab in embeddings]},
                                     manual, rec.stem, cfg.voiceprints.min_seconds)
    if changed:
        vp.save()
    return matches


def recognize_voices(cfg: Config, rec: Recording) -> dict:
    """Compare the transcript's stored embeddings of still-unnamed labels with the voice prints collected since
    (no re-transcription). Matches go to speakers.json + voice_matches; exports are regenerated."""
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data = rec.read_json(rec.transcript_path)
    embeddings = data.get("speaker_embeddings") or {}
    if not embeddings:
        raise RecordingError(f"{rec.stem}: the transcript has no voice embeddings (transcribed before voice prints "
                             f"existed); run `transcribe --force`")
    durations = speech_seconds(data.get("segments") or [])
    matches = _voiceprints_step(cfg, rec, embeddings, durations, {}, data.get("diarize_model") or "")
    data["voice_matches"] = {**(data.get("voice_matches") or {}), **matches}
    if matches and "voiceprint" not in data.get("speaker_sources", []):
        data["speaker_sources"] = [s for s in data.get("speaker_sources", []) if s != "diarization"] + ["voiceprint", "diarization"]
    rec.write_json(rec.transcript_path, data)
    do_export(cfg, rec)
    return matches


def enroll_names(cfg: Config, rec: Recording, names: dict[str, str]) -> int:
    """Store voice prints for labels that just got a person (review page, label-speakers)."""
    if not cfg.voiceprints.enabled or not rec.transcript_path.exists():
        return 0
    vp = Voiceprints.load(cfg.out_dir)
    added = enroll_from_recording(vp, rec.read_json(rec.transcript_path), names, rec.stem, cfg.voiceprints.min_seconds)
    if added:
        vp.save()
    return added


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
    People.load(cfg.out_dir, cfg.people_display).apply(segs)
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

def _summary_input(cfg: Config, rec: Recording):
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data, segs = load_segments(rec)
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    if names:
        apply_manual_names(segs, names)
    people = People.load(cfg.out_dir, cfg.people_display)
    people.apply(segs)
    header = {"start": rec.sidecar.get("start"), "duration": f"{rec.sidecar.get('duration_s', 0) // 60} min",
              "language": data.get("language"), "participants": ", ".join(rec.participants) or None,
              "speakers": ", ".join(speaker_list(segs)),
              "speaker labels": ", ".join(f"{k} = {people.display(v)}" for k, v in names.items()) or None}
    return segs, header


def do_summarize(cfg: Config, rec: Recording, *, force: bool = False) -> Path:
    from .summarize import summarize
    if rec.summary_path.exists() and not force:
        log.info("%s: summary exists, skipping (use --force)", rec.stem)
        return rec.summary_path
    segs, header = _summary_input(cfg, rec)
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
            segs, header = _summary_input(cfg, rec)
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
