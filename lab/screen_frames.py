"""Inspect a captured Teams window video: sample frames, draw the accent-coloured label boxes the analysis
sees, and print how many boxes toggle. Use it to check the colour filter on live windows (the recorded Teams
MP4 and the live gallery may render the active-speaker highlight differently).

  .venv/Scripts/python lab/screen_frames.py D:\\meetings\\2026\\09\\<stem>\\<stem>_screen1.mp4 [OUT_DIR] [--every 30]

Writes <OUT_DIR>/frame_<t>.png with red rectangles around detected boxes.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from teamsrec_transcribe.media import ensure_ffmpeg_on_path  # noqa: E402
from teamsrec_transcribe.video_speakers import _accent_boxes, _cluster_key, _frames  # noqa: E402


def main() -> None:
    ensure_ffmpeg_on_path()
    video = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else video.parent / "_frames"
    every = int(sys.argv[sys.argv.index("--every") + 1]) if "--every" in sys.argv else 30
    out.mkdir(parents=True, exist_ok=True)
    fps = 2.0
    clusters: Counter = Counter()
    n = saved = 0
    for t, img in _frames(video, fps):
        n += 1
        boxes = _accent_boxes(img)
        for b in boxes:
            clusters[_cluster_key(b)] += 1
        if n % every == 1:
            im = Image.fromarray(img)
            d = ImageDraw.Draw(im)
            for x0, y0, x1, y1 in boxes:
                d.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=2)
            im.save(out / f"frame_{t:07.1f}s.png")
            saved += 1
            print(f"t={t:7.1f}s boxes={len(boxes)} {boxes[:4]}")
    print(f"\n{n} frames, {saved} saved to {out}")
    print("label clusters (position -> frames seen), most frequent first:")
    for key, cnt in clusters.most_common(12):
        print(f"  x~{key[0]*12:4d} y~{key[1]*12:4d}  {cnt:5d} frames ({100*cnt/max(n,1):4.1f} %)")
    print("\nA speaker label toggles: expect several clusters each seen in 5-60 % of frames. A cluster in ~100 % is static UI.")


if __name__ == "__main__":
    main()
