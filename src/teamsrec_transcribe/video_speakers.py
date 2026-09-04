"""Active-speaker timeline from a Teams meeting recording video.

Teams highlights the active speaker's name label with its accent colour (~RGB 96,98,166) in the gallery, in the
participant strip next to a shared screen and in the presenter label bottom-left. We sample frames, find
accent-coloured label-sized blobs, cluster them by position, drop static clusters (shared-screen UI never toggles),
OCR one representative frame per cluster, fuzzy-match to known participant names and merge into per-name intervals.

Output (contract <stem>.speakers_video.json):
  {"format": 1, "source": "teams-video", "fps": 2, "speakers": {"Name": [[start, end], ...]}}
Returns None when no toggling label clusters are found (not a Teams layout / no labels visible).
"""

from __future__ import annotations

import difflib
import logging
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import FORMAT_VERSION
from .media import _bin

log = logging.getLogger(__name__)

W, H = 960, 540  # analysis resolution


@dataclass
class VideoTimeline:
    fps: float
    speakers: dict[str, list[list[float]]]
    clusters: list[dict]

    def to_json(self) -> dict:
        return {"format": FORMAT_VERSION, "source": "teams-video", "fps": self.fps,
                "speakers": self.speakers, "clusters": self.clusters}

    @classmethod
    def from_json(cls, data: dict) -> "VideoTimeline":
        return cls(fps=float(data.get("fps", 2)), speakers=data["speakers"], clusters=data.get("clusters", []))

    def total_seconds(self, name: str) -> float:
        return sum(e - s for s, e in self.speakers.get(name, []))


def _frames(video: Path, fps: float):
    cmd = [_bin("ffmpeg"), "-loglevel", "error", "-i", str(video), "-vf", f"fps={fps},scale={W}:{H}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=W * H * 3 * 4)
    n = W * H * 3
    i = 0
    try:
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                break
            yield i / fps, np.frombuffer(buf, np.uint8).reshape(H, W, 3)
            i += 1
    finally:
        p.stdout.close()
        p.wait()


def _accent_boxes(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    from scipy import ndimage
    r, g, b = img[..., 0].astype(int), img[..., 1].astype(int), img[..., 2].astype(int)
    mask = (b > 130) & (r > 60) & (r < 150) & (g > 60) & (g < 150) & (b - r > 40) & (b - g > 40)
    lab, _ = ndimage.label(mask)
    boxes = []
    for sl in ndimage.find_objects(lab):
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        h, w = y1 - y0, x1 - x0
        if 6 <= h <= 24 and 20 <= w <= 200 and w > 1.8 * h and mask[sl].mean() > 0.45:
            boxes.append((x0, y0, x1, y1))
    return boxes


def _cluster_key(box) -> tuple[int, int]:
    return (round(box[0] / 12), round(box[1] / 12))


def _merge(times: list[float], step: float, gap: float = 1.5) -> list[list[float]]:
    out: list[list[float]] = []
    for t in times:
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t + step
        else:
            out.append([t, t + step])
    return [[round(s, 2), round(e, 2)] for s, e in out]


def _ocr_label(video: Path, t: float, box, reader, full_w: int, full_h: int) -> str:
    raw = subprocess.run([_bin("ffmpeg"), "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    if len(raw) < full_w * full_h * 3:
        return ""
    img = np.frombuffer(raw, np.uint8).reshape(full_h, full_w, 3)
    sx, sy = full_w / W, full_h / H
    x0, y0, x1, y1 = int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy)
    pad = 6
    crop = img[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
    return " ".join(reader.readtext(crop, detail=0, paragraph=True)).strip()


def _normalize(txt: str, known: list[str]) -> str:
    txt = re.sub(r"[^\w' -]", "", txt).strip()
    if known:
        m = difflib.get_close_matches(txt, known, n=1, cutoff=0.6)
        if m:
            return m[0]
    return txt or "?"


def analyze_video(video: Path, *, fps: float = 2.0, names: list[str] | None = None,
                  min_samples: int = 4, width: int = 1920, height: int = 1080) -> VideoTimeline | None:
    step = 1.0 / fps
    hits: dict[tuple[int, int], list[tuple[float, tuple]]] = defaultdict(list)
    n = 0
    for t, img in _frames(video, fps):
        n += 1
        for box in _accent_boxes(img):
            hits[_cluster_key(box)].append((t, box))
    log.info("video: %d frames, %d raw label clusters", n, len(hits))

    def is_static(lst):
        iv = _merge(sorted(t for t, _ in lst), step)
        return len(iv) <= 2 and sum(e - s for s, e in iv) > 120

    hits = {k: v for k, v in hits.items() if len(v) >= min_samples and not is_static(v)}
    if not hits:
        log.info("video: no toggling name labels found; not a Teams layout?")
        return None

    import easyocr
    reader = easyocr.Reader(["cs", "en"], gpu=True, verbose=False)
    known = names or []
    clusters, by_name = [], defaultdict(set)
    for key, lst in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        t_mid, box_mid = lst[len(lst) // 2]
        name = _normalize(_ocr_label(video, t_mid, box_mid, reader, width, height), known)
        clusters.append({"box": [int(v) for v in box_mid], "samples": len(lst), "name": name})
        for t, _ in lst:
            by_name[name].add(round(t, 3))
        log.info("video: label at %s x%d -> %s", box_mid, len(lst), name)
    speakers = {name: _merge(sorted(ts), step) for name, ts in by_name.items() if name != "?"}
    return VideoTimeline(fps=fps, speakers=speakers, clusters=clusters)
