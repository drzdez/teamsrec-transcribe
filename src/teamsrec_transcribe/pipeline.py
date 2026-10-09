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
from .timings import step
from .prompt import build_prompt
from .providers import get_provider
from .providers.base import Segment, Word
from .recording import (Recording, RecordingError, is_media_file, is_sidecar, iter_recordings, make_stem,
                        resolve_recording, slugify)
from .speakers import apply_manual_names, speaker_list, video_label_mapping
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
    them count: a live window is noisy and OCR invents people ("Michal Bartoš" came out as "Mihoy
    Bardtnbnsc" and collected 48 minutes under a name nobody could place). An anonymous SPEAKER_XX that voice
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
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
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
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints) if cfg.voiceprints.enabled else None
    prints = 0
    done = []
    for pid, labels in groups.items():
        keep = max(labels, key=lambda l: (secs.get(l, 0.0), l))
        gone = [l for l in labels if l != keep]
        person = people.get(pid)
        if vp is not None and person and person.voiceprint:  # unregistered names / opted-out people: no prints
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


def do_transcribe_compare(cfg: Config, rec: Recording, provider_name: str) -> Path:
    """Transcribe with another provider into side files - <stem>.transcript.<provider>.json and
    <stem>.<provider>.txt - to compare it with the main transcript, which stays untouched. The user is named
    from the microphone track like in a real run, so the texts read alike."""
    from dataclasses import replace
    from .export import write_exports
    audio = _ensure_mix(rec)
    ts = replace(cfg.transcribe, provider=provider_name)
    lang = rec.sidecar.get("language") or ts.language
    language = None if lang in ("auto", "", None) else lang
    provider = get_provider(provider_name)
    log.info("%s: comparison transcript with %s", rec.stem, provider_name)
    res = provider.transcribe(audio, language=language, prompt=build_prompt(rec, ts.glossary), settings=ts,
                              diarize=True)
    mic = rec.track_path("mic")
    named = {}
    if cfg.user_name and mic and mic.exists() and rec.source != "onsite":
        named = apply_mic_track(res.segments, mic, cfg.user_name)
    out = rec.file(f".transcript.{provider_name}.json")
    rec.write_json(out, {"format": 1, "provider": res.provider, "provider_version": res.provider_version,
                         "model": res.model, "language": res.language, "created": utc_now_iso(),
                         "timings": res.timings, "comparison": True,
                         "languages": sorted({s.language or res.language for s in res.segments} | {res.language}),
                         "speaker_sources": (["mic"] if named else []) + ["diarization"],
                         "speakers": speaker_list(res.segments), "segments": [s.to_json() for s in res.segments]})
    header = {"start": rec.sidecar.get("start"), "duration": f"{rec.sidecar.get('duration_s', 0) // 60} min",
              "language": res.language, "provider": f"{res.provider} {res.model}",
              "speakers": ", ".join(speaker_list(res.segments))}
    write_exports(res.segments, rec.file(f".{provider_name}.txt"), None, title=f"{rec.title} ({provider_name})",
                  header=header)
    return out


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


UNASSIGNED = (None, "", "UNKNOWN")  # a reply the diarization gave nobody (no turn overlapped it)


NEW_SPEAKER = "@new"  # a move target: one new speaker for the whole request (a voice the diarization missed)


def assign_segments(cfg: Config, rec: Recording, moves: list[dict]) -> int:
    """Move replies to another speaker, one by one (review page): moves = [{"start": s, "speaker": label,
    "from"?: label}]. Without `from` the reply is an unassigned one (the "Nepřiřazeno" card); with it, a reply of that
    speaker (a group that mixes two voices). The target is one of this transcript's speakers, NEW_SPEAKER (a new
    SPEAKER_NN, the same one for every such move of the request) or "UNKNOWN" (back to unassigned). Marked
    `assigned: manual`; exports are regenerated. Returns how many replies moved."""
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data = rec.read_json(rec.transcript_path)
    speakers = list(data.get("speakers", []))
    labels = {lab for lab in speakers if lab not in UNASSIGNED}
    new_label = ""
    n = 0
    for m in moves:
        label, start = str(m.get("speaker") or ""), float(m.get("start", -1))
        source = m.get("from")
        if label == NEW_SPEAKER:
            if not new_label:
                taken = {s.get("speaker") for s in data["segments"]} | set(speakers)
                new_label = next(f"SPEAKER_{i:02d}" for i in range(100) if f"SPEAKER_{i:02d}" not in taken)
                speakers.append(new_label)
            label = new_label
        elif label not in labels and label != "UNKNOWN":
            raise RecordingError(f"{rec.stem}: no speaker {label!r} in this recording")
        for s in data["segments"]:
            here = s.get("speaker") in UNASSIGNED if source in (None, "", "UNKNOWN") else s.get("speaker") == source
            if here and abs(float(s["start"]) - start) < 0.01:
                if label == "UNKNOWN":
                    s["speaker"] = None
                    s.pop("assigned", None)
                    if "UNKNOWN" not in speakers:
                        speakers.append("UNKNOWN")
                else:
                    s["speaker"], s["assigned"] = label, "manual"
                n += 1
                break
    if n:
        present = {s.get("speaker") for s in data["segments"]}
        unassigned_left = any(s.get("speaker") in UNASSIGNED for s in data["segments"])
        emptied = {str(m.get("from")) for m in moves if m.get("from")} - present  # every reply moved away
        data["speakers"] = [lab for lab in speakers if lab not in emptied
                            and (lab not in UNASSIGNED or unassigned_left)]
        rec.write_json(rec.transcript_path, data)
        do_export(cfg, rec)
        log.info("%s: %d replies moved to another speaker", rec.stem, n)
    return n


def speaker_replies(rec: Recording, label: str) -> list[dict]:
    """All replies of one speaker, for going through them on the page (play, read, move)."""
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data = rec.read_json(rec.transcript_path)
    want = (lambda s: s.get("speaker") in UNASSIGNED) if label in UNASSIGNED else (lambda s: s.get("speaker") == label)
    return [{"start": round(float(s["start"]), 3), "end": round(min(float(s["end"]), float(s["start"]) + 20.0), 3),
             "at": f"{int(s['start']) // 3600:02d}:{int(s['start']) % 3600 // 60:02d}:{int(s['start']) % 60:02d}",
             "text": (s.get("text") or "")[:300], "manual": s.get("assigned") == "manual"}
            for s in data.get("segments") or [] if want(s)]


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
        purged = rec.sidecar.get("audio_purged")
        raise RecordingError(f"{rec.stem}: no audio" + (f" (deleted by purge-audio on {purged}; the texts are kept)"
                                                        if purged else " (mix and tracks missing)"))
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
            with step("analýza oken Teams"):
                do_video_screens(cfg, rec)  # live recording with captured Teams windows
        except Exception as e:  # never block the transcript on the video step
            log.error("%s: screen analysis failed: %s", rec.stem, e)
    if rec.speakers_video_path.exists():
        timeline = VideoTimeline.from_json(rec.read_json(rec.speakers_video_path))
    want_diarize = ts.diarize if diarize is None else diarize

    provider = get_provider(ts.provider)
    log.info("%s: transcribing with %s (%s, %s, language=%s, diarize=%s)", rec.stem, provider.name, ts.model,
             ts.compute_type, language or "auto", want_diarize)
    parts: dict = {}
    with step(f"přepis ({provider.name})", parts):
        res = provider.transcribe(audio, language=language, prompt=prompt, settings=ts, diarize=want_diarize)
        parts.update(res.timings or {})  # the provider's own phases: model, ASR, alignment, diarization, ...
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
        with step("mluvčí z mikrofonu"):
            mic_mapping = apply_mic_track(res.segments, mic, cfg.user_name)
        if mic_mapping:
            speaker_sources.append("mic")
    elif mic and mic.exists():
        log.info("%s: mic track present but [user] name is not set, your voice stays SPEAKER_xx", rec.stem)
    # Voices before the video, and the video names whole voice groups only (2026-10-05): naming single replies
    # by the highlighted tile made mixed groups – a live window keeps the previous speaker highlighted, so a
    # "Peter" group collected replies of whoever was highlighted, next to the clean voice group that was Peter.
    # Better more groups that are each one voice (merged later on the page) than one group of several voices.
    embeddings = remap_embeddings(res.speaker_embeddings or {}, labels_before, [s.speaker for s in res.segments])
    durations = speech_seconds([{"start": s.start, "end": s.end, "speaker": s.speaker} for s in res.segments])
    with step("poznání po hlase"):
        voice_matches = _voiceprints_step(cfg, rec, embeddings, durations, mic_mapping, res.diarize_model)
    call = _direct_call_step(cfg, rec, durations, voice_matches)
    if voice_matches:
        speaker_sources.append("voiceprint")
    if call and call.get("applied"):
        speaker_sources.append("call")
    if timeline:
        held = {**voice_matches, **({call["label"]: {"person": call.get("person", "")}} if call else {})}
        if _video_names_whole_groups(cfg, rec, res.segments, timeline, held):
            speaker_sources.append("video")
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
        "languages": sorted({s.language or res.language for s in res.segments} | {res.language}),
        "speaker_embeddings": embeddings,  # keyed by the final speaker names, unit vectors
        "voice_matches": voice_matches,
        **({"direct_call": call} if call else {}),
        "segments": [s.to_json() for s in res.segments],
    }
    rec.write_json(rec.transcript_path, transcript)
    return rec.transcript_path


def _looks_like_a_name(text: str) -> bool:
    """OCR of a live window is noisier than a Teams recording: only register 'First Last'-shaped strings."""
    import re
    return bool(re.fullmatch(r"[^\W\d_](?:[^\W\d_]|['.-])+(?: [^\W\d_](?:[^\W\d_]|['.-])+){1,3}", text))


def _video_names_whole_groups(cfg: Config, rec: Recording, segments: list, timeline, voice_matches: dict) -> dict:
    """The video names whole voice groups it clearly attributes (video_label_mapping), never single replies, and
    only groups that neither the microphone nor a voice print named. A person the voice already found in another
    group is not given a second group: the voice is the stronger evidence (the page offers merging the groups).
    Returns the mapping applied."""
    mapping = video_label_mapping(segments, timeline)
    people = People.load(cfg.out_dir, cfg.people_display)
    voiced = {m["person"] for m in voice_matches.values()}
    applied: dict[str, str] = {}
    for label, name in mapping.items():
        if label in voice_matches:
            continue
        person = people.find(name)
        if person is not None and person.id in voiced:
            log.info("%s: video says %s is %s, but the voice found %s in another group – left as %s", rec.stem,
                     label, name, name, label)
            continue
        applied[label] = name
    n = 0
    for seg in segments:
        if seg.speaker in applied:
            seg.speaker = applied[seg.speaker]
            n += 1
    if applied:
        log.info("%s: speakers from video (whole groups): %s, %d replies", rec.stem,
                 ", ".join(f"{k}->{v}" for k, v in applied.items()), n)
    return applied


def _voiceprints_step(cfg: Config, rec: Recording, embeddings: dict[str, list[float]], durations: dict[str, float],
                      mic_mapping: dict[str, str], model: str) -> dict:
    """Name still-unknown labels by voice, using the prints collected so far. Nothing is stored here: a print
    only goes into the shared registry when a person confirms the name (the review page or `label-speakers`),
    otherwise a wrong guess would teach the next recording the same mistake. The embeddings of this recording
    stay in its own transcript, so confirming later still works. Returns {label: {"person", "score"}}."""
    if not cfg.voiceprints.enabled or not embeddings:
        return {}
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    people = People.load(cfg.out_dir, cfg.people_display)
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    matches: dict = {}
    unknown = {lab: v for lab, v in embeddings.items() if lab.startswith("SPEAKER_") and not names.get(lab)}
    rejected = (rec.read_json(rec.transcript_path).get("voice_rejected") or {}) if rec.transcript_path.exists() else {}
    vs = cfg.voiceprints
    for label, (pid, score) in vp.recognize(unknown, vs.threshold, vs.margin, durations, vs.min_seconds,
                                            exclude_stem=rec.stem).items():
        person = people.get(pid)
        if person is None:  # print of a person that was deleted from the registry
            continue
        if pid in rejected.get(label, []):  # the user said "not them" for this group (Lidé → ke kontrole)
            continue
        names[label] = pid
        matches[label] = {"person": pid, "score": score}
        log.info("%s: %s recognised by voice as %s (%.2f)", rec.stem, label, person.full, score)
    if matches:
        rec.write_json(rec.speakers_path, names)
    return matches


MEETING_VIEWS = {"kompaktní zobrazení schůzky", "meeting compact view", "compact view"}


def direct_call_name(cfg: Config, rec: Recording) -> str:
    """The other person's name when the recording is a direct (1:1) Teams call: the title was read from the call
    window ("Jana Nováková | Microsoft Teams"), it is shaped like a name, it is not the user, and no calendar meeting
    is linked. "" otherwise."""
    sc = rec.sidecar
    title = (rec.title or "").strip()
    if sc.get("title_source") != "window" or sc.get("calendar") or not _looks_like_a_name(title):
        return ""
    # a person's name has every word capitalised; a meeting subject often not ("Archi standup", 2026-10-07)
    if not all(w[:1].isupper() for w in title.split()):
        return ""
    # the meeting's own windows ("Kompaktní zobrazení schůzky | Archi standup | …") or its calendar entry in Teams
    # ("Calendar | Archi standup | …") name a meeting, not a person
    for seen in sc.get("teams_windows_seen") or []:
        parts = [p.strip() for p in seen.split("|")]
        if len(parts) >= 3 and parts[1] == title and parts[0].lower() in MEETING_VIEWS:
            return ""
    if cfg.user_name and _norm_name(title) == _norm_name(cfg.user_name):
        return ""
    return title


def _norm_name(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).casefold().strip()


def _direct_call_step(cfg: Config, rec: Recording, durations: dict[str, float], voice_matches: dict) -> dict | None:
    """A direct call has one other person, and the call window names them. When exactly one unnamed voice group
    spoke long enough (UNNAMED_MIN_SECONDS), that group is the caller:
    - the voice agrees, or found nobody: the group gets the caller's name (a known person: speakers.json, not
      confirmed yet – the print is stored only when the user saves);
    - the voice says someone else: neither name is applied, the page asks (two people with similar voices, or a
      wrong print – 2026-10-06: a call with one colleague recognised as another at 0.83);
    - the caller is not a known person yet: the page offers the name.
    Changes speakers.json and `voice_matches` in place; returns what the page shows ({"label", "name", "person",
    "applied"?, "voice"?}) or None when this is not a direct call."""
    name = direct_call_name(cfg, rec)
    if not name:
        return None
    big = [lab for lab, secs in durations.items() if lab.startswith("SPEAKER_") and secs >= UNNAMED_MIN_SECONDS]
    if len(big) != 1:
        log.info("%s: direct call with %s, but %d long unnamed voice groups – not named by the call", rec.stem,
                 name, len(big))
        return None
    label = big[0]
    person = People.load(cfg.out_dir, cfg.people_display).find(name)
    info: dict = {"label": label, "name": person.full if person else name, "person": person.id if person else ""}
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    voice = voice_matches.get(label)
    if voice and person and voice["person"] == person.id:
        return info  # the voice already says the same
    if voice:
        info["voice"] = voice
        voice_matches.pop(label, None)
        if names.get(label) == voice["person"]:
            names.pop(label)
            rec.write_json(rec.speakers_path, names)
        log.info("%s: direct call with %s, but the voice of %s resembles %s (%.2f) – left for the user", rec.stem,
                 name, label, voice["person"], voice["score"])
        return info
    if person and not names.get(label):
        names[label] = person.id
        rec.write_json(rec.speakers_path, names)
        info["applied"] = True
        log.info("%s: %s named by the direct call: %s", rec.stem, label, person.full)
    return info


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
    known = {**(data.get("voice_matches") or {}), **matches}
    call = _direct_call_step(cfg, rec, durations, known)  # may take back a voice match that contradicts the call
    if call:
        data["direct_call"] = call
    else:
        data.pop("direct_call", None)  # no longer a direct call (an older rule took a meeting for one)
    matches = {lab: m for lab, m in matches.items() if lab in known}
    data["voice_matches"] = known
    if matches and "voiceprint" not in data.get("speaker_sources", []):
        data["speaker_sources"] = [s for s in data.get("speaker_sources", []) if s != "diarization"] + ["voiceprint", "diarization"]
    rec.write_json(rec.transcript_path, data)
    do_export(cfg, rec)
    return matches


def enroll_names(cfg: Config, rec: Recording, names: dict[str, str]) -> int:
    """Store voice prints for labels that just got a person (review page, label-speakers)."""
    if not cfg.voiceprints.enabled or not rec.transcript_path.exists():
        return 0
    opted_out = People.load(cfg.out_dir, cfg.people_display).no_voiceprint()
    names = {lab: pid for lab, pid in names.items() if pid not in opted_out}
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
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


SUMMARY_PROVIDERS = ("ollama", "anthropic", "openai")


def summary_path_for(cfg: Config, rec: Recording, provider: str, model: str) -> Path:
    """The configured provider/model writes the main <stem>.summary.md, any other <stem>.summary.<model>.md."""
    from .recording import slugify
    if (provider, model) == (cfg.summarize.provider, cfg.summarize.model):
        return rec.summary_path
    return rec.file(f".summary.{slugify(model)}.md")


def summarize_as(cfg: Config, rec: Recording, provider: str, model: str) -> Path:
    """One summary with the given provider/model (the review page's "generate with this model")."""
    from dataclasses import replace
    from .summarize import summarize
    if provider not in SUMMARY_PROVIDERS:
        raise RecordingError(f"unknown summary provider {provider!r} (ollama | anthropic)")
    if not model.strip():
        raise RecordingError("no model given")
    segs, header = _summary_input(cfg, rec)
    words = sum(len(s.text.split()) for s in segs)
    if words < MIN_SUMMARY_WORDS:
        raise RecordingError(f"{rec.stem}: only {words} words transcribed, nothing to summarize")
    path = summary_path_for(cfg, rec, provider, model)
    text = summarize(rec, segs, header, replace(cfg.summarize, provider=provider, model=model.strip()))
    path.write_text(text, encoding="utf-8")
    return path


def do_summarize_compare(cfg: Config, rec: Recording, *, force: bool = False) -> list[Path]:
    """Extra summaries with other providers/models (config `compare`), each to <stem>.summary.<model>.md."""
    out = []
    for spec in cfg.summarize.compare:
        provider, _, model = spec.partition(":")
        if not model:
            log.error("%s: compare entry %r must be provider:model", rec.stem, spec)
            continue
        if (provider, model) == (cfg.summarize.provider, cfg.summarize.model):
            continue  # the main minutes are this model already (e.g. the fast track with Claude): no second copy
        if summary_path_for(cfg, rec, provider, model).exists() and not force:
            continue
        try:
            with step(f"srovnávací zápis ({provider} {model})"):
                out.append(summarize_as(cfg, rec, provider, model))
        except RecordingError as e:
            log.info("%s: no %s summary: %s", rec.stem, spec, e)
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


AUDIO_PATTERNS = ("_sys.wav", "_mic.wav", "_mix.wav", "_screen*.mp4")  # what purge_audio deletes


def audio_files(rec: Recording) -> list[Path]:
    return sorted(p for pat in AUDIO_PATTERNS for p in rec.dir.glob(f"{rec.stem}{pat}") if p.is_file())


def is_finished(rec: Recording) -> bool:
    """Transcript, summary and every speaker named: nothing left that would need to hear the audio again."""
    if not (rec.transcript_path.exists() and rec.summary_path.exists()):
        return False
    labels = rec.read_json(rec.transcript_path).get("speakers", [])
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    return not any(l.startswith("SPEAKER_") and not names.get(l) for l in labels)  # unassigned replies are no speaker


def purge_audio(cfg: Config, days: int, *, dry_run: bool = False, now: datetime | None = None) -> list[dict]:
    """Delete the WAVs and window videos of recordings older than `days` that are finished (is_finished).
    Transcript, exports, summary, names and the sidecar stay; the sidecar records `audio_purged`. Unfinished
    recordings are left alone - naming a speaker needs the audio samples. Returns what was (or would be) freed."""
    if days <= 0:
        raise RecordingError("purge-audio needs a positive number of days")
    now = now or datetime.now()
    out = []
    for rec in iter_recordings(cfg.out_dir):
        start = rec.sidecar.get("start")
        try:
            age = (now - datetime.fromisoformat(start)).days if start else -1
        except ValueError:
            age = -1
        files = audio_files(rec)
        if age < days or not files or not is_finished(rec):
            continue
        size = sum(p.stat().st_size for p in files)
        out.append({"stem": rec.stem, "age_days": age, "files": [p.name for p in files], "bytes": size})
        if dry_run:
            continue
        for p in files:
            p.unlink(missing_ok=True)
        rec.sidecar["audio_purged"] = now.date().isoformat()
        rec.save_sidecar()
        log.info("%s: audio purged (%d files, %.0f MB)", rec.stem, len(files), size / 1e6)
    return out


def reset_voiceprints(cfg: Config, rec: Recording) -> dict[str, int]:
    """A transcript from scratch redoes the voices too: the prints this recording gave go (only these; the people's
    prints from other recordings stay) and come back from the new transcript when the names are saved."""
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    gone = vp.forget_stem(rec.stem)
    if gone:
        vp.save()
        log.info("%s: voice prints of this recording dropped before a fresh transcript: %s", rec.stem,
                 ", ".join(f"{pid} ({n})" for pid, n in gone.items()))
    return gone


def reset_names(rec: Recording) -> dict:
    """Forget the manual label -> person assignment of one recording (the labels change with a new transcript,
    and a wrong assignment must not survive the re-run). The old mapping goes to the log, nowhere else."""
    old = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    if old:
        log.info("%s: dropping the manual speaker names before a fresh transcript: %s", rec.stem, old)
        rec.speakers_path.unlink()
    return old


def auto_purge(cfg: Config) -> list[dict]:
    """[retention] audio_days > 0: run purge_audio after processing. 0 (default) keeps everything."""
    if cfg.retention.audio_days <= 0:
        return []
    try:
        return purge_audio(cfg, cfg.retention.audio_days)
    except Exception as e:  # housekeeping must never fail a processing run
        log.error("purge-audio: %s", e)
        return []


UNNAMED_MIN_SECONDS = 20.0  # an unnamed speaker this long holds the minutes back (shorter ones: a cough, a "mhm")


def unnamed_speakers(rec: Recording, min_seconds: float = UNNAMED_MIN_SECONDS) -> list[str]:
    """Speaker labels of the transcript nobody named yet (SPEAKER_xx, no entry in speakers.json) that said at least
    `min_seconds`. Unassigned replies (UNKNOWN) are not a speaker to name."""
    if not rec.transcript_path.exists():
        return []
    data = rec.read_json(rec.transcript_path)
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    secs = speech_seconds(data.get("segments") or [])
    return [lab for lab in data.get("speakers", [])
            if lab.startswith("SPEAKER_") and not names.get(lab) and secs.get(lab, 0.0) >= min_seconds]


def do_process(cfg: Config, rec: Recording, *, force: bool = False, minutes_anyway: bool = True,
               before_minutes=None) -> None:
    """Transcript, exports and the minutes. With minutes_anyway=False (the review page, local or fast track) the
    minutes wait while speakers are unnamed: they would be written with SPEAKER_xx and regenerated right after the
    names are filled in (Uložit a přegenerovat zápis). `before_minutes(cfg, rec)` runs between the transcript and
    that decision (the fast track's local voices, which may name the speakers); its failure only means no names."""
    do_transcribe(cfg, rec, force=force)
    with step("export (txt, srt)"):
        do_export(cfg, rec)
    if before_minutes:
        try:
            before_minutes(cfg, rec)
        except Exception as e:
            log.error("%s: hlasy se nepodařilo doplnit: %s", rec.stem, e)
    waiting = [] if minutes_anyway else unnamed_speakers(rec)
    if cfg.summarize.enabled and waiting:
        log.info("%s: minutes held back – unnamed speakers: %s (name them, then Uložit a přegenerovat zápis)",
                 rec.stem, ", ".join(waiting))
        print(f"PROGRESS zápis počká: nepojmenovaní mluvčí {', '.join(waiting)}", flush=True)
    elif cfg.summarize.enabled:
        try:
            with step(f"zápis ({cfg.summarize.provider} {cfg.summarize.model})"):
                do_summarize(cfg, rec, force=force)
        except Exception as e:  # summary is optional: no model, no API key, network, refusal
            log.error("%s: zápis se nepodařil: %s", rec.stem, e)
        do_summarize_compare(cfg, rec, force=force)


def pending_recordings(cfg: Config) -> list[Recording]:
    """Recordings missing a transcript, or (when summaries are on) missing a summary."""
    out = []
    for r in iter_recordings(cfg.out_dir):
        if not r.transcript_path.exists() or (cfg.summarize.enabled and not r.summary_path.exists()):
            out.append(r)
    return out
