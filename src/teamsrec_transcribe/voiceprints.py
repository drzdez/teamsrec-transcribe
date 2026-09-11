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
MAX_PER_PERSON = 10
MIN_SECONDS = 30.0  # embeddings of shorter speech are noisy (calibration: 12 s of the user scored 0.43 vs 0.94)


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
    def __init__(self, path: Path):
        self.path = path
        self.model = ""
        self.people: dict[str, list[dict]] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.model = data.get("model", "")
            self.people = {pid: list(prints) for pid, prints in data.get("people", {}).items()}

    @classmethod
    def load(cls, out_dir: Path) -> "Voiceprints":
        return cls(out_dir / "_speakers" / FILE_NAME)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"format": 1, "model": self.model, "people": self.people}
        self.path.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")

    # ---- enrolment
    def enroll(self, pid: str, vector: list[float], stem: str, label: str, model: str = "") -> bool:
        """Store one print for a person; the same recording+label is never stored twice. Returns True if added."""
        u = _unit(vector)
        if not u:
            return False
        if model:
            self.model = model
        prints = self.people.setdefault(pid, [])
        if any(p.get("stem") == stem and p.get("label") == label for p in prints):
            return False
        prints.append({"v": [round(x, 6) for x in u], "stem": stem, "label": label,
                       "added": datetime.now().replace(microsecond=0).isoformat()})
        del prints[:-MAX_PER_PERSON]
        return True

    def forget(self, pid: str) -> None:
        self.people.pop(pid, None)

    def rename(self, old: str, new: str) -> None:
        if old in self.people:
            self.people.setdefault(new, []).extend(self.people.pop(old))
            del self.people[new][:-MAX_PER_PERSON]

    def count(self, pid: str) -> int:
        return len(self.people.get(pid, []))

    # ---- matching
    def scores(self, vector: list[float]) -> list[tuple[str, float]]:
        """Best similarity per person, highest first."""
        u = _unit(vector)
        if not u:
            return []
        out = []
        for pid, prints in self.people.items():
            if prints:
                out.append((pid, max(cosine(u, p["v"]) for p in prints)))
        return sorted(out, key=lambda kv: -kv[1])

    def recognize(self, embeddings: dict[str, list[float]], threshold: float, margin: float,
                  durations: dict[str, float] | None = None, min_seconds: float = MIN_SECONDS) -> dict[str, tuple[str, float]]:
        """{label: (person id, score)} for labels whose best person is >= threshold and >= margin ahead.
        Labels with less than min_seconds of speech are skipped (their embedding is unreliable)."""
        out: dict[str, tuple[str, float]] = {}
        for label, vec in embeddings.items():
            if durations is not None and durations.get(label, 0.0) < min_seconds:
                continue
            ranked = self.scores(vec)
            if not ranked:
                continue
            best_pid, best = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else -1.0
            if best >= threshold and best - second >= margin:
                out[label] = (best_pid, round(best, 3))
        return out


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
    added = 0
    for label, pid in names.items():
        vec = emb.get(label)
        if vec and pid and not pid.startswith("SPEAKER_") and secs.get(label, 0.0) >= min_seconds:
            added += vp.enroll(pid, vec, stem, label, model)
    return added
