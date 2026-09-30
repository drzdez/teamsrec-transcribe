"""Settings of both apps on one page: the fields of the shared teamsrec.toml (with what they mean), writing them
back without losing comments, and the API keys, which never go into the file.

The TOML is shared with teamsrec-capture: its [capture] keys are listed here too, and the capture app re-reads the
file when it changes. Keys are looked up in the environment first (TEAMSREC_<VENDOR>_API_KEY, then the vendor's own
name), then in the Windows Credential Manager (service "teamsrec", via `keyring`), where the settings page stores
them - encrypted for the Windows user, never shown again, usable at once without a restart.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .config import (Config, RetentionSettings, SummarizeSettings, TranscribeSettings, VideoSettings,
                     VoiceprintSettings, default_config_path)

KEYRING_SERVICE = "teamsrec"


@dataclass(frozen=True)
class Field:
    key: str            # "section.name" in the TOML
    label: str          # Czech, for the page
    type: str           # str | bool | int | float | enum | list
    default: Any
    help: str = ""
    choices: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()  # what the page shows for each of `choices` (same order); empty = the values
    suggest: str = ""   # name of a suggestion list (suggestions()): the field becomes an editable dropdown;
                        # "inputs" = the audio input devices of this PC
    restart: bool = False  # takes effect only after the review server / the apps start again


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    note: str
    fields: tuple[Field, ...]


def _d(cls, name: str) -> Any:
    """Default of a config dataclass field (tuples as lists for the page)."""
    for f in fields(cls):
        if f.name == name:
            v = f.default
            return list(v) if isinstance(v, tuple) else v
    raise KeyError(name)


T, S, V, P, R = TranscribeSettings, SummarizeSettings, VideoSettings, VoiceprintSettings, RetentionSettings

SECTIONS: tuple[Section, ...] = (
    Section("general", "Obecné", "", (
        Field("user.name", "Tvoje jméno", "str", "",
              "Mikrofonní stopa živé nahrávky dostane tohle jméno (prázdné = vypnuto)."),
        Field("people.display", "Jména v zápisu", "enum", "nick", "",
              choices=("first", "full", "nick"),
              labels=("křestní jméno", "celé jméno", "přezdívka (bez přezdívky křestní jméno)")),
        Field("calendar.outlook", "Kalendář z Outlooku", "bool", False,
              "Název a účastníci schůzky z klasického Outlooku na tomto PC (lokálně, přes COM)."),
        Field("recordings.out_dir", "Složka nahrávek", "str", "D:/meetings",
              "Kam se nahrávky ukládají; _inbox je uvnitř.", restart=True),
        Field("capture.tray_open", "Poklepání na ikonu v liště otevře", "enum", "app",
              "Ikona aplikace pro nahrávání; totéž platí pro Settings… v její nabídce.", choices=("app", "web"),
              labels=("tuto stránku v okně aplikace", "tuto stránku v prohlížeči")),
    )),
    Section("capture", "Nahrávání", "Používá aplikace pro nahrávání (ikona v liště); změnu si načte sama.", (
        Field("capture.onsite_mic", "Mikrofon pro schůzky na místě", "str", "",
              "Nahrává se z prvního zařízení, jehož název obsahuje tento text. V zasedačce použijte mikrofonní pole "
              "notebooku, ne sluchátka – ta slyší jen vás.", suggest="inputs"),
        Field("capture.device_missing", "Když zvolený mikrofon chybí", "enum", "ask", "",
              choices=("ask", "fail", "fallback"),
              labels=("Zeptat se, čím nahrávat (nabídne dostupná zařízení)", "Nenahrávat a nahlásit chybu",
                      "Tiše použít výchozí vstup Windows")),
        Field("capture.onsite_offer", "Nabízet nahrávání podle kalendáře", "enum", "never",
              "Nahrávání se spustí hned na začátku schůzky a zobrazí se okénko „Zahodit?“ – když neodpovíte, "
              "nahrávka se zachová. Potřebuje zapnutý kalendář z Outlooku (Obecné).",
              choices=("never", "calendar", "always"),
              labels=("Nikdy – nahrávání na místě spouštím sám z menu ikony",
                      "U schůzek bez odkazu na Teams (pravděpodobně na místě)", "U každé schůzky v kalendáři")),
        Field("capture.onsite_upgrade", "Když během nahrávání na místě začne hovor v Teams, přejít na živý záznam",
              "bool", True, "Nahrávka na místě skončí a hned začne živá (zvuk systému + mikrofon); obě se propojí."),
        Field("capture.other_apps", "Hovory v jiných aplikacích", "enum", "record",
              "Zoom, Webex, Slack, Discord, WhatsApp, Skype, Signal a schůzka v prohlížeči (Google Meet, Zoom, Webex, "
              "Teams na webu, Jitsi, Whereby v názvu okna). Mikrofon se bere ten, který aplikace hovoru používá; "
              "snímání oken a jmen z dlaždic je jen pro Teams.",
              choices=("record", "off"),
              labels=("Nahrávat stejně jako Teams (od první vteřiny, oznámení v liště)", "Nenahrávat – jen Teams")),
        Field("capture.prompt_default", "Na začátku hovoru v Teams", "enum", "record",
              "Nahrává se vždy od první vteřiny; volba mění jen to, jak se aplikace ptá.",
              choices=("record", "ask", "skip"),
              labels=("Nahrávat a jen oznámit (zahodit lze z menu ikony)",
                      "Nahrávat a zeptat se „Zahodit?“ – bez odpovědi zachovat",
                      "Nahrávat a zeptat se „Zahodit?“ – bez odpovědi zahodit")),
    )),
    Section("transcribe", "Přepis", "Cloudové služby posílají zvuk mimo počítač (docs/privacy.md).", (
        Field("transcribe.provider", "Služba přepisu", "enum", _d(T, "provider"),
              "Cloudové služby potřebují klíč (Klíče API nahoře).",
              choices=("whisperx", "openai", "elevenlabs"),
              labels=("WhisperX – v tomto počítači (grafická karta)", "OpenAI – cloud", "ElevenLabs – cloud")),
        Field("transcribe.language", "Jazyk", "str", _d(T, "language"),
              "auto nebo kód jazyka (cs, sk, en).", suggest="language"),
        Field("transcribe.languages", "Jazyky pro auto", "list", _d(T, "languages"),
              "S jazykem auto se vybírá jen z nich."),
        Field("transcribe.per_speaker_language", "Jazyk po mluvčích", "bool", _d(T, "per_speaker_language"),
              "Mluvčí v jiném z jazyků se přepíše znovu v tom jazyce (smíšené cs/sk schůzky)."),
        Field("transcribe.diarize", "Rozlišit mluvčí (diarizace)", "bool", _d(T, "diarize"),
              "pyannote; přeskočí se, když jména pokryje video oken Teams."),
        Field("transcribe.diarize_model", "Model diarizace", "str", _d(T, "diarize_model"),
              "Z Hugging Face; nestažený se stáhne při prvním přepisu (potřebuje hf auth login).", suggest="diarize"),
        Field("transcribe.model", "Model Whisper", "str", _d(T, "model"),
              "whisperx; nestažený se stáhne při prvním přepisu.", suggest="whisper"),
        Field("transcribe.compute_type", "Přesnost výpočtu", "enum", _d(T, "compute_type"),
              "Podle paměti grafické karty (docs/hardware-portability.md).",
              choices=("float16", "int8_float16", "int8")),
        Field("transcribe.device", "Zařízení", "enum", _d(T, "device"), choices=("cuda", "cpu")),
        Field("transcribe.batch_size", "Dávka", "int", _d(T, "batch_size")),
        Field("transcribe.beam_size", "Beam size", "int", _d(T, "beam_size")),
        Field("transcribe.align", "Časování slov", "bool", _d(T, "align")),
        Field("transcribe.openai_model", "Model OpenAI", "str", _d(T, "openai_model"), suggest="openai"),
        Field("transcribe.elevenlabs_model", "Model ElevenLabs", "str", _d(T, "elevenlabs_model"),
              suggest="elevenlabs"),
        Field("transcribe.glossary", "Slovníček", "list", _d(T, "glossary"),
              "Odborné pojmy a jména pro přepis, jeden na řádek."),
    )),
    Section("video", "Video oken Teams", "", (
        Field("video.enabled", "Číst jména z videa", "bool", _d(V, "enabled"),
              "Kdo mluví, podle zvýrazněné dlaždice v oknech Teams."),
        Field("video.fps", "Snímků za sekundu", "float", _d(V, "fps")),
    )),
    Section("summarize", "Zápis", "Claude posílá text přepisu mimo počítač; Ollama běží lokálně.", (
        Field("summarize.enabled", "Dělat zápis", "bool", _d(S, "enabled"), "Součást zpracování nahrávky."),
        Field("summarize.provider", "Služba", "enum", _d(S, "provider"),
              "Claude potřebuje klíč (Klíče API nahoře).",
              choices=("ollama", "anthropic"),
              labels=("Ollama – v tomto počítači (grafická karta)", "Claude – cloud")),
        Field("summarize.model", "Model", "str", _d(S, "model"),
              "Nabídka podle zvolené služby: modely Ollamy v tomto počítači, nebo modely Claude dostupné pro váš klíč.",
              suggest="summary"),
        Field("summarize.language", "Jazyk zápisu", "str", _d(S, "language"), suggest="language"),
        Field("summarize.compare", "Porovnávací zápisy", "list", _d(S, "compare"),
              "Další zápisy pro porovnání, např. anthropic:claude-opus-5-5 (jeden na řádek). Vzniknou při zpracování i při přegenerování zápisu; na stránce se přepínají nad zápisem.",
              suggest="compare"),
        Field("summarize.ollama_url", "Adresa Ollamy", "str", _d(S, "ollama_url")),
        Field("summarize.ollama_think", "Ollama: přemýšlení", "bool", _d(S, "ollama_think"),
              "Pomalejší, někdy lepší."),
        Field("summarize.ollama_max_ctx", "Ollama: kontext (tokeny)", "int", _d(S, "ollama_max_ctx"),
              "Delší přepisy se shrnou po částech."),
        Field("summarize.ollama_timeout_s", "Ollama: časový limit (s)", "int", _d(S, "ollama_timeout_s")),
    )),
    Section("voiceprints", "Hlasové otisky", "Biometrické údaje kolegů, jen v tomto počítači (docs/privacy.md).", (
        Field("voiceprints.enabled", "Poznávat lidi po hlase", "bool", _d(P, "enabled")),
        Field("voiceprints.threshold", "Práh shody", "float", _d(P, "threshold"), "Kosinová podobnost 0–1."),
        Field("voiceprints.margin", "Náskok před druhým", "float", _d(P, "margin")),
        Field("voiceprints.min_seconds", "Nejméně řeči (s)", "float", _d(P, "min_seconds"),
              "Kratší mluvčí se neporovnávají ani neukládají."),
    )),
    Section("retention", "Uchování", "", (
        Field("retention.audio_days", "Mazat zvuk po (dnech)", "int", _d(R, "audio_days"),
              "Zvuk a videa hotových nahrávek; přepisy a zápisy zůstanou. 0 = nemazat."),
    )),
)

FIELDS: dict[str, Field] = {f.key: f for s in SECTIONS for f in s.fields}


@dataclass(frozen=True)
class Secret:
    name: str
    label: str
    env: tuple[str, ...]  # looked up in this order; the first is the tool's own name
    used_for: str


SECRETS: dict[str, Secret] = {s.name: s for s in (
    Secret("anthropic", "Claude (Anthropic)", ("TEAMSREC_ANTHROPIC_API_KEY",), "zápis přes Claude"),
    Secret("openai", "OpenAI", ("TEAMSREC_OPENAI_API_KEY", "OPENAI_API_KEY"), "přepis přes OpenAI"),
    Secret("elevenlabs", "ElevenLabs", ("TEAMSREC_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"), "přepis přes ElevenLabs"),
)}


class SettingsError(ValueError):
    """A value the page sent does not fit its field (Czech message for the page)."""


# ------------------------------------------------------------------ reading
def _get(data: dict, key: str) -> Any:
    sec, name = key.split(".", 1)
    s = data.get(sec)
    return s.get(name) if isinstance(s, dict) else None


def read_values(path: Path | None = None) -> dict[str, Any]:
    """Every field's value from the file, or its default when the file does not set it."""
    path = path or default_config_path()
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    out = {}
    for f in FIELDS.values():
        v = _get(data, f.key)
        out[f.key] = f.default if v is None else v
    return out


def describe(path: Path | None = None) -> dict:
    """What the settings page shows: sections with fields and values, key states, input devices."""
    path = path or default_config_path()
    values = read_values(path)
    return {
        "path": str(path),
        "sections": [{"id": s.id, "title": s.title, "note": s.note,
                      "fields": [{"key": f.key, "label": f.label, "type": f.type, "help": f.help,
                                  "choices": list(f.choices), "labels": list(f.labels), "default": f.default,
                                  "value": values[f.key],
                                  "suggest": f.suggest, "restart": f.restart} for f in s.fields]}
                     for s in SECTIONS],
        "secrets": secret_states(),
        "vault": vault_available(),
        "inputs": input_devices(),
        "suggestions": suggestions(values),
    }


# ------------------------------------------------------------------ validating
def coerce(f: Field, value: Any) -> Any:
    """The page's value in the field's type; SettingsError when it does not fit."""
    if f.type == "bool":
        if isinstance(value, bool):
            return value
        raise SettingsError(f"{f.label}: čekám ano/ne")
    if f.type in ("int", "float"):
        if isinstance(value, bool):
            raise SettingsError(f"{f.label}: čekám číslo")
        try:
            n = float(str(value).replace(",", ".").strip())
        except ValueError:
            raise SettingsError(f"{f.label}: „{value}“ není číslo") from None
        if n < 0:
            raise SettingsError(f"{f.label}: nesmí být záporné")
        if f.type == "int":
            if n != int(n):
                raise SettingsError(f"{f.label}: čekám celé číslo")
            return int(n)
        return n
    if f.type == "list":
        items = value.splitlines() if isinstance(value, str) else value
        if not isinstance(items, list):
            raise SettingsError(f"{f.label}: čekám seznam")
        return [str(x).strip() for x in items if str(x).strip()]
    text = "" if value is None else str(value).strip()
    if "\n" in text:
        raise SettingsError(f"{f.label}: jen jeden řádek")
    if f.type == "enum" and text not in f.choices:
        raise SettingsError(f"{f.label}: neznámá hodnota „{text}“ (možnosti: {', '.join(f.choices)})")
    return text


# ------------------------------------------------------------------ writing
def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    s = str(v)
    esc = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return '"' + esc + '"'


def _value_end(rest: str) -> int | None:
    """Where the value in `key = <rest>` ends on this line (a '#' inside a string is not a comment); None when
    the value goes on to the next lines (a multi-line array)."""
    candidates = [i for i, c in enumerate(rest) if c == "#"] + [len(rest)]
    for i in candidates:
        try:
            tomllib.loads("x = " + rest[:i])
            return i
        except tomllib.TOMLDecodeError:
            continue
    return None


def write_changes(changes: dict[str, Any], path: Path | None = None) -> None:
    """Set "section.key" values in the TOML in place: comments, order and every other key stay as they are.
    A key that is not in the file yet goes at the end of its section (or a new section at the end)."""
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    todo = {tuple(k.split(".", 1)): v for k, v in changes.items()}
    section, last_of = "", {}
    i = 0
    while i < len(lines):
        line = lines[i]
        head = re.match(r"\s*\[([^\]]+)\]\s*(#.*)?$", line)
        if head:
            section = head.group(1).strip()
            last_of[section] = i
            i += 1
            continue
        km = re.match(r"(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$", line)
        span = 1
        if km:
            end = _value_end(km.group(4))
            comment = ""
            if end is None:  # a value over several lines: find where it closes
                for j in range(i + 1, len(lines)):
                    try:
                        tomllib.loads("x = " + "\n".join([km.group(4)] + lines[i + 1:j + 1]))
                        span = j - i + 1
                        break
                    except tomllib.TOMLDecodeError:
                        continue
            else:
                comment = km.group(4)[end:].strip()
            if (section, km.group(2)) in todo:
                value = todo.pop((section, km.group(2)))
                new = f"{km.group(1)}{km.group(2)}{km.group(3)}{toml_value(value)}"
                if comment:
                    new += "  " + comment
                lines[i:i + span] = [new]
                span = 1
        if section and line.strip():
            last_of[section] = i + span - 1
        i += span
    for (sec, key), value in todo.items():
        line = f"{key} = {toml_value(value)}"
        if sec in last_of:
            at = last_of[sec] + 1
            lines.insert(at, line)
            for other, idx in last_of.items():
                if idx >= at:
                    last_of[other] = idx + 1
            last_of[sec] = at
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [f"[{sec}]", line]
            last_of[sec] = len(lines) - 1
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)  # never leave a broken file behind
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save(values: dict[str, Any], path: Path | None = None) -> dict:
    """Validate the page's values and write the ones that changed. Returns the changed keys and whether any
    of them needs a restart."""
    path = path or default_config_path()
    unknown = [k for k in values if k not in FIELDS]
    if unknown:
        raise SettingsError(f"neznámé nastavení: {', '.join(unknown)}")
    current = read_values(path)
    changes = {}
    for key, raw in values.items():
        v = coerce(FIELDS[key], raw)
        if v != current[key]:
            changes[key] = v
    if changes:
        write_changes(changes, path)
    return {"changed": sorted(changes), "restart": any(FIELDS[k].restart for k in changes)}


# ------------------------------------------------------------------ API keys
def _keyring():
    try:
        import keyring  # Windows: the Credential Manager (WinVaultKeyring)
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def vault_available() -> bool:
    return _keyring() is not None


def _vault_get(name: str) -> str:
    kr = _keyring()
    if kr is None:
        return ""
    try:
        return (kr.get_password(KEYRING_SERVICE, name) or "").strip()
    except Exception:
        return ""


def get_secret(name: str) -> str:
    """The API key: environment first (so keys set with setx keep working), then the Credential Manager.
    "" when there is none. Never logged, never sent to the page."""
    for var in SECRETS[name].env:
        v = os.environ.get(var, "").strip()
        if v:
            return v
    return _vault_get(name)


def secret_states() -> list[dict]:
    out = []
    for s in SECRETS.values():
        env = next((var for var in s.env if os.environ.get(var, "").strip()), "")
        state = "env" if env else ("vault" if _vault_get(s.name) else "missing")
        out.append({"name": s.name, "label": s.label, "used_for": s.used_for, "state": state, "env": env,
                    "env_names": list(s.env)})
    return out


def set_secret(name: str, value: str) -> None:
    if name not in SECRETS:
        raise SettingsError(f"neznámý klíč {name}")
    value = (value or "").strip()
    if not value or any(c.isspace() for c in value):
        raise SettingsError("klíč je prázdný nebo obsahuje mezery")
    kr = _keyring()
    if kr is None:
        raise SettingsError("úložiště klíčů (Správce přihlašovacích údajů) není dostupné")
    kr.set_password(KEYRING_SERVICE, name, value)


def delete_secret(name: str) -> bool:
    if name not in SECRETS:
        raise SettingsError(f"neznámý klíč {name}")
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(KEYRING_SERVICE, name)
        return True
    except Exception:  # keyring.errors.PasswordDeleteError: there was none
        return False


# ------------------------------------------------------------------ suggestions for the dropdowns
# (value, size of the download) of the models whisperx / pyannote fetch from Hugging Face on first use
WHISPER = (("large-v3", "~3 GB"), ("large-v3-turbo", "~1,6 GB"), ("large-v2", "~3 GB"), ("medium", "~1,5 GB"),
           ("small", "~0,5 GB"), ("base", "~0,15 GB"))
DIARIZE = (("pyannote/speaker-diarization-community-1", "~30 MB"), ("pyannote/speaker-diarization-3.1", "~30 MB"))
OPENAI = ("gpt-4o-transcribe-diarize", "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1")
ELEVENLABS = ("scribe_v2", "scribe_v1")
LANGUAGES = (("auto", "rozpoznat"), ("cs", "čeština"), ("sk", "slovenština"), ("en", "angličtina"),
             ("de", "němčina"), ("pl", "polština"))
_cache: dict[str, list[str]] = {}  # the last live lists (Ollama tags, Claude models), until the server stops


def _opt(value: str, note: str, local: bool | None = None) -> dict:
    """One dropdown entry: local = True on this PC, False to be downloaded, None = not a download (cloud, code)."""
    return {"value": value, "note": note, "local": local}


def _cached(key: str, fetch, live: bool) -> list[str] | None:
    """A live list (Ollama, Claude API). Only live=True asks (seconds: Ollama may be down, Claude is on the
    internet), so opening the settings stays instant; otherwise the last answer, or None = not asked yet."""
    if not live:
        return _cache.get(key)
    try:
        items = fetch()
    except Exception:  # Ollama not running, no key, offline: an empty list, the field still takes free text
        items = []
    _cache[key] = items
    return items


def ollama_models(url: str, live: bool = False) -> list[str] | None:
    def fetch():
        import json
        import urllib.request
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=1.0) as r:
            return sorted(m["name"] for m in json.loads(r.read()).get("models", []))
    return _cached("ollama:" + url, fetch, live)


def claude_models(live: bool = False) -> list[str] | None:
    """The models the stored/env key can use (listing models costs nothing and sends no text)."""
    key = get_secret("anthropic")
    if not key:
        return []

    def fetch():
        import anthropic
        client = anthropic.Anthropic(api_key=key, timeout=5.0, max_retries=0)
        return [m.id for m in client.models.list(limit=100)]
    return _cached("claude:" + key[-6:], fetch, live)


def hf_cached() -> set[str]:
    """Hugging Face repos in the local cache, as "org/name" (models--Systran--faster-whisper-large-v3 ->
    Systran/faster-whisper-large-v3)."""
    base = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub")
    try:
        return {p.name[len("models--"):].replace("--", "/") for p in base.iterdir() if p.name.startswith("models--")}
    except OSError:
        return set()


def suggestions(values: dict[str, Any], live: bool = False) -> dict[str, Any]:
    """Every dropdown list, entries marked: on this PC / to be downloaded / cloud. The summary models depend on
    the chosen service, so both lists are sent; a configured model that is not installed is listed as such.
    Without live the Ollama and Claude lists are the last known ones (models_loaded = False until asked once);
    the configured models are always in them."""
    cached = hf_cached()
    whisper = [_opt(v, "v počítači" if any(r.endswith("faster-whisper-" + v) for r in cached)
                    else f"stáhne se při prvním přepisu ({size})",
                    any(r.endswith("faster-whisper-" + v) for r in cached)) for v, size in WHISPER]
    diarize = [_opt(v, "v počítači" if v in cached else f"stáhne se při prvním přepisu ({size})", v in cached)
               for v, size in DIARIZE]
    ollama_known = ollama_models(str(values.get("summarize.ollama_url") or _d(S, "ollama_url")), live)
    claude_known = claude_models(live)
    ollama = ollama_known or []
    ollama_opts = [_opt(m, "v počítači (Ollama)", True) for m in ollama]
    compare = list(values.get("summarize.compare") or [])
    wanted = {c.partition(":")[2] for c in compare if c.startswith("ollama:")}
    if values.get("summarize.provider") != "anthropic":
        wanted.add(str(values.get("summarize.model") or ""))
    for m in sorted(wanted - set(ollama) - {""}):  # configured but not pulled: say so, and how
        ollama_opts.append(_opt(m, f"není stažený – ollama pull {m}", False) if ollama_known is not None
                           else _opt(m, "nastavený model (seznam zatím nenačten)"))
    claude_ids = list(claude_known or [])
    for m in [c.partition(":")[2] for c in compare if c.startswith("anthropic:")] + (
            [str(values.get("summarize.model") or "")] if values.get("summarize.provider") == "anthropic" else []):
        if m and m not in claude_ids:  # the configured Claude models, also before the list is loaded
            claude_ids.append(m)
    claude = [_opt(m, "cloud (Claude API)") for m in claude_ids]
    return {
        "language": [_opt(v, n) for v, n in LANGUAGES],
        "whisper": whisper,
        "diarize": diarize,
        "openai": [_opt(v, "cloud (OpenAI)") for v in OPENAI],
        "elevenlabs": [_opt(v, "cloud (ElevenLabs)") for v in ELEVENLABS],
        "summary": {"ollama": ollama_opts, "anthropic": claude},
        "compare": [_opt("anthropic:" + o["value"], o["note"]) for o in claude]
                   + [_opt("ollama:" + o["value"], o["note"], o["local"]) for o in ollama_opts],
        "ollama_ready": bool(ollama),
        "models_loaded": ollama_known is not None and claude_known is not None,
    }


# ------------------------------------------------------------------ audio inputs (Windows)
def input_devices() -> list[str]:
    """Names of the active audio inputs, as Windows and teamsrec-capture show them ("Pole mikrofonu (…)").
    Read from the registry (MMDevices), so no audio library is needed; [] elsewhere or on error."""
    if sys.platform != "win32":
        return []
    import winreg
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
    desc_key, iface_key = "{a45c254e-df1c-4efd-8020-67d146a850e0},2", "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                dev_id = winreg.EnumKey(root, i)
                try:
                    with winreg.OpenKey(root, dev_id) as dev:
                        if winreg.QueryValueEx(dev, "DeviceState")[0] != 1:  # DEVICE_STATE_ACTIVE
                            continue
                    with winreg.OpenKey(root, dev_id + r"\Properties") as props:
                        desc = winreg.QueryValueEx(props, desc_key)[0]
                        iface = winreg.QueryValueEx(props, iface_key)[0]
                    names.append(f"{desc} ({iface})")
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(set(names))


def reload(cfg: Config, path: Path) -> Config:
    """The config after a save, for a running server (out_dir changes only after a restart)."""
    from dataclasses import replace
    from .config import load_config
    return replace(load_config(path), out_dir=cfg.out_dir)
