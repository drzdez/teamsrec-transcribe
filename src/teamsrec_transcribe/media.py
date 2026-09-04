"""ffmpeg / ffprobe helpers. Both binaries must be on PATH (or given via TEAMSREC_FFMPEG_DIR)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".aac", ".wma"}


class MediaError(RuntimeError):
    pass


def ensure_ffmpeg_on_path() -> None:
    """Prepend TEAMSREC_FFMPEG_DIR to PATH so that libraries calling `ffmpeg` by name (whisperx) find it too."""
    d = os.environ.get("TEAMSREC_FFMPEG_DIR")
    if d and d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _bin(name: str) -> str:
    ensure_ffmpeg_on_path()
    found = shutil.which(name)
    if not found:
        raise MediaError(f"{name} not found on PATH (set TEAMSREC_FFMPEG_DIR or install ffmpeg)")
    return found


def ffmpeg_available() -> bool:
    try:
        _bin("ffmpeg"); _bin("ffprobe")
        return True
    except MediaError:
        return False


@dataclass(frozen=True)
class MediaInfo:
    duration_s: float
    has_video: bool
    has_audio: bool
    creation_time: datetime | None  # local, naive
    width: int | None = None
    height: int | None = None
    sample_rate: int | None = None
    channels: int | None = None


def probe(path: Path) -> MediaInfo:
    cmd = [_bin("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise MediaError(f"ffprobe failed for {path.name}: {r.stderr.strip()[-300:]}")
    data = json.loads(r.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) == 0), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    ct = fmt.get("tags", {}).get("creation_time") or (audio or {}).get("tags", {}).get("creation_time")
    creation = None
    if ct:
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            creation = dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
            if creation.year < 2000:  # bogus epoch-ish values
                creation = None
        except ValueError:
            creation = None
    return MediaInfo(
        duration_s=float(fmt.get("duration", 0) or 0),
        has_video=video is not None,
        has_audio=audio is not None,
        creation_time=creation,
        width=int(video["width"]) if video and "width" in video else None,
        height=int(video["height"]) if video and "height" in video else None,
        sample_rate=int(audio["sample_rate"]) if audio and "sample_rate" in audio else None,
        channels=int(audio["channels"]) if audio and "channels" in audio else None,
    )


def to_mix_wav(src: Path, dst: Path, sample_rate: int = 16000) -> None:
    """Extract/convert audio to mono 16 kHz PCM16 WAV (WhisperX-ready)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_bin("ffmpeg"), "-y", "-loglevel", "error", "-i", str(src), "-vn",
           "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise MediaError(f"ffmpeg failed: {r.stderr.strip()[-300:]}")


def mix_tracks(tracks: list[Path], dst: Path, sample_rate: int = 16000) -> None:
    """Sum several WAV tracks into one mono 16 kHz file (fallback when capture produced no _mix)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_bin("ffmpeg"), "-y", "-loglevel", "error"]
    for t in tracks:
        cmd += ["-i", str(t)]
    cmd += ["-filter_complex", f"amix=inputs={len(tracks)}:duration=longest:normalize=0",
            "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise MediaError(f"ffmpeg amix failed: {r.stderr.strip()[-300:]}")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()
