# teamsrec-transcribe

Command-line post-processing for meeting recordings made by
[teamsrec-capture](https://github.com/drzdez/teamsrec-capture) or imported from any audio/video file
(typically a Teams meeting recording): transcription with speaker attribution, speaker naming,
exports (txt/srt) and meeting summaries. Runs locally on an NVIDIA GPU (WhisperX); other backends later.

Input/output contract: [recording-format.md](https://github.com/drzdez/teamsrec-capture/blob/main/docs/recording-format.md)
(owned by the capture project; this project reads `format: 1`).

## Status

Working, early. Implemented and verified on a real 70-minute Teams recording:

- `import` (any audio/video file, metadata from the Teams file name / container / file), Teams video analysis
  for active speakers, `transcribe` (WhisperX on CUDA: ASR + alignment + diarization, speaker names from the video),
  `export` (txt/srt), `label-speakers`, `summarize` (minutes via a local Ollama model or the Claude API),
  `process` (inbox + all pending), `list`, `config`.
- Not yet: `purge-audio`, per-speaker language, the `me` track for live captures, CPU/cloud providers.

User guide (Czech): [docs/user-guide.md](docs/user-guide.md). Background: `lab/FINDINGS.md` (measurements and decisions), `docs/hardware-portability.md` (other GPUs, CPU, Mac, cloud).

## Usage

Everything works on a recordings folder laid out as `<OUT_DIR>/YYYY/MM/<stem>/<stem>.*`: one folder per recording,
every file in it prefixed with the stem (`<date>_<time>_<title-slug>`). A recording is a `.json` sidecar plus audio
(`_mix.wav`, and `_sys.wav`/`_mic.wav` for live captures); transcripts and minutes are written next to them.

```
teamsrec-transcribe process                     # import everything in <OUT_DIR>/_inbox, then transcribe + export
                                                #   + summarize every recording that has no transcript yet
teamsrec-transcribe process <stem|file>         # the same for one recording or one ad-hoc media file
teamsrec-transcribe process --latest            # import the inbox, then only the newest recording
                                                # (`latest` also works as <stem> in every command)
teamsrec-transcribe import <file> [--title ...] [--start ...] [--participants "A,B"] [--video]
teamsrec-transcribe transcribe <stem|file> [--provider whisperx] [--language auto|cs|sk|en] [--no-diarize]
teamsrec-transcribe label-speakers <stem>       # interactive: SPEAKER_00 -> "Jana Nováková"
teamsrec-transcribe export <stem> [--txt] [--srt]
teamsrec-transcribe summarize <stem> [--provider ollama|anthropic] [--model ...] [--language cs]
teamsrec-transcribe purge-audio [--older-than 30d]   # later: delete WAVs, keep transcripts
```

Typical day:

1. Live meeting: teamsrec-capture records it into `<OUT_DIR>/2026/09/2026-09-04_1400_tydenni-sync/`.
2. Stored Teams recording: download the `…-Meeting Recording.mp4` and drop it into `<OUT_DIR>/_inbox/`.
3. Run `teamsrec-transcribe process`. Both recordings get `.transcript.json`, `.txt`, `.srt` and `.summary.md`.
4. If a speaker is still `SPEAKER_03` (audio-only recording), run `label-speakers` once; exports are regenerated.

Speaker names come from, in priority order: the Teams video (highlighted name labels, imported recordings),
the microphone track (live captures: that is `me`), and pyannote diarization as the fallback.

## Configuration

One TOML file shared with teamsrec-capture: `%APPDATA%\teamsrec\teamsrec.toml` (`~/.config/teamsrec/teamsrec.toml`
elsewhere). Every value can be overridden by a CLI flag; the `TEAMSREC_CONFIG` environment variable points to a
different file.

```toml
[recordings]
out_dir = "D:/meetings"          # the recordings folder; _inbox lives inside it

[transcribe]
provider = "whisperx"            # whisperx | (later) faster-whisper-cpu, cloud providers
language = "auto"                # auto | cs | sk | en ; per-speaker detection planned (v2)
model = "large-v3"
compute_type = "float16"         # float16 | int8_float16 | int8  (pick by VRAM, see docs/hardware-portability.md)
batch_size = 16
align = true                     # word timestamps; needs an alignment model for the language
diarize = true                   # pyannote; skipped automatically when the video timeline covers the recording
diarize_model = "pyannote/speaker-diarization-community-1"
glossary = ["WFMS", "NOTAM", "Eurocontrol", "BPMN", "Entra ID"]   # goes into the ASR prompt with title + participants

[video]
enabled = true                   # analyse Teams video on import; ignored when the file has no video stream
fps = 2

[summarize]
enabled = true                   # run as part of `process`; a failure (no model, no key) is logged, not fatal
provider = "ollama"              # ollama = local GPU, everything stays on the machine | anthropic = Claude API
model = "gemma4:31b"             # ollama: `ollama pull gemma4:31b` (20 GB, fits 24 GB VRAM) | anthropic: claude-opus-5
language = "cs"                  # language of the minutes, independent of the meeting language
ollama_think = false             # thinking mode for local models: slower, sometimes better
compare = ["anthropic:claude-opus-5"]   # optional: extra summaries for comparison -> <stem>.summary.claude-opus-5.md
```

Secrets are never in the config file:

- HuggingFace (pyannote models): `hf auth login` once; accept the terms of `pyannote/speaker-diarization-community-1`.
- Claude API (only with `provider = "anthropic"`): `ANTHROPIC_API_KEY` environment variable.
- Ollama (default): install Ollama, `ollama pull gemma4:31b`; no keys, the transcript never leaves the machine.

## Installation

From source (until a release exists):

```
git clone https://github.com/drzdez/teamsrec-transcribe
cd teamsrec-transcribe
uv sync --extra all            # venv with whisperx + CUDA 12.8 torch (Windows/Linux) + video analysis
uv run teamsrec-transcribe config --init
```

Requirements: Python 3.12 (uv fetches it), NVIDIA driver 570+ for the CUDA 12.8 wheels, `ffmpeg` on PATH
(`winget install Gyan.FFmpeg`) or `TEAMSREC_FFMPEG_DIR` pointing at its `bin` folder, and a HuggingFace login
(`hf auth login`) with the terms of `pyannote/speaker-diarization-community-1` accepted. Models (~5 GB) download
on first use into the HuggingFace cache.

`bin	eamsrec-transcribe.cmd` runs the checkout's venv and finds the WinGet ffmpeg by itself; add `bin` to PATH to use the command from anywhere.

Tests: `uv run pytest`.

## Lab scripts

The experiments the CLI grew out of are kept in `lab/` and still run on their own:

```
cd lab
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-lab.txt

# 1. audio out of any recording (mono 16 kHz)
ffmpeg -i "meeting.mp4" -vn -ac 1 -ar 16000 -c:a pcm_s16le samples/meeting_mix.wav

# 2. transcription + alignment + diarization; outputs in out/<name>/
.venv/Scripts/python run_whisperx.py samples/meeting_mix.wav --name meeting --language auto ^
    --initial-prompt "Schůzka o WFMS. Témata: NOTAM, Eurocontrol, BPMN."

# 3. speaker names from the Teams video, applied to the transcript -> out/video/transcript_named.txt
.venv/Scripts/python video_speakers.py "meeting.mp4" --out out/video ^
    --transcript out/meeting/transcript.json --names "Jana Nováková,Petr Svoboda"
```

`run_whisperx.py --help` lists all knobs (model, compute type, batch, VAD, speaker count bounds). On Windows the
WinGet ffmpeg is not on Git Bash's PATH; see `lab/README.md`.

## Stack

- Python 3.12, `uv`, CLI via `typer`, config via TOML (`src/teamsrec_transcribe/`)
- Providers behind one interface producing `<stem>.transcript.json`: `whisperx` (local); a CPU fallback and a
  cloud provider (Azure AI Speech or ElevenLabs Scribe) are planned
- `video_speakers`: frame sampling + accent-colour label detection + easyocr
- `summarize`: one prompt (summary / topics / decisions / action items / open questions / terms), two backends in
  `llm.py`: Ollama REST (`/api/chat`, context sized to the transcript) and the Anthropic SDK (streaming, cached system prompt)
