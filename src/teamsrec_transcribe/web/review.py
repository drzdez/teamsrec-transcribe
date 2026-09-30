"""Local review page: name the speakers of a recording by ear.

A tiny HTTP server on 127.0.0.1 serves one HTML page (`index.html`, vanilla JS) and a JSON API. Nothing leaves
the machine. The page reads the same files the CLI uses and writes only `<stem>.speakers.json`, then
regenerates the exports (and, on request, the summary). The API is the boundary: a native client could call
it later without touching this module's data functions.

REST API (described in web/openapi.py, served at /api/openapi.json)
  GET    /api/recordings?limit=                        recordings with their processing state
  GET    /api/recordings/{stem}                        everything the page needs for one recording
  GET    /api/recordings/{stem}/clip?start=&end=       a WAV clip cut from the 16 kHz mix (max CLIP_MAX_S seconds)
  GET    /api/recordings/{stem}/docs/{file}            a transcript .txt or a .summary*.md of the recording
  PUT    /api/recordings/{stem}/names                  {"names": {label: name|fields}, "title"?, "summary"?}
  POST   /api/recordings/{stem}/process                {"force"?} transcribe + export + summarize in the background
  POST   /api/recordings/{stem}/recognize              match unnamed labels against the voice prints
  POST   /api/recordings/{stem}/summaries              {"provider", "model"} a summary with that model, in the background
  DELETE /api/recordings/{stem}/summaries/{file}       delete that summary (closing its tab)
  GET    /api/summary-models                           models for summaries: Ollama on this PC, Claude for the key
  POST   /api/recordings/{stem}/speakers/merge         fold the labels of one person into one speaker
  POST   /api/recordings/{stem}/speakers/unmerge       undo the merges that remember their origin
  DELETE /api/recordings/{stem}/speakers/{label}       drop that speaker's segments (noise turned into text)
  POST   /api/recordings/{stem}/meeting                {"action": confirm|detach|attach, "candidate"?}
  GET    /api/recordings/{stem}/meeting/candidates     nearby Outlook items to link instead
  GET    /api/people / PUT /api/people                 the registry (PUT replaces it; opted-out people lose prints)
  POST   /api/people/merge                             {"keep", "drop", "stem"?}
  GET    /api/people/{id}                              one person with their voice prints
  DELETE /api/people/{id}/voiceprints?stem=&label=     one print, or all of them
  GET    /api/settings / PUT /api/settings             fields of the shared teamsrec.toml / {"values": {key: value}}
  PUT    /api/secrets/{name} / DELETE                  {"value"} an API key into / out of the Credential Manager
  GET    /api/help/{doc}                               user-guide | install | privacy
  GET    /api/status                                   job state + recent events
  GET    /api/events                                   Server-Sent Events (event: log), replayed after reconnects
  GET    /api/openapi.json                             this API as OpenAPI 3.1
  POST   /api/ping, /api/quit                          heartbeat (the server exits IDLE_S after the last), stop

One server per machine: the running instance is recorded in a lock file in the temp folder, a second `review`
just opens the existing page. Closing the browser tab ends the heartbeat, so the server (and its console window)
goes away by itself.
"""

from __future__ import annotations

import io
import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import urllib.request
import wave
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import settings
from ..config import Config, default_config_path
from ..people import DISPLAY_MODES, People, Person
from ..pipeline import (do_export, do_process, do_summarize, do_summarize_compare, enroll_names, summarize_as,
                        summary_path_for, load_segments, meeting_info,
                        merge_same_person, recognize_voices, remove_speaker, rename_recording, reset_names,
                        same_person_groups, set_meeting_link, unmerge_speakers)
from ..voiceprints import Voiceprints
from ..providers.base import Segment
from ..recording import Recording, RecordingError, iter_recordings, resolve_recording
from ..speakers import speaker_list

log = logging.getLogger(__name__)

CLIP_MAX_S = 8.0
SAMPLE_MIN_S = 2.0
SAMPLES_PER_SPEAKER = 3
EXCERPTS_PER_SPEAKER = 2
RECENT_RECORDINGS = 30
PAGE = Path(__file__).with_name("index.html")
IDLE_S = 90.0  # exit this long after the page's last heartbeat (the page pings every 15 s)
MAX_EVENTS = 200   # kept in memory only: the log is a convenience, the files are the truth
LOCK = Path(tempfile.gettempdir()) / "teamsrec-review.json"


def is_label(name: str | None) -> bool:
    return bool(name) and (name.startswith("SPEAKER_") or name == "UNKNOWN")


# ---------------------------------------------------------------- data for the page

def pick_samples(segs: list[Segment], n: int = SAMPLES_PER_SPEAKER) -> list[Segment]:
    """Up to n segments spread over the recording, the longest first, each at least SAMPLE_MIN_S long."""
    good = [s for s in segs if s.end - s.start >= SAMPLE_MIN_S] or list(segs)
    if not good:
        return []
    longest = max(good, key=lambda s: s.end - s.start)
    picks = [longest]
    if len(good) > 1 and n > 1:
        span = good[-1].start - good[0].start or 1.0
        for frac in (0.15, 0.85)[: n - 1]:
            target = good[0].start + frac * span
            cand = min((s for s in good if s not in picks), key=lambda s: abs(s.start - target), default=None)
            if cand is not None:
                picks.append(cand)
    picks.sort(key=lambda s: s.start)
    return picks


def summary_hints(rec: Recording) -> dict[str, str]:
    """Notes from the Speakers table at the end of the summary: '| SPEAKER_00 | ? | led the meeting |'."""
    if not rec.summary_path.exists():
        return {}
    hints: dict[str, str] = {}
    for line in rec.summary_path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and is_label(cells[0]):
            note = cells[2]
            if cells[1] not in ("?", "", "–", "-"):
                note = f"{cells[1]}: {note}" if note else cells[1]
            hints[cells[0]] = note
    return hints


def known_names(cfg: Config, people: People) -> list[str]:
    """Names to suggest: registered people (full name), the user, participants of any recording."""
    seen: dict[str, None] = {}
    for p in people.people:
        seen[p.full] = None
    if cfg.user_name:
        seen[cfg.user_name] = None
    for r in iter_recordings(cfg.out_dir):
        for p in r.participants:
            seen[p] = None
    return sorted(seen, key=str.casefold)


def _person_info(people: People, value: str) -> dict | None:
    p = people.get(value) or people.find(value)
    if not p:
        return None
    return {"id": p.id, "first": p.first, "last": p.last, "nick": p.nick, "display": p.display,
            "full": p.full, "shown": p.name(people.default_mode)}


VOICE_HINT_MIN = 0.40  # below this a "closest print" is noise, not a hint


def _voice_hint(vp: Voiceprints, people: People, vec, unresolved: bool, secs: float, threshold: float) -> dict | None:
    """The closest voice print for a still-unknown label that did not pass the automatic threshold, so the
    user can confirm it with one click instead of guessing."""
    if not (unresolved and vec and vp.people):
        return None
    ranked = vp.scores(vec)
    if not ranked or ranked[0][1] < VOICE_HINT_MIN:
        return None
    pid, score = ranked[0]
    p = people.get(pid)
    if p is None:
        return None
    second = ranked[1][1] if len(ranked) > 1 else -1.0
    why = "krátká promluva" if secs < 30 else ("malý odstup od dalšího" if score - second < 0.10 else "pod prahem")
    return {"person": pid, "name": p.full, "score": round(score, 2), "fields": {"first": p.first, "last": p.last,
            "nick": p.nick, "display": p.display}, "why": why, "threshold": threshold}


def _fmt_hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def speaker_languages(segs: list[Segment], default: str | None) -> dict[str, dict]:
    """Per label: the language most of its speech was transcribed in, and whether that was a second pass in
    another language than the recording's (per-speaker language)."""
    spoken: dict[str, dict[str, float]] = {}
    for s in segs:
        lang = s.language or default or "?"
        by = spoken.setdefault(s.speaker or "UNKNOWN", {})
        by[lang] = by.get(lang, 0.0) + max(s.end - s.start, 0.0)
    out = {}
    for label, by in spoken.items():
        main = max(by, key=by.get)
        out[label] = {"language": main, "own": bool(default) and main not in (default, "?"),
                      "mixed": len([v for v in by.values() if v >= 5]) > 1}
    return out


def build_review(cfg: Config, rec: Recording) -> dict:
    if not rec.transcript_path.exists():  # the page offers to process it
        return {"stem": rec.stem, "title": rec.title, "start": rec.sidecar.get("start"),
                "duration_s": rec.sidecar.get("duration_s"), "source": rec.source, "transcribed": False,
                "has_mix": bool(rec.mix_path and rec.mix_path.exists()) or bool(rec.track_path("sys")),
                "speakers": [], "known_names": [], "people": [], "display_default": cfg.people_display,
                "language": None, "speaker_sources": [], "has_summary": False}
    data, segs = load_segments(rec)  # raw provider speakers, without speakers.json applied
    manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    people = People.load(cfg.out_dir, cfg.people_display)
    hints = summary_hints(rec)
    voice = data.get("voice_matches") or {}
    vp = Voiceprints.load(cfg.out_dir)
    embeddings = data.get("speaker_embeddings") or {}
    by: dict[str, list[Segment]] = {}
    for s in segs:
        by.setdefault(s.speaker or "UNKNOWN", []).append(s)
    langs = speaker_languages(segs, data.get("language"))
    speakers = []
    for label, ss in sorted(by.items(), key=lambda kv: -sum(s.end - s.start for s in kv[1])):
        secs = sum(s.end - s.start for s in ss)
        excerpts = sorted(ss, key=lambda s: -len(s.text))[:EXCERPTS_PER_SPEAKER]
        value = manual.get(label, "" if is_label(label) else label)
        person = _person_info(people, value) if value else None
        if person:
            fields = {k: person[k] for k in ("first", "last", "nick", "display")}
        elif value:  # a literal name (video OCR, mic config) that is not registered yet
            parts = value.split()
            fields = {"first": parts[0], "last": " ".join(parts[1:]), "nick": "", "display": ""}
        else:
            fields = {"first": "", "last": "", "nick": "", "display": ""}
        speakers.append({
            "label": label,
            "unresolved": is_label(label),
            "name": person["full"] if person else value,
            "fields": fields,  # what the inputs show
            "person": person,  # None for literal names (video / mic) that are not registered
            "voice": ({"person": voice[label]["person"], "score": voice[label]["score"],
                       "name": (people.get(voice[label]["person"]) or Person(voice[label]["person"])).full}
                      if label in voice else None),
            "voice_hint": _voice_hint(vp, people, embeddings.get(label), is_label(label) and not value, secs,
                                      cfg.voiceprints.threshold),
            "confirmed": bool(value) and bool(manual.get(label)) and label not in voice,
            "seconds": round(secs, 1),
            "count": len(ss),
            **langs.get(label, {"language": data.get("language"), "own": False, "mixed": False}),
            "hint": hints.get(label, ""),
            "samples": [{"start": round(s.start, 2), "end": round(min(s.end, s.start + CLIP_MAX_S), 2),
                         "at": _fmt_hms(s.start), "text": s.text[:140]} for s in pick_samples(ss)],
            "excerpts": [{"at": _fmt_hms(s.start), "text": s.text[:200]} for s in
                         sorted(excerpts, key=lambda s: s.start)],
        })
    return {
        "stem": rec.stem, "title": rec.title, "start": rec.sidecar.get("start"),
        "duration_s": rec.sidecar.get("duration_s"), "source": rec.source, "transcribed": True,
        "language": data.get("language"), "speaker_sources": data.get("speaker_sources", []),
        "transcribed_with": {"provider": data.get("provider"), "model": data.get("model"),
                             "languages": data.get("languages") or []},
        "has_summary": rec.summary_path.exists(), "has_mix": bool(rec.mix_path and rec.mix_path.exists()),
        "speakers": speakers, "known_names": known_names(cfg, people),
        "same_person": [{"person": pid, "labels": labels, "name": (people.get(pid) or Person(pid)).full}
                        for pid, labels in same_person_groups(rec, people).items()],
        "can_unmerge": any(s.get("merged_from") for s in (data.get("segments") or [])),
        "people": people.to_json(), "display_default": people.default_mode,
        "meeting": meeting_info(cfg, rec),
        "docs": recording_docs(rec),
    }


SUMMARY_STAMP = re.compile(r"<!--\s*teamsrec-transcribe summary \| (?P<provider>[^:|]+):\s*(?P<model>[^|]+?)\s*(?:\|[^>]*?created:\s*(?P<created>[^|>]+?))?\s*(?:\||-->)")


def summary_info(path: Path, main: bool) -> dict:
    """A summary file for the page: which provider/model wrote it (from its first-line stamp) and when."""
    try:
        with path.open(encoding="utf-8") as f:
            first = f.readline()
    except OSError:
        first = ""
    m = SUMMARY_STAMP.search(first)
    provider, model = (m.group("provider").strip(), m.group("model").strip()) if m else ("", "")
    fallback = "hlavní" if main else path.name.split(".summary.", 1)[-1][:-3]
    return {"file": path.name, "label": model or fallback, "provider": provider, "model": model,
            "created": (m.group("created") or "").strip() if m else "", "main": main}


def recording_docs(rec: Recording) -> dict:
    """Readable documents of a recording for the Přepis / Zápis tabs."""
    summaries = []
    if rec.summary_path.exists():
        summaries.append(summary_info(rec.summary_path, True))
    for p in sorted(rec.dir.glob(f"{rec.stem}.summary.*.md")):
        summaries.append(summary_info(p, False))
    others = [{"file": p.name, "label": f"přepis ({p.name[len(rec.stem) + 1:-4]})"}
              for p in sorted(rec.dir.glob(f"{rec.stem}.*.txt"))]
    return {"transcript": rec.file(".txt").name if rec.file(".txt").exists() else None, "summaries": summaries,
            "transcripts": others}


def delete_summary(rec: Recording, name: str) -> None:
    """Delete one summary of the recording (the page's tab close). Only <stem>.summary*.md, nothing else."""
    if ("/" in name or "\\" in name or not name.startswith(rec.stem + ".summary") or not name.endswith(".md")):
        raise RecordingError("not a summary of this recording")
    path = rec.dir / name
    if not path.exists():
        raise RecordingError(f"{name} does not exist")
    path.unlink()


def read_doc(rec: Recording, name: str) -> str:
    """Only the recording's own text documents (no path tricks)."""
    if "/" in name or "\\" in name or not name.startswith(rec.stem) or not (name.endswith(".md") or name.endswith(".txt")):
        raise RecordingError("not a document of this recording")
    path = rec.dir / name
    if not path.exists():
        raise RecordingError(f"{name} does not exist")
    return path.read_text(encoding="utf-8")


HELP_DOCS = {"user-guide": "user-guide.md", "install": "install.md", "privacy": "privacy.md"}
DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"  # the source checkout; an installed wheel has no docs
DOCS_URL = "https://github.com/drzdez/teamsrec-transcribe/blob/main/docs/"


def help_doc(name: str) -> dict:
    """One of the guides for the page's help: the local file of this version, else its page on GitHub."""
    fname = HELP_DOCS.get(name)
    if not fname:
        raise RecordingError(f"unknown help document {name!r} (available: {', '.join(HELP_DOCS)})")
    path = DOCS_DIR / fname
    if path.exists():
        return {"doc": name, "text": path.read_text(encoding="utf-8")}
    return {"doc": name, "url": DOCS_URL + fname}


def list_recordings(cfg: Config, limit: int = RECENT_RECORDINGS) -> list[dict]:
    out = []
    recs = sorted(iter_recordings(cfg.out_dir), key=lambda r: r.stem, reverse=True)[:limit]
    for r in recs:
        unresolved = None
        if r.transcript_path.exists():
            try:
                names = r.read_json(r.speakers_path) if r.speakers_path.exists() else {}
                labels = r.read_json(r.transcript_path).get("speakers", [])
                unresolved = sum(1 for l in labels if is_label(l) and not names.get(l))
            except Exception:
                unresolved = None
        out.append({"stem": r.stem, "title": r.title, "start": r.sidecar.get("start"),
                    "transcribed": r.transcript_path.exists(), "unresolved": unresolved,
                    "summary": r.summary_path.exists()})
    return out


# ---------------------------------------------------------------- audio clips

def clip_wav(mix: Path, start: float, end: float) -> bytes:
    """A slice of the mix as a complete WAV file (in memory)."""
    with wave.open(str(mix), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        total = w.getnframes()
        a = max(0, min(int(start * sr), total))
        b = max(a, min(int(end * sr), total))
        w.setpos(a)
        frames = w.readframes(b - a)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as o:
        o.setnchannels(ch); o.setsampwidth(sw); o.setframerate(sr)
        o.writeframes(frames)
    return buf.getvalue()


# ---------------------------------------------------------------- saving

def _person_from_fields(people: People, fields: dict) -> "Person | None":
    """Find or create the person for {first, last, nick, display}; updates nick/display of a known person."""
    first, last, nick = (str(fields.get(k) or "").strip() for k in ("first", "last", "nick"))
    display = str(fields.get("display") or "")
    if not (first or last or nick):
        return None
    full = f"{first} {last}".strip()
    p = (people.find(full) if full else None) or (people.find(nick) if nick and not full else None)
    if p is None:
        p = Person.from_text(full or nick, {q.id for q in people.people})
        if not full:
            p.first = ""
        people.people.append(p)
    if nick:
        p.nick = nick
    if display in DISPLAY_MODES or display == "":
        p.display = display
    return p


def save_title(cfg: Config, rec: Recording, title: str) -> Recording:
    """New meeting title: folder, files, sidecar, voice prints and summary headings are renamed along
    (see pipeline.rename_recording). Returns the (possibly moved) recording."""
    title = " ".join(title.split())
    if not title or title == rec.title:
        return rec
    return rename_recording(cfg, rec, title)


def save_names(cfg: Config, rec: Recording, names: dict) -> dict[str, str]:
    """Write speakers.json with person ids (people created/updated from the fields), regenerate txt/srt.
    Returns {label: person id} of what was written."""
    people = People.load(cfg.out_dir, cfg.people_display)
    snapshot = json.dumps(people.to_json(), sort_keys=True)
    clean: dict[str, str] = {}
    for label, name in names.items():
        if isinstance(name, dict):
            p = _person_from_fields(people, name)
            if p is not None:
                clean[label] = p.id
            continue
        name = (name or "").strip()
        if not name or name == label or is_label(name):
            continue
        clean[label] = people.ensure(name).id
    if json.dumps(people.to_json(), sort_keys=True) != snapshot:
        people.save()
    rec.write_json(rec.speakers_path, clean)
    do_export(cfg, rec)
    if clean and rec.transcript_path.exists():  # a name the user saw and saved is confirmed, not a guess any more
        data = rec.read_json(rec.transcript_path)
        voice = data.get("voice_matches") or {}
        if any(lab in voice for lab in clean):
            data["voice_matches"] = {lab: v for lab, v in voice.items() if lab not in clean}
            rec.write_json(rec.transcript_path, data)
    enroll_names(cfg, rec, clean)  # only now does the print go into the shared registry
    return clean


def merge_people(cfg: Config, keep: str, drop: str, stem: str | None = None) -> list[dict]:
    people = People.load(cfg.out_dir, cfg.people_display)
    changed = people.merge(keep, drop, iter_recordings(cfg.out_dir))
    people.save()
    vp = Voiceprints.load(cfg.out_dir)
    vp.rename(drop, keep)
    vp.save()
    for r in changed:
        if r.transcript_path.exists():
            do_export(cfg, r)
    if stem and stem not in {r.stem for r in changed}:
        r = resolve_recording(stem, cfg.out_dir)
        if r.transcript_path.exists():
            do_export(cfg, r)
    return people.to_json()


def person_detail(cfg: Config, pid: str) -> dict:
    people = People.load(cfg.out_dir, cfg.people_display)
    p = people.get(pid)
    if p is None:
        raise RecordingError(f"unknown person {pid!r}")
    vp = Voiceprints.load(cfg.out_dir)
    recs = {r.stem: r for r in iter_recordings(cfg.out_dir)}
    prints = []
    for pr in vp.people.get(pid, []):
        row = {"stem": pr.get("stem"), "label": pr.get("label"), "added": pr.get("added"), "dims": len(pr.get("v", [])),
               "title": None, "start": None, "seconds": None, "samples": [], "has_mix": False}
        rec = recs.get(pr.get("stem") or "")
        if rec is not None:
            row["title"], row["start"] = rec.title, rec.sidecar.get("start")
            row["has_mix"] = bool(rec.mix_path and rec.mix_path.exists())
            if rec.transcript_path.exists():
                try:
                    _, segs = load_segments(rec)
                    mine = [s for s in segs if s.speaker == pr.get("label")]
                    row["seconds"] = round(sum(s.end - s.start for s in mine), 1)
                    row["samples"] = [{"start": round(s.start, 2), "end": round(min(s.end, s.start + CLIP_MAX_S), 2),
                                       "at": _fmt_hms(s.start), "text": s.text[:140]} for s in pick_samples(mine)]
                except Exception:
                    pass
        prints.append(row)
    d = {"id": p.id, "first": p.first, "last": p.last, "nick": p.nick, "display": p.display, "aliases": p.aliases,
         "shown": p.name(people.default_mode), "model": vp.model, "prints": prints,
         "recordings": sorted({r.stem for r in recs.values() if r.speakers_path.exists()
                               and pid in r.read_json(r.speakers_path).values()}, reverse=True)}
    return d


def forget_print(cfg: Config, pid: str, stem: str | None, label: str | None) -> int:
    vp = Voiceprints.load(cfg.out_dir)
    before = vp.count(pid)
    if stem and label:
        vp.people[pid] = [pr for pr in vp.people.get(pid, []) if not (pr.get("stem") == stem and pr.get("label") == label)]
        if not vp.people[pid]:
            del vp.people[pid]
    else:
        vp.forget(pid)
    vp.save()
    return before - vp.count(pid)


def people_rows(cfg: Config) -> dict:
    people = People.load(cfg.out_dir, cfg.people_display)
    vp = Voiceprints.load(cfg.out_dir)
    rows = people.to_json()
    for r in rows:
        r["prints"] = vp.count(r["id"])
    return {"people": rows, "display_default": people.default_mode, "modes": list(DISPLAY_MODES),
            "path": str(people.path)}


def _drop_opted_out_prints(cfg: Config, people: People) -> list[str]:
    """Somebody opted out of voice recognition: their prints go right away, not at the next enrolment."""
    vp = Voiceprints.load(cfg.out_dir)
    gone = [pid for pid in people.no_voiceprint() if vp.count(pid)]
    for pid in gone:
        vp.forget(pid)
    if gone:
        vp.save()
        log.info("voice prints deleted on request: %s", ", ".join(gone))
    return gone


def save_people(cfg: Config, rows: list[dict], stem: str | None = None) -> list[dict]:
    people = People.load(cfg.out_dir, cfg.people_display)
    people.replace_all(rows)
    people.save()
    _drop_opted_out_prints(cfg, people)
    if stem:
        rec = resolve_recording(stem, cfg.out_dir)
        if rec.transcript_path.exists():
            do_export(cfg, rec)
    return people_rows(cfg)["people"]  # with the print counts, so the page shows what is left


# ---------------------------------------------------------------- server

class ReviewState:
    settings_path: Path  # the teamsrec.toml the settings page edits (the one the config came from)

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.settings_path = cfg.source_path or default_config_path()
        self.lock = threading.RLock()
        self.busy = False
        self.message = ""
        self.error = ""
        self.last_seen = 0.0  # time of the last request from the page (0 = no page yet)
        self.events: list[dict] = []
        self.seq = 0
        self.subscribers: list[queue.Queue] = []  # one per open /api/events stream

    def seen(self) -> None:
        self.last_seen = time.monotonic()

    def event(self, text: str, level: str = "ok", stem: str = "", reload: bool = False, job_end: bool = False) -> int:
        """One line of history: what the server did, for the status bar and the list behind it. A long job is
        started and forgotten, so the page has to be able to ask later what happened. job_end marks the line that
        closes a background job (the page waiting for it wakes on that)."""
        with self.lock:
            self.seq += 1
            e = {"n": self.seq, "at": datetime.now().strftime("%H:%M:%S"), "text": text,
                 "level": level, "stem": stem, "reload": reload, "busy": self.busy, "job_end": job_end}
            self.events.append(e)
            del self.events[:-MAX_EVENTS]
            for q in self.subscribers:
                q.put(e)
        log.info("%s", text)
        return self.seq

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def events_since(self, n: int | None, last: int = 40) -> list[dict]:
        """What a (re)connecting page missed: everything after event n, or the last few on a first connect."""
        with self.lock:
            return [e for e in self.events if e["n"] > n] if n is not None else self.events[-last:]

    def _job_errors(self, stem: str) -> logging.Handler:
        """A log handler for the time of a job: what the pipeline logs as an error without failing the job (a
        comparison summary with an unknown model, a missing key) goes to the page's history, not only to the
        console window nobody looks at."""
        state = self

        class ToEvents(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    state.event(record.getMessage(), "err", stem)
                except Exception:  # never let the log break the job
                    pass

        h = ToEvents(logging.ERROR)
        logging.getLogger("teamsrec_transcribe").addHandler(h)
        return h

    def run_job(self, name: str, fn, done: str, stem: str = "", start: str = "") -> bool:
        """Run fn() in a background thread, one job at a time. Its end is an event with job_end (SSE to the page;
        /api/status tells the same to a client without the stream)."""
        with self.lock:
            if self.busy:
                return False
            self.busy, self.message, self.error, self.job = True, name, "", name
            if start:
                self.event(start, "busy", stem)  # logged before the work, so the order in the log is the real one

        def work():
            outcome = (f"{done}: {stem}" if stem else done, "ok")
            errors = self._job_errors(stem)
            try:
                fn()
                with self.lock:
                    self.message = done
            except Exception as e:  # shown on the page, not fatal
                with self.lock:
                    self.error = str(e)
                outcome = (f"{name} selhalo: {e}", "err")
            finally:
                logging.getLogger("teamsrec_transcribe").removeHandler(errors)
                with self.lock:
                    self.busy = False
            self.event(outcome[0], outcome[1], stem, reload=True, job_end=True)  # after busy clears: the page sees it idle
        threading.Thread(target=work, daemon=True).start()
        return True

    def run_summary(self, rec: Recording) -> None:
        def work():
            do_summarize(self.cfg, rec, force=True)
            do_summarize_compare(self.cfg, rec, force=True)  # the comparison summaries follow the new names too
        self.run_job("summary", work, "zápis přegenerován", rec.stem, start=f"{rec.stem}: zápis se generuje"
                     + (f" (i srovnávací: {', '.join(self.cfg.summarize.compare)})" if self.cfg.summarize.compare else ""))

    def run_process(self, rec: Recording, force: bool = False) -> bool:
        """force = transcribe again from scratch (new diarization, new labels), so the manual names go first:
        they are keyed by labels that will not exist any more, and a wrong one must not come back."""
        def work():
            if force:
                old = reset_names(rec)
                if old:
                    self.event(f"{rec.stem}: ruční přiřazení jmen zahozeno ({len(old)})", "info", rec.stem)
                if rec.speakers_video_path.exists():
                    # the cached timeline holds the names OCR read last time; from scratch means those too
                    rec.speakers_video_path.unlink()
                    self.event(f"{rec.stem}: okna Teams se projdou znovu (jmenovky z minule zahozeny)",
                               "info", rec.stem)
            do_process(self.cfg, rec, force=force)
        return self.run_job("process", work, "zpracováno", rec.stem,
                            start=f"{rec.stem}: {'nový přepis od nuly' if force else 'zpracování'} spuštěno")

    def status(self, events: int = 40) -> dict:
        with self.lock:
            return {"app": "teamsrec-review", "out_dir": str(self.cfg.out_dir), "job": getattr(self, "job", ""),
                    "busy": self.busy, "message": self.message, "error": self.error,
                    "seq": self.seq, "events": self.events[-events:]}


STEM_RE_PART = r"(?P<stem>[^/]+)"
ROUTES = [  # (method, path pattern, handler method) - keep web/openapi.py in step
    ("GET", r"/", "page"),
    ("GET", r"/api/recordings", "recordings"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}", "recording"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/clip", "clip"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/docs/(?P<file>[^/]+)", "doc"),
    ("PUT", rf"/api/recordings/{STEM_RE_PART}/names", "save_names"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/process", "process"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/recognize", "recognize"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/summaries", "summarize_as"),
    ("DELETE", rf"/api/recordings/{STEM_RE_PART}/summaries/(?P<file>[^/]+)", "delete_summary"),
    ("GET", r"/api/summary-models", "summary_models"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/speakers/merge", "merge"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/speakers/unmerge", "unmerge"),
    ("DELETE", rf"/api/recordings/{STEM_RE_PART}/speakers/(?P<label>[^/]+)", "remove_speaker"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/meeting", "meeting"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/meeting/candidates", "meeting_candidates"),
    ("GET", r"/api/people", "people"),
    ("PUT", r"/api/people", "save_people"),
    ("POST", r"/api/people/merge", "merge_people"),
    ("GET", r"/api/people/(?P<pid>[^/]+)", "person"),
    ("DELETE", r"/api/people/(?P<pid>[^/]+)/voiceprints", "forget_prints"),
    ("GET", r"/api/settings", "settings"),
    ("PUT", r"/api/settings", "save_settings"),
    ("PUT", r"/api/secrets/(?P<name>[^/]+)", "set_secret"),
    ("DELETE", r"/api/secrets/(?P<name>[^/]+)", "delete_secret"),
    ("GET", r"/api/help/(?P<doc>[^/]+)", "help"),
    ("GET", r"/api/status", "status"),
    ("GET", r"/api/events", "events"),
    ("GET", r"/api/openapi.json", "openapi"),
    ("POST", r"/api/ping", "ping"),
    ("POST", r"/api/quit", "quit"),
]
_COMPILED = [(m, re.compile(pat + "$"), name) for m, pat, name in ROUTES]
MEETING_ACTIONS = {"confirm": "spojení potvrzeno", "detach": "odpojeno", "attach": "spojeno s jinou"}
SSE_KEEPALIVE_S = 15.0


def match_route(method: str, path: str) -> tuple[str | None, dict[str, str], bool]:
    """(handler name, path parameters, path known under another method). Pure, testable."""
    known = False
    for m, rx, name in _COMPILED:
        hit = rx.match(path)
        if hit:
            if m == method:
                return name, {k: unquote(v) for k, v in hit.groupdict().items()}, True
            known = True
    return None, {}, known


def _handler(state: ReviewState, server_ref: dict):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive; every response carries Content-Length (SSE closes itself)

        def log_message(self, fmt, *args):  # quiet; the CLI log is enough
            log.debug("http " + fmt, *args)

        # ---- helpers
        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _bytes(self, data: bytes, ctype: str):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("the request body must be a JSON object")
            return data

        def _dispatch(self, method: str):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            state.seen()
            name, params, known = match_route(method, u.path)
            if name is None:
                self._json({"error": "method not allowed" if known else "not found"},
                           HTTPStatus.METHOD_NOT_ALLOWED if known else HTTPStatus.NOT_FOUND)
                return
            try:
                body = self._body() if method in ("POST", "PUT", "DELETE") else {}
                getattr(self, "r_" + name)(q=q, body=body, **params)
            except (RecordingError, ValueError, KeyError) as e:
                if method != "GET":
                    state.event(f"{method} {u.path}: {e}", "err")
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _recording(self, stem: str) -> Recording:
            return resolve_recording(stem, state.cfg.out_dir)

        # ---- recordings
        def r_page(self, q, body):
            self._bytes(PAGE.read_bytes(), "text/html; charset=utf-8")

        def r_recordings(self, q, body):
            want = (q.get("limit") or [""])[0]
            limit = min(int(want), 2000) if want.isdigit() else RECENT_RECORDINGS
            self._json(list_recordings(state.cfg, limit))

        def r_recording(self, q, body, stem):
            self._json(build_review(state.cfg, self._recording(stem)))

        def r_clip(self, q, body, stem):
            rec = self._recording(stem)
            if not rec.mix_path or not rec.mix_path.exists():
                raise RecordingError("no mix audio")
            start = float(q.get("start", ["0"])[0])
            end = min(float(q.get("end", ["0"])[0]), start + CLIP_MAX_S)
            self._bytes(clip_wav(rec.mix_path, start, end), "audio/wav")

        def r_doc(self, q, body, stem, file):
            self._json({"file": file, "text": read_doc(self._recording(stem), file)})

        def r_save_names(self, q, body, stem):
            rec = self._recording(stem)
            if body.get("title") is not None:
                rec = save_title(state.cfg, rec, str(body.get("title")))
            written = save_names(state.cfg, rec, body.get("names") or {})
            state.event(f"{rec.stem}: uloženo {len(written)} jmen, přepis a titulky přegenerovány", "ok", rec.stem)
            if body.get("summary"):
                state.run_summary(rec)
            self._json({"ok": True, "written": written, "stem": rec.stem, "title": rec.title,
                        "status": state.status()})

        def r_process(self, q, body, stem):
            started = state.run_process(self._recording(stem), force=bool(body.get("force")))
            if not started:
                self._json({"error": "another job is still running", "status": state.status()}, HTTPStatus.CONFLICT)
                return
            self._json({"ok": True, "status": state.status()})

        def r_summarize_as(self, q, body, stem):
            rec = self._recording(stem)
            provider, model = str(body.get("provider") or "").strip(), str(body.get("model") or "").strip()
            if provider not in ("ollama", "anthropic") or not model:
                raise ValueError("provider (ollama | anthropic) and model are needed")
            started = state.run_job("summary", lambda: summarize_as(state.cfg, rec, provider, model),
                                    f"zápis {model} hotový", rec.stem, start=f"{rec.stem}: zápis {model} se generuje")
            if not started:
                self._json({"error": "another job is still running", "status": state.status()}, HTTPStatus.CONFLICT)
                return
            self._json({"ok": True, "file": summary_path_for(state.cfg, rec, provider, model).name})

        def r_delete_summary(self, q, body, stem, file):
            rec = self._recording(stem)
            delete_summary(rec, file)
            state.event(f"{rec.stem}: zápis {file} smazán", "info", rec.stem, reload=True)
            self._json({"ok": True})

        def r_summary_models(self, q, body):
            sug = settings.suggestions(settings.read_values(state.settings_path))
            self._json({**sug["summary"], "default": {"provider": state.cfg.summarize.provider,
                                                     "model": state.cfg.summarize.model}})

        def r_recognize(self, q, body, stem):
            rec = self._recording(stem)
            m = recognize_voices(state.cfg, rec)
            state.event(f"{rec.stem}: po hlase poznáno {len(m)} mluvčích" + (" (nepotvrzeno)" if m else ""),
                        "ok" if m else "info", rec.stem, reload=True)
            self._json({"ok": True, "matches": m})

        def r_merge(self, q, body, stem):
            rec = self._recording(stem)
            res = merge_same_person(state.cfg, rec)
            state.event(f"{rec.stem}: sloučeno {res['merged']} označení, otisků přibylo {res['prints']}",
                        "ok" if res["merged"] else "info", rec.stem, reload=True)
            self._json({"ok": True, **res})

        def r_unmerge(self, q, body, stem):
            rec = self._recording(stem)
            n = unmerge_speakers(state.cfg, rec)
            state.event(f"{rec.stem}: sloučení vráceno, {n} replik zpět", "ok", rec.stem, reload=True)
            self._json({"ok": True, "restored": n})

        def r_remove_speaker(self, q, body, stem, label):
            rec = self._recording(stem)
            n = remove_speaker(state.cfg, rec, label)
            state.event(f"{rec.stem}: smazáno {n} replik mluvčího {label}", "ok", rec.stem, reload=True)
            self._json({"ok": True, "removed": n})

        def r_meeting(self, q, body, stem):
            action = str(body.get("action") or "")
            rec = set_meeting_link(state.cfg, self._recording(stem), action, body.get("candidate"))
            if rec.transcript_path.exists():
                do_export(state.cfg, rec)
            state.event(f"{rec.stem}: schůzka z kalendáře – {MEETING_ACTIONS.get(action, action)}", "ok", rec.stem,
                        reload=True)
            self._json({"ok": True, "stem": rec.stem, "meeting": meeting_info(state.cfg, rec)})

        def r_meeting_candidates(self, q, body, stem):
            from ..outlook import candidates_for
            start = self._recording(stem).sidecar.get("start")
            self._json({"candidates": candidates_for(datetime.fromisoformat(start), 4 * 3600)
                        if start and state.cfg.calendar_outlook else []})

        # ---- people
        def r_people(self, q, body):
            self._json(people_rows(state.cfg))

        def r_save_people(self, q, body):
            rows = save_people(state.cfg, body.get("people") or [], body.get("stem") or None)
            state.event(f"seznam lidí uložen ({len(rows)} osob)", "ok")
            self._json({"ok": True, "people": rows})

        def r_merge_people(self, q, body):
            keep, drop = str(body.get("keep") or ""), str(body.get("drop") or "")
            rows = merge_people(state.cfg, keep, drop, body.get("stem") or None)
            state.event(f"osoba {drop} sloučena do {keep} (ve všech nahrávkách)", "ok", reload=True)
            self._json({"ok": True, "people": rows})

        def r_person(self, q, body, pid):
            self._json(person_detail(state.cfg, pid))

        def r_forget_prints(self, q, body, pid):
            n = forget_print(state.cfg, pid, (q.get("stem") or [None])[0], (q.get("label") or [None])[0])
            state.event(f"{pid}: smazáno {n} hlasových otisků", "ok")
            self._json({"ok": True, "removed": n})

        # ---- server
        # ---- settings (shared teamsrec.toml) and API keys; key values never come back
        def r_settings(self, q, body):
            self._json(settings.describe(state.settings_path))

        def r_save_settings(self, q, body):
            values = body.get("values")
            if not isinstance(values, dict):
                raise ValueError("values must be an object")
            res = settings.save(values, state.settings_path)
            if res["changed"]:
                state.cfg = settings.reload(state.cfg, state.settings_path)
                state.event(f"nastavení uloženo: {', '.join(res['changed'])}"
                            + (" (složka nahrávek platí po restartu)" if res["restart"] else ""), "ok")
            self._json({"ok": True, **res, **settings.describe(state.settings_path)})

        def r_set_secret(self, q, body, name):
            settings.set_secret(name, str(body.get("value") or ""))
            state.event(f"klíč {name} uložen do Správce přihlašovacích údajů", "ok")
            self._json({"ok": True, "secrets": settings.secret_states()})

        def r_delete_secret(self, q, body, name):
            removed = settings.delete_secret(name)
            state.event(f"klíč {name} " + ("smazán ze Správce přihlašovacích údajů" if removed else "tam nebyl"), "info")
            self._json({"ok": True, "removed": removed, "secrets": settings.secret_states()})

        def r_help(self, q, body, doc):
            self._json(help_doc(doc))

        def r_status(self, q, body):
            self._json(state.status())

        def r_openapi(self, q, body):
            from .openapi import spec
            from .. import __version__
            self._json(spec(__version__))

        def r_ping(self, q, body):
            self._json({"ok": True})

        def r_quit(self, q, body):
            if state.status()["busy"]:
                self._json({"error": "a job is still running, wait for it to finish"}, HTTPStatus.CONFLICT)
                return
            self._json({"ok": True})
            threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()

        def r_events(self, q, body):
            """Server-Sent Events: every state.event() as `event: log`; on connect the recent history (or what
            was missed since Last-Event-ID), then a comment every SSE_KEEPALIVE_S so dead streams are noticed."""
            last = self.headers.get("Last-Event-ID") or (q.get("since") or [""])[0]
            since = int(last) if str(last).isdigit() else None
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            sub = state.subscribe()
            try:
                self._sse("hello", {"seq": state.seq, "busy": state.busy, "replay": since is None})
                for e in state.events_since(since):
                    self._sse("log", {**e, "replay": since is None}, e["n"])
                while not getattr(server_ref.get("server"), "_teamsrec_stopping", False):
                    try:
                        e = sub.get(timeout=SSE_KEEPALIVE_S)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self._sse("log", e, e["n"])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass  # the page went away
            finally:
                state.unsubscribe(sub)

        def _sse(self, event: str, data: dict, eid: int | None = None):
            frame = (f"id: {eid}\n" if eid is not None else "") + f"event: {event}\n" \
                    + f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()
    return Handler


def running_instance(cfg: Config, lock: Path = LOCK) -> str | None:
    """URL of a review server already running for this recordings folder, if it answers."""
    try:
        info = json.loads(lock.read_text(encoding="utf-8"))
        if Path(info.get("out_dir", "")) != cfg.out_dir:
            return None
        with urllib.request.urlopen(info["url"] + "api/status", timeout=1.0) as r:
            st = json.loads(r.read().decode("utf-8"))
            if st.get("app") == "teamsrec-review" and Path(st.get("out_dir", "")) == cfg.out_dir:
                return info["url"]  # alive and really ours; a stale lock (crash, reboot) fails here
    except Exception:
        pass
    return None


def _idle_watchdog(state: ReviewState, server: ThreadingHTTPServer, idle_s: float) -> None:
    """Stop the server once the page has gone quiet (tab closed). Never while a summary is being generated."""
    while True:
        time.sleep(min(5.0, idle_s / 3))
        if state.last_seen and not state.busy and time.monotonic() - state.last_seen > idle_s:
            log.info("review page closed, stopping")
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


def serve(cfg: Config, rec: Recording | None, *, port: int = 0, open_browser: bool = True,
          idle_s: float = IDLE_S, lock: Path = LOCK) -> str:
    """Run the review server until the page's Close button, the tab is closed, or Ctrl+C. Returns the URL.
    A second call while one is running only opens the existing page."""
    fragment = f"#{rec.stem}" if rec else ""
    existing = running_instance(cfg, lock)
    if existing:
        log.info("review page already running: %s", existing)
        if open_browser:
            webbrowser.open(existing + fragment)
        return existing
    state = ReviewState(cfg)
    ref: dict = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(state, ref))
    ref["server"] = server
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    lock.write_text(json.dumps({"url": base, "pid": os.getpid(), "out_dir": str(cfg.out_dir)}), encoding="utf-8")
    log.info("review page: %s  (closes itself when the tab is closed; Ctrl+C also works)", base + fragment)
    if open_browser:
        webbrowser.open(base + fragment)
    threading.Thread(target=_idle_watchdog, args=(state, server, idle_s), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
                lock.unlink()
        except Exception:
            pass
    return base


def unresolved_labels(rec: Recording) -> list[str]:
    if not rec.transcript_path.exists():
        return []
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    return [l for l in rec.read_json(rec.transcript_path).get("speakers", []) if is_label(l) and not names.get(l)]
