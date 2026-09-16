"""Meeting title and participants from the classic Outlook calendar on this machine (COM, local, no network).

Used for imported recordings (Teams recordings, ad-hoc files) whose sidecar has no participants yet: the calendar
item running at the recording's start time supplies the subject, organizer and attendee names. Live recordings get
the same from teamsrec-capture at call start. Off unless `[calendar] outlook = true` (asked by `config --init`).
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

GENERIC_TITLES = {"meeting", "join meeting", "meeting compact view", "compact view", "call", "teams-call", "teams call",
                  "připojení ke schůzce", "kompaktní zobrazení schůzky", "hovor", "schůzka", "ovládací panel sdílení"}


def _title_norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def is_generic_title(title: str | None) -> bool:
    t = (title or "").strip().lower()
    return not t or t in GENERIC_TITLES or t.startswith("schůzka s:") or t.startswith("meeting with")


def candidates_at(items: list[dict], at: datetime, window_s: int = 3600) -> list[dict]:
    """Items whose span comes within `window_s` of `at`, closest start first."""
    out = [it for it in items if it["start"] - timedelta(seconds=window_s) <= at <= it["end"] + timedelta(seconds=window_s)]
    return sorted(out, key=lambda it: abs((it["start"] - at).total_seconds()))


def pick_meeting(items: list[dict], at: datetime, title: str | None = None,
                 before_s: int = 600, after_s: int = 300) -> tuple[dict | None, str]:
    """The calendar item for a recording: by title first (the Teams window / recording file carries the subject,
    which settles ad-hoc calls and parallel meetings), else by time (the item running at `at`: containing `at`
    first, then Teams meetings, then the closest start). Returns (item, match) with match "title" | "time" | ""."""
    if title and not is_generic_title(title):
        t = _title_norm(title)
        near = candidates_at(items, at, window_s=7200)
        subjects = [_title_norm(it["subject"]) for it in near]
        hit = [it for it, s in zip(near, subjects) if s and (s == t or s in t or t in s)]
        if not hit:
            close = difflib.get_close_matches(t, [s for s in subjects if s], n=1, cutoff=0.8)
            hit = [it for it, s in zip(near, subjects) if close and s == close[0]]
        if hit:
            return hit[0], "title"
    best = None
    for it in items:
        if not (it["start"] - timedelta(seconds=before_s) <= at <= it["end"] + timedelta(seconds=after_s)):
            continue
        inside = it["start"] <= at <= it["end"]
        key = (0 if inside else 1, 0 if it.get("teams") else 1, abs((it["start"] - at).total_seconds()))
        if best is None or key < best[0]:
            best = (key, it)
    return (best[1], "time") if best else (None, "")


def outlook_items(day: datetime) -> list[dict]:
    """Calendar items of one day from classic Outlook. Raises when Outlook/COM is not available."""
    import pythoncom  # pywin32, Windows only
    import win32com.client
    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        cal = app.GetNamespace("MAPI").GetDefaultFolder(9)  # olFolderCalendar
        items = cal.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")
        flt = f"[Start] >= '{day:%m/%d/%Y} 00:00' AND [Start] <= '{day:%m/%d/%Y} 23:59'"
        out = []
        for it in items.Restrict(flt):
            try:
                text = f"{it.Location or ''} {it.Body or ''}"
                out.append({
                    "subject": (it.Subject or "").strip(),
                    "start": datetime(it.Start.year, it.Start.month, it.Start.day, it.Start.hour, it.Start.minute),
                    "end": datetime(it.End.year, it.End.month, it.End.day, it.End.hour, it.End.minute),
                    "organizer": (it.Organizer or "").strip(),
                    "attendees": [r.Name for r in it.Recipients if r.Name],
                    "teams": "teams.microsoft.com" in text.lower(),
                })
            except Exception:
                continue
        return out
    finally:
        pythoncom.CoUninitialize()


def meeting_at(at: datetime, title: str | None = None) -> dict | None:
    """The Outlook meeting for a recording starting at `at` (title match first), with `match` and `candidates`;
    None when Outlook is off / not installed / nothing matches."""
    try:
        items = outlook_items(at)
    except Exception as e:
        log.info("outlook calendar not available: %s", str(e)[:120])
        return None
    it, match = pick_meeting(items, at, title)
    if it is None:
        return None
    out = dict(it)
    out["match"] = match
    out["candidates"] = [_brief(c) for c in candidates_at(items, at) if c is not it][:5]
    return out


def candidates_for(at: datetime, window_s: int = 3600) -> list[dict]:
    """Nearby Outlook items for the review page ("choose another meeting")."""
    try:
        return [dict(_brief(c), attendees=c.get("attendees", []), organizer=c.get("organizer", ""))
                for c in candidates_at(outlook_items(at), at, window_s)]
    except Exception as e:
        log.info("outlook calendar not available: %s", str(e)[:120])
        return []


def _brief(c: dict) -> dict:
    return {"subject": c["subject"], "start": c["start"].isoformat(timespec="minutes"),
            "end": c["end"].isoformat(timespec="minutes"), "teams": bool(c.get("teams"))}


def calendar_fields(m: dict, status: str = "auto") -> dict:
    """Sidecar fields for a matched meeting (contract: `participants` with source, `calendar` with match/status)."""
    start = m["start"] if isinstance(m["start"], str) else m["start"].isoformat(timespec="minutes")
    end = m["end"] if isinstance(m["end"], str) else m["end"].isoformat(timespec="minutes")
    return {"participants": [{"name": n, "source": "calendar"} for n in m.get("attendees", [])],
            "calendar": {"source": "outlook", "subject": m.get("subject"), "organizer": m.get("organizer"),
                         "start": start, "end": end, "match": m.get("match", "manual"), "status": status,
                         "candidates": m.get("candidates", [])}}
