"""How long each part of a job took: `<stem>.timings.json` next to the recording (derived, deletable).

A background job of the review page (run-job) is one *run*: the steps inside it (window video analysis, the
transcription and its own phases, naming the speakers, exports, each minutes) are timed with `step()` and the run is
written when the job ends, the newest first, the last RUNS_KEPT kept. Outside a run (a CLI command) `step()` only
measures and logs. Informative only – nothing compares or decides by it.

    {"format": 1, "runs": [{"job": "process", "started": "...", "ok": true, "total_s": 512.3,
                            "steps": [{"step": "přepis", "s": 140.2, "parts": {"transcribe_s": 61.0, ...}}, ...]}]}
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

RUNS_KEPT = 10
_run: dict | None = None  # the run of this process (a job runs in a process of its own)


def path_for(rec) -> Path:
    return rec.file(".timings.json")


def start_run(job: str) -> None:
    global _run
    _run = {"job": job, "started": datetime.now().isoformat(timespec="seconds"), "steps": [], "_t": time.monotonic()}


@contextmanager
def step(name: str, parts: dict | None = None):
    """Time one part of the work; `parts` (filled by the caller while it runs) are its own sub-times."""
    t = time.monotonic()
    try:
        yield
    finally:
        s = round(time.monotonic() - t, 1)
        log.info("time: %s %.1f s", name, s)
        if _run is not None:
            entry: dict = {"step": name, "s": s}
            if parts:
                entry["parts"] = {k: v for k, v in parts.items() if isinstance(v, (int, float))}
            _run["steps"].append(entry)


def finish_run(rec, ok: bool) -> None:
    """Write the run to <stem>.timings.json (newest first); a job without a recording writes nothing."""
    global _run
    run, _run = _run, None
    if run is None or rec is None:
        return
    run["total_s"] = round(time.monotonic() - run.pop("_t"), 1)
    run["ok"] = ok
    p = path_for(rec)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        data = {}
    runs = [run] + [r for r in data.get("runs", []) if isinstance(r, dict)]
    p.write_text(json.dumps({"format": 1, "runs": runs[:RUNS_KEPT]}, ensure_ascii=False, indent=1), encoding="utf-8")


def latest(rec, n: int = 3) -> list[dict]:
    p = path_for(rec)
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("runs", [])[:n] if p.exists() else []
    except (OSError, ValueError):
        return []
