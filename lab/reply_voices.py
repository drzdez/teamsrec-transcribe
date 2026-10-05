"""Per-reply voice embeddings: can they find replies that belong to someone else, without listening to all of them?

Usage: python lab/reply_voices.py <stem dir>
Embeds every reply (>= MIN_S seconds of speech) with the embedding model of the diarization pipeline
(pyannote community-1, the same space as the voice prints), then for each speaker label prints how many of its
replies sound more like another label or a known person. Writes <stem>.voices.json next to the recording.
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

MIN_S = 1.5
MAX_S = 10.0


def main(folder: Path) -> None:
    stem = folder.name
    tr = json.loads((folder / f"{stem}.transcript.json").read_text(encoding="utf-8"))
    import torch
    import whisperx
    from whisperx.diarize import DiarizationPipeline

    wav = whisperx.load_audio(str(folder / f"{stem}_mix.wav"))  # 16 kHz float32
    dp = DiarizationPipeline(model_name=tr.get("diarize_model") or "pyannote/speaker-diarization-community-1",
                             device="cuda")
    emb_model = dp.model._embedding
    t = time.time()
    segs = [s for s in tr["segments"] if s["end"] - s["start"] >= MIN_S]
    vecs = []
    batch = []

    def flush():
        if not batch:
            return
        n = max(len(b) for b in batch)
        x = np.zeros((len(batch), 1, n), dtype=np.float32)
        mask = np.zeros((len(batch), n), dtype=np.float32)
        for i, b in enumerate(batch):
            x[i, 0, :len(b)] = b
            mask[i, :len(b)] = 1
        out = emb_model(torch.from_numpy(x), masks=torch.from_numpy(mask))
        vecs.extend(out)
        batch.clear()

    for s in segs:
        a = wav[int(s["start"] * 16000):int(min(s["end"], s["start"] + MAX_S) * 16000)]
        batch.append(a)
        if len(batch) == 32:
            flush()
    flush()
    print(f"{len(segs)} replies embedded in {time.time() - t:.1f} s")
    V = np.array(vecs, dtype=np.float32)
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9

    by = defaultdict(list)
    for i, s in enumerate(segs):
        by[s.get("speaker") or "UNKNOWN"].append(i)
    cent = {}
    for lab, idx in by.items():
        w = np.array([segs[i]["end"] - segs[i]["start"] for i in idx])
        c = (V[idx] * w[:, None]).sum(0)
        cent[lab] = c / (np.linalg.norm(c) + 1e-9)
    prints = {}
    vp = folder.parents[2] / "_speakers" / "voiceprints.json"
    if vp.exists():
        for pid, items in json.loads(vp.read_text(encoding="utf-8"))["people"].items():
            m = np.mean([np.array(it["v"]) for it in items], axis=0)
            prints["@" + pid] = m / (np.linalg.norm(m) + 1e-9)
    refs = {**cent, **prints}
    names = list(refs)
    R = np.array([refs[k] for k in names])
    for lab, idx in sorted(by.items(), key=lambda kv: -len(kv[1])):
        own = V[idx] @ cent[lab]
        sims = V[idx] @ R.T
        odd = []
        for k, i in enumerate(idx):
            order = np.argsort(-sims[k])
            best = next(names[j] for j in order if names[j] != lab)
            bs = sims[k][names.index(best)]
            if bs - own[k] > 0.05:
                odd.append((segs[i]["start"], round(float(own[k]), 2), best, round(float(bs), 2)))
        print(f"{lab:28s} {len(idx):4d} replies, own-sim median {np.median(own):.2f} min {own.min():.2f}; "
              f"{len(odd)} sound more like someone else")
        for o in odd[:8]:
            print(f"    {o[0]:8.1f}s own {o[1]}  -> {o[2]} {o[3]}")
    out = {"format": 1, "model": tr.get("diarize_model"), "min_s": MIN_S,
           "replies": {f"{s['start']:.2f}": [round(float(x), 4) for x in V[i]] for i, s in enumerate(segs)}}
    (folder / f"{stem}.voices.json").write_text(json.dumps(out), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
