"""Local review page: name the speakers of a recording by ear.

A tiny HTTP server on 127.0.0.1 serves one HTML page (`index.html`, vanilla JS) and a JSON API. Nothing leaves
the machine. The page reads the same files the CLI uses and writes only `<stem>.speakers.json`, then
regenerates the exports (and, on request, the summary). The API is the boundary: a native client could call
it later without touching this module's data functions.

API
  GET  /                         the page
  GET  /api/recordings           recent recordings with their unresolved-speaker count
  GET  /api/recording?stem=      everything the page needs for one recording
  GET  /api/clip?stem=&start=&end=   a WAV clip cut from the 16 kHz mix (max CLIP_MAX_S seconds)
  POST /api/save                 {"stem", "names": {label: name}, "summary": bool, "title"?}; a name is either a string
                                 ("Petr Svoboda") or {"first", "last", "nick", "display"}. speakers.json holds
                                 person ids (people are created or updated from the fields), exports regenerated
  GET  /api/people               the people registry (_speakers/people.json) with voice-print counts
  POST /api/people               {"people": [...], "stem"?} -> replace the registry, re-export that recording
  POST /api/people/merge         {"keep", "drop", "stem"?} -> fold one person into another everywhere
  GET  /api/person?id=           one person with their voice prints (recording, label, when, sample to play)
  POST /api/person/forget        {"id", "stem"?, "label"?} -> drop one print (stem+label) or all prints of the person
  POST /api/speaker/remove       {"stem", "label"} -> drop that speaker's segments (noise turned into text)
  POST /api/process              {"stem"} -> transcribe + export + summarize in the background (status polls it)
  GET  /api/status               background job (summary) state
  POST /api/ping                 heartbeat from the page; the server exits IDLE_S after the last one
  POST /api/quit                 stop the server

One server per machine: the running instance is recorded in a lock file in the temp folder, a second `review`
just opens the existing page. Closing the browser tab ends the heartbeat, so the server (and its console window)
goes away by itself.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.request
import wave
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..people import DISPLAY_MODES, People, Person
from ..pipeline import (do_export, do_process, do_summarize, enroll_names, load_segments, remove_speaker,
                        rename_recording)
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


def _fmt_hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


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
    by: dict[str, list[Segment]] = {}
    for s in segs:
        by.setdefault(s.speaker or "UNKNOWN", []).append(s)
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
            "seconds": round(secs, 1),
            "count": len(ss),
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
        "has_summary": rec.summary_path.exists(), "has_mix": bool(rec.mix_path and rec.mix_path.exists()),
        "speakers": speakers, "known_names": known_names(cfg, people),
        "people": people.to_json(), "display_default": people.default_mode,
    }


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
                    "transcribed": r.transcript_path.exists(), "unresolved": unresolved})
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
    enroll_names(cfg, rec, clean)
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


def save_people(cfg: Config, rows: list[dict], stem: str | None = None) -> list[dict]:
    people = People.load(cfg.out_dir, cfg.people_display)
    people.replace_all(rows)
    people.save()
    if stem:
        rec = resolve_recording(stem, cfg.out_dir)
        if rec.transcript_path.exists():
            do_export(cfg, rec)
    return people.to_json()


# ---------------------------------------------------------------- server

class ReviewState:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.busy = False
        self.message = ""
        self.error = ""
        self.last_seen = 0.0  # time of the last request from the page (0 = no page yet)

    def seen(self) -> None:
        self.last_seen = time.monotonic()

    def run_job(self, name: str, fn, done: str) -> bool:
        """Run fn() in a background thread; the page polls /api/status. One job at a time."""
        with self.lock:
            if self.busy:
                return False
            self.busy, self.message, self.error, self.job = True, name, "", name

        def work():
            try:
                fn()
                with self.lock:
                    self.message = done
            except Exception as e:  # shown on the page, not fatal
                with self.lock:
                    self.error = str(e)
            finally:
                with self.lock:
                    self.busy = False
        threading.Thread(target=work, daemon=True).start()
        return True

    def run_summary(self, rec: Recording) -> None:
        self.run_job("summary", lambda: do_summarize(self.cfg, rec, force=True), "summary regenerated")

    def run_process(self, rec: Recording) -> bool:
        return self.run_job("process", lambda: do_process(self.cfg, rec), "processed")

    def status(self) -> dict:
        with self.lock:
            return {"app": "teamsrec-review", "out_dir": str(self.cfg.out_dir), "job": getattr(self, "job", ""),
                    "busy": self.busy, "message": self.message, "error": self.error}


def _handler(state: ReviewState, server_ref: dict):
    class Handler(BaseHTTPRequestHandler):
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

        def _rec(self, q) -> Recording:
            stem = (q.get("stem") or [""])[0]
            if not stem:
                raise RecordingError("missing stem")
            return resolve_recording(stem, state.cfg.out_dir)

        # ---- routes
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            state.seen()
            try:
                if u.path == "/":
                    self._bytes(PAGE.read_bytes(), "text/html; charset=utf-8")
                elif u.path == "/api/recordings":
                    self._json(list_recordings(state.cfg))
                elif u.path == "/api/recording":
                    self._json(build_review(state.cfg, self._rec(q)))
                elif u.path == "/api/clip":
                    rec = self._rec(q)
                    if not rec.mix_path or not rec.mix_path.exists():
                        raise RecordingError("no mix audio")
                    start = float(q.get("start", ["0"])[0])
                    end = min(float(q.get("end", ["0"])[0]), start + CLIP_MAX_S)
                    self._bytes(clip_wav(rec.mix_path, start, end), "audio/wav")
                elif u.path == "/api/people":
                    self._json(people_rows(state.cfg))
                elif u.path == "/api/person":
                    self._json(person_detail(state.cfg, (q.get("id") or [""])[0]))
                elif u.path == "/api/status":
                    self._json(state.status())
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (RecordingError, ValueError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

        def do_POST(self):
            u = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            state.seen()
            try:
                if u.path == "/api/ping":
                    self._json({"ok": True})
                    return
                if u.path == "/api/save":
                    rec = resolve_recording(body.get("stem", ""), state.cfg.out_dir)
                    if body.get("title") is not None:
                        rec = save_title(state.cfg, rec, str(body.get("title")))
                    written = save_names(state.cfg, rec, body.get("names") or {})
                    if body.get("summary"):
                        state.run_summary(rec)
                    self._json({"ok": True, "written": written, "stem": rec.stem, "title": rec.title,
                                "status": state.status()})
                elif u.path == "/api/people":
                    rows = save_people(state.cfg, body.get("people") or [], body.get("stem") or None)
                    self._json({"ok": True, "people": rows})
                elif u.path == "/api/process":
                    rec = resolve_recording(body.get("stem", ""), state.cfg.out_dir)
                    started = state.run_process(rec)
                    self._json({"ok": started, "status": state.status(),
                                **({} if started else {"error": "another job is still running"})})
                elif u.path == "/api/speaker/remove":
                    rec = resolve_recording(body.get("stem", ""), state.cfg.out_dir)
                    n = remove_speaker(state.cfg, rec, str(body.get("label") or ""))
                    self._json({"ok": True, "removed": n})
                elif u.path == "/api/person/forget":
                    n = forget_print(state.cfg, str(body.get("id") or ""), body.get("stem"), body.get("label"))
                    self._json({"ok": True, "removed": n})
                elif u.path == "/api/people/merge":
                    rows = merge_people(state.cfg, str(body.get("keep") or ""), str(body.get("drop") or ""),
                                        body.get("stem") or None)
                    self._json({"ok": True, "people": rows})
                elif u.path == "/api/quit":
                    if state.status()["busy"]:
                        self._json({"error": "the summary is still being generated, wait for it to finish"},
                                   HTTPStatus.CONFLICT)
                        return
                    self._json({"ok": True})
                    threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (RecordingError, ValueError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
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
