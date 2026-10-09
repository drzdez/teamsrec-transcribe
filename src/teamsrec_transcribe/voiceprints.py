"""Voice prints: recognise people by their voice across recordings.

The diarization pipeline (pyannote community-1, via whisperx) returns one speaker embedding per diarization
label for free. When a label gets a name (review page, `label-speakers`, the mic track of the user), that
embedding is stored under the person in `<out_dir>/_speakers/voiceprints.json`. On the next recording every
still-unnamed label is compared (cosine similarity) with the stored prints and named when the best person is
clearly above the threshold and clearly ahead of the runner-up. The match is written to `speakers.json`
like a manual assignment, plus `voice_matches` in the transcript so the review page can show
"recognised by voice (0.72)" and the user can correct it.

Biometric data of colleagues: stays local, one file, delete a person's entry to forget them.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

FILE_NAME = "voiceprints.json"
MAX_PER_PERSON = 20  # more prints cover more headsets, rooms and calls; over the limit the weakest one goes (prune)
GOOD_SECONDS = 60.0  # a print from less speech than this is a weaker average of the voice
OUTLIER_GAP = 0.25  # a print this far below the person's typical agreement is likely mixed with other voices or broken
MIN_SECONDS = 30.0  # embeddings of shorter speech are noisy (calibration: 12 s of the user scored 0.43 vs 0.94)
NEAR_DUPLICATE = 0.95  # a print this similar to one already stored adds nothing (same voice, same conditions:
                       # two recordings of the same person on the same microphone scored 0.94, see lab/FINDINGS.md)


def _unit(v: list[float]) -> list[float] | None:
    n = math.sqrt(sum(x * x for x in v))
    if not n or not math.isfinite(n):
        return None
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def remap_embeddings(emb: dict[str, list[float]], before: list[str | None], after: list[str | None]) -> dict[str, list[float]]:
    """Diarization embeddings are keyed by the provider labels; after the video/mic steps renamed segments,
    key them by the final speaker names (labels merged into one name get the mean vector)."""
    if not emb:
        return {}
    pairs = Counter((b, a) for b, a in zip(before, after) if b and a)
    target: dict[str, str] = {}
    for label in emb:
        cands = {a: n for (b, a), n in pairs.items() if b == label}
        target[label] = max(cands.items(), key=lambda kv: kv[1])[0] if cands else label
    grouped: dict[str, list[list[float]]] = {}
    for label, vec in emb.items():
        u = _unit(vec)
        if u:
            grouped.setdefault(target[label], []).append(u)
    out: dict[str, list[float]] = {}
    for name, vecs in grouped.items():
        mean = [sum(col) / len(vecs) for col in zip(*vecs)]
        u = _unit(mean)
        if u:
            out[name] = [round(x, 6) for x in u]
    return out


class Voiceprints:
    def __init__(self, path: Path, limit: int | None = None):
        self.path = path
        self.limit = max(1, int(limit)) if limit else MAX_PER_PERSON  # [voiceprints] max_prints
        self.model = ""
        self.people: dict[str, list[dict]] = {}
        self.excluded: dict[str, list[dict]] = {}  # {person: [{stem, label, at}]} – not used for recognition
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.model = data.get("model", "")
            self.people = {pid: list(prints) for pid, prints in data.get("people", {}).items()}
            self.excluded = {pid: list(rows) for pid, rows in data.get("excluded", {}).items()}

    @classmethod
    def load(cls, out_dir: Path, limit: int | None = None) -> "Voiceprints":
        return cls(out_dir / "_speakers" / FILE_NAME, limit)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"format": 1, "model": self.model, "people": self.people}
        if any(self.excluded.values()):
            data["excluded"] = {pid: rows for pid, rows in self.excluded.items() if rows}
        self.path.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")

    # ---- enrolment
    def enroll(self, pid: str, vector: list[float], stem: str, label: str, model: str = "",
               near_duplicate: float = NEAR_DUPLICATE, seconds: float | None = None, mixed: bool = False) -> bool:
        """Store one print for a person; the same recording+label is never stored twice, and neither is a print
        that is nearly identical to one already there (the slots are worth more when they cover different
        microphones and rooms), nor one the user excluded from recognition (Lidé → Nepoužívat). Returns True if
        added."""
        u = _unit(vector)
        if not u or self.is_excluded(pid, stem, label):
            return False
        if model:
            self.model = model
        prints = self.people.setdefault(pid, [])
        if any(p.get("stem") == stem and p.get("label") == label for p in prints):
            return False
        if any(cosine(u, p["v"]) >= near_duplicate for p in prints):
            return False
        row = {"v": [round(x, 6) for x in u], "stem": stem, "label": label,
               "added": datetime.now().replace(microsecond=0).isoformat()}
        if seconds is not None:
            row["seconds"] = round(seconds, 1)
        if mixed:
            row["mixed"] = True
        prints.append(row)
        prune(prints, self.limit)
        return True

    def forget(self, pid: str) -> None:
        self.people.pop(pid, None)
        self.excluded.pop(pid, None)

    # ---- excluded from recognition (the group stays the person's, its voice is not a sample)
    def is_excluded(self, pid: str, stem: str, label: str) -> bool:
        return any(e.get("stem") == stem and e.get("label") == label for e in self.excluded.get(pid, []))

    def exclude(self, pid: str, stem: str, label: str) -> bool:
        """Lidé → Nepoužívat: the print leaves recognition and does not come back when the meeting is saved again.
        True if a print was removed."""
        before = self.count(pid)
        self.people[pid] = [p for p in self.people.get(pid, []) if not (p.get("stem") == stem and p.get("label") == label)]
        if not self.people[pid]:
            del self.people[pid]
        if not self.is_excluded(pid, stem, label):
            self.excluded.setdefault(pid, []).append(
                {"stem": stem, "label": label, "at": datetime.now().replace(microsecond=0).isoformat()})
        return self.count(pid) < before

    def include(self, pid: str, stem: str, label: str) -> None:
        """Undo exclude: the group may give a print again."""
        rows = [e for e in self.excluded.get(pid, []) if not (e.get("stem") == stem and e.get("label") == label)]
        if rows:
            self.excluded[pid] = rows
        else:
            self.excluded.pop(pid, None)

    def forget_stem(self, stem: str) -> dict[str, int]:
        """Drop the prints taken from one recording, of every person (their other prints stay). {person: n}."""
        gone: dict[str, int] = {}
        for pid in list(self.excluded):  # a new transcript: new groups, the old exclusions mean nothing
            self.excluded[pid] = [e for e in self.excluded[pid] if e.get("stem") != stem]
            if not self.excluded[pid]:
                del self.excluded[pid]
        for pid in list(self.people):
            keep = [p for p in self.people[pid] if p.get("stem") != stem]
            if len(keep) != len(self.people[pid]):
                gone[pid] = len(self.people[pid]) - len(keep)
                if keep:
                    self.people[pid] = keep
                else:
                    del self.people[pid]
        return gone

    def rename(self, old: str, new: str) -> None:
        if old in self.people:
            self.people.setdefault(new, []).extend(self.people.pop(old))
            prune(self.people[new], self.limit)
        if old in self.excluded:
            self.excluded.setdefault(new, []).extend(self.excluded.pop(old))

    def count(self, pid: str) -> int:
        return len(self.people.get(pid, []))

    # ---- matching
    def scores(self, vector: list[float], exclude_stem: str | None = None) -> list[tuple[str, float]]:
        """Best similarity per person, highest first. Prints taken from `exclude_stem` do not count: a recording
        must not be recognised by its own voices (that scored 1.00 and proved nothing)."""
        u = _unit(vector)
        if not u:
            return []
        out = []
        for pid, prints in self.people.items():
            usable = [p for p in prints if not exclude_stem or p.get("stem") != exclude_stem]
            if usable:
                out.append((pid, max(cosine(u, p["v"]) for p in usable)))
        return sorted(out, key=lambda kv: -kv[1])

    def recognize(self, embeddings: dict[str, list[float]], threshold: float, margin: float,
                  durations: dict[str, float] | None = None, min_seconds: float = MIN_SECONDS,
                  exclude_stem: str | None = None) -> dict[str, tuple[str, float]]:
        """{label: (person id, score)} for labels whose best person is >= threshold and >= margin ahead.
        Labels with less than min_seconds of speech are skipped (their embedding is unreliable)."""
        out: dict[str, tuple[str, float]] = {}
        for label, vec in embeddings.items():
            if durations is not None and durations.get(label, 0.0) < min_seconds:
                continue
            ranked = self.scores(vec, exclude_stem)
            if not ranked:
                continue
            best_pid, best = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else -1.0
            if best >= threshold and best - second >= margin:
                out[label] = (best_pid, round(best, 3))
        return out


def prune(prints: list[dict], limit: int = MAX_PER_PERSON) -> list[dict]:
    """Keep at most `limit` prints, dropping the weakest one at a time (in place; returns the dropped):
    1. a print of poor quality first – flagged as possibly mixed voices, an outlier that agrees with the person's
       other prints far less than they agree with each other (mixed with another voice, broken audio, a wrong
       name), or one from little speech; the worst of them goes;
    2. otherwise the most redundant one – the closest to another print (it adds the least: same voice, same
       conditions); the older of the two goes.
    Prints from different headsets and rooms stay, so recognition keeps working across conditions."""
    dropped: list[dict] = []
    while len(prints) > limit:
        n = len(prints)
        sims = [[cosine(prints[i]["v"], prints[j]["v"]) if i != j else -1.0 for j in range(n)] for i in range(n)]
        mean = [sum(s for s in row if s > -1.0) / (n - 1) for row in sims]
        typical = sorted(mean)[n // 2]

        def poor(i: int) -> float:  # 0 = fine, higher = worse
            p = prints[i]
            score = 0.0
            if p.get("mixed"):
                score += 2.0
            if mean[i] < typical - OUTLIER_GAP:
                score += 1.0 + (typical - mean[i])
            secs = p.get("seconds")
            if secs is not None and secs < GOOD_SECONDS:
                score += 0.5 + (GOOD_SECONDS - secs) / GOOD_SECONDS
            return score

        bad = max(range(n), key=poor)
        if poor(bad) > 0:
            victim = bad
        else:
            i = max(range(n), key=lambda k: max(sims[k]))
            j = max(range(n), key=lambda k: sims[i][k])
            victim = min(i, j, key=lambda k: prints[k].get("added", ""))
        dropped.append(prints.pop(victim))
    return dropped


def speech_seconds(segments: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in segments:
        who = s.get("speaker")
        if who:
            out[who] = out.get(who, 0.0) + max(0.0, float(s.get("end", 0)) - float(s.get("start", 0)))
    return out


def enroll_from_recording(vp: Voiceprints, transcript: dict, names: dict[str, str], stem: str,
                          min_seconds: float = MIN_SECONDS) -> int:
    """After labels got names: store the transcript's embeddings under those people (only labels with enough
    speech). Returns how many were added."""
    emb = transcript.get("speaker_embeddings") or {}
    model = transcript.get("diarize_model") or ""
    secs = speech_seconds(transcript.get("segments") or [])
    mixed = set(((transcript.get("voices_from") or {}).get("mixed") or {}))  # fast track: a group of two voices
    added = 0
    for label, pid in names.items():
        vec = emb.get(label)
        if vec and pid and not pid.startswith("SPEAKER_") and secs.get(label, 0.0) >= min_seconds:
            added += vp.enroll(pid, vec, stem, label, model, seconds=secs.get(label), mixed=label in mixed)
    return added
