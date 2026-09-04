"""A recording = a stem in <OUT_DIR>/YYYY/MM plus its sidecar and derived files (contract format v1)."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import FORMAT_VERSION
from .media import MEDIA_SUFFIXES

STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_[a-z0-9-]+$")


def slugify(text: str, n: int = 60) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:n].strip("-") or "recording"


def make_stem(start: datetime, title: str) -> str:
    return f"{start:%Y-%m-%d_%H%M}_{slugify(title)}"


def recording_dir(out_dir: Path, start: datetime) -> Path:
    return out_dir / f"{start:%Y}" / f"{start:%m}"


class RecordingError(RuntimeError):
    pass


@dataclass
class Recording:
    stem_path: Path  # directory/stem without suffix
    sidecar: dict[str, Any]

    # ---- paths
    @property
    def stem(self) -> str:
        return self.stem_path.name

    @property
    def dir(self) -> Path:
        return self.stem_path.parent

    def file(self, suffix: str) -> Path:
        return self.stem_path.with_name(self.stem + suffix)

    @property
    def sidecar_path(self) -> Path:
        return self.file(".json")

    @property
    def transcript_path(self) -> Path:
        return self.file(".transcript.json")

    @property
    def speakers_path(self) -> Path:
        return self.file(".speakers.json")

    @property
    def speakers_video_path(self) -> Path:
        return self.file(".speakers_video.json")

    @property
    def summary_path(self) -> Path:
        return self.file(".summary.md")

    def track_path(self, name: str) -> Path | None:
        t = (self.sidecar.get("tracks") or {}).get(name)
        return self.dir / t["file"] if t else None

    @property
    def mix_path(self) -> Path | None:
        m = self.sidecar.get("mix")
        return self.dir / m["file"] if m else None

    @property
    def title(self) -> str:
        return self.sidecar.get("title", self.stem)

    @property
    def source(self) -> str:
        return self.sidecar.get("source", "live")

    @property
    def participants(self) -> list[str]:
        return [p["name"] for p in self.sidecar.get("participants", []) if p.get("name")]

    @property
    def origin_path(self) -> Path | None:
        p = self.sidecar.get("origin_path")
        return Path(p) if p else None

    # ---- io
    @classmethod
    def load(cls, sidecar_path: Path) -> "Recording":
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        fmt = data.get("format")
        if fmt != FORMAT_VERSION:
            raise RecordingError(f"{sidecar_path.name}: unsupported sidecar format {fmt!r} (expected {FORMAT_VERSION})")
        stem = sidecar_path.name[: -len(".json")]
        return cls(stem_path=sidecar_path.with_name(stem), sidecar=data)

    def save_sidecar(self) -> None:
        self.sidecar_path.write_text(json.dumps(self.sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def write_json(self, path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    def read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))


def is_sidecar(path: Path) -> bool:
    """Sidecars end with .json but not with the derived .transcript.json / .speakers.json etc."""
    name = path.name
    return name.endswith(".json") and name.count(".") == 1 and STEM_RE.match(name[:-5]) is not None


def is_media_file(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_SUFFIXES


def resolve_recording(target: Path | str, out_dir: Path) -> Recording:
    """Accepts a sidecar path, a stem path (with or without suffix), or a bare stem searched under out_dir."""
    p = Path(target)
    if p.exists() and p.is_file():
        if is_sidecar(p):
            return Recording.load(p)
        # any file of the recording set: strip everything after the stem
        m = re.match(r"^(\d{4}-\d{2}-\d{2}_\d{4}_[a-z0-9-]+?)(?:_sys|_mic|_mix)?\.[^/\\]+$", p.name)
        if m and (p.with_name(m.group(1) + ".json")).exists():
            return Recording.load(p.with_name(m.group(1) + ".json"))
        raise RecordingError(f"{p} is not a recording (no sidecar next to it)")
    stem = p.name
    if p.parent != Path(".") and (p.parent / (stem + ".json")).exists():
        return Recording.load(p.parent / (stem + ".json"))
    if STEM_RE.match(stem):
        y, mth = stem[:4], stem[5:7]
        cand = out_dir / y / mth / (stem + ".json")
        if cand.exists():
            return Recording.load(cand)
    raise RecordingError(f"recording not found: {target}")


def iter_recordings(out_dir: Path) -> Iterator[Recording]:
    """All recordings under out_dir/YYYY/MM, oldest first."""
    for sidecar in sorted(out_dir.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.json")):
        if is_sidecar(sidecar):
            try:
                yield Recording.load(sidecar)
            except RecordingError:
                continue
