"""Configuration: one TOML file shared with teamsrec-capture, overridable per CLI call.

Location (first found wins): $TEAMSREC_CONFIG, %APPDATA%/teamsrec/teamsrec.toml (Windows),
~/.config/teamsrec/teamsrec.toml (elsewhere). Missing file = defaults.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TranscribeSettings:
    provider: str = "whisperx"
    language: str = "auto"  # auto | BCP-47 code
    model: str = "large-v3"
    compute_type: str = "float16"
    batch_size: int = 16
    beam_size: int = 5
    align: bool = True
    diarize: bool = True
    diarize_model: str = "pyannote/speaker-diarization-community-1"
    glossary: tuple[str, ...] = ()
    device: str = "cuda"


@dataclass(frozen=True)
class VideoSettings:
    enabled: bool = True
    fps: float = 2.0


@dataclass(frozen=True)
class SummarizeSettings:
    enabled: bool = True  # part of `process`; failures (no model / no API key) are logged, not fatal
    provider: str = "ollama"  # ollama (local GPU) | anthropic (Claude API)
    model: str = "gemma4:31b"  # ollama tag, or a Claude model id such as claude-opus-5
    language: str = "cs"  # language of the minutes, independent of the meeting language
    ollama_url: str = "http://localhost:11434"
    ollama_think: bool = False  # let thinking models reason first (slower, sometimes better)
    ollama_max_ctx: int = 65536  # cap for the context window we ask Ollama for
    ollama_timeout_s: int = 1800
    compare: tuple[str, ...] = ()  # extra "provider:model" runs written to <stem>.summary.<model>.md (POC comparison)


@dataclass(frozen=True)
class Config:
    out_dir: Path = field(default_factory=lambda: Path.home() / "meetings")
    transcribe: TranscribeSettings = field(default_factory=TranscribeSettings)
    video: VideoSettings = field(default_factory=VideoSettings)
    summarize: SummarizeSettings = field(default_factory=SummarizeSettings)
    source_path: Path | None = None  # where the config was loaded from, None = defaults

    @property
    def inbox_dir(self) -> Path:
        return self.out_dir / "_inbox"


def default_config_path() -> Path:
    env = os.environ.get("TEAMSREC_CONFIG")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "teamsrec" / "teamsrec.toml"


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    sec = data.get(name, {})
    return sec if isinstance(sec, dict) else {}


def _build(cls, values: dict[str, Any]):
    known = {f for f in cls.__dataclass_fields__}
    kwargs = {}
    for k, v in values.items():
        if k not in known:
            continue
        if k in ("glossary", "compare"):
            v = tuple(str(x) for x in v)
        kwargs[k] = v
    return cls(**kwargs)


def load_config(path: Path | None = None) -> Config:
    """Load the TOML config; a missing file yields defaults. Unknown keys are ignored."""
    path = path or default_config_path()
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rec = _section(data, "recordings")
    out_dir = Path(rec["out_dir"]).expanduser() if "out_dir" in rec else Config().out_dir
    return Config(
        out_dir=out_dir,
        transcribe=_build(TranscribeSettings, _section(data, "transcribe")),
        video=_build(VideoSettings, _section(data, "video")),
        summarize=_build(SummarizeSettings, _section(data, "summarize")),
        source_path=path,
    )


def with_overrides(cfg: Config, **overrides: Any) -> Config:
    """Apply CLI overrides: top-level keys map to Config, dotted keys ('transcribe.language') to sections."""
    top: dict[str, Any] = {}
    sections: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        if "." in key:
            sec, k = key.split(".", 1)
            sections.setdefault(sec, {})[k] = value
        else:
            top[key] = value
    for sec, vals in sections.items():
        top[sec] = replace(getattr(cfg, sec), **vals)
    return replace(cfg, **top)


DEFAULT_TOML = """\
[recordings]
out_dir = "{out_dir}"

[transcribe]
provider = "whisperx"
language = "auto"            # auto | cs | sk | en
model = "large-v3"
compute_type = "float16"     # float16 | int8_float16 | int8
batch_size = 16
align = true
diarize = true
diarize_model = "pyannote/speaker-diarization-community-1"
glossary = []                # domain terms added to the ASR prompt

[video]
enabled = true
fps = 2

[summarize]
enabled = true               # run as part of `process`
provider = "ollama"          # ollama = local GPU (ollama pull <model>) | anthropic = Claude API (TEAMSREC_ANTHROPIC_API_KEY)
model = "gemma4:31b"         # ollama tag, or e.g. "claude-opus-5" with provider = "anthropic"
language = "cs"              # language of the minutes
ollama_think = false         # thinking mode for local models: slower, sometimes better
compare = []                 # e.g. ["anthropic:claude-opus-5"] -> extra <stem>.summary.claude-opus-5.md for comparison
"""
