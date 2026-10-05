"""Saving a copy of the minutes elsewhere ("Uložit jako…" on the review page), remembering the folder per meeting.

The folder chosen for a meeting title is kept in `<OUT_DIR>/_export_folders.json` ({folded title: folder}), so the
next meeting of the same name offers the same folder. The copy is the minutes' Markdown as it is on disk; a file of
the same name in the folder is not overwritten (" (2)", " (3)", … is added).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import unicodedata
from pathlib import Path

from .recording import Recording, RecordingError

log = logging.getLogger(__name__)

FILE_NAME = "_export_folders.json"


def title_key(title: str) -> str:
    """Meetings of one name share a folder: case, diacritics and spacing do not matter."""
    folded = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", folded).strip()


def _path(out_dir: Path) -> Path:
    return out_dir / FILE_NAME


def _load(out_dir: Path) -> dict:
    try:
        data = json.loads(_path(out_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def folder_for(out_dir: Path, title: str) -> str:
    """The folder last used for this meeting name ("" = none yet)."""
    return str(_load(out_dir).get(title_key(title), ""))


def remember(out_dir: Path, title: str, folder: str) -> None:
    data = _load(out_dir)
    data[title_key(title)] = folder
    _path(out_dir).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def copy_name(rec: Recording, summary: Path) -> str:
    """"2026-10-02 1313 SDS-KPPI Postgres Migration steps – zápis.md": date and time first, so the copies sort by when
    the meeting was (+ the model for a comparison minutes)."""
    start = str(rec.sidecar.get("start") or "")
    date = f"{start[:10]} {start[11:13]}{start[14:16]}" if len(start) >= 16 else rec.stem[:15].replace("_", " ")
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", rec.title or rec.stem).strip(" .-") or rec.stem
    extra = summary.name[len(rec.stem) + len(".summary."):-len(".md")] if summary.name != rec.summary_path.name else ""
    return f"{date} {safe} – zápis{f' ({extra})' if extra else ''}.md"


def save_copy(rec: Recording, summary_file: str, folder: str, out_dir: Path) -> Path:
    """Copy one minutes of the recording into `folder` and remember the folder for its meeting name."""
    if "/" in summary_file or "\\" in summary_file or not summary_file.startswith(rec.stem + ".summary") \
            or not summary_file.endswith(".md"):
        raise RecordingError("not a summary of this recording")
    src = rec.dir / summary_file
    if not src.exists():
        raise RecordingError(f"{summary_file} does not exist")
    target_dir = Path(folder).expanduser()
    if not str(folder).strip() or not target_dir.is_absolute():
        raise RecordingError("choose a folder (a full path)")
    target_dir.mkdir(parents=True, exist_ok=True)
    name = copy_name(rec, src)
    target = target_dir / name
    n = 2
    while target.exists():
        target = target_dir / f"{name[:-3]} ({n}).md"
        n += 1
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    remember(out_dir, rec.title, str(target_dir))
    log.info("%s: minutes copied to %s", rec.stem, target)
    return target


def pick_folder(initial: str = "") -> str:
    """The modern Windows folder dialog (address bar, a "Folder" box to type or paste a path), opened by the local
    server so it works for the browser and the desktop window alike; "" when cancelled. pick_folder.ps1 does it."""
    import os
    if os.name != "nt":
        raise RecordingError("the folder dialog is available on Windows only; type the folder instead")
    script = Path(__file__).with_name("pick_folder.ps1")
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script),
                        "-Initial", initial], capture_output=True, text=True, encoding="utf-8", timeout=900)
    if r.returncode != 0:
        log.warning("folder dialog failed: %s", r.stderr.strip()[-300:])
        raise RecordingError("the folder dialog did not open; type the folder instead")
    return r.stdout.strip()
