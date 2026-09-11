"""People registry: first name, last name, nickname, and what to print for each person.

Stored in `<out_dir>/_speakers/people.json` (one file, hand-editable), the voice samples will live next to it
later. Transcripts and `speakers.json` keep whatever identifies the person (a person id, or a literal name from
the video / the mic config); the display form is decided only when the exports and the summary are written, so
changing a nickname or the display mode later just needs a re-export.

Display modes: "first" (Petr), "full" (Petr Svoboda), "nick" (Péťa; falls back to the first name when the
person has no nickname). The default mode is `[people] display` in the config, a person may override it.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .providers.base import Segment

DISPLAY_MODES = ("first", "full", "nick")
FILE_NAME = "people.json"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().casefold()


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", _norm(s)).strip("-")
    return s or "person"


@dataclass
class Person:
    id: str
    first: str = ""
    last: str = ""
    nick: str = ""
    display: str = ""  # "" = use the default from the config
    aliases: list[str] = field(default_factory=list)  # other spellings, e.g. how Teams shows the name

    @property
    def full(self) -> str:
        return " ".join(p for p in (self.first, self.last) if p) or self.nick or self.id

    def name(self, default_mode: str = "nick") -> str:
        mode = self.display if self.display in DISPLAY_MODES else default_mode
        if mode == "nick" and self.nick:
            return self.nick
        if mode in ("nick", "first") and self.first:
            return self.first
        return self.full

    def matches(self, text: str) -> bool:
        t = _norm(text)
        if not t:
            return False
        cands = {self.id, self.full, self.nick, *self.aliases}
        if self.first and self.last:
            cands.add(f"{self.last} {self.first}")
        return t in {_norm(c) for c in cands if c}

    @staticmethod
    def from_text(text: str, existing_ids: set[str] = frozenset()) -> "Person":
        """'Petr Svoboda' -> first Petr, last Svoboda; a single word is a first name."""
        parts = text.strip().split()
        first, last = (parts[0], " ".join(parts[1:])) if parts else ("", "")
        base = _slug(text)
        pid, n = base, 2
        while pid in existing_ids:
            pid, n = f"{base}-{n}", n + 1
        return Person(id=pid, first=first, last=last)


class People:
    def __init__(self, path: Path, default_mode: str = "nick"):
        self.path = path
        self.default_mode = default_mode if default_mode in DISPLAY_MODES else "nick"
        self.people: list[Person] = []
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for p in data.get("people", []):
                self.people.append(Person(id=p["id"], first=p.get("first", ""), last=p.get("last", ""),
                                          nick=p.get("nick", ""), display=p.get("display", ""),
                                          aliases=list(p.get("aliases", []))))

    @classmethod
    def load(cls, out_dir: Path, default_mode: str = "nick") -> "People":
        return cls(out_dir / "_speakers" / FILE_NAME, default_mode)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"format": 1, "people": [asdict(p) for p in self.people]}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # ---- lookup
    def get(self, pid: str) -> Person | None:
        return next((p for p in self.people if p.id == pid), None)

    def find(self, text: str) -> Person | None:
        """By id, full name, nickname or alias; a bare first name only when it is unique; a full name also
        matches the single person who has that first name and no last name yet (created from a mic/video
        first name before the user typed the surname)."""
        if not text:
            return None
        hit = [p for p in self.people if p.matches(text)]
        if len(hit) == 1:
            return hit[0]
        if not hit:
            parts = text.split()
            t = _norm(parts[0]) if parts else ""
            if len(parts) == 1:
                by_first = [p for p in self.people if _norm(p.first) == t]
            else:
                by_first = [p for p in self.people if _norm(p.first) == t and not p.last]
            if len(by_first) == 1:
                return by_first[0]
        return None

    def ensure(self, text: str) -> Person:
        """The person for a typed name, created from the text when unknown; a first-name-only person gets the
        surname from the typed full name instead of a duplicate."""
        p = self.find(text)
        if p is None:
            p = Person.from_text(text, {q.id for q in self.people})
            self.people.append(p)
        elif not p.last and len(text.split()) > 1 and _norm(p.first) == _norm(text.split()[0]):
            p.last = " ".join(text.split()[1:])
        return p

    def merge(self, keep_id: str, drop_id: str, recordings: Iterable = ()) -> list:
        """Fold `drop` into `keep`: nickname/aliases carried over, every speakers.json that points at `drop`
        rewritten. Returns the recordings that were rewritten (caller re-exports them)."""
        keep, drop = self.get(keep_id), self.get(drop_id)
        if keep is None or drop is None or keep is drop:
            raise ValueError("merge needs two different known people")
        if not keep.nick and drop.nick:
            keep.nick = drop.nick
        if not keep.last and drop.last:
            keep.last = drop.last
        for alias in [drop.full, drop.nick, *drop.aliases]:
            if alias and not keep.matches(alias) and alias not in keep.aliases:
                keep.aliases.append(alias)
        self.people = [p for p in self.people if p.id != drop_id]
        changed = []
        for rec in recordings:
            if rec.speakers_path.exists():
                names = rec.read_json(rec.speakers_path)
                if drop_id in names.values():
                    rec.write_json(rec.speakers_path, {k: (keep_id if v == drop_id else v) for k, v in names.items()})
                    changed.append(rec)
        return changed

    # ---- display
    def display(self, speaker: str | None) -> str | None:
        """What to print for a transcript speaker value (person id, literal name, or SPEAKER_xx)."""
        if not speaker:
            return speaker
        p = self.get(speaker) or self.find(speaker)
        return p.name(self.default_mode) if p else speaker

    def apply(self, segments: list[Segment]) -> dict[str, str]:
        """Replace speakers in place by their display names. Returns {original: display} for the changed ones.
        Two different people who would print the same (two Petrs in "first" mode) get their full names instead."""
        originals = {seg.speaker for seg in segments if seg.speaker}
        shown = {o: self.display(o) for o in originals}
        counts: dict[str, int] = {}
        for d in shown.values():
            counts[d] = counts.get(d, 0) + 1
        for o, d in shown.items():
            if counts[d] > 1:
                person = self.get(o) or self.find(o)
                if person:
                    shown[o] = person.full
        changed: dict[str, str] = {}
        for seg in segments:
            if seg.speaker and shown.get(seg.speaker, seg.speaker) != seg.speaker:
                changed[seg.speaker] = shown[seg.speaker]
                seg.speaker = shown[seg.speaker]
        return changed

    def to_json(self) -> list[dict]:
        return [asdict(p) for p in self.people]

    def replace_all(self, rows: list[dict]) -> None:
        """Bulk update from the page: keeps ids, drops rows without any name."""
        out: list[Person] = []
        ids: set[str] = set()
        for r in rows:
            first, last, nick = (str(r.get(k, "")).strip() for k in ("first", "last", "nick"))
            if not (first or last or nick):
                continue
            pid = str(r.get("id") or "").strip() or Person.from_text(f"{first} {last}".strip() or nick, ids).id
            if pid in ids:
                continue
            ids.add(pid)
            display = str(r.get("display") or "")
            aliases = [a.strip() for a in (r.get("aliases") or []) if str(a).strip()]
            out.append(Person(id=pid, first=first, last=last, nick=nick,
                              display=display if display in DISPLAY_MODES else "", aliases=aliases))
        self.people = out
