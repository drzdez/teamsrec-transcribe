"""Speaker attribution: video timeline (priority 1) -> diarization labels (fallback) -> manual speakers.json."""

from __future__ import annotations

import logging
from collections import defaultdict

from .providers.base import Segment
from .video_speakers import VideoTimeline

log = logging.getLogger(__name__)

VIDEO_MIN_OVERLAP = 0.3  # fraction of the segment that must be covered by a name's intervals


def _overlap(seg: Segment, intervals: list[list[float]]) -> float:
    return sum(max(0.0, min(e, seg.end) - max(s, seg.start)) for s, e in intervals)


def apply_video_timeline(segments: list[Segment], timeline: VideoTimeline) -> dict[str, str]:
    """Rename segment speakers in place using the video. Returns the diarization-label -> name mapping that was
    inferred by overlap (used for segments the video did not cover). Segments keep their provider label when
    neither source applies."""
    # 1) infer label -> name from total overlap (used as fallback)
    overlap: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for seg in segments:
        if seg.speaker:
            for name, iv in timeline.speakers.items():
                o = _overlap(seg, iv)
                if o > 0:
                    overlap[seg.speaker][name] += o
    mapping: dict[str, str] = {}
    for label, row in overlap.items():
        name, secs = max(row.items(), key=lambda kv: kv[1])
        if secs >= 10:  # at least 10 s of agreement before we trust the mapping
            mapping[label] = name

    # 2) per segment: best name by overlap, else fallback mapping
    n_video = n_fallback = n_kept = 0
    for seg in segments:
        best, best_o = None, 0.0
        for name, iv in timeline.speakers.items():
            o = _overlap(seg, iv)
            if o > best_o:
                best, best_o = name, o
        if best and best_o >= VIDEO_MIN_OVERLAP * max(seg.end - seg.start, 0.1):
            seg.speaker = best
            n_video += 1
        elif seg.speaker in mapping:
            seg.speaker = mapping[seg.speaker]
            n_fallback += 1
        else:
            n_kept += 1
    log.info("speakers from video: %d, from diarization mapping: %d, unresolved: %d", n_video, n_fallback, n_kept)
    return mapping


def apply_manual_names(segments: list[Segment], names: dict[str, str]) -> None:
    for seg in segments:
        if seg.speaker in names:
            seg.speaker = names[seg.speaker]


def speaker_list(segments: list[Segment]) -> list[str]:
    seen: dict[str, None] = {}
    for seg in segments:
        seen.setdefault(seg.speaker or "UNKNOWN", None)
    return list(seen)
