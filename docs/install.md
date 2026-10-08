# Installation and configuration

teamsrec is two applications that share one recordings folder and one configuration file:

- **teamsrec-capture** – a tray icon that records calls (Teams and other apps) and on-site meetings. Windows only
  (.NET 10); the earlier Python prototype is kept in its `legacy` folder.
- **teamsrec-transcribe** – transcript, speaker names, minutes and the review page (in a browser or as a desktop
  window).

## The suite installer (Windows) – the usual way

One per-user MSI installs everything, without admin rights: download `teamsrec-capture-<version>-x64.msi` from
[the releases](https://github.com/drzdez/teamsrec-capture/releases/latest) and run it. It is not code-signed yet, so
Windows SmartScreen may warn the first time ("More info" → "Run anyway").

What it installs into `%LOCALAPPDATA%\Programs\teamsrec-capture`:

| part | what it is |
|---|---|
| `teamsrec-capture.exe` | the recording app in the tray, started at login (Startup shortcut) |
| `teamsrec-review.exe` | the window with the transcripts (Start menu: *teamsrec - prepisy*; the tray icon opens it too) |
| `uv.exe`, `transcribe\` | teamsrec-transcribe with its exact package lock, from which the window builds the Python environment |
| `ffmpeg\` | ffmpeg and ffprobe (GPL, a separate program; `SOURCE.txt` says where its sources are) |

**First start of the window** (Start menu or the tray icon): it prepares the Python environment for the voice
processing – Python 3.12, PyTorch with CUDA, WhisperX, pyannote, about 4–6 GB to download, 7–8 GB on disk – into
`%LOCALAPPDATA%\teamsrec\env`, with the progress on its start screen; 5–20 minutes by the connection. Then the
**setup wizard** asks, step by step:

1. the recordings folder, your name (your microphone track gets it), the meeting languages and the minutes language;
2. the transcription – on this PC (WhisperX; the wizard recommends by the graphics card it finds) or in the cloud
   (ElevenLabs, OpenAI, with a key) – and, locally, the speaker diarization: pyannote needs a free Hugging Face
   account, the model's terms accepted once, and a Read token; the wizard checks the token;
3. the minutes – Ollama on this PC (the model it recommends is the one measured for the card), Claude or OpenAI (with
   a key), or none;
4. voice prints and the Outlook calendar (both opt-in).

Keys go into the Windows Credential Manager, never into files. The wizard opens by itself only on a fresh install
(no name in the configuration yet); any time later from Nastavení → *Průvodce…* or the tray menu → *Setup wizard…*; everything it sets is also in Nastavení. The transcription models download on the first processing
(about 3 GB, once). Ollama itself is a separate program from [ollama.com](https://ollama.com) (the wizard says when
it is missing).

**Updates:** the tray app looks for a new release once a day and offers it (*Install version …*). The update replaces
the programs; the next start of the window brings the environment up to date (only what changed is downloaded).
Uninstalling (Settings → Apps) removes the programs; the environment, the downloaded models and your recordings stay
(delete `%LOCALAPPDATA%\teamsrec` by hand to remove the environment).

The rest of this page is the manual setup from the sources, for development. The steps below are for one PC with
Windows 11 and an NVIDIA graphics card.

## Requirements

| what | why |
|---|---|
| Windows 11 | recording (WASAPI, Outlook through COM) |
| NVIDIA GPU, driver 570+ | WhisperX transcription and diarization on CUDA 12.8; local minutes through Ollama (the 31B model wants ~24 GB VRAM) |
| ~20 GB free space | models (~5 GB transcription + ~20 GB Ollama model, by size) and recordings (~0.5 GB per hour) |
| `git`, `uv` | `winget install Git.Git astral-sh.uv` |
| `ffmpeg` | `winget install Gyan.FFmpeg` (both apps find it in the WinGet folder by themselves) |
| .NET 10 SDK | building teamsrec-capture: `winget install Microsoft.DotNet.SDK.10` |
| a Hugging Face account | downloading the diarization model; its terms must be accepted once |
| Rust, Node.js (optional) | building the desktop window of the review page (`desktop/`) |

## 1. Transcription and review page (teamsrec-transcribe)

```
git clone https://github.com/drzdez/teamsrec-transcribe
cd teamsrec-transcribe
uv sync --extra all
uv run hf auth login
```

On <https://huggingface.co/pyannote/speaker-diarization-community-1> accept the model's terms (the only manual step).
Then create the configuration – the wizard asks for your name, the Outlook calendar and voice prints:

```
uv run teamsrec-transcribe config --init
```

The command from anywhere: add the `bin` folder to PATH (the launcher `bin\teamsrec-transcribe.cmd` uses the
repository's virtual environment and finds ffmpeg). Check: `teamsrec-transcribe config` prints what applies.

**Desktop window (optional):** `cd desktop`, `npm install`, `npm run build`; copy
`src-tauri\target\release\teamsrec-review.exe` to `%LOCALAPPDATA%\Programs\teamsrec-review\` (that is where the tray
icon of teamsrec-capture looks for it) and make a Start menu shortcut to it. Details: `desktop/README.md`.

## 2. Minutes

- **Local (default):** `winget install Ollama.Ollama`, then `ollama pull gemma4:31b`. Nothing leaves the PC.
- **Claude (optional, as a comparison or the main one):** the key under **Nastavení → Klíče API** on the review page (or
  the environment variable `TEAMSREC_ANTHROPIC_API_KEY` – the tool's own name on purpose, a plain `ANTHROPIC_API_KEY`
  would be taken by other tools). Then choose Claude as the summary service, or add `anthropic:claude-opus-5-5` to the
  comparison summaries.
- **OpenAI:** the key under **Klíče API**, then OpenAI as the summary service and a model from its list.
- **Ollama on another computer** (a stronger machine on the network, e.g. an NVIDIA DGX Spark or a workstation with a
  big GPU): set **Nastavení → Zápis → Adresa Ollamy** to `http://<computer>:11434`; the minutes and the *Stáhnout*
  button for models then work there. On that computer Ollama must listen on the network (`OLLAMA_HOST=0.0.0.0`); it
  has no sign-in and no encryption, so only on a trusted network or a VPN – the transcript text travels there. The
  transcription itself stays on this PC (or in the cloud). Not tested by us yet, but nothing in teamsrec ties Ollama
  to this machine.

## 3. Recording (teamsrec-capture)

**From the MSI (recommended):** download `teamsrec-capture-<version>-x64.msi` from the
[releases](https://github.com/drzdez/teamsrec-capture/releases) and run it. It needs no admin rights: it installs for
the current user into `%LOCALAPPDATA%\Programs\teamsrec-capture` (a single self-contained exe, no .NET runtime
needed), adds a Start menu shortcut and a shortcut in the Startup folder (autostart at login), and starts the app.
Its one question is the recordings folder, prefilled with `%USERPROFILE%\meetings` – click Next to keep it. The choice
goes into `[recordings] out_dir` of the shared configuration, so teamsrec-transcribe uses the same folder.

A grey icon appears in the tray; it is red while recording, yellow means no sound is arriving. A double click opens
the review page; **Settings…** opens its Nastavení.

**Updates:** the app looks for a newer release at start and then once a day (`[capture] update_check`). When there is
one, a balloon offers it and the tray menu gets **Install version X…**; **Check for updates** asks right away. After a
confirmation the app downloads the MSI, checks its size and SHA-256 against the release, and runs it: the installer
quits the app and starts the new version. Never during a recording – the offer comes after it. A declined version gets
no more balloons, only the menu item. Running a newer MSI by hand works the same way; during a recording the app
refuses and the installer stops with a message. Uninstall in Settings → Apps (recordings and configuration stay).

**From source** (development):

```
git clone https://github.com/drzdez/teamsrec-capture
cd teamsrec-capture\dotnet
powershell -File installer\build-msi.ps1     # -> installer\bin\x64\Release\teamsrec-capture-<version>-x64.msi
```

**Optional watchdog** that starts the app again should it crash or be quit (a second instance quits at once, so it
does not matter that it runs every 5 minutes):

```
schtasks /Create /TN teamsrec-capture /SC MINUTE /MO 5 /F /TR "\"%LOCALAPPDATA%\Programs\teamsrec-capture\teamsrec-capture.exe\""
```

**Microphones:** for on-site meetings enable the laptop's microphone array (Nastavení → Nahrávání → "Otevřít nastavení
zvuku Windows" → Recording → right click → Enable), choose it as the on-site microphone, and try it with **Test
microphone** in the tray menu.

## 4. Configuration

One file for both apps: `%APPDATA%\teamsrec\teamsrec.toml`. Every setting can be changed on the review page
(**Nastavení**; it writes to the same file and keeps the comments); the recording app picks up changes by itself.

| section, key | what it does | default |
|---|---|---|
| `[recordings] out_dir` | the recordings folder (restart both apps after a change) | `~/meetings` |
| `[user] name` | your name; the microphone track of a live call gets it | – |
| `[people] display` | how people are written in transcript and minutes: `first` / `full` / `nick` | `nick` |
| `[calendar] outlook` | meeting title and participants from classic Outlook (locally, through COM) | `false` |
| `[capture] onsite_mic` | the microphone for on-site meetings (part of the device name) | default input |
| `[capture] device_missing` | the chosen microphone is missing: `ask` / `fail` / `fallback` | `ask` |
| `[capture] onsite_offer` | record calendar meetings: `never` / `calendar` (no Teams link) / `always` | `never` |
| `[capture] onsite_upgrade` | an on-site meeting that turns into a call continues as a live recording | `true` |
| `[capture] prompt_default` | the start of a call: `record` (only notify) / `ask` / `skip` | `record` |
| `[capture] other_apps` | also record calls in Zoom, Webex, Slack, Discord, WhatsApp, Skype, Signal and meetings in a browser: `record` / `off` | `record` |
| `[capture] tray_open` | what a double click on the tray icon opens: `app` (desktop window) / `web` (browser) | `app` |
| `[capture] update_check` | look for a new version on GitHub once a day and offer to install it | `true` |
| `[transcribe] provider` | `whisperx` (local) / `openai` / `elevenlabs` (cloud, sends the audio) | `whisperx` |
| `[transcribe] language`, `languages` | the language or `auto`; with `auto` only among `languages` | `auto`, `cs sk en` |
| `[transcribe] per_speaker_language` | in a mixed meeting transcribe each speaker in their language | `true` |
| `[transcribe] when_recording` | processing while a recording starts: `ask` / `stop` / `continue` | `ask` |
| `[transcribe] glossary` | terms for the transcription prompt (system names, abbreviations) | – |
| `[voiceprints] enabled` | recognise people by voice (colleagues' biometrics, see Privacy) | `false` |
| `[summarize] provider`, `model`, `compare` | minutes locally (Ollama) or through Claude; `compare` = extra minutes | `ollama`, `gemma4:31b` |
| `[retention] audio_days` | after how many days the audio of finished meetings is deleted (texts stay); `0` = never | `0` |

## 5. Keys for the services

Never in the configuration or the repository. The easiest is to enter them under **Nastavení** on the review page
(`teamsrec-transcribe review` → Nastavení → Klíče API): they are stored encrypted in the Windows Credential Manager for
your account and apply at once, without a restart. User environment variables work too and win:

| variable | for |
|---|---|
| `TEAMSREC_ANTHROPIC_API_KEY` | minutes through Claude |
| `TEAMSREC_OPENAI_API_KEY` (or `OPENAI_API_KEY`) | transcription `provider = "openai"` |
| `TEAMSREC_ELEVENLABS_API_KEY` (or `ELEVENLABS_API_KEY`) | transcription `provider = "elevenlabs"`; the key needs the Speech to Text permission |

A variable: Start → "Edit environment variables for your account" → New. Running apps see it only after a restart.

## 6. The first recording

1. Start a call in Teams (*Calendar → Meet now*) – the icon turns red and a notification appears.
2. After the call: double click the tray icon (or `teamsrec-transcribe process --latest`); on the page confirm
   "Ano, přepsat a zpracovat".
3. The review page shows the speakers; name them and click Uložit.

Using it in detail: [User guide](user-guide.md). What is stored and sent: [Privacy](privacy.md).
