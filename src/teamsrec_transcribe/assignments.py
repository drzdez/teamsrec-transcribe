"""Who is assigned where: every speaker group named after a person, across all recordings, and how its voice compares
with the person's other voice prints.

A group is *confirmed* when the user saved the name (speakers.json, not a voice match or a direct call waiting for
confirmation) and *to review* when the application put the name there by itself – a voice match, a direct call, a
name tag of the Teams video, the microphone track. Only confirmed groups store voice prints (enroll_names); a print
whose group is gone (the recording was transcribed again, the label changed) is listed on its own.

The comparison (Lidé → a person): each group's voice embedding against the person's prints – its own print left out
– as the mean of the three closest (a person recorded with two headsets has two clusters; the closest ones say
whether this voice belongs to one of them), and against every other person the same way. A group much less similar
than the rest, or closer to somebody else, is marked for attention: a wrong name, two voices in one group, a bad
microphone. Only embeddings of the same diarization model are comparable; others get no number.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Config
from .people import People
from .recording import iter_recordings
from .voiceprints import Voiceprints, speech_seconds

TOP_K = 3
# The levels of the comparison (cosine similarity, pyannote community-1; calibration: the same person on another
# microphone 0.6–0.9, two people rarely above 0.5 – lab/FINDINGS.md, and the recognition threshold is 0.55).
LEVELS = ((0.70, 0, "odpovídá ostatním vzorkům"),
          (0.55, 1, "trochu jiný – jiný mikrofon nebo místnost, obvykle v pořádku"),
          (0.40, 2, "jiný – poslechněte si ho: jiný člověk, dva hlasy ve skupině, nebo špatný zvuk"),
          (-1.0, 3, "velmi jiný – nejspíš jiná osoba nebo směs hlasů"))
CLOSER_MARGIN = 0.05  # closer to somebody else by this much: as bad as "velmi jiný"


def _is_label(name: str) -> bool:
    return name.startswith("SPEAKER_") or name == "UNKNOWN"


@dataclass
class Row:
    stem: str
    title: str
    start: str | None
    label: str
    status: str            # "confirmed" | "review" | "print_only"
    source: str            # "ručně" | "hlas" | "přímý hovor" | "video" | "mikrofon" | "otisk"
    score: float | None    # the voice match's own score (status review, source hlas)
    seconds: float
    has_print: bool
    can_reject: bool
    short: bool = False  # too little speech for a voice print
    vec: list[float] | None = None
    match: dict | None = None

    def public(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "vec"}
        d["seconds"] = round(self.seconds, 1)
        return d


def collect(cfg: Config, people: People | None = None, vp: Voiceprints | None = None) -> dict[str, list[Row]]:
    """{person id: rows} for every person somebody assigned a group to, plus the prints without a group."""
    people = people or People.load(cfg.out_dir, cfg.people_display)
    vp = vp or Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    prints = {(pid, p.get("stem"), p.get("label")) for pid, ps in vp.people.items() for p in ps}
    out: dict[str, list[Row]] = {}
    seen: set[tuple[str, str, str]] = set()
    for rec in iter_recordings(cfg.out_dir):
        if not rec.transcript_path.exists():
            continue
        try:
            data = rec.read_json(rec.transcript_path)
            manual = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
        except (OSError, ValueError):
            continue
        voice = data.get("voice_matches") or {}
        call = data.get("direct_call") or {}
        rejected = data.get("voice_rejected") or {}
        emb = data.get("speaker_embeddings") or {}
        same_model = not vp.model or not data.get("diarize_model") or data.get("diarize_model") == vp.model
        for label, seconds in speech_seconds(data.get("segments") or []).items():
            if seconds <= 0 or label == "UNKNOWN":
                continue
            score = None
            if label in voice:
                pid, status, source, score = voice[label].get("person"), "review", "hlas", voice[label].get("score")
            elif call.get("label") == label and call.get("applied"):
                pid, status, source = call.get("person"), "review", "přímý hovor"
            elif manual.get(label):
                pid, status, source = manual[label], "confirmed", "ručně"
            elif not _is_label(label):  # a literal name: the Teams video or the microphone, never confirmed
                person = people.find(label)
                pid, status = (person.id if person else None), "review"
                source = "mikrofon" if cfg.user_name and label == cfg.user_name else "video"
            else:
                continue
            if not pid or people.get(pid) is None or pid in rejected.get(label, []):
                continue
            vec = emb.get(label) if same_model else None
            out.setdefault(pid, []).append(Row(
                stem=rec.stem, title=rec.title, start=rec.sidecar.get("start"), label=label, status=status,
                source=source, score=score, seconds=seconds, has_print=(pid, rec.stem, label) in prints,
                can_reject=True, short=seconds < cfg.voiceprints.min_seconds,
                vec=list(vec) if vec else None))
            seen.add((pid, rec.stem, label))
    recs = {r.stem: r for r in iter_recordings(cfg.out_dir)}
    for pid, ps in vp.people.items():  # prints whose group is gone (transcribed again, renamed, removed)
        if people.get(pid) is None:
            continue
        for p in ps:
            key = (pid, p.get("stem") or "", p.get("label") or "")
            if key in seen:
                continue
            rec = recs.get(key[1])
            out.setdefault(pid, []).append(Row(
                stem=key[1], title=rec.title if rec else key[1], start=rec.sidecar.get("start") if rec else None,
                label=key[2], status="print_only", source="otisk", score=None, seconds=0.0, has_print=True,
                can_reject=False, vec=list(p.get("v") or []) or None))
    for rows in out.values():
        rows.sort(key=lambda r: (r.stem, r.label), reverse=True)
    return out


def compare(rows_by_person: dict[str, list[Row]], vp: Voiceprints, people: People) -> None:
    """Fill each row's `match`: its similarity to the person's prints (its own print left out), the closest other
    person, the level and the sentence for the tooltip. In place."""
    keys, mats = [], []
    for pid, ps in vp.people.items():
        for p in ps:
            v = p.get("v")
            if v:
                keys.append((pid, p.get("stem"), p.get("label")))
                mats.append(v)
    if not mats:
        return
    P = np.asarray(mats, dtype=np.float32)
    P /= np.linalg.norm(P, axis=1, keepdims=True) + 1e-9
    owners = np.asarray([k[0] for k in keys])
    all_rows = [(pid, r) for pid, rows in rows_by_person.items() for r in rows if r.vec and len(r.vec) == P.shape[1]]
    if not all_rows:
        return
    R = np.asarray([r.vec for _, r in all_rows], dtype=np.float32)
    R /= np.linalg.norm(R, axis=1, keepdims=True) + 1e-9
    S = R @ P.T  # rows × prints
    for i, (pid, r) in enumerate(all_rows):
        sims = S[i].copy()
        own_mask = owners == pid
        if r.has_print:  # leave its own print out: it would match itself
            for j, k in enumerate(keys):
                if k == (pid, r.stem, r.label):
                    own_mask[j] = False
        own = np.sort(sims[own_mask])[::-1][:TOP_K]
        best_other, other_pid = None, None
        for q in set(owners.tolist()) - {pid}:
            o = np.sort(sims[owners == q])[::-1][:TOP_K]
            if len(o) and (best_other is None or o.mean() > best_other):
                best_other, other_pid = float(o.mean()), q
        if not len(own):
            r.match = {"own": None, "n": 0, "level": None, "why": "není s čím srovnat – osoba nemá jiný otisk",
                       "other": _other(other_pid, best_other, people)}
            continue
        own_score = float(own.mean())
        level, why = next((lv, w) for th, lv, w in LEVELS if own_score >= th)
        if best_other is not None and best_other > own_score + CLOSER_MARGIN:
            level = 3
            who = people.get(other_pid)
            why = f"podobnější jiné osobě: {who.full if who else other_pid} ({best_other:.2f}) – nejspíš špatné jméno"
        r.match = {"own": round(own_score, 2), "n": int(own_mask.sum()), "level": level, "why": why,
                   "other": _other(other_pid, best_other, people)}


def _other(pid: str | None, score: float | None, people: People) -> dict | None:
    if pid is None or score is None:
        return None
    p = people.get(pid)
    return {"person": pid, "name": p.full if p else pid, "score": round(score, 2)}


def for_people(cfg: Config) -> dict[str, list[Row]]:
    """Every person's rows with the comparison filled in."""
    people = People.load(cfg.out_dir, cfg.people_display)
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    rows = collect(cfg, people, vp)
    compare(rows, vp, people)
    return rows


def summary(rows: list[Row]) -> dict:
    """The counts for the people list."""
    return {"confirmed": sum(r.status == "confirmed" for r in rows),
            "with_print": sum(r.has_print and r.status != "print_only" for r in rows),
            "review": sum(r.status == "review" for r in rows),
            "print_only": sum(r.status == "print_only" for r in rows),
            "attention": sum(bool(r.match and r.match.get("level") is not None and r.match["level"] >= 2) for r in rows)}


def _level(score: float) -> tuple[int, str]:
    return next((lv, w) for th, lv, w in LEVELS if score >= th)


def sample_view(cfg: Config, pid: str, rows: list[Row], vp: Voiceprints) -> dict:
    """Lidé → osoba → Vzorky pro rozpoznávání: the prints used for recognition (at most the limit), each compared
    with the person's other prints only (left out itself, mean of the 3 closest); the groups that could become a
    print, best first (similarity to the prints, then speech); the excluded ones."""
    prints = vp.people.get(pid, [])
    by_key = {(r.stem, r.label): r for r in rows}
    P = np.asarray([p["v"] for p in prints], dtype=np.float32) if prints else np.zeros((0, 1), dtype=np.float32)
    if len(P):
        P /= np.linalg.norm(P, axis=1, keepdims=True) + 1e-9
    used = []
    for i, p in enumerate(prints):
        r = by_key.get((p.get("stem"), p.get("label")))
        match = None
        if len(P) > 1:
            sims = np.delete(P @ P[i], i)
            own = float(np.sort(sims)[::-1][:TOP_K].mean())
            level, why = _level(own)
            match = {"own": round(own, 2), "n": len(P) - 1, "level": level, "why": why}
        used.append({"stem": p.get("stem"), "label": p.get("label"), "added": p.get("added"),
                     "title": r.title if r else p.get("stem"), "start": r.start if r else None,
                     "seconds": round(r.seconds, 1) if r else p.get("seconds"),
                     "status": r.status if r else "print_only", "match": match})
    addable = []
    for r in rows:
        if r.has_print or r.status == "print_only" or not r.vec or vp.is_excluded(pid, r.stem, r.label):
            continue
        q = None
        if len(P):
            v = np.asarray(r.vec, dtype=np.float32)
            v /= np.linalg.norm(v) + 1e-9
            q = float(np.sort(P @ v)[::-1][:TOP_K].mean())
        addable.append({"stem": r.stem, "label": r.label, "title": r.title, "start": r.start,
                        "seconds": round(r.seconds, 1), "status": r.status, "source": r.source,
                        "short": r.seconds < cfg.voiceprints.min_seconds,
                        "match": ({"own": round(q, 2), "level": _level(q)[0], "why": _level(q)[1]}
                                  if q is not None else None)})
    addable.sort(key=lambda a: (a["short"], -(a["match"]["own"] if a["match"] else 0), -a["seconds"]))
    titles = {(r.stem, r.label): r.title for r in rows}
    excluded = [{**e, "title": titles.get((e.get("stem"), e.get("label")), e.get("stem"))}
                for e in vp.excluded.get(pid, [])]
    return {"used": used, "limit": vp.limit, "addable": addable, "excluded": excluded}
