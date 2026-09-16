"""Speaker attribution: video timeline (priority 1) -> diarization labels (fallback) -> manual speakers.json."""

from __future__ import annotations

import logging
from collections import defaultdict

from .providers.base import Segment
from .video_speakers import VideoTimeline

log = logging.getLogger(__name__)

VIDEO_MIN_OVERLAP = 0.3  # fraction of the segment that must be covered by a name's intervals
MAP_MIN_SECONDS = 10     # label -> name mapping needs this much agreement ...
MAP_MIN_SHARE = 0.6      # ... which must be most of the label's video-covered time ...
MAP_MIN_COVERAGE = 0.25  # ... and a quarter of everything the label said (a 4-minute highlight cannot own 30 minutes)


def _overlap(seg: Segment, intervals: list[list[float]]) -> float:
    return sum(max(0.0, min(e, seg.end) - max(s, seg.start)) for s, e in intervals)


def video_label_mapping(segments: list[Segment], timeline: VideoTimeline,
                        original: list[str | None] | None = None) -> dict[str, str]:
    """Diarization label -> name, inferred from how much of the label's speech the video attributes to one
    person. `original` are the labels before any renaming (the direct pass already renamed covered segments,
    and those are exactly the evidence). Strict on purpose: the highlight covers only part of the speech, and
    with many participants a short highlight must not swallow a long label (a 4.5-minute highlight once
    "owned" 47 minutes)."""
    original = original or [s.speaker for s in segments]
    overlap: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    total: dict[str, float] = defaultdict(float)
    for seg, label in zip(segments, original):
        if label and label.startswith("SPEAKER_"):
            total[label] += max(seg.end - seg.start, 0.0)
            for name, iv in timeline.speakers.items():
                o = _overlap(seg, iv)
                if o > 0:
                    overlap[label][name] += o
    mapping: dict[str, str] = {}
    for label, row in overlap.items():
        name, secs = max(row.items(), key=lambda kv: kv[1])
        covered = sum(row.values())
        if secs >= MAP_MIN_SECONDS and secs >= MAP_MIN_SHARE * covered and secs >= MAP_MIN_COVERAGE * total[label]:
            mapping[label] = name
        else:
            log.info("video mapping: %s stays (best %s %.0f s of %.0f s covered, %.0f s total)", label, name, secs,
                     covered, total[label])
    return mapping


def apply_video_timeline(segments: list[Segment], timeline: VideoTimeline, fallback: bool = True) -> dict[str, str]:
    """Rename segment speakers in place using the video: a segment that the highlight covers gets that name;
    with `fallback`, whole labels are mapped by video_label_mapping. Returns the mapping applied."""
    original = [s.speaker for s in segments]
    n_video = n_kept = 0
    for seg in segments:
        best, best_o = None, 0.0
        for name, iv in timeline.speakers.items():
            o = _overlap(seg, iv)
            if o > best_o:
                best, best_o = name, o
        if best and best_o >= VIDEO_MIN_OVERLAP * max(seg.end - seg.start, 0.1):
            seg.speaker = best
            n_video += 1
        else:
            n_kept += 1
    log.info("speakers from video: %d segments, %d not covered", n_video, n_kept)
    return apply_video_fallback(segments, timeline, original) if fallback else {}


def apply_video_fallback(segments: list[Segment], timeline: VideoTimeline,
                         original: list[str | None] | None = None) -> dict[str, str]:
    """Second pass (after the mic track named the user): labels still unnamed that the video attributes
    clearly, judged on the original labels (`original`, the labels before the direct pass)."""
    mapping = video_label_mapping(segments, timeline, original)
    n = 0
    for seg in segments:
        if seg.speaker in mapping:  # still carrying the label, i.e. not named by the highlight or the mic
            seg.speaker = mapping[seg.speaker]
            n += 1
    if mapping:
        log.info("speakers from video mapping: %d segments (%s)", n, ", ".join(f"{k}->{v}" for k, v in mapping.items()))
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
