# Instalace a konfigurace

teamsrec jsou dvě aplikace, které sdílejí jednu složku nahrávek a jeden konfigurační soubor:

- **teamsrec-capture** – ikona v liště, která nahrává hovory (Teams i jiné aplikace) a schůzky na místě.
  Jen Windows. Zatím Python prototyp ve složce `legacy`.
- **teamsrec-transcribe** – přepis, jména mluvčích, zápis a kontrolní stránka v prohlížeči.

Postup níže je pro jeden počítač s Windows 11 a grafickou kartou NVIDIA.

## Požadavky

| co | proč |
|---|---|
| Windows 11 | nahrávání (WASAPI, Outlook přes COM) |
| NVIDIA GPU, ovladač 570+ | přepis WhisperX a diarizace na CUDA 12.8; lokální zápisy přes Ollamu (model 31B chce ~24 GB VRAM) |
| ~20 GB volného místa | modely (~5 GB přepis + ~20 GB Ollama model podle velikosti) a nahrávky (~0,5 GB za hodinu) |
| `git`, `uv` | `winget install Git.Git astral-sh.uv` |
| `ffmpeg` | `winget install Gyan.FFmpeg` (obě aplikace ho najdou ve složce WinGet samy) |
| účet na HuggingFace | stažení modelu diarizace, jednou je potřeba odsouhlasit jeho podmínky |

## 1. Přepis a kontrolní stránka (teamsrec-transcribe)

```
git clone https://github.com/drzdez/teamsrec-transcribe
cd teamsrec-transcribe
uv sync --extra all
uv run hf auth login
```

Na <https://huggingface.co/pyannote/speaker-diarization-community-1> přijměte podmínky modelu (jediný ruční krok).
Pak vytvořte konfiguraci – průvodce se zeptá na vaše jméno, kalendář Outlook a hlasové otisky:

```
uv run teamsrec-transcribe config --init
```

Příkaz odkudkoli: přidejte složku `bin` do proměnné PATH (spouštěč `bin\teamsrec-transcribe.cmd` použije
virtuální prostředí repozitáře a najde ffmpeg). Kontrola: `teamsrec-transcribe config` vypíše, co platí.

## 2. Zápisy

- **Lokálně (výchozí):** `winget install Ollama.Ollama`, pak `ollama pull gemma4:31b`. Nic neopouští počítač.
- **Claude (volitelně, srovnání nebo hlavní):** klíč do proměnné prostředí `TEAMSREC_ANTHROPIC_API_KEY`
  (vlastní jméno schválně – obecnou `ANTHROPIC_API_KEY` by si vzaly jiné nástroje). Pak v konfiguraci
  `[summarize] provider = "anthropic"` nebo `compare = ["anthropic:claude-opus-5"]`.

## 3. Nahrávání (teamsrec-capture)

```
git clone https://github.com/drzdez/teamsrec-capture
cd teamsrec-capture\legacy
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe pyaudiowpatch pystray pillow pywin32 psutil pycaw
```

Spuštění: dvojklik na `legacy\run_teamsrec.cmd` (bez konzole) nebo `run_teamsrec_console.cmd` (ukáže chyby).
V liště se objeví šedá ikona; při nahrávání je červená, žlutá znamená, že nejde zvuk.

**Automatické spouštění:**

1. Zástupce na `legacy\run_teamsrec.cmd` do složky Po spuštění (Win+R → `shell:startup`).
2. Hlídací úloha, která aplikaci znovu spustí, kdyby spadla nebo byla ukončena (druhá instance se hned sama
   ukončí, takže nevadí, že běží každých 5 minut):

   ```
   schtasks /Create /TN teamsrec-capture /SC MINUTE /MO 5 /F /TR "\"<cesta>\legacy\.venv\Scripts\pythonw.exe\" \"<cesta>\legacy\teamsrec.py\""
   ```

**Mikrofony:** pro schůzky na místě povolte mikrofonní pole notebooku (`mmsys.cpl` → Záznam → pravým →
Povolit) a v menu ikony dejte **Settings…** → vyberte mikrofon → **Test mikrofonu**.

Kontrola bez hovoru: `.venv\Scripts\python.exe smoke_test.py` (s `--hardware` nahraje 2 s z mikrofonu).

## 4. Konfigurace

Jeden soubor pro obě aplikace: `%APPDATA%\teamsrec\teamsrec.toml`. Nastavení nahrávání jde měnit i na
stránce **Settings…** z ikony v liště (zapisuje do stejného souboru, komentáře zachová).

| sekce, klíč | co dělá | výchozí |
|---|---|---|
| `[recordings] out_dir` | složka nahrávek (po změně restartovat obě aplikace) | `~/meetings` |
| `[user] name` | vaše jméno; mikrofonní stopa živého hovoru ho dostane | – |
| `[people] display` | jak psát lidi do přepisu a zápisu: `first` / `full` / `nick` | `nick` |
| `[calendar] outlook` | název a účastníci schůzky z klasického Outlooku (lokálně přes COM) | `false` |
| `[capture] onsite_mic` | mikrofon pro schůzky na místě (část názvu zařízení) | výchozí vstup |
| `[capture] device_missing` | chybí nastavený mikrofon: `ask` / `fail` / `fallback` | `ask` |
| `[capture] onsite_offer` | nahrávat schůzky z kalendáře: `never` / `calendar` (bez odkazu na Teams) / `always` | `never` |
| `[capture] onsite_upgrade` | schůzka na místě, která přejde do hovoru, pokračuje živou nahrávkou | `true` |
| `[capture] prompt_default` | začátek hovoru: `record` (jen oznámit) / `ask` / `skip` | `record` |
| `[capture] other_apps` | nahrávat i hovory v Zoomu, Webexu, Slacku, Discordu, WhatsAppu, Skypu, Signalu a schůzky v prohlížeči: `record` / `off` | `record` |
| `[transcribe] provider` | `whisperx` (lokálně) / `openai` / `elevenlabs` (cloud, posílá zvuk) | `whisperx` |
| `[transcribe] language`, `languages` | jazyk nebo `auto`; při `auto` se vybírá jen z `languages` | `auto`, `cs sk en` |
| `[transcribe] per_speaker_language` | u smíšené schůzky přepsat každého mluvčího v jeho jazyce | `true` |
| `[transcribe] glossary` | pojmy do nápovědy přepisu (názvy systémů, zkratky) | – |
| `[voiceprints] enabled` | poznávat lidi po hlase (biometrie kolegů, viz Soukromí) | `false` |
| `[summarize] provider`, `model`, `compare` | zápis lokálně (Ollama) nebo přes Claude; `compare` = zápis navíc | `ollama`, `gemma4:31b` |
| `[retention] audio_days` | po kolika dnech smazat zvuk hotových schůzek (texty zůstanou); `0` = nikdy | `0` |

## 5. Klíče ke službám

Jen v proměnných prostředí uživatele, nikdy v konfiguraci ani v repozitáři:

| proměnná | k čemu |
|---|---|
| `TEAMSREC_ANTHROPIC_API_KEY` | zápisy přes Claude |
| `TEAMSREC_OPENAI_API_KEY` (nebo `OPENAI_API_KEY`) | přepis `provider = "openai"` |
| `TEAMSREC_ELEVENLABS_API_KEY` (nebo `ELEVENLABS_API_KEY`) | přepis `provider = "elevenlabs"`; klíč musí mít oprávnění Speech to Text |

Nastavení: Start → „Upravit proměnné prostředí pro váš účet“ → Nová. Běžící aplikace je uvidí až po restartu.

## 6. První nahrávka

1. Zavolejte si v Teams (*Kalendář → Sejít se hned*) – ikona zčervená a přijde oznámení.
2. Po hovoru: `teamsrec-transcribe process --latest` (přepis, jména, zápis; na ploše si na to můžete udělat zástupce).
3. Kontrolní stránka (`teamsrec-transcribe review`) ukáže mluvčí; pojmenujte je a dejte Uložit.

Podrobně o používání: [Uživatelská příručka](user-guide.md). Co se ukládá a posílá: [Soukromí](privacy.md).
