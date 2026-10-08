"""The setup wizard of the review page: what this PC has, what to recommend, and whether the wizard ran.

The installed suite opens the page once with the wizard (no marker yet); it writes the usual settings (the TOML and
the key vault, settings.py) and then the marker. Recommendations follow docs/hardware-portability.md: the GPU's
VRAM picks the Whisper model and precision; without an NVIDIA GPU the cloud is recommended (CPU transcription of an
hour takes about an hour). The minutes model of Ollama is recommended only where measured (gemma4:31b on 24 GB);
elsewhere the user picks an installed one or a cloud service."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .config import default_config_path

MARKER = "setup-done"
DIARIZE_FILE = "config.yaml"  # a file of the pyannote pipeline repo: readable only after the licence is accepted


def marker_path(config_path: Path | None = None) -> Path:
    return (config_path or default_config_path()).parent / MARKER


def needed(config_path: Path | None = None) -> bool:
    """A fresh install: the wizard never ran and nobody configured teamsrec yet. A configuration with the user's
    name already set (an older install, set up by hand or in Nastavení) is left alone – the wizard is then only in
    Nastavení → Průvodce (2026-10-08: it popped up for the long-time user after an update)."""
    if marker_path(config_path).exists():
        return False
    from .settings import read_values
    try:
        return not str(read_values(config_path or default_config_path()).get("user.name") or "").strip()
    except Exception:  # an unreadable TOML: the wizard writes a good one
        return True


def mark_done(config_path: Path | None = None) -> None:
    p = marker_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("the setup wizard of the review page ran; delete this file to see it again\n", encoding="utf-8")


def gpu() -> dict | None:
    """The NVIDIA GPU (name, VRAM in GB) from nvidia-smi, or None: no NVIDIA card or no driver."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        name, mem = [x.strip() for x in out.strip().splitlines()[0].split(",")[:2]]
        return {"name": name, "vram_gb": round(int(mem) / 1024)}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def recommend(card: dict | None) -> dict:
    """Settings for this hardware (docs/hardware-portability.md) and a sentence why."""
    if not card:
        return {"local": False, "why": "Bez grafické karty NVIDIA by přepis hodiny trval zhruba hodinu – doporučuji "
                                       "přepis v cloudu (ElevenLabs), lokálně jde, ale pomalu.",
                "values": {"transcribe.device": "cpu", "transcribe.model": "medium", "transcribe.compute_type": "int8",
                           "transcribe.batch_size": 4}}
    vram = card["vram_gb"]
    if vram >= 8:
        vals = {"transcribe.model": "large-v3", "transcribe.compute_type": "float16",
                "transcribe.batch_size": 16 if vram >= 16 else 8}
    elif vram >= 6:
        vals = {"transcribe.model": "large-v3", "transcribe.compute_type": "int8_float16", "transcribe.batch_size": 4}
    elif vram >= 4:
        vals = {"transcribe.model": "large-v3", "transcribe.compute_type": "int8", "transcribe.batch_size": 2}
    else:
        return {"local": False, "why": f"{card['name']} má jen {vram} GB paměti – na lokální přepis nestačí, "
                                       "doporučuji cloud.", "values": {"transcribe.device": "cpu"}}
    return {"local": True, "why": f"{card['name']}, {vram} GB: lokální přepis zvládne (model {vals['transcribe.model']}, "
                                  f"{vals['transcribe.compute_type']}).",
            "values": {"transcribe.device": "cuda", **vals},
            "ollama_model": "gemma4:31b" if vram >= 24 else ""}


def hf_access(repo: str, token: str) -> dict:
    """Can this token download the gated diarization model? {"ok", "message"} in Czech for the wizard."""
    import requests
    if not token:
        return {"ok": False, "message": "Zadejte token z huggingface.co/settings/tokens (stačí typ Read)."}
    try:
        r = requests.head(f"https://huggingface.co/{repo}/resolve/main/{DIARIZE_FILE}", allow_redirects=True,
                          headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except requests.RequestException as e:
        return {"ok": False, "message": f"Hugging Face není dostupný: {e}"}
    if r.status_code == 200:
        return {"ok": True, "message": "Token platí a licence modelu je přijatá – model se stáhne při prvním přepisu."}
    if r.status_code == 401:
        return {"ok": False, "message": "Token neplatí – zkontrolujte ho (huggingface.co/settings/tokens)."}
    if r.status_code in (403, 404):
        return {"ok": False, "message": f"Token platí, ale licence modelu ještě není přijatá: otevřete "
                                        f"huggingface.co/{repo}, přihlaste se a potvrďte podmínky."}
    return {"ok": False, "message": f"Hugging Face odpověděl HTTP {r.status_code}."}


def ollama_running(url: str) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/").replace("//localhost", "//127.0.0.1") + "/api/version", timeout=1):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- how long processing takes

# What each part costs: seconds = fixed + per_min × minutes of recording. The defaults are this project's own
# measurements on an RTX 5090 Laptop (24 GB), 2026-09/10 (<stem>.timings.json of ~25 runs): local transcription with
# alignment and diarization 60 s + 4.4 s/min, the Teams window video 10 s + 1.4 s/min (CPU), the minutes by Ollama
# gemma4:31b 300 s + 15 s/min (the slowest part!), ElevenLabs 80 s + 1 s/min, Claude 45 s + 0.6 s/min.
DEFAULT_COST = {
    "transcribe_local": (60.0, 4.4),
    "video": (10.0, 1.4),
    "minutes_local": (300.0, 15.0),
    "transcribe_cloud": (80.0, 1.0),
    "minutes_cloud": (45.0, 0.6),
}
# a slower card transcribes slower (docs/hardware-portability.md: ASR of 70 min 1 min on 24 GB … 8–15 min on 4–6 GB)
GPU_FACTOR = ((16, 1.0), (8, 2.5), (6, 3.5), (4, 10.0))
CPU_FACTOR = 12.0  # rough: an hour on the CPU ≈ an hour (int8, medium); not measured here
STEP_KIND = (("přepis (whisperx", "transcribe_local"), ("analýza oken", "video"), ("zápis (ollama", "minutes_local"),
             ("přepis (elevenlabs", "transcribe_cloud"), ("přepis (openai", "transcribe_cloud"),
             ("zápis (anthropic", "minutes_cloud"), ("zápis (openai", "minutes_cloud"))
MIN_RUNS = 3  # this PC's own measurements replace the defaults from this many


def measured_costs(out_dir: Path, limit: int = 200) -> dict:
    """Fit seconds = fixed + per_min × minutes per kind of step from this PC's <stem>.timings.json (the newest
    recordings first, at most `limit`). Kinds with fewer than MIN_RUNS points are left out. {kind: (fixed, per_min, n)}"""
    import json
    files = sorted(Path(out_dir).glob("*/*/*/*.timings.json"), reverse=True)[:limit]
    pts: dict[str, list[tuple[float, float]]] = {}
    for f in files:
        try:
            minutes = json.loads(f.with_name(f.name.replace(".timings.json", ".json")).read_text(encoding="utf-8"))["duration_s"] / 60
            runs = json.loads(f.read_text(encoding="utf-8")).get("runs") or []
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for run in runs:
            if not run.get("ok"):
                continue
            for st in run.get("steps") or []:
                kind = next((k for prefix, k in STEP_KIND if str(st.get("step", "")).startswith(prefix)), None)
                if kind and float(st.get("s") or 0) >= 5:  # a step that failed at once (no model) says nothing
                    pts.setdefault(kind, []).append((minutes, float(st["s"])))
    out = {}
    for kind, xy in pts.items():
        if len(xy) < MIN_RUNS:
            continue
        # Theil–Sen: the median of the pairwise slopes – one odd run (a busy GPU, a retry) does not tilt the line
        from statistics import median
        slopes = [(y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(xy) for x2, y2 in xy[i + 1:] if abs(x2 - x1) >= 1]
        per_min = max(0.0, median(slopes)) if slopes else 0.0
        fixed = max(0.0, median(y - per_min * x for x, y in xy))
        out[kind] = (round(fixed, 1), round(per_min, 2), len(xy))
    return out


def estimates(card: dict | None, out_dir: Path, minutes_local: bool = True) -> dict:
    """Processing time of a 15-minute and a 1-hour meeting: locally on this PC and by the fast track (cloud), with
    advice. This PC's own measurements where there are enough, else the defaults scaled by the card."""
    factor = CPU_FACTOR if not card else next((f for vram, f in GPU_FACTOR if card["vram_gb"] >= vram), CPU_FACTOR)
    measured = measured_costs(out_dir)
    cost = {}
    for kind, (fixed, per_min) in DEFAULT_COST.items():
        if kind in measured:
            cost[kind] = measured[kind][:2]
        elif kind == "transcribe_local":
            cost[kind] = (fixed, per_min * factor)
        else:
            cost[kind] = (fixed, per_min)
    if not card or card["vram_gb"] < 24:
        if "minutes_local" not in measured:
            minutes_local = False  # no measured local minutes model below 24 GB: the cloud's minutes in the estimate

    def t(kind: str, m: float) -> float:
        fixed, per_min = cost[kind]
        return fixed + per_min * m

    rows = []
    for label, m in (("15 min", 15.0), ("1 h", 60.0)):
        local = t("transcribe_local", m) + t("video", m) + t("minutes_local" if minutes_local else "minutes_cloud", m)
        rows.append({"label": label, "local": round(local), "local_cloud_minutes": round(
            t("transcribe_local", m) + t("video", m) + t("minutes_cloud", m)), "cloud": round(
            t("transcribe_cloud", m) + t("minutes_cloud", m)),
            "minutes_local": round(t("minutes_local", m)) if minutes_local else None})
    hour = rows[1]
    if not card or card["vram_gb"] < 4:
        advice = ("Na tomto počítači doporučuji jako běžnou cestu ⚡ rychle přes cloud; lokálně jen tehdy, když zvuk "
                  "nesmí opustit počítač (hodina schůzky může trvat i přes hodinu).")
    elif hour["local"] <= 20 * 60:
        advice = ("Běžně zpracovávejte lokálně – zvuk i text zůstanou v počítači. ⚡ Rychle přes cloud jen ve spěchu.")
    else:
        advice = ("Lokálně to jde, jen hodina schůzky trvá déle – nechte zpracování běžet na pozadí (frontu najdete "
                  "dole). Ve spěchu ⚡ rychle přes cloud.")
    if minutes_local and hour["minutes_local"] and hour["minutes_local"] > 5 * 60:
        advice += (f" Nejpomalejší je lokální zápis (Ollama, ~{round(hour['minutes_local'] / 60)} min na hodinu); "
                   f"se zápisem přes Claude by lokální zpracování trvalo ~{round(hour['local_cloud_minutes'] / 60)} min.")
    src = [k for k in ("transcribe_local", "minutes_local", "transcribe_cloud", "minutes_cloud") if k in measured]
    runs = max((measured[k][2] for k in src), default=0)
    return {"rows": rows, "advice": advice, "minutes_local": minutes_local,
            "source": (f"podle měření na tomto počítači (až {runs} běhů)" if src
                       else "odhad podle grafické karty; zpřesní se po prvních zpracováních")}


def status(values: dict, config_path: Path | None = None) -> dict:
    """Everything the wizard shows before the user chooses."""
    from . import settings
    card = gpu()
    url = str(values.get("summarize.ollama_url") or "http://localhost:11434")
    models = settings.ollama_models(url, live=True)
    return {
        "needed": needed(config_path),
        "platform": sys.platform,
        "gpu": card,
        "recommend": recommend(card),
        "estimates": estimates(card, Path(str(values.get("recordings.out_dir") or "~/meetings")).expanduser()),
        "ollama": {"running": ollama_running(url), "models": models or []},
        "secrets": {s["name"]: ("login" if s["name"] == "huggingface" and s["state"] == "missing"
                                and settings.hf_login_token() else s["state"])
                    for s in settings.secret_states()},
        "outlook": sys.platform == "win32",
        "values": values,
    }
