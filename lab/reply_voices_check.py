"""Second look at <stem>.voices.json (made by reply_voices.py): where do the replies sit relative to each other?

For every label: similarity of its replies to the known people's prints, and to each other (pairwise median),
and the same for all pairs of labels. A label whose replies are close to everyone is a sign of a channel effect
(the same microphone / codec) rather than a voice.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np


def main(folder: Path) -> None:
    stem = folder.name
    tr = json.loads((folder / f"{stem}.transcript.json").read_text(encoding="utf-8"))
    vo = json.loads((folder / f"{stem}.voices.json").read_text(encoding="utf-8"))["replies"]
    segs = [s for s in tr["segments"] if f"{s['start']:.2f}" in vo]
    V = np.array([vo[f"{s['start']:.2f}"] for s in segs], dtype=np.float32)
    by = defaultdict(list)
    for i, s in enumerate(segs):
        by[s.get("speaker") or "UNKNOWN"].append(i)
    labs = sorted(by, key=lambda k: -len(by[k]))
    print("pairwise median similarity of replies (rows x cols):")
    print(" " * 22 + "".join(f"{l[:10]:>11s}" for l in labs))
    for a in labs:
        row = []
        for b in labs:
            m = V[by[a]] @ V[by[b]].T
            if a == b:
                m = m[~np.eye(len(by[a]), dtype=bool)]
            row.append(np.median(m))
        print(f"{a[:22]:22s}" + "".join(f"{x:11.2f}" for x in row))
    vp = folder.parents[2] / "_speakers" / "voiceprints.json"
    people = json.loads(vp.read_text(encoding="utf-8"))["people"]
    print("\nmedian similarity of each label's replies to the stored prints (mean print per person):")
    pids = sorted(people)
    P = np.array([np.mean([it["v"] for it in people[p]], axis=0) for p in pids])
    P /= np.linalg.norm(P, axis=1, keepdims=True)
    print(" " * 22 + "".join(f"{p[:10]:>11s}" for p in pids))
    for a in labs:
        print(f"{a[:22]:22s}" + "".join(f"{x:11.2f}" for x in np.median(V[by[a]] @ P.T, axis=0)))
    # which track: mic-attributed replies vs the rest
    mic = [s for s in segs if s.get("track") == "mic"]
    print(f"\nreplies with track=mic: {len(mic)}; speaker_sources {tr.get('speaker_sources')}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
