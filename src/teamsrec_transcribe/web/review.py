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
  PUT    /api/recordings/{stem}/title                  {"title"} rename the meeting only (folder and files follow)
  POST   /api/recordings/{stem}/process                {"force"?, "cloud"?} transcribe + export + summarize in the background
  POST   /api/recordings/{stem}/voices                 fast-track post-processing: local voices for a cloud transcript
  POST   /api/recordings/{stem}/recognize              match unnamed labels against the voice prints
  POST   /api/recordings/{stem}/summaries              {"provider", "model"} a summary with that model, in the background
  DELETE /api/recordings/{stem}/summaries/{file}       delete that summary (closing its tab)
  GET    /api/summary-models                           models for summaries: Ollama on this PC, Claude for the key
  POST   /api/recordings/{stem}/speakers/merge         fold the labels of one person into one speaker
  POST   /api/recordings/{stem}/speakers/unmerge       undo the merges that remember their origin
  DELETE /api/recordings/{stem}/speakers/{label}       drop that speaker's segments (noise turned into text)
  POST   /api/recordings/{stem}/segments/assign        {"segments": [{"start", "speaker", "from"?}]} move replies to a
                                                        speaker ("@new" = a new one, "UNKNOWN" = unassigned)
  GET    /api/recordings/{stem}/speakers/{label}/replies   all replies of one speaker
  POST   /api/recordings/{stem}/meeting                {"action": confirm|detach|attach, "candidate"?}
  GET    /api/recordings/{stem}/meeting/candidates     nearby Outlook items to link instead
  GET    /api/people / PUT /api/people                 the registry (PUT replaces it; opted-out people lose prints)
  POST   /api/people/merge                             {"keep", "drop", "stem"?}
  GET    /api/people/{id}                              one person with their voice prints
  DELETE /api/people/{id}/voiceprints?stem=&label=     one print, or all of them
  GET    /api/settings / PUT /api/settings             fields of the shared teamsrec.toml / {"values": {key: value}}
  PUT    /api/secrets/{name} / DELETE                  {"value"} an API key into / out of the Credential Manager
  POST   /api/jobs/pause                               stop the running job for a recording (it resumes after it)
  POST   /api/jobs/continue                            let it run during the recording
  POST   /api/jobs/{id}/next                           a waiting job runs right after the current one
  POST   /api/jobs/{id}/now                            ... or at once: the current one stops and runs again after it
  POST   /api/jobs/{id}/cancel                         drop a waiting job, or stop the running one for good
  GET    /api/recordings/{stem}/summaries/{file}/export   the folder remembered for this meeting name, the copy's name
  POST   /api/recordings/{stem}/summaries/{file}/export   {"folder"} save a copy there (and remember the folder)
  POST   /api/system/pick-folder                       {"initial"?} the Windows folder dialog -> {"folder"} ("" = cancel)
  POST   /api/system/sound-settings                    open the Windows sound dialog (devices, levels, the mic array)
  GET    /api/settings/models                          the model lists asked live (Ollama on this PC, Claude API)
  GET    /api/help/{doc}                               user-guide | install | privacy
  GET    /api/status                                   job state + recent events + what teamsrec-capture records
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
from ..pipeline import (NEW_SPEAKER, assign_segments, speaker_replies, do_export, do_process, do_summarize, do_summarize_compare, enroll_names,
                        summarize_as,
                        summary_path_for, load_segments, meeting_info,
                        merge_same_person, recognize_voices, remove_speaker, rename_recording,
                        same_person_groups, set_meeting_link, unmerge_speakers)
from ..voiceprints import Voiceprints, _unit, cosine, speech_seconds
from .. import exports, timings
from ..fasttrack import cloud_ready, needs_voices  # the emergency fast track, separate from the local path
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
CAPTURE_STATUS = Path(tempfile.gettempdir()) / "teamsrec-capture.json"  # written by teamsrec-capture on every change
CAPTURE_POLL_S = 2.0


RECORDING_ASK_S = 10.0  # with when_recording = ask: no answer within this -> stop (the sound is breaking meanwhile)


def run_cli(argv: list[str], on_proc, on_line) -> None:
    """Run `teamsrec-transcribe <argv>` as a child process (a job that can be stopped). on_proc(proc) gets the
    process as soon as it runs; on_line(text) every line it logs. Raises RuntimeError with its last message when it
    fails. Tests replace this function."""
    import subprocess
    import sys as _sys
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([_sys.executable, "-m", "teamsrec_transcribe.cli", *argv], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            creationflags=flags, env=env)
    on_proc(proc)
    last = ""
    for line in proc.stdout or []:
        line = line.rstrip()
        if line:
            last = line
            on_line(line)
    if proc.wait() != 0:
        raise RuntimeError(last or f"exit code {proc.returncode}")


def _kill_tree(proc) -> None:
    """Stop a job's process with everything it started (ffmpeg, ...). Ollama stops generating when the
    connection goes away."""
    try:
        if os.name == "nt":
            import subprocess
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=0x08000000)
        else:
            proc.kill()
    except Exception as e:  # already gone
        log.debug("kill job: %s", e)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def capture_status(path: Path = CAPTURE_STATUS) -> dict:
    """What teamsrec-capture is doing: {"running", "recording", "title", "stem", "source", "started"}. A file left
    by an app that is gone (crash, power loss) counts as not running."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False, "recording": False}
    if not d.get("running") or not _pid_alive(int(d.get("pid") or 0)):
        return {"running": False, "recording": False}
    return {k: d.get(k) for k in ("running", "recording", "title", "stem", "source", "started")}


def is_label(name: str | None) -> bool:
    return bool(name) and (name.startswith("SPEAKER_") or name == "UNKNOWN")


def unnamed_speaker(name: str | None) -> bool:
    """A diarization label without a name. Replies the diarization gave nobody ("UNKNOWN") are not a speaker to
    name: they are given out one by one, and do not keep a recording unfinished."""
    return bool(name) and name.startswith("SPEAKER_")


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


def _voice_hint(vp: Voiceprints, people: People, vec, unresolved: bool, secs: float, threshold: float,
                exclude_stem: str | None = None) -> dict | None:
    """The closest voice print for a still-unknown label that did not pass the automatic threshold, so the
    user can confirm it with one click instead of guessing."""
    if not (unresolved and vec and vp.people):
        return None
    ranked = vp.scores(vec, exclude_stem)
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


# Two voice groups of one meeting are worth a look from this similarity on. Calibrated 2026-10-05 on 25 meetings
# (group embeddings of the diarization): the same person split in two 0.25-0.71 (median 0.45), different people
# 0.07-0.54 (median 0.25) – so this is a hint to listen, never an automatic merge.
GROUP_HINT_MIN = 0.45


def _nearest_group(label: str, embeddings: dict, secs: dict) -> dict | None:
    """The voice group of this meeting that sounds most like `label`, when it is close enough to suggest."""
    vec = embeddings.get(label)
    if not vec:
        return None
    u = _unit(vec)
    if not u:
        return None
    best = max(((other, cosine(u, w)) for other, v in embeddings.items() if other != label and v and
                secs.get(other, 0.0) > 0 and (w := _unit(v))), key=lambda kv: kv[1], default=None)
    if best is None or best[1] < GROUP_HINT_MIN:
        return None
    return {"label": best[0], "score": round(best[1], 2)}


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
        return {"stem": rec.stem, "title": rec.title, "start": rec.sidecar.get("start"), "cloud": cloud_ready(cfg),
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
    group_secs = speech_seconds(data.get("segments") or [])  # the groups that still have replies
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
                                      cfg.voiceprints.threshold, rec.stem),
            "nearest": _nearest_group(label, embeddings, group_secs),
            "confirmed": bool(value) and bool(manual.get(label)) and label not in voice,
            "seconds": round(secs, 1),
            "count": len(ss),
            "unassigned": label == "UNKNOWN",
            # unassigned replies are given out one by one: all of them, not samples
            "replies": ([{"start": round(s.start, 3), "end": round(min(s.end, s.start + CLIP_MAX_S), 3),
                          "at": _fmt_hms(s.start), "text": s.text[:300]} for s in ss[:300]]
                        if label == "UNKNOWN" else []),
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
                             "languages": data.get("languages") or [],
                             # no word alignment: replies stay ~30 s long and can mix voices (2026-10-05)
                             "unaligned": is_unaligned(data)},
        "has_summary": has_minutes(rec), "has_mix": bool(rec.mix_path and rec.mix_path.exists()),
        "cloud": cloud_ready(cfg),
        "timings": timings.latest(rec),  # how long each part of the last jobs took
        "export_folder": exports.folder_for(cfg.out_dir, rec.title),  # "Uložit jako minule" for this meeting name
        "voices_missing": needs_voices(data),  # a fast-track transcript: voices can be added locally
        "voices_mixed": sorted(((data.get("voices_from") or {}).get("mixed") or {}).keys()),
        "speakers": speakers, "known_names": known_names(cfg, people),
        "same_person": [{"person": pid, "labels": labels, "name": (people.get(pid) or Person(pid)).full}
                        for pid, labels in same_person_groups(rec, people).items()],
        "can_unmerge": any(s.get("merged_from") for s in (data.get("segments") or [])),
        "people": people.to_json(), "display_default": people.default_mode,
        "meeting": meeting_info(cfg, rec),
        "docs": recording_docs(rec),
    }


SUMMARY_STAMP = re.compile(r"<!--\s*teamsrec-transcribe summary \| (?P<provider>[^:|]+):\s*(?P<model>[^|]+?)\s*(?:\|[^>]*?created:\s*(?P<created>[^|>]+?))?\s*(?:\||-->)")


def is_unaligned(data: dict) -> bool:
    """A local transcript without word alignment: replies ~30 s long that can mix voices (2026-10-05) – worth
    transcribing again. Cloud transcripts bring their own word times."""
    return data.get("provider") == "whisperx" and "align_s" not in (data.get("timings") or {})


def has_minutes(rec: Recording) -> bool:
    """Any minutes at all: the main one or one by another model (the list must not say "bez zápisu" when the only
    minutes are a Claude one)."""
    return rec.summary_path.exists() or any(rec.dir.glob(f"{rec.stem}.summary.*.md"))


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
    # two tabs of the same model (the main minutes and a comparison by it): the main one says so, so a close does
    # not delete the wrong one
    for s in summaries:
        if s["main"] and any(o is not s and o["label"] == s["label"] for o in summaries):
            s["label"] = f"{s['label']} · hlavní"
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
        unaligned = False
        if r.transcript_path.exists():
            try:
                names = r.read_json(r.speakers_path) if r.speakers_path.exists() else {}
                data = r.read_json(r.transcript_path)
                labels = data.get("speakers", [])
                unresolved = sum(1 for l in labels if unnamed_speaker(l) and not names.get(l))
                unaligned = is_unaligned(data)
            except Exception:
                unresolved = None
        out.append({"stem": r.stem, "title": r.title, "start": r.sidecar.get("start"),
                    "transcribed": r.transcript_path.exists(), "unresolved": unresolved,
                    "summary": has_minutes(r), "unaligned": unaligned})
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
        self.capture: dict = {"running": False, "recording": False}  # teamsrec-capture, from _capture_watch
        self.lock = threading.RLock()
        self.busy = False
        self.message = ""
        self.error = ""
        self.last_seen = 0.0  # time of the last request from the page (0 = no page yet)
        self.events: list[dict] = []
        self.seq = 0
        self.subscribers: list[queue.Queue] = []  # one per open /api/events stream
        self.jobs: list[dict] = []        # waiting background jobs, in order
        self.current: dict | None = None  # the job running now (id, name, stem, text, started)
        self.running: dict | None = None  # ... the whole job (its process, to stop it)
        self.next_job = 0
        self.held = False    # a recording runs: jobs wait (the GPU would break the recorded sound)
        self.asking = False  # the pages are asked whether to stop the running job for the recording
        self.wake = threading.Condition(self.lock)

    def seen(self) -> None:
        self.last_seen = time.monotonic()

    def event(self, text: str, level: str = "ok", stem: str = "", reload: bool = False, job_end: bool = False,
              job: int | None = None) -> int:
        """One line of history: what the server did, for the status bar and the list behind it. A long job is
        started and forgotten, so the page has to be able to ask later what happened. job_end marks the line that
        closes a background job; `job` is its id (the page waiting for that job wakes on it)."""
        with self.lock:
            self.seq += 1
            e = {"n": self.seq, "at": datetime.now().strftime("%H:%M:%S"), "text": text,
                 "level": level, "stem": stem, "reload": reload, "busy": self.busy, "job_end": job_end, "job": job}
            self.events.append(e)
            del self.events[:-MAX_EVENTS]
            for q in self.subscribers:
                q.put(e)
        log.info("%s", text)
        return self.seq

    def set_capture(self, status: dict) -> None:
        """The capture app's state changed: tell the pages (SSE `capture`), and log the start and end."""
        with self.lock:
            before, self.capture = getattr(self, "capture", {"recording": False}), status
            subscribers = list(self.subscribers)
        for q in subscribers:
            q.put({"type": "capture", **status})
        if status.get("recording") and not before.get("recording"):
            self.event(f"nahrávání probíhá: {status.get('title') or status.get('stem')}", "info")
            self._recording_started()
        elif before.get("recording") and not status.get("recording"):
            # the new recording appears in the list once its sidecar is written: reload
            self.event(f"nahrávání skončilo: {before.get('title') or before.get('stem')}", "info",
                       before.get("stem") or "", reload=True)
            self._recording_ended()

    # ---- a recording must not be broken by processing
    def _recording_started(self) -> None:
        with self.lock:
            working = bool(self.current or self.jobs)
            mode = self.cfg.transcribe.when_recording
            pages = bool(self.subscribers)
        if mode == "continue":
            if working:
                self.event("zpracování běží dál i během nahrávání (nastavení: nechat běžet)", "info")
            return
        if not working:  # nothing to stop, but what is asked for during the recording waits for its end
            with self.lock:
                self.held = True
            self._push_jobs()
            return
        if mode == "ask" and pages:
            with self.lock:
                self.asking = True
            self._push_jobs()

            def timeout():
                time.sleep(RECORDING_ASK_S)
                if self.asking and self.capture.get("recording"):
                    self.pause_for_recording("bez odpovědi do %d s" % RECORDING_ASK_S)
            threading.Thread(target=timeout, daemon=True).start()
            return
        self.pause_for_recording("nastavení: hned přerušit" if mode == "stop" else "žádná stránka není otevřená")

    def pause_for_recording(self, why: str = "") -> None:
        """Stop the running job (it goes back to the front of the queue) and let nothing start until the recording
        ends. A job that runs in this process (tests) cannot be stopped: it finishes, the queue waits."""
        with self.lock:
            self.held, self.asking = True, False
            job = self.running
            if job and job.get("proc"):
                job["stopped"] = True
        if job and job.get("proc"):
            _kill_tree(job["proc"])
        # Ollama keeps the summary model in VRAM for minutes after the last request (2026-10-02: 21 GB held
        # through a call); let the GPU go to the meeting as well
        threading.Thread(target=_unload_ollama, args=(self.cfg,), daemon=True).start()
        self.event("zpracování přerušeno kvůli nahrávání" + (f" ({why})" if why else "")
                   + ", doběhne samo po jeho konci", "info")
        self._push_jobs()

    def move_job(self, job_id: int, now: bool = False) -> str:
        """Move a waiting job to the front of the queue. now: also stop the running job; it goes back right behind
        the moved one and starts again from the beginning (what it had done so far is lost). Returns what happened,
        for the page."""
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if job is None:
                raise ValueError(f"job {job_id} is not waiting (it runs, finished, or never was)")
            self.jobs.remove(job)
            self.jobs.insert(0, job)
            run = self.running
            stoppable = bool(now and run and run.get("proc") and not run.get("stopped"))
            if stoppable:
                run["stopped"] = "preempted"
            current = run["stem"] if run else ""
        if stoppable:
            _kill_tree(run["proc"])
            text = f"{job['stem'] or job['name']}: zpracuje se hned, {current} se přerušilo a pokračuje po něm od začátku"
        elif now and run:
            text = f"{job['stem'] or job['name']}: zpracuje se hned po {current} (to se zastavit nedá)"
        else:
            text = f"{job['stem'] or job['name']}: zpracuje se jako další"
        self.event(text, "info", job["stem"], job=job["id"])
        self._push_jobs()
        return text

    def cancel_job(self, job_id: int) -> str:
        """Drop a waiting job, or stop the running one for good (what it did so far stays as it is, half done; the
        recording can be processed again). Returns what happened, for the page."""
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            if job is not None:
                self.jobs.remove(job)
            run = self.running if self.running and self.running["id"] == job_id else None
            if job is None and run is None:
                raise ValueError(f"job {job_id} is neither waiting nor running")
            if run is not None:
                if not run.get("proc"):
                    raise ValueError("this job cannot be stopped, it finishes by itself")
                run["stopped"] = "cancelled"
        if run is not None:
            _kill_tree(run["proc"])  # the worker notes the end and goes on with the queue
            self._push_jobs()
            return f"{run['stem'] or run['name']}: zpracování se ruší"
        text = f"{job['stem'] or job['name']}: zpracování zrušeno, vyřazeno z fronty"
        self.event(text, "info", job["stem"], reload=True, job_end=True, job=job["id"])
        self._push_jobs()
        return text

    def keep_running(self) -> None:
        with self.lock:
            self.asking = False
        self.event("zpracování běží dál i během nahrávání (rozhodnuto na stránce)", "info")
        self._push_jobs()

    def _recording_ended(self) -> None:
        with self.lock:
            was_held, self.held, self.asking = self.held, False, False
            waiting = len(self.jobs)
            self.wake.notify_all()
        if was_held and waiting:
            self.event(f"nahrávání skončilo, zpracování pokračuje ({waiting} ve frontě)", "info")
        self._push_jobs()

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

    def jobs_state(self) -> dict:
        """What runs and what waits, for every page (SSE `jobs`, hello, /api/status)."""
        with self.lock:
            return {"current": dict(self.current) if self.current else None,
                    "queue": [{**{k: j[k] for k in ("id", "name", "stem", "text")}, "force": j.get("force", False)}
                              for j in self.jobs],
                    "held": self.held, "asking": self.asking, "ask_s": RECORDING_ASK_S}

    def _push_jobs(self) -> None:
        state = self.jobs_state()
        with self.lock:
            subscribers = list(self.subscribers)
        for q in subscribers:
            q.put({"type": "jobs", **state})

    def run_job(self, name: str, fn, done: str, stem: str = "", start: str = "") -> tuple[int, int]:
        """Queue fn() as a background job. One runs at a time (the GPU does one thing), in order; a request while
        another runs waits instead of being refused. Returns (job id, position: 1 = runs now, 2 = next ...).
        Its start and end are events carrying the id (the end with job_end), and every change of what runs or
        waits goes to the pages as SSE `jobs`."""
        with self.lock:
            self.next_job += 1
            job = {"id": self.next_job, "name": name, "stem": stem, "text": start or name, "fn": fn, "done": done,
                   "force": isinstance(fn, list) and "--force" in fn}
            if isinstance(fn, list):  # a command: run in a child process that can be stopped
                job["argv"], job["fn"] = fn, None
            self.jobs.append(job)
            position = len(self.jobs) + (1 if self.current else 0)
            starts_worker = not self.busy
            self.busy = True
            held = self.held
        if held:
            self.event(f"{job['text']} – čeká na konec nahrávání" + (f" (před ní {position - 1})" if position > 1 else ""),
                       "info", stem, job=job["id"])
        elif not starts_worker:
            self.event(f"{job['text']} – ve frontě (před ní {position - 1})", "info", stem, job=job["id"])
        self._push_jobs()
        if starts_worker:
            threading.Thread(target=self._work, daemon=True).start()
        return job["id"], position

    def _work(self) -> None:
        """The one worker: runs the queued jobs one after another until the queue is empty."""
        while True:
            with self.lock:
                while self.held and self.jobs:  # a recording runs: wait for its end
                    self.wake.wait(timeout=5)
                if not self.jobs:
                    self.busy, self.current = False, None
                    break
                job = self.jobs.pop(0)
                self.running = job
                self.current = {"id": job["id"], "name": job["name"], "stem": job["stem"], "text": job["text"],
                                "force": job.get("force", False), "started": datetime.now().strftime("%H:%M"),
                                "started_at": datetime.now().isoformat(timespec="seconds")}
                self.message, self.error, self.job = job["name"], "", job["name"]
            # logged before the work, so the order in the log is the real one
            self.event(job["text"], "busy", job["stem"], job=job["id"])
            self._push_jobs()
            stem, done, name = job["stem"], job["done"], job["name"]
            outcome = (f"{done}: {stem}" if stem else done, "ok")
            errors = self._job_errors(stem)
            try:
                if job.get("argv"):
                    self._run_command(job)
                else:
                    job["fn"]()
                with self.lock:
                    self.message = done
            except Exception as e:  # shown on the page, not fatal
                with self.lock:
                    self.error = str(e)
                outcome = (f"{name} selhalo: {e}", "err")
            finally:
                logging.getLogger("teamsrec_transcribe").removeHandler(errors)
                with self.lock:
                    self.current = self.running = None
                    stopped = job.pop("stopped", False)
                    job.pop("proc", None)
                    if stopped == "cancelled":  # the user dropped it: not back into the queue
                        pass
                    elif stopped == "preempted":  # another job goes first, this one right behind it
                        self.jobs.insert(1 if self.jobs else 0, job)
                    elif stopped:  # stopped for a recording: back to the front, it runs again after it
                        self.jobs.insert(0, job)
                    if not self.jobs:
                        self.busy = False  # before the end event: the page sees it idle
            if stopped == "cancelled":
                self.event(f"{stem or name}: zpracování zrušeno (zastaveno během běhu)", "info", stem, reload=True,
                           job_end=True, job=job["id"])
            elif stopped == "preempted":
                self.event(f"{job['text']} – přerušeno kvůli přednostnímu zpracování, poběží znovu po něm", "info",
                           stem, job=job["id"])
            elif stopped:
                self.event(f"{job['text']} – přerušeno, pokračuje po konci nahrávání", "info", stem, job=job["id"])
            else:
                self.event(outcome[0], outcome[1], stem, reload=True, job_end=True, job=job["id"])
            self._push_jobs()

    def _run_command(self, job: dict) -> None:
        """A job's command in a child process; what it logs as an error goes to the history."""
        args = list(job["argv"])
        if self.cfg.source_path or self.settings_path.exists():
            args = ["--config", str(self.cfg.source_path or self.settings_path)] + args
        args = ["--out-dir", str(self.cfg.out_dir)] + args

        def on_proc(proc):
            with self.lock:
                job["proc"] = proc
                stop_now = self.held  # a recording started while it was being launched
            if stop_now:
                job["stopped"] = True
                _kill_tree(proc)

        def on_line(line: str):
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[1] == "ERROR":
                self.event(parts[2], "err", job["stem"])
        try:
            run_cli(args, on_proc, on_line)
        except RuntimeError:
            if job.get("stopped"):
                return  # stopped for a recording, not a failure
            raise

    def run_summary(self, rec: Recording) -> tuple[int, int]:
        # in a child process (it can be stopped for a recording); the compare summaries follow the new names too
        return self.run_job("summary", ["run-job", "summary", rec.stem], "zápis přegenerován", rec.stem,
                            start=f"{rec.stem}: zápis se generuje"
                     + (f" (i srovnávací: {', '.join(self.cfg.summarize.compare)})" if self.cfg.summarize.compare else ""))

    def run_voices(self, rec: Recording) -> tuple[int, int]:
        """Fast-track post-processing: local voices for a cloud transcript (fasttrack.add_voices), on the GPU."""
        return self.run_job("voices", ["run-job", "voices", rec.stem], "hlasy doplněny", rec.stem,
                            start=f"{rec.stem}: doplňují se hlasy (lokální diarizace)")

    def run_process(self, rec: Recording, force: bool = False, cloud: bool = False) -> tuple[int, int]:
        """force = transcribe again from scratch (new diarization, new labels), so the manual names go first:
        they are keyed by labels that will not exist any more, and a wrong one must not come back."""
        # from scratch drops the manual names and the cached window analysis when the job starts (run-job), so a
        # recording waiting in the queue keeps everything and can be cancelled without loss
        if cloud and not cloud_ready(self.cfg)["ok"]:
            raise ValueError("rychle přes cloud potřebuje klíče: " + cloud_ready(self.cfg)["missing"])
        what = ("nový přepis od nuly" if force else "zpracování") + (" (cloud)" if cloud else "")
        return self.run_job("process", ["run-job", "process", rec.stem] + (["--force"] if force else [])
                            + (["--cloud"] if cloud else []),
                            "zpracováno", rec.stem, start=f"{rec.stem}: {what} spuštěno")

    def status(self, events: int = 40) -> dict:
        with self.lock:
            return {"app": "teamsrec-review", "out_dir": str(self.cfg.out_dir), "job": getattr(self, "job", ""),
                    "busy": self.busy, "message": self.message, "error": self.error,
                    "seq": self.seq, "events": self.events[-events:], "capture": self.capture,
                    "jobs": self.jobs_state()}


STEM_RE_PART = r"(?P<stem>[^/]+)"
ROUTES = [  # (method, path pattern, handler method) - keep web/openapi.py in step
    ("GET", r"/", "page"),
    ("GET", r"/api/recordings", "recordings"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}", "recording"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/clip", "clip"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/docs/(?P<file>[^/]+)", "doc"),
    ("PUT", rf"/api/recordings/{STEM_RE_PART}/names", "save_names"),
    ("PUT", rf"/api/recordings/{STEM_RE_PART}/title", "save_title"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/process", "process"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/voices", "voices"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/recognize", "recognize"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/summaries", "summarize_as"),
    ("DELETE", rf"/api/recordings/{STEM_RE_PART}/summaries/(?P<file>[^/]+)", "delete_summary"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/summaries/(?P<file>[^/]+)/export", "export_info"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/summaries/(?P<file>[^/]+)/export", "export_summary"),
    ("POST", r"/api/system/pick-folder", "pick_folder"),
    ("GET", r"/api/summary-models", "summary_models"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/speakers/merge", "merge"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/speakers/unmerge", "unmerge"),
    ("DELETE", rf"/api/recordings/{STEM_RE_PART}/speakers/(?P<label>[^/]+)", "remove_speaker"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/speakers/(?P<label>[^/]+)/replies", "speaker_replies"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/segments/assign", "assign_segments"),
    ("POST", rf"/api/recordings/{STEM_RE_PART}/meeting", "meeting"),
    ("GET", rf"/api/recordings/{STEM_RE_PART}/meeting/candidates", "meeting_candidates"),
    ("GET", r"/api/people", "people"),
    ("PUT", r"/api/people", "save_people"),
    ("POST", r"/api/people/merge", "merge_people"),
    ("GET", r"/api/people/(?P<pid>[^/]+)", "person"),
    ("DELETE", r"/api/people/(?P<pid>[^/]+)/voiceprints", "forget_prints"),
    ("GET", r"/api/settings", "settings"),
    ("PUT", r"/api/settings", "save_settings"),
    ("GET", r"/api/settings/models", "settings_models"),
    ("PUT", r"/api/secrets/(?P<name>[^/]+)", "set_secret"),
    ("DELETE", r"/api/secrets/(?P<name>[^/]+)", "delete_secret"),
    ("POST", r"/api/system/sound-settings", "sound_settings"),
    ("POST", r"/api/jobs/pause", "jobs_pause"),
    ("POST", r"/api/jobs/continue", "jobs_continue"),
    ("POST", r"/api/jobs/(?P<job>[^/]+)/next", "job_next"),
    ("POST", r"/api/jobs/(?P<job>[^/]+)/now", "job_now"),
    ("POST", r"/api/jobs/(?P<job>[^/]+)/cancel", "job_cancel"),
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
            if not rec.transcript_path.exists():  # right after recording: only the title can be set yet
                state.event(f"{rec.stem}: název uložen („{rec.title}“)", "ok", rec.stem, reload=True)
                self._json({"ok": True, "written": {}, "stem": rec.stem, "title": rec.title, "status": state.status()})
                return
            written = save_names(state.cfg, rec, body.get("names") or {})
            state.event(f"{rec.stem}: uloženo {len(written)} jmen, přepis a titulky přegenerovány", "ok", rec.stem)
            job = state.run_summary(rec) if body.get("summary") else None
            self._json({"ok": True, "written": written, "stem": rec.stem, "title": rec.title,
                        "job": job[0] if job else None, "position": job[1] if job else None,
                        "status": state.status()})

        def r_save_title(self, q, body, stem):
            title = str(body.get("title") or "").strip()
            if not title:
                raise ValueError("title: an empty title is not a title")
            rec = save_title(state.cfg, self._recording(stem), title)
            state.event(f"{rec.stem}: název uložen („{rec.title}“)", "ok", rec.stem)
            self._json({"ok": True, "stem": rec.stem, "title": rec.title})

        def r_voices(self, q, body, stem):
            rec = self._recording(stem)
            if not rec.transcript_path.exists() or not needs_voices(rec.read_json(rec.transcript_path)):
                raise ValueError("hlasy se doplňují jen k přepisu z rychlé cesty přes cloud, který je ještě nemá")
            job, position = state.run_voices(rec)
            self._json({"ok": True, "job": job, "position": position, "status": state.status()})

        def r_process(self, q, body, stem):
            job, position = state.run_process(self._recording(stem), force=bool(body.get("force")),
                                              cloud=bool(body.get("cloud")))
            self._json({"ok": True, "job": job, "position": position, "status": state.status()})

        def r_speaker_replies(self, q, body, stem, label):
            self._json({"label": label, "replies": speaker_replies(self._recording(stem), unquote(label)),
                        "new_speaker": NEW_SPEAKER})

        def r_assign_segments(self, q, body, stem):
            rec = self._recording(stem)
            moves = body.get("segments")
            if not isinstance(moves, list) or not moves:
                raise ValueError("segments: [{start, speaker, from?}] needed")
            n = assign_segments(state.cfg, rec, moves)
            verb = "přesunuto" if any(isinstance(m, dict) and m.get("from") for m in moves) else "přiřazeno"
            state.event(f"{rec.stem}: {verb} {n} replik, přepis a titulky přegenerovány", "ok", rec.stem, reload=True)
            self._json({"ok": True, "assigned": n})

        def r_summarize_as(self, q, body, stem):
            rec = self._recording(stem)
            provider, model = str(body.get("provider") or "").strip(), str(body.get("model") or "").strip()
            if provider not in ("ollama", "anthropic") or not model:
                raise ValueError("provider (ollama | anthropic) and model are needed")
            job, position = state.run_job("summary", ["run-job", "summarize-as", rec.stem, "--provider", provider,
                                                      "--model", model],
                                          f"zápis {model} hotový", rec.stem, start=f"{rec.stem}: zápis {model} se generuje")
            self._json({"ok": True, "job": job, "position": position,
                        "file": summary_path_for(state.cfg, rec, provider, model).name})

        def r_delete_summary(self, q, body, stem, file):
            rec = self._recording(stem)
            delete_summary(rec, file)
            state.event(f"{rec.stem}: zápis {file} smazán", "info", rec.stem, reload=True)
            self._json({"ok": True})

        def r_summary_models(self, q, body):
            live = (q.get("live") or [""])[0] in ("1", "true")
            sug = settings.suggestions(settings.read_values(state.settings_path), live=live)
            self._json({**sug["summary"], "loaded": sug["models_loaded"],
                        "default": {"provider": state.cfg.summarize.provider, "model": state.cfg.summarize.model}})

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

        def r_settings_models(self, q, body):
            sug = settings.suggestions(settings.read_values(state.settings_path), live=True)
            self._json({"suggestions": sug})

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

        def r_jobs_pause(self, q, body):
            state.pause_for_recording("rozhodnuto na stránce")
            self._json({"ok": True, "jobs": state.jobs_state()})

        def r_jobs_continue(self, q, body):
            state.keep_running()
            self._json({"ok": True, "jobs": state.jobs_state()})

        def r_job_next(self, q, body, job):
            self._json({"ok": True, "text": state.move_job(_job_id(job)), "jobs": state.jobs_state()})

        def r_job_now(self, q, body, job):
            self._json({"ok": True, "text": state.move_job(_job_id(job), now=True), "jobs": state.jobs_state()})

        def r_job_cancel(self, q, body, job):
            self._json({"ok": True, "text": state.cancel_job(_job_id(job)), "jobs": state.jobs_state()})

        def r_export_info(self, q, body, stem, file):
            rec = self._recording(stem)
            path = rec.dir / unquote(file)
            self._json({"folder": exports.folder_for(state.cfg.out_dir, rec.title),
                        "name": exports.copy_name(rec, path)})

        def r_export_summary(self, q, body, stem, file):
            rec = self._recording(stem)
            target = exports.save_copy(rec, unquote(file), str(body.get("folder") or ""), state.cfg.out_dir)
            state.event(f"{rec.stem}: kopie zápisu uložena do {target}", "ok", rec.stem)
            self._json({"ok": True, "path": str(target), "folder": str(target.parent)})

        def r_pick_folder(self, q, body):
            self._json({"folder": exports.pick_folder(str(body.get("initial") or ""))})

        def r_sound_settings(self, q, body):
            if os.name != "nt":
                raise ValueError("the sound dialog is a Windows thing")
            import subprocess
            subprocess.Popen(["control", "mmsys.cpl"])  # the classic dialog: recording devices, levels, enable/disable
            self._json({"ok": True})

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
            if state.status()["busy"]:  # running or waiting jobs
                self._json({"error": "a job is still running or waiting, wait for it to finish"}, HTTPStatus.CONFLICT)
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
                self._sse("hello", {"seq": state.seq, "busy": state.busy, "replay": since is None,
                                    "capture": state.capture, "jobs": state.jobs_state()})
                for e in state.events_since(since):
                    self._sse("log", {**e, "replay": since is None}, e["n"])
                while not getattr(server_ref.get("server"), "_teamsrec_stopping", False):
                    try:
                        e = sub.get(timeout=SSE_KEEPALIVE_S)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if e.get("type") in ("capture", "jobs"):  # states, not history (no id: not replayed)
                        self._sse(e["type"], {k: v for k, v in e.items() if k != "type"})
                    else:
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


def _job_id(text: str) -> int:
    if not text.isdigit():
        raise ValueError(f"not a job id: {text}")
    return int(text)


def _unload_ollama(cfg: Config) -> None:
    """Ask Ollama to drop the configured summary model from memory now (keep_alive 0). Quiet when Ollama is not
    used or not running."""
    sm = cfg.summarize
    if sm.provider != "ollama" and not any(c.startswith("ollama:") for c in sm.compare):
        return
    body = json.dumps({"model": sm.model, "keep_alive": 0}).encode("utf-8")
    req = urllib.request.Request(sm.ollama_url.rstrip("/") + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        log.info("ollama: %s unloaded for the recording", sm.model)
    except Exception as e:  # not running, another model: nothing to free
        log.debug("ollama unload: %s", e)


def _idle_watchdog(state: ReviewState, server: ThreadingHTTPServer, idle_s: float) -> None:
    """Stop the server once the page has gone quiet (tab closed). Never while a job runs, and never while a page
    holds its event stream open: a hidden or minimized window throttles its timers, so its pings may stop for
    minutes (2026-10-02: the desktop window lost its server and did not show new recordings). A stream whose page
    is gone fails on the next keepalive and unsubscribes."""
    while True:
        time.sleep(min(5.0, idle_s / 3))
        with state.lock:
            if state.subscribers:
                state.last_seen = time.monotonic()
        if state.last_seen and not state.busy and time.monotonic() - state.last_seen > idle_s:
            log.info("review page closed, stopping")
            threading.Thread(target=server.shutdown, daemon=True).start()
            return


def _capture_watch(state: "ReviewState", path: Path = CAPTURE_STATUS, poll_s: float = CAPTURE_POLL_S,
                   stop: threading.Event | None = None) -> None:
    """Poll the capture app's status file and push changes (a file, not a socket: the capture app needs no server)."""
    last = None
    while not (stop and stop.is_set()):
        now = capture_status(path)
        if now != last:
            state.set_capture(now)
            last = now
        time.sleep(poll_s)


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
    threading.Thread(target=_capture_watch, args=(state,), daemon=True).start()
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
    return [l for l in rec.read_json(rec.transcript_path).get("speakers", []) if unnamed_speaker(l) and not names.get(l)]
