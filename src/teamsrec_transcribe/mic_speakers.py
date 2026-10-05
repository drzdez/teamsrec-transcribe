"""Name the user's own voice from the microphone track of a live recording.

teamsrec-capture writes two tracks: `sys` (what the others say, via loopback) and `mic` (only the user's
microphone). Wherever the mic carries speech, the person talking is the user, so the diarization label that
coincides with mic activity gets the name from the config (`[user] name`). No model involved, no guessing.

Calibrated on a real 29-minute Teams call (headset over a Bluetooth dongle): the user's label had the mic
active 88 % of its time, the other three labels 13-21 % (cross-talk, "mhm", room noise while others spoke).

"Active" also needs an absolute level (ACTIVE_MIN_DB). A headset with its own noise gate (Sony WH-1000XM6
connected directly over Bluetooth, 2026-09-30) sends almost digital silence between words: the floor was
-104 dBFS, so floor + 10 dB counted its -80..-90 dB residue while the others talked as speech, and the others'
segments had the mic "active" 61 % of the time; all three people ended up under the user's name. With the
absolute minimum it is 5 % (user 92 %); on the dongle recordings 11-12 % (was 28-34 %), user 86-88 %.
"""

from __future__ import annotations

import logging
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

from .providers.base import Segment

log = logging.getLogger(__name__)

FRAME_S = 0.1
FLOOR_PERCENTILE = 20      # the quiet part of the track defines the noise floor
ACTIVE_ABOVE_FLOOR_DB = 10  # a frame is "speech" this far above the floor
ACTIVE_MIN_DB = -55.0       # ... and at least this loud: residue of a noise-gated headset is not speech
LABEL_MIN_ACTIVE = 0.6      # a diarization label is the user when >= 60 % of its time has the mic active
LABEL_MIN_SECONDS = 10      # ... and it spoke at least this long
SEGMENT_MIN_ACTIVE = 0.8    # a single segment of an unmapped label is the user when >= 80 % mic-active
SEGMENT_MIN_SECONDS = 1.5   # ... and it is long enough to mean more than "mhm"
SEGMENT_MAX_SILENT = 0.35   # a reply of the user's label with the mic this quiet was said (mostly) by someone else


def _rms_db(path: Path, frame_s: float = FRAME_S) -> tuple[np.ndarray, float]:
    """Per-frame RMS level in dBFS for a PCM wav (16/32-bit, any channel count)."""
    with wave.open(str(path), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"{path.name}: unsupported sample width {sw}")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    hop = max(int(sr * frame_s), 1)
    nfr = len(a) // hop
    if nfr == 0:
        return np.zeros(0, dtype=np.float32), frame_s
    fr = a[: nfr * hop].reshape(nfr, hop)
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9), frame_s


def mic_activity(mic_wav: Path) -> tuple[np.ndarray, float] | None:
    """Boolean per-frame speech mask for the mic track, or None when the track looks unusable
    (dead mic, or a mic that is 'on' the whole time, e.g. no headset and the speakers leak into it)."""
    db, frame_s = _rms_db(mic_wav)
    if len(db) < 10 / frame_s:  # under 10 s
        return None
    floor = float(np.percentile(db, FLOOR_PERCENTILE))
    active = db > max(floor + ACTIVE_ABOVE_FLOOR_DB, ACTIVE_MIN_DB)
    share = float(active.mean())
    if share < 0.01 or share > 0.97:
        log.info("mic track: %.0f%% active frames, not usable for speaker naming", share * 100)
        return None
    return active, frame_s


def _active_fraction(active: np.ndarray, frame_s: float, start: float, end: float) -> float:
    a, b = int(start / frame_s), int(end / frame_s) + 1
    fr = active[a:b]
    return float(fr.mean()) if len(fr) else 0.0


def apply_mic_track(segments: list[Segment], mic_wav: Path, me: str) -> dict[str, str]:
    """Rename the user's diarization label(s) to `me` in place; only labels still unnamed (SPEAKER_*) are
    touched, so names from the video win. Returns the label -> name mapping that was applied."""
    act = mic_activity(mic_wav)
    if act is None:
        return {}
    active, frame_s = act

    # per label: how much of its speaking time has the mic active
    time: dict[str, float] = defaultdict(float)
    hot: dict[str, float] = defaultdict(float)
    for seg in segments:
        if seg.speaker and seg.speaker.startswith("SPEAKER_"):
            dur = max(seg.end - seg.start, 0.0)
            time[seg.speaker] += dur
            hot[seg.speaker] += dur * _active_fraction(active, frame_s, seg.start, seg.end)
    mapping = {lab: me for lab in time
               if time[lab] >= LABEL_MIN_SECONDS and hot[lab] / time[lab] >= LABEL_MIN_ACTIVE}
    for lab in sorted(time):
        log.info("mic track: %s %5.1f min, mic active %3.0f%%%s", lab, time[lab] / 60,
                 100 * hot[lab] / max(time[lab], 1e-9), "  -> " + me if lab in mapping else "")

    n_label = n_seg = n_kept = 0
    for seg in segments:
        if seg.speaker in mapping:
            # the label is the user, but a longer reply in it with the mic silent is somebody else's: it keeps the
            # label, so it shows as its own group (2026-10-05: the cloud diarization put another person's replies
            # into the user's label, and the whole label became the user)
            if (seg.end - seg.start >= SEGMENT_MIN_SECONDS
                    and _active_fraction(active, frame_s, seg.start, seg.end) < SEGMENT_MAX_SILENT):
                n_kept += 1
                continue
            seg.speaker = me
            n_label += 1
        elif ((not seg.speaker or seg.speaker == "UNKNOWN" or seg.speaker.startswith("SPEAKER_"))
              and seg.end - seg.start >= SEGMENT_MIN_SECONDS
              and _active_fraction(active, frame_s, seg.start, seg.end) >= SEGMENT_MIN_ACTIVE):
            # also a reply the diarization gave nobody: the mic says it was the user
            seg.speaker = me
            n_seg += 1
    log.info("speakers from mic track: %d segments via label mapping, %d via segment activity, %d left to their "
             "label (mic silent)", n_label, n_seg, n_kept)
    return mapping
