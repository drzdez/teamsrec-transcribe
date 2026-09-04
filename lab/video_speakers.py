"""Lab: derive an active-speaker timeline from a Teams meeting recording video.

Teams highlights the name label of the active speaker with the accent colour (~RGB 96,98,166).
We sample frames, find accent-coloured label boxes, cluster them by position, OCR each cluster once
to get the participant name, and emit name intervals. Optionally maps diarization speakers to names.

Usage:
  python video_speakers.py <video.mp4> --out out/video-speakers [--fps 2] [--transcript out/<cfg>/transcript.json]
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import ndimage

FFMPEG = "ffmpeg"
W, H = 960, 540  # analysis resolution (half of 1080p)


def frames(video, fps):
    cmd = [FFMPEG, "-loglevel", "error", "-i", video, "-vf", f"fps={fps},scale={W}:{H}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=W * H * 3 * 4)
    n = W * H * 3
    i = 0
    while True:
        buf = p.stdout.read(n)
        if len(buf) < n:
            break
        yield i / fps, np.frombuffer(buf, np.uint8).reshape(H, W, 3)
        i += 1
    p.wait()


def accent_boxes(img):
    r, g, b = img[..., 0].astype(int), img[..., 1].astype(int), img[..., 2].astype(int)
    mask = (b > 130) & (r > 60) & (r < 150) & (g > 60) & (g < 150) & (b - r > 40) & (b - g > 40)
    lab, n = ndimage.label(mask)
    boxes = []
    for sl in ndimage.find_objects(lab):
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        h, w = y1 - y0, x1 - x0
        if 6 <= h <= 24 and 20 <= w <= 200 and w > 1.8 * h:
            fill = mask[sl].mean()
            if fill > 0.45:
                boxes.append((x0, y0, x1, y1))
    return boxes


def cluster_key(box):
    x0, y0, x1, y1 = box
    return (round(x0 / 12), round(y0 / 12))


def ocr_name(video, t, box, reader):
    """Grab the full-res frame at time t and OCR the (scaled-up) box region."""
    out = subprocess.run([FFMPEG, "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    img = np.frombuffer(out, np.uint8).reshape(1080, 1920, 3)
    x0, y0, x1, y1 = [v * 2 for v in box]
    pad = 6
    crop = img[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
    res = reader.readtext(crop, detail=0, paragraph=True)
    return " ".join(res).strip()


def merge_intervals(times, step, gap=1.5):
    """times: sorted list of sample times -> list of [start, end] merged when gaps <= gap."""
    out = []
    for t in times:
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t + step
        else:
            out.append([t, t + step])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--transcript", help="whisperx transcript.json to map diarization speakers to names")
    ap.add_argument("--min-samples", type=int, default=4, help="ignore clusters seen fewer times")
    ap.add_argument("--names", help="comma-separated participant names; OCR text is fuzzy-matched to these")
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    step = 1.0 / args.fps

    hits = defaultdict(list)   # cluster key -> [(t, box)]
    cache = out / "hits.json"
    if cache.exists():
        for k, lst in json.loads(cache.read_text(encoding="utf-8")).items():
            hits[tuple(json.loads(k))] = [(t, tuple(b)) for t, b in lst]
        print(f"loaded cached hits: {len(hits)} clusters", file=sys.stderr)
    else:
        n = 0
        for t, img in frames(args.video, args.fps):
            n += 1
            for box in accent_boxes(img):
                hits[cluster_key(box)].append((t, box))
            if n % 600 == 0:
                print(f"  {t/60:5.1f} min, clusters so far: {len(hits)}", file=sys.stderr)
        print(f"frames: {n}, raw clusters: {len(hits)}", file=sys.stderr)
        cache.write_text(json.dumps({json.dumps(list(k)): v for k, v in hits.items()}), encoding="utf-8")

    # drop static clusters: UI elements of a shared screen stay lit for minutes without toggling
    def is_static(lst):
        iv = merge_intervals(sorted(t for t, _ in lst), step)
        total = sum(e - s_ for s_, e in iv)
        return len(iv) <= 2 and total > 120
    hits = defaultdict(list, {k: v for k, v in hits.items() if not is_static(v)})
    print(f"after static filter: {len(hits)} clusters", file=sys.stderr)

    import easyocr
    import difflib, re
    reader = easyocr.Reader(["cs", "en"], gpu=True, verbose=False)
    known = [n.strip() for n in args.names.split(",")] if args.names else []

    def normalize(txt):
        txt = re.sub(r"[^\w' -]", "", txt).strip()
        if known:
            m = difflib.get_close_matches(txt, known, n=1, cutoff=0.6)
            if m:
                return m[0]
        return txt or "?"
    clusters = []
    for key, lst in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        if len(lst) < args.min_samples:
            continue
        t_mid, box_mid = lst[len(lst) // 2]
        name = normalize(ocr_name(args.video, t_mid, box_mid, reader))
        clusters.append({"key": key, "samples": len(lst), "box": box_mid, "sample_time": t_mid, "ocr": name})
        print(f"cluster {key} box={box_mid} samples={len(lst)} ocr='{name}'", file=sys.stderr)
    (out / "clusters.json").write_text(json.dumps(clusters, ensure_ascii=False, indent=1), encoding="utf-8")

    # name -> times (union of clusters with same OCR text)
    by_name = defaultdict(set)
    for c in clusters:
        for t, _ in hits[tuple(c["key"])]:
            by_name[c["ocr"]].add(round(t, 3))
    timeline = {name: merge_intervals(sorted(ts), step) for name, ts in by_name.items()}
    (out / "speakers_video.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, iv in timeline.items():
        total = sum(e - s for s, e in iv)
        print(f"{name:28s} {total/60:6.1f} min in {len(iv)} intervals")

    if args.transcript:
        tr = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
        names = list(timeline)
        overlap = defaultdict(lambda: defaultdict(float))
        for seg in tr["segments"]:
            spk = seg.get("speaker", "UNKNOWN")
            for name in names:
                for s, e in timeline[name]:
                    o = min(e, seg["end"]) - max(s, seg["start"])
                    if o > 0:
                        overlap[spk][name] += o
        mapping = {}
        print("\ndiarization speaker -> video name (overlap minutes)")
        for spk in sorted(overlap):
            row = sorted(overlap[spk].items(), key=lambda kv: -kv[1])
            best, second = row[0], (row[1] if len(row) > 1 else (None, 0))
            mapping[spk] = best[0]
            print(f"  {spk:11s} -> {best[0]:26s} {best[1]/60:5.1f}   (next: {second[0]} {second[1]/60:.1f})")
        (out / "speakers.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")

        # label each segment: video name with max overlap; fallback to diarization mapping
        from datetime import timedelta
        lines, stats = [], defaultdict(int)
        for seg in tr["segments"]:
            best, best_o = None, 0.0
            for name in names:
                o = sum(max(0.0, min(e, seg["end"]) - max(s_, seg["start"])) for s_, e in timeline[name])
                if o > best_o:
                    best, best_o = name, o
            dur = seg["end"] - seg["start"]
            if best and best_o >= 0.3 * dur:
                who, src = best, "video"
            else:
                who, src = mapping.get(seg.get("speaker", "UNKNOWN"), "?"), "diar"
            stats[src] += 1
            ts = str(timedelta(seconds=int(seg["start"])))
            lines.append(f"[{ts:>8}] {who}: {seg['text'].strip()}")
        (out / "transcript_named.txt").write_text("\n".join(lines), encoding="utf-8")
        print(f"segments labelled from video: {stats['video']}, from diarization fallback: {stats['diar']}")


if __name__ == "__main__":
    main()
