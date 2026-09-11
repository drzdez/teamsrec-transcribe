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
  POST /api/save                 {"stem", "names": {label: name}, "summary": bool} -> writes speakers.json, exports
  GET  /api/status               background job (summary) state
  POST /api/quit                 stop the server
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import wave
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..pipeline import do_export, do_summarize, load_segments
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


def known_names(cfg: Config) -> list[str]:
    """Names to suggest: the user, everyone named in any speakers.json, participants of any recording."""
    seen: dict[str, None] = {}
    if cfg.user_name:
        seen[cfg.user_name] = None
    for r in iter_recordings(cfg.out_dir):
        if r.speakers_path.exists():
            try:
                for v in r.read_json(r.speakers_path).values():
                    if v and not is_label(v):
                        seen[str(v)] = None
            except Exception:
                pass
        for p in r.participants:
            seen[p] = None
    return sorted(seen, key=str.casefold)


def _fmt_hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def build_review(cfg: Config, rec: Recording) -> dict:
    if not rec.transcript_path.exists():
        raise RecordingError(f"{rec.stem}: no transcript yet")
    data, segs = load_segments(rec)  # raw provider speakers, without speakers.json applied
    manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    hints = summary_hints(rec)
    by: dict[str, list[Segment]] = {}
    for s in segs:
        by.setdefault(s.speaker or "UNKNOWN", []).append(s)
    speakers = []
    for label, ss in sorted(by.items(), key=lambda kv: -sum(s.end - s.start for s in kv[1])):
        secs = sum(s.end - s.start for s in ss)
        excerpts = sorted(ss, key=lambda s: -len(s.text))[:EXCERPTS_PER_SPEAKER]
        speakers.append({
            "label": label,
            "unresolved": is_label(label),
            "name": manual.get(label, "" if is_label(label) else label),
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
        "duration_s": rec.sidecar.get("duration_s"), "source": rec.source,
        "language": data.get("language"), "speaker_sources": data.get("speaker_sources", []),
        "has_summary": rec.summary_path.exists(), "has_mix": bool(rec.mix_path and rec.mix_path.exists()),
        "speakers": speakers, "known_names": known_names(cfg),
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

def save_names(cfg: Config, rec: Recording, names: dict[str, str]) -> dict[str, str]:
    """Write speakers.json (only real renames), regenerate txt/srt. Returns what was written."""
    clean: dict[str, str] = {}
    for label, name in names.items():
        name = (name or "").strip()
        if name and name != label:
            clean[label] = name
    rec.write_json(rec.speakers_path, clean)
    do_export(cfg, rec)
    return clean


# ---------------------------------------------------------------- server

class ReviewState:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.busy = False
        self.message = ""
        self.error = ""

    def run_summary(self, rec: Recording) -> None:
        with self.lock:
            if self.busy:
                return
            self.busy, self.message, self.error = True, "generating the summary", ""

        def work():
            try:
                do_summarize(self.cfg, rec, force=True)
                with self.lock:
                    self.message = "summary regenerated"
            except Exception as e:  # shown on the page, not fatal
                with self.lock:
                    self.error = str(e)
            finally:
                with self.lock:
                    self.busy = False
        threading.Thread(target=work, daemon=True).start()

    def status(self) -> dict:
        with self.lock:
            return {"busy": self.busy, "message": self.message, "error": self.error}


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
            try:
                if u.path == "/api/save":
                    rec = resolve_recording(body.get("stem", ""), state.cfg.out_dir)
                    written = save_names(state.cfg, rec, body.get("names") or {})
                    if body.get("summary"):
                        state.run_summary(rec)
                    self._json({"ok": True, "written": written, "status": state.status()})
                elif u.path == "/api/quit":
                    self._json({"ok": True})
                    threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (RecordingError, ValueError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
    return Handler


def serve(cfg: Config, rec: Recording | None, *, port: int = 0, open_browser: bool = True) -> None:
    """Run the review server until the page's Close button (or Ctrl+C)."""
    state = ReviewState(cfg)
    ref: dict = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(state, ref))
    ref["server"] = server
    url = f"http://127.0.0.1:{server.server_address[1]}/" + (f"#{rec.stem}" if rec else "")
    log.info("review page: %s  (Ctrl+C or the Close button stops it)", url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def unresolved_labels(rec: Recording) -> list[str]:
    if not rec.transcript_path.exists():
        return []
    names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    return [l for l in rec.read_json(rec.transcript_path).get("speakers", []) if is_label(l) and not names.get(l)]
