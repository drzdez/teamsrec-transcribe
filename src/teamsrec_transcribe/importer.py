"""Import any audio/video file into the recordings folder as a `source: import` recording.

Metadata precedence: Teams file-name pattern -> container creation_time -> file name + mtime; user flags override all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import APP_NAME, FORMAT_VERSION, __version__
from .media import MediaError, probe, to_mix_wav
from .recording import Recording, RecordingError, make_stem, recording_dir, slugify

log = logging.getLogger(__name__)

# "WFMS Future Version-20260903_133158-Meeting Recording.mp4"
TEAMS_NAME_RE = re.compile(r"^(?P<title>.+?)-(?P<date>\d{8})_(?P<time>\d{6})-Meeting Recording(?: ?\(\d+\))?$")


@dataclass(frozen=True)
class ImportMeta:
    title: str
    start: datetime
    source: str  # teams-name | container | file | user


def derive_metadata(path: Path, creation_time: datetime | None) -> ImportMeta:
    m = TEAMS_NAME_RE.match(path.stem)
    if m:
        start = datetime.strptime(m.group("date") + m.group("time"), "%Y%m%d%H%M%S")
        return ImportMeta(title=m.group("title").strip(), start=start, source="teams-name")
    if creation_time:
        return ImportMeta(title=path.stem, start=creation_time, source="container")
    mtime = datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0)
    return ImportMeta(title=path.stem, start=mtime, source="file")


def import_file(
    src: Path,
    out_dir: Path,
    *,
    title: str | None = None,
    start: datetime | None = None,
    language: str | None = None,
    participants: list[str] | None = None,
    force: bool = False,
) -> Recording:
    """Create <out_dir>/YYYY/MM/<stem>/<stem>{_mix.wav,.json} for the file. Returns the existing recording when
    it was imported before (same origin_path) unless force=True."""
    src = src.resolve()
    if not src.exists():
        raise RecordingError(f"file not found: {src}")
    info = probe(src)
    if not info.has_audio:
        raise MediaError(f"{src.name} has no audio stream")

    meta = derive_metadata(src, info.creation_time)
    if title or start:
        meta = ImportMeta(title=title or meta.title, start=start or meta.start, source="user")

    stem = make_stem(meta.start, meta.title)
    dest_dir = recording_dir(out_dir, meta.start, stem)
    stem_path = dest_dir / stem
    sidecar_path = stem_path.with_name(stem + ".json")
    if sidecar_path.exists() and not force:
        rec = Recording.load(sidecar_path)
        log.info("already imported: %s", rec.stem)
        return rec

    mix_name = stem + "_mix.wav"
    log.info("importing %s -> %s", src.name, stem_path)
    to_mix_wav(src, dest_dir / mix_name)

    sidecar = {
        "format": FORMAT_VERSION,
        "app": APP_NAME,
        "app_version": __version__,
        "title": meta.title,
        "slug": slugify(meta.title),
        "source": "import",
        "start": meta.start.replace(microsecond=0).isoformat(),
        "end": (meta.start.replace(microsecond=0) + _seconds(info.duration_s)).isoformat(),
        "duration_s": int(round(info.duration_s)),
        "stop_reason": "n/a",
        "tracks": {},
        "mix": {"file": mix_name, "sample_rate": 16000, "channels": 1},
        "origin_file": src.name,
        "origin_path": str(src),
        "metadata_source": meta.source,
    }
    if language:
        sidecar["language"] = language
    if participants:
        sidecar["participants"] = [{"name": p.strip()} for p in participants if p.strip()]
    rec = Recording(stem_path=stem_path, sidecar=sidecar)
    rec.save_sidecar()  # written last: marks the recording as complete
    return rec


def _seconds(s: float):
    from datetime import timedelta
    return timedelta(seconds=int(round(s)))
