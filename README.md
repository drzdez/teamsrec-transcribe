# teamsrec-transcribe

Command-line post-processing for meeting recordings made by
[teamsrec-capture](https://github.com/drzdez/teamsrec-capture) or imported from any audio/video file
(typically a Teams meeting recording): transcription with speaker attribution, speaker naming,
exports (txt/srt) and meeting summaries. Runs locally on an NVIDIA GPU (WhisperX); other backends later.

Input/output contract: [recording-format.md](https://github.com/drzdez/teamsrec-capture/blob/main/docs/recording-format.md)
(owned by the capture project; this project reads `format: 1`).

## Status

**No packaged code yet.** What exists:

- `lab/` — working experiment scripts and a validated CUDA environment (see [Using the lab today](#using-the-lab-today)).
- `lab/FINDINGS.md` — measured results on a real 70-minute recording and the decisions derived from them.
- `docs/hardware-portability.md` — how this behaves on other GPUs, CPU-only machines, Macs and cloud.

The CLI described below is the target design; commands and options may still change while it is being written.

## How it will be used

Everything works on a recordings folder laid out as `<OUT_DIR>/YYYY/MM/<stem>.*`. A recording is a set of files
sharing a stem: a `.json` sidecar plus audio (`_mix.wav`, and `_sys.wav`/`_mic.wav` for live captures).

```
teamsrec-transcribe process                     # import everything in <OUT_DIR>/_inbox, then transcribe + export
                                                #   + summarize every recording that has no transcript yet
teamsrec-transcribe process <stem|file>         # the same for one recording or one ad-hoc media file
teamsrec-transcribe import <file> [--title ...] [--start ...] [--participants "A,B"] [--video]
teamsrec-transcribe transcribe <stem|file> [--provider whisperx] [--language auto|cs|sk|en] [--no-diarize]
teamsrec-transcribe label-speakers <stem>       # interactive: SPEAKER_00 -> "Jana Nováková"
teamsrec-transcribe export <stem> [--txt] [--srt]
teamsrec-transcribe summarize <stem>            # transcript -> summary + action items (Claude API)
teamsrec-transcribe purge-audio [--older-than 30d]   # later: delete WAVs, keep transcripts
```

Typical day:

1. Live meeting: teamsrec-capture records it into `<OUT_DIR>/2026/09/2026-09-04_1400_tydenni-sync.*`.
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
provider = "anthropic"
model = "claude-sonnet-5"        # any current Claude model
language = "cs"                  # language of the summary, independent of the meeting language
```

Secrets are never in the config file:

- HuggingFace (pyannote models): `hf auth login` once; accept the terms of `pyannote/speaker-diarization-community-1`.
- Claude API: `ANTHROPIC_API_KEY` environment variable.

## Installation (target)

```
uv tool install teamsrec-transcribe            # CPU-only torch
# NVIDIA GPU: replace torch with the CUDA build matching the pinned version
uv tool install teamsrec-transcribe --with "torch==2.8.0" --with "torchaudio==2.8.0" --index https://download.pytorch.org/whl/cu128
```

`ffmpeg` must be on PATH (`winget install Gyan.FFmpeg`). Models (~5 GB) download on first use into the HuggingFace cache.

## Using the lab today

Until the CLI exists, the same pipeline can be run by hand from `lab/`:

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

## Planned stack

- Python 3.12, `uv`, CLI via `typer`, config via TOML
- Providers behind one interface producing `<stem>.transcript.json`:
  `whisperx` (local, first), later a CPU fallback and a cloud provider (Azure AI Speech or ElevenLabs Scribe)
- `video_speakers` module ported from the lab script
- `summarize` via the Anthropic SDK
