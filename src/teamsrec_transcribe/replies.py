"""Every reply's own voice, compared with a person's voice prints (Lidé → osoba → Všechny repliky).

The diarization gives one voice embedding per speaker group; a group that mixes two voices, or a reply said by
somebody else, hides inside that average. Here every reply of at least REPLY_MIN_S gets its own embedding from the
very model the prints come from (the diarization pipeline's embedding model, so the numbers are comparable:
2026-10-09 on a real call, a reply against its own group 0.83–0.89, against the other group 0.33). It is GPU work –
about 40 ms a reply, half a minute for an hour's meeting – so it runs only when the user asks (a queued job, held
during a recording like every job) and is kept in `<stem>.reply_voices.json` (derived, deletable).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from .config import Config
from .recording import Recording, iter_recordings, resolve_recording

log = logging.getLogger(__name__)

REPLY_MIN_S = 1.5        # shorter replies give a noisy embedding
SECONDS_PER_REPLY = 0.04  # measured on an RTX 5090 Laptop (2026-10-09), for the estimate before asking
CACHE_SUFFIX = ".reply_voices.json"


def cache_path(rec: Recording) -> Path:
    return rec.file(CACHE_SUFFIX)


def _key(start: float) -> str:
    return f"{float(start):.2f}"


def load_cache(rec: Recording, model: str) -> dict[str, list[float]]:
    p = cache_path(rec)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("replies") or {} if data.get("model") == model else {}


def replies_of(rec: Recording) -> list[dict]:
    """The replies worth an embedding: long enough, with a speaker."""
    data = rec.read_json(rec.transcript_path)
    return [s for s in data.get("segments") or []
            if s.get("speaker") and float(s["end"]) - float(s["start"]) >= REPLY_MIN_S]


def missing(rec: Recording, model: str) -> int:
    """How many replies of this recording still have no embedding."""
    if not rec.transcript_path.exists():
        return 0
    have = load_cache(rec, model)
    return sum(1 for s in replies_of(rec) if _key(s["start"]) not in have)


def compute(cfg: Config, recs: list[Recording], progress=lambda text: None) -> int:
    """Embed the replies of these recordings that have none yet (GPU). Returns how many were computed."""
    model = cfg.transcribe.diarize_model
    todo = [r for r in recs if missing(r, model)]
    if not todo:
        return 0
    import torch  # only now: nothing to compute needs no GPU libraries
    import whisperx
    from pyannote.audio import Pipeline
    device = torch.device(cfg.transcribe.device if cfg.transcribe.device == "cuda" and torch.cuda.is_available() else "cpu")
    pipe = Pipeline.from_pretrained(model).to(device)
    embed = pipe._embedding  # the model the diarization's (and so the prints') embeddings come from
    done = 0
    try:
        for i, rec in enumerate(todo, 1):
            progress(f"repliky: schůzka {i}/{len(todo)} – {rec.title}")
            audio_path = rec.mix_path if rec.mix_path and rec.mix_path.exists() else None
            if audio_path is None:
                log.warning("%s: no audio, replies not evaluated", rec.stem)
                continue
            audio = whisperx.load_audio(str(audio_path))
            have = load_cache(rec, model)
            for s in replies_of(rec):
                k = _key(s["start"])
                if k in have:
                    continue
                a, b = int(float(s["start"]) * 16000), int(float(s["end"]) * 16000)
                if b - a < 16000 * REPLY_MIN_S * 0.9:
                    continue
                with torch.inference_mode():
                    v = np.asarray(embed(torch.from_numpy(audio[a:b]).float()[None, None, :]))[0]
                n = float(np.linalg.norm(v))
                if not np.isfinite(n) or n == 0:
                    continue
                have[k] = [round(float(x) / n, 4) for x in v]
                done += 1
            cache_path(rec).write_text(json.dumps({"format": 1, "model": model, "replies": have}), encoding="utf-8")
    finally:
        del pipe
    return done


def evaluate(cfg: Config, pid: str) -> dict:
    """Every reply of the person's groups (confirmed or guessed) that has an embedding: its similarity to the
    person's prints (mean of the 3 closest) and to the closest other person, with the level of the samples views.
    Also which of the person's recordings are not evaluated yet and how long that would take."""
    from .assignments import CLOSER_MARGIN, LEVELS, TOP_K, collect
    from .people import People
    from .voiceprints import Voiceprints
    people = People.load(cfg.out_dir, cfg.people_display)
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    model = vp.model or cfg.transcribe.diarize_model
    groups = [r for r in collect(cfg, people, vp).get(pid, []) if r.status != "print_only"]
    keys, mats = [], []
    for q, ps in vp.people.items():
        for p in ps:
            if p.get("v"):
                keys.append(q)
                mats.append(p["v"])
    P = np.asarray(mats, dtype=np.float32) if mats else np.zeros((0, 256), dtype=np.float32)
    if len(P):
        P /= np.linalg.norm(P, axis=1, keepdims=True) + 1e-9
    owners = np.asarray(keys)
    rows, todo, todo_replies = [], [], 0
    by_stem: dict[str, list] = {}
    for g in groups:
        by_stem.setdefault(g.stem, []).append(g)
    for stem, gs in by_stem.items():
        try:
            rec = resolve_recording(stem, cfg.out_dir)
        except Exception:
            continue
        cache = load_cache(rec, model)
        labels = {g.label for g in gs}
        mine = [s for s in replies_of(rec) if s["speaker"] in labels]
        lacking = [s for s in mine if _key(s["start"]) not in cache]
        if lacking:
            todo.append(stem)
            todo_replies += len(lacking)
        for s in mine:
            v = cache.get(_key(s["start"]))
            if v is None:
                continue
            own, best, best_pid = None, None, None
            if len(P) and len(v) == P.shape[1]:  # no prints at all: listed, nothing to compare with
                sims = P @ np.asarray(v, dtype=np.float32)
                own_s = np.sort(sims[owners == pid])[::-1][:TOP_K]
                own = float(own_s.mean()) if len(own_s) else None
                for q in set(keys) - {pid}:
                    o = np.sort(sims[owners == q])[::-1][:TOP_K]
                    if len(o) and (best is None or o.mean() > best):
                        best, best_pid = float(o.mean()), q
            level, why = (None, "osoba nemá vzorky") if own is None else next((lv, w) for th, lv, w in LEVELS if own >= th)
            if own is not None and best is not None and best > own + CLOSER_MARGIN:
                who = people.get(best_pid)
                level, why = 3, f"podobnější jiné osobě: {who.full if who else best_pid} ({best:.2f})"
            g = next(x for x in gs if x.label == s["speaker"])
            rows.append({"stem": stem, "title": rec.title, "date": rec.sidecar.get("start"), "label": s["speaker"],
                         "status": g.status, "start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2),
                         "text": (s.get("text") or "").strip()[:200], "own": round(own, 2) if own is not None else None,
                         "level": level, "why": why,
                         "other": ({"person": best_pid, "name": (people.get(best_pid).full if people.get(best_pid) else best_pid),
                                    "score": round(best, 2)} if best is not None else None)})
    return {"replies": rows, "todo": todo, "todo_replies": todo_replies,
            "estimate_s": round(todo_replies * SECONDS_PER_REPLY + (5 if todo else 0)),
            "evaluated_recordings": len(by_stem) - len(todo), "recordings": len(by_stem)}


def recordings_of(cfg: Config, pid: str) -> list[Recording]:
    """The recordings where the person has a group (what the job evaluates)."""
    from .assignments import collect
    stems = {r.stem for r in collect(cfg).get(pid, []) if r.status != "print_only"}
    return [r for r in iter_recordings(cfg.out_dir) if r.stem in stems]


# ---------------------------------------------------------------- recalculating a print from its group's replies

def group_vector(rec: Recording, label: str, model: str) -> tuple[list[float] | None, int]:
    """The group's voice now: the mean of its replies' own voices, weighted by their length (unit length). After
    replies were moved this is the group as it is, unlike the diarization's average from the transcription.
    (None, 0) when no reply of the group has a computed voice."""
    cache = load_cache(rec, model)
    vecs, weights = [], []
    for s in replies_of(rec):
        if s["speaker"] != label:
            continue
        v = cache.get(_key(s["start"]))
        if v is not None:
            vecs.append(v)
            weights.append(float(s["end"]) - float(s["start"]))
    if not vecs:
        return None, 0
    m = np.average(np.asarray(vecs, dtype=np.float32), axis=0, weights=np.asarray(weights))
    n = float(np.linalg.norm(m))
    return ([round(float(x) / n, 6) for x in m] if n else None), len(vecs)


def recompute_targets(cfg: Config, pid: str, only: tuple[str, str] | None = None) -> list[tuple[Recording, str]]:
    """The person's prints to recalculate whose group still exists: one (only), else all of them."""
    from .voiceprints import Voiceprints
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    out = []
    for p in vp.people.get(pid, []):
        key = (p.get("stem") or "", p.get("label") or "")
        if only and key != only:
            continue
        try:
            rec = resolve_recording(key[0], cfg.out_dir)
        except Exception:
            continue
        if rec.transcript_path.exists() and any(s["speaker"] == key[1] for s in replies_of(rec)):
            out.append((rec, key[1]))
    return out


def recompute_estimate(cfg: Config, pid: str, only: tuple[str, str] | None = None) -> dict:
    """How much GPU work a recalculation needs (the replies without a computed voice)."""
    model = cfg.transcribe.diarize_model
    recs = {rec.stem: rec for rec, _ in recompute_targets(cfg, pid, only)}
    todo = sum(missing(r, model) for r in recs.values())
    return {"prints": len(recompute_targets(cfg, pid, only)), "missing_replies": todo,
            "estimate_s": round(todo * SECONDS_PER_REPLY + (5 if todo else 0))}


def recompute_prints(cfg: Config, pid: str, only: tuple[str, str] | None = None, progress=lambda text: None) -> dict:
    """Recalculate the person's prints (one, or all) from their groups' current replies: the replies' voices that are
    missing are computed first (GPU), then each print becomes its group's mean voice and loses the "stale" mark."""
    from .voiceprints import Voiceprints
    targets = recompute_targets(cfg, pid, only)
    compute(cfg, list({rec.stem: rec for rec, _ in targets}.values()), progress)
    model = cfg.transcribe.diarize_model
    vp = Voiceprints.load(cfg.out_dir, cfg.voiceprints.max_prints)
    done, skipped = 0, 0
    for rec, label in targets:
        vec, n = group_vector(rec, label, model)
        if vec is None:
            skipped += 1
            continue
        for p in vp.people.get(pid, []):
            if p.get("stem") == rec.stem and p.get("label") == label:
                p["v"] = vec
                p.pop("stale", None)
                p["recomputed"] = n
                done += 1
    if vp.model and vp.model != model:
        log.warning("prints were made with %s, the replies with %s", vp.model, model)
    vp.save()
    progress(f"přepočítáno {done} vzorků" + (f", {skipped} bez replik" if skipped else ""))
    return {"recomputed": done, "skipped": skipped}
