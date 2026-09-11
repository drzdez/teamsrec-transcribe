"""Calibrate voice-print matching: diarize every recording in OUT_DIR with speaker embeddings, name the
diarization labels by overlap with the existing transcript (which already carries names from the mic track,
the video and speakers.json), then print the cosine similarity between every pair of (recording, speaker).

Same person across recordings should score clearly above different people. Run:
  .venv/Scripts/python lab/voiceprints_calib.py [OUT_DIR]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from teamsrec_transcribe.config import load_config  # noqa: E402
from teamsrec_transcribe.media import ensure_ffmpeg_on_path  # noqa: E402
from teamsrec_transcribe.people import People  # noqa: E402
from teamsrec_transcribe.recording import iter_recordings  # noqa: E402


def main() -> None:
    ensure_ffmpeg_on_path()
    cfg = load_config()
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else cfg.out_dir
    people = People.load(out_dir, cfg.people_display)
    import whisperx
    from whisperx.diarize import DiarizationPipeline
    pipe = DiarizationPipeline(model_name=cfg.transcribe.diarize_model, device="cuda")

    rows: list[tuple[str, str, np.ndarray]] = []  # (stem, name, unit vector)
    for rec in iter_recordings(out_dir):
        if not (rec.mix_path and rec.mix_path.exists() and rec.transcript_path.exists()):
            continue
        t = rec.read_json(rec.transcript_path)
        names = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
        segs = [(s["start"], s["end"], people.display(names.get(s["speaker"], s["speaker"])) or "?") for s in t["segments"]]
        wav = whisperx.load_audio(str(rec.mix_path))
        dia, emb = pipe(wav, return_embeddings=True)
        # name each diarization label by the transcript speaker it overlaps most
        overlap: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for _, r in dia.iterrows():
            for s, e, who in segs:
                o = max(0.0, min(e, r["end"]) - max(s, r["start"]))
                if o > 0:
                    overlap[r["speaker"]][who] += o
        for label, vec in (emb or {}).items():
            v = np.asarray(vec, dtype=np.float32)
            if not np.isfinite(v).all():
                continue
            v /= np.linalg.norm(v) + 1e-9
            who = max(overlap[label].items(), key=lambda kv: kv[1])[0] if overlap[label] else "?"
            secs = sum(overlap[label].values())
            rows.append((rec.stem[:16], f"{who}[{label[-2:]}]", v))
            print(f"{rec.stem[:16]} {label} -> {who:24s} {secs/60:5.1f} min")

    print("\ncosine similarity (same person across recordings should be high):")
    names = [f"{s} {n}" for s, n, _ in rows]
    w = max(len(n) for n in names)
    print(" " * w, " ".join(f"{i:>5d}" for i in range(len(rows))))
    for i, (_, _, a) in enumerate(rows):
        sims = [float(a @ b) for _, _, b in rows]
        print(f"{names[i]:{w}s}", " ".join(f"{x:5.2f}" for x in sims), f"  #{i}")
    same, diff = [], []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            ni, nj = rows[i][1].split("[")[0], rows[j][1].split("[")[0]
            if ni == "?" or nj == "?" or ni.startswith("SPEAKER") or nj.startswith("SPEAKER"):
                continue
            (same if ni == nj else diff).append(float(rows[i][2] @ rows[j][2]))
    if same:
        print(f"\nsame person: n={len(same)} min={min(same):.2f} mean={np.mean(same):.2f}")
    if diff:
        print(f"different:   n={len(diff)} max={max(diff):.2f} mean={np.mean(diff):.2f}")


if __name__ == "__main__":
    main()
