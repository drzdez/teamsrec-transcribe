"""The steps behind the CLI commands, working on Recording objects."""

from __future__ import annotations

import json
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
from .speakers import apply_manual_names, apply_video_fallback, apply_video_timeline, speaker_list
from .video_speakers import VideoTimeline, analyze_screen, analyze_video, merge_timelines
from .voiceprints import Voiceprints, enroll_from_recording, remap_embeddings, speech_seconds

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- import

def do_import(cfg: Config, src: Path, *, title: str | None = None, start: datetime | None = None,
              language: str | None = None, participants: list[str] | None = None,
              video: bool | None = None, force: bool = False) -> Recording:
    rec = import_file(src, cfg.out_dir, title=title, start=start, language=language,
                      participants=participants, force=force)
    if cfg.calendar_outlook and not rec.participants and rec.sidecar.get("start"):
        from .outlook import calendar_fields, meeting_at
        m = meeting_at(datetime.fromisoformat(rec.sidecar["start"]), rec.title)
        if m:
            rec.sidecar.update(calendar_fields(m))
            rec.sidecar.setdefault("title_source", rec.sidecar.get("metadata_source", "file"))
            rec.save_sidecar()
            log.info("%s: Outlook: '%s' (by %s), %d participants", rec.stem, m.get("subject"), m.get("match"),
                     len(m.get("attendees", [])))
    want_video = cfg.video.enabled if video is None else video
    if want_video and (force or not rec.speakers_video_path.exists()):
        do_video(cfg, rec)
    return rec


def keep_video_names(found: dict, candidates) -> dict:
    """Which names read from a live Teams window are worth keeping. With a list of participants only those
    that matched one of them (OCR of a live tile invents people); without one, anything name-shaped."""
    cands = set(candidates)
    return {n: iv for n, iv in found.items() if n in cands or (not cands and _looks_like_a_name(n))}


def _screen_names(cfg: Config, rec: Recording) -> list[str]:
    """Candidate names for the label OCR of a live recording: everyone in the registry plus participants."""
    people = People.load(cfg.out_dir, cfg.people_display)
    names = [p.full for p in people.people] + [a for p in people.people for a in p.aliases] + list(rec.participants)
    return sorted({n for n in names if n})


TEAMS_NAV_HEADS = {"activity", "chat", "teams", "calendar", "calls", "files", "apps", "copilot", "onedrive", "meet",
                   "viva", "planner", "aktivita", "týmy", "kalendář", "hovory", "soubory", "aplikace"}


def is_meeting_screen(titles: list[str]) -> bool:
    """The Teams main window (Calendar, Chat, ...) is captured too; its purple event boxes look like name labels.
    Only windows that never carried a navigation-section title are meeting windows worth analysing."""
    if not titles:
        return True
    for t in titles:
        head = t.split("|")[0].strip().lower()
        if head in TEAMS_NAV_HEADS:
            return False
    return True


def do_video_screens(cfg: Config, rec: Recording) -> VideoTimeline | None:
    """Live recording: the capture app saved every Teams window as <stem>_screen<N>.mp4 (sidecar `screens`).
    Each meeting window is analysed like a Teams recording video; the timelines are shifted by their start
    offset and merged. When the meeting has participants (calendar, registry), only names that matched one of
    them count: a live window is noisy and OCR invents people ("Miroslav Bystriansky" came out as "Miory
    Baotnbnsc" and collected 48 minutes under a name nobody could place). An anonymous SPEAKER_XX that voice
    prints or the user can name is worth more than a made-up one. Without participants (imported recordings)
    anything name-shaped is kept, as before."""
    screens = rec.sidecar.get("screens") or []
    parts = []
    candidates = _screen_names(cfg, rec)
    names = candidates or None
    for sc in screens:
        path = rec.dir / sc["file"]
        if not path.exists():
            continue
        if not is_meeting_screen(sc.get("titles") or []):
            log.info("%s: %s is the Teams main window (%s), skipped", rec.stem, sc["file"], (sc.get("titles") or ["?"])[0])
            continue
        log.info("%s: analysing %s (%s)", rec.stem, sc["file"], "; ".join(sc.get("titles", [])[:2]))
        tl = analyze_screen(path, fps=cfg.video.fps, names=names, width=sc.get("width", 1600), height=sc.get("height", 900))
        if tl is not None:
            kept = keep_video_names(tl.speakers, candidates)
            dropped = sorted(set(tl.speakers) - set(kept))
            if dropped:
                log.info("%s: dropped OCR names that match no participant: %s", rec.stem, dropped)
            tl.speakers = kept
            if kept:
                parts.append(tl.shifted(float(sc.get("start_offset_s", 0.0))))
    tl = merge_timelines(parts)
    if tl is None:
        log.info("%s: no name labels found in the captured Teams windows", rec.stem)
        if rec.speakers_video_path.exists():
            rec.speakers_video_path.unlink()  # a stale timeline from an earlier analysis must not be applied
        return None
    rec.write_json(rec.speakers_video_path, tl.to_json())
    for name in sorted(tl.speakers, key=lambda n: -tl.total_seconds(n)):
        log.info("  %-28s %5.1f min", name, tl.total_seconds(name) / 60)
    return tl


def do_video(cfg: Config, rec: Recording) -> VideoTimeline | None:
    if rec.sidecar.get("screens"):
        return do_video_screens(cfg, rec)
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
    new_stem = make_stem(start, title) if start else f"{rec.stem[:15]}_{slugify(title)}"
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
    for scr in sc.get("screens") or []:
        if isinstance(scr, dict) and str(scr.get("file", "")).startswith(old_stem):
            scr["file"] = new_stem + scr["file"][len(old_stem):]
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

def same_person_groups(rec: Recording, people: People) -> dict[str, list[str]]:
    """{person id: labels of this recording that resolve to them}, only where there is more than one label.
    Diarization splits a person over two labels often enough (a second microphone, a long meeting, somebody
    joining twice), and after naming them the page shows two cards for one person."""
    if not rec.transcript_path.exists():
        return {}
    data = rec.read_json(rec.transcript_path)
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    labels = [s for s in (data.get("speakers") or []) if s]
    if not labels:
        labels = sorted({s.get("speaker") for s in data.get("segments") or [] if s.get("speaker")})
    groups: dict[str, list[str]] = {}
    for label in labels:
        value = names.get(label) or ("" if label.startswith("SPEAKER_") or label == "UNKNOWN" else label)
        if not value:
            continue
        person = people.get(value) or people.find(value)
        groups.setdefault(person.id if person else value.strip().casefold(), []).append(label)
    return {pid: ls for pid, ls in groups.items() if len(ls) > 1}


def merge_same_person(cfg: Config, rec: Recording) -> dict:
    """Fold every group of labels that belongs to one person into a single speaker. Their embeddings are kept
    as voice prints first - the same voice recorded under different conditions is what makes later recognition
    work - and only then do the segments move to the label that spoke the most.

    The voices are not second-guessed here: one person can be clean on the microphone track and muffled on
    the loopback, which is exactly the case the embeddings get wrong and the user hears in one second. Every
    moved segment remembers where it came from, so `unmerge_speakers` puts it back without a new transcript."""
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    people = People.load(cfg.out_dir, cfg.people_display)
    groups = same_person_groups(rec, people)
    if not groups:
        return {"groups": [], "merged": 0, "prints": 0}
    data = rec.read_json(rec.transcript_path)
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    segments = data.get("segments") or []
    secs = speech_seconds(segments)
    emb = data.get("speaker_embeddings") or {}
    vp = Voiceprints.load(cfg.out_dir) if cfg.voiceprints.enabled else None
    prints = 0
    done = []
    for pid, labels in groups.items():
        keep = max(labels, key=lambda l: (secs.get(l, 0.0), l))
        gone = [l for l in labels if l != keep]
        if vp is not None and people.get(pid):  # an unregistered literal name has nowhere to store prints
            for label in [keep, *gone]:
                vec = emb.get(label)
                if vec and secs.get(label, 0.0) >= cfg.voiceprints.min_seconds:
                    prints += vp.enroll(pid, vec, rec.stem, label, data.get("diarize_model") or "")
        for s in segments:
            if s.get("speaker") in gone:
                s["merged_from"] = s["speaker"]  # so the merge can be undone without transcribing again
                s["speaker"] = keep
        for label in gone:
            emb.pop(label, None)
            if isinstance(data.get("voice_matches"), dict):
                data["voice_matches"].pop(label, None)
            names.pop(label, None)
        if people.get(pid):  # a registered person keeps the mapping; an unregistered literal name has none
            names[keep] = pid
        data["speakers"] = [l for l in (data.get("speakers") or []) if l not in gone]
        done.append({"person": pid, "kept": keep, "merged": gone,
                     "seconds": round(sum(secs.get(l, 0.0) for l in [keep, *gone]), 1)})
        log.info("%s: %s merged into %s (%s)", rec.stem, ", ".join(gone), keep, pid)
    data.setdefault("merged_speakers", []).extend([dict(g, at=utc_now_iso()) for g in done])
    rec.write_json(rec.transcript_path, data)
    rec.write_json(rec.speakers_path, names)
    if vp is not None and prints:
        vp.save()
    do_export(cfg, rec)
    return {"groups": done, "merged": sum(len(g["merged"]) for g in done), "prints": prints}


def unmerge_speakers(cfg: Config, rec: Recording) -> int:
    """Undo `merge_same_person`: every segment that remembers a `merged_from` label goes back to it, and the
    labels get their entry in speakers.json again (the same person as the label they were merged into). The
    embeddings of the restored labels are gone - they live on as voice prints - so recognition keeps working
    from the label that stayed. Returns the number of restored segments."""
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data = rec.read_json(rec.transcript_path)
    merged = data.get("merged_speakers") or []
    back = 0
    restored: set[str] = set()
    for s in data.get("segments") or []:
        origin = s.pop("merged_from", None)
        if origin:
            s["speaker"] = origin
            restored.add(origin)
            back += 1
    if not back:
        raise RecordingError(f"{rec.stem}: nothing to undo"
                             + (" (the merge was made before this was recorded, use transcribe --force)"
                                if merged else ""))
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    for group in merged:
        for label in group.get("merged", []):
            if label in restored and group.get("person"):
                names[label] = group["person"]
    data["speakers"] = sorted({s.get("speaker") for s in data["segments"] if s.get("speaker")})
    data["merged_speakers"] = [g for g in merged if not set(g.get("merged", [])) & restored]
    rec.write_json(rec.transcript_path, data)
    rec.write_json(rec.speakers_path, names)
    do_export(cfg, rec)
    log.info("%s: merge undone, %d segments back on %s", rec.stem, back, ", ".join(sorted(restored)))
    return back


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


# ---------------------------------------------------------------- meeting <-> calendar link (review page)

def meeting_info(cfg: Config, rec: Recording) -> dict:
    """What the page shows next to the speakers: where the title and the participants came from, the calendar
    link and its status, so the user can confirm, detach or pick another meeting."""
    sc = rec.sidecar
    cal = sc.get("calendar")
    participants = [{"name": p.get("name"), "source": p.get("source", "manual")} for p in sc.get("participants", []) if p.get("name")]
    return {"title": rec.title, "title_source": sc.get("title_source") or sc.get("metadata_source") or "unknown",
            "calendar": cal, "participants": participants, "outlook_enabled": cfg.calendar_outlook,
            "start": sc.get("start")}


def set_meeting_link(cfg: Config, rec: Recording, action: str, candidate: dict | None = None) -> Recording:
    """confirm: keep the calendar link and mark it confirmed. detach: drop the calendar link and the participants
    that came from it (the title stays, edit it separately). attach: link the given Outlook item instead (title,
    participants, folder name follow)."""
    from .outlook import calendar_fields, candidates_for
    sc = rec.sidecar
    if action == "confirm":
        if sc.get("calendar"):
            sc["calendar"]["status"] = "confirmed"
        rec.save_sidecar()
        return rec
    if action == "detach":
        sc.pop("calendar", None)
        sc["participants"] = [p for p in sc.get("participants", []) if p.get("source") not in (None, "calendar")]
        if sc.get("title_source") == "calendar":
            sc["title_source"] = "manual"
        rec.save_sidecar()
        return rec
    if action == "attach":
        if not candidate or not sc.get("start"):
            raise RecordingError("attach needs a candidate meeting")
        found = None
        for c in candidates_for(datetime.fromisoformat(sc["start"]), window_s=4 * 3600):
            if c["subject"] == candidate.get("subject") and c["start"] == candidate.get("start"):
                found = c
                break
        if found is None:
            raise RecordingError("that meeting is no longer in the calendar")
        found["match"] = "manual"
        sc.update(calendar_fields(found, status="confirmed"))
        sc["title_source"] = "calendar"
        rec.save_sidecar()
        return rename_recording(cfg, rec, found["subject"])
    raise RecordingError(f"unknown action {action!r}")


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
    if rec.sidecar.get("audio_silent"):  # the capture app heard nothing at all: headset asleep, device taken
        raise RecordingError(f"{rec.stem}: the recording has no audible audio (audio_silent), nothing to transcribe")
    audio = _ensure_mix(rec)
    if audio.stat().st_size < 16000 * 2:  # under one second of 16 kHz PCM
        raise RecordingError(f"{rec.stem}: audio is empty ({audio.name}, {audio.stat().st_size} bytes)")
    ts = cfg.transcribe
    lang = rec.sidecar.get("language") or ts.language
    language = None if lang in ("auto", "", None) else lang
    prompt = build_prompt(rec, ts.glossary)

    timeline = None
    if not rec.speakers_video_path.exists() and rec.sidecar.get("screens") and cfg.video.enabled:
        try:
            do_video_screens(cfg, rec)  # live recording with captured Teams windows
        except Exception as e:  # never block the transcript on the video step
            log.error("%s: screen analysis failed: %s", rec.stem, e)
    if rec.speakers_video_path.exists():
        timeline = VideoTimeline.from_json(rec.read_json(rec.speakers_video_path))
    want_diarize = ts.diarize if diarize is None else diarize

    provider = get_provider(ts.provider)
    log.info("%s: transcribing with %s (%s, %s, language=%s, diarize=%s)", rec.stem, provider.name, ts.model,
             ts.compute_type, language or "auto", want_diarize)
    res = provider.transcribe(audio, language=language, prompt=prompt, settings=ts, diarize=want_diarize)
    log.info("%s: %d segments, language %s, timings %s", rec.stem, len(res.segments), res.language, res.timings)

    labels_before = [s.speaker for s in res.segments]
    mic = rec.track_path("mic")
    speaker_sources = []
    mic_mapping: dict[str, str] = {}
    if rec.source == "onsite":
        mic = None  # the room microphone carries everybody; only voice prints and diarization can tell them apart
        log.info("%s: on-site recording, speakers from voice prints / diarization only", rec.stem)
    # The microphone goes first: it is the user's own hardware, while Teams never draws the speaking outline
    # around the local user's own tile, so the video keeps the previous speaker highlighted while the user
    # talks (2026-09-24: 48 minutes of the user landed on the participant highlighted before him).
    if cfg.user_name and mic and mic.exists():
        mic_mapping = apply_mic_track(res.segments, mic, cfg.user_name)
        if mic_mapping:
            speaker_sources.append("mic")
    elif mic and mic.exists():
        log.info("%s: mic track present but [user] name is not set, your voice stays SPEAKER_xx", rec.stem)
    if timeline:
        speaker_sources.append("video")
        apply_video_timeline(res.segments, timeline, fallback=False, keep_named=True)  # what the highlight covers
        apply_video_fallback(res.segments, timeline, labels_before)  # whole labels the video attributes clearly
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


def _looks_like_a_name(text: str) -> bool:
    """OCR of a live window is noisier than a Teams recording: only register 'First Last'-shaped strings."""
    import re
    return bool(re.fullmatch(r"[^\W\d_](?:[^\W\d_]|['.-])+(?: [^\W\d_](?:[^\W\d_]|['.-])+){1,3}", text))


def _voiceprints_step(cfg: Config, rec: Recording, embeddings: dict[str, list[float]], durations: dict[str, float],
                      mic_mapping: dict[str, str], model: str) -> dict:
    """Name still-unknown labels by voice, using the prints collected so far. Nothing is stored here: a print
    only goes into the shared registry when a person confirms the name (the review page or `label-speakers`),
    otherwise a wrong guess would teach the next recording the same mistake. The embeddings of this recording
    stay in its own transcript, so confirming later still works. Returns {label: {"person", "score"}}."""
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
    cal = rec.sidecar.get("calendar") or {}
    header = {"start": rec.sidecar.get("start"), "duration": f"{rec.sidecar.get('duration_s', 0) // 60} min",
              "language": data.get("language"), "participants": ", ".join(rec.participants) or None,
              "organizer": cal.get("organizer") or None,
              "scheduled": f"{cal['start']} to {cal['end']} (calendar: {cal.get('subject')})" if cal.get("start") else None,
              "speakers": ", ".join(speaker_list(segs)),
              "speaker labels": ", ".join(f"{k} = {people.display(v)}" for k, v in names.items()) or None}
    return segs, header


MIN_SUMMARY_WORDS = 40  # a transcript with fewer words is a failed recording, not a meeting


def do_summarize(cfg: Config, rec: Recording, *, force: bool = False) -> Path:
    from .summarize import summarize
    if rec.summary_path.exists() and not force:
        log.info("%s: summary exists, skipping (use --force)", rec.stem)
        return rec.summary_path
    segs, header = _summary_input(cfg, rec)
    words = sum(len(s.text.split()) for s in segs)
    if words < MIN_SUMMARY_WORDS:
        raise RecordingError(f"{rec.stem}: only {words} words transcribed, nothing to summarize (no audio recorded?)")
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
            if sum(len(s.text.split()) for s in segs) < MIN_SUMMARY_WORDS:
                log.info("%s: too few words for a %s summary", rec.stem, spec)
                continue
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


def reset_names(rec: Recording) -> dict:
    """Forget the manual label -> person assignment of one recording (the labels change with a new transcript,
    and a wrong assignment must not survive the re-run). The old mapping goes to the log, nowhere else."""
    old = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    if old:
        log.info("%s: dropping the manual speaker names before a fresh transcript: %s", rec.stem, old)
        rec.speakers_path.unlink()
    return old


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
