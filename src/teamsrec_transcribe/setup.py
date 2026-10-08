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
        "ollama": {"running": ollama_running(url), "models": models or []},
        "secrets": {s["name"]: ("login" if s["name"] == "huggingface" and s["state"] == "missing"
                                and settings.hf_login_token() else s["state"])
                    for s in settings.secret_states()},
        "outlook": sys.platform == "win32",
        "values": values,
    }
