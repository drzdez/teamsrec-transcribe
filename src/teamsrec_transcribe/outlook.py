"""Meeting title and participants from the classic Outlook calendar on this machine (COM, local, no network).

Used for imported recordings (Teams recordings, ad-hoc files) whose sidecar has no participants yet: the calendar
item running at the recording's start time supplies the subject, organizer and attendee names. Live recordings get
the same from teamsrec-capture at call start. Off unless `[calendar] outlook = true` (asked by `config --init`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


def pick_meeting(items: list[dict], at: datetime, before_s: int = 600, after_s: int = 300) -> dict | None:
    """The item running at `at` (start - 10 min .. end + 5 min): one that really contains `at` first, then Teams
    meetings, then the closest start. Pure function over dicts (testable without Outlook)."""
    best = None
    for it in items:
        if not (it["start"] - timedelta(seconds=before_s) <= at <= it["end"] + timedelta(seconds=after_s)):
            continue
        inside = it["start"] <= at <= it["end"]
        key = (0 if inside else 1, 0 if it.get("teams") else 1, abs((it["start"] - at).total_seconds()))
        if best is None or key < best[0]:
            best = (key, it)
    return best[1] if best else None


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


def meeting_at(at: datetime) -> dict | None:
    """The Outlook meeting running at `at`, or None (Outlook off / not installed / nothing running)."""
    try:
        return pick_meeting(outlook_items(at), at)
    except Exception as e:
        log.info("outlook calendar not available: %s", str(e)[:120])
        return None


def calendar_fields(m: dict) -> dict:
    """Sidecar fields for a matched meeting (contract: `participants`, `calendar`)."""
    return {"participants": [{"name": n} for n in m.get("attendees", [])],
            "calendar": {"source": "outlook", "subject": m.get("subject"), "organizer": m.get("organizer"),
                         "start": m["start"].isoformat(timespec="minutes"), "end": m["end"].isoformat(timespec="minutes")}}
