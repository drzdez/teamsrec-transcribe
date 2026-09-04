# teamsrec-transcribe

Command-line post-processing for recordings made by
[teamsrec-capture](https://github.com/drzdez/teamsrec-capture): transcription with speaker
diarization, speaker naming, exports (txt/srt) and meeting summaries.

Input/output contract: [recording-format.md](https://github.com/drzdez/teamsrec-capture/blob/main/docs/recording-format.md)
(owned by the capture project; this project reads `format: 1`).

## Status

Design phase. Lab results on a real recording: [lab/FINDINGS.md](lab/FINDINGS.md).

## Planned stack

- Python 3.12, `uv`, CLI via `typer`
- Transcription providers behind one interface producing `<stem>.transcript.json`:
  - `whisperx` — local, `large-v3`, Czech, diarization via pyannote (CUDA); first provider
  - a cloud provider later (Azure AI Speech or ElevenLabs Scribe, to be evaluated)
- `import` — bring an external recording (typically a Teams `…-Meeting Recording.mp4`) into the recordings folder as a `source: import` recording with `_mix.wav` + sidecar; with `--video` it also derives the active-speaker timeline from the Teams video (highlighted name labels → OCR) into `<stem>.speakers_video.json`, which takes priority over diarization when naming speakers
- `label-speakers` — interactive mapping of diarized speakers to names → `<stem>.speakers.json`
- `summarize` — transcript → summary + action items via Claude API → `<stem>.summary.md`
- `purge-audio` — (later, not in first iterations) delete WAV files of already transcribed recordings; transcripts and sidecars are kept

## Commands (planned)

```
teamsrec-transcribe import <file.mp4> [--title ...] [--start ...]   # Teams recording → recordings/YYYY/MM/<stem>
teamsrec-transcribe transcribe <recording-dir|stem> [--provider whisperx]
teamsrec-transcribe label-speakers <stem>
teamsrec-transcribe export <stem> [--txt] [--srt]
teamsrec-transcribe summarize <stem>
teamsrec-transcribe process <stem>        # transcribe + export + summarize
teamsrec-transcribe purge-audio [--older-than 30d]   # later: delete WAVs of transcribed recordings, keep transcripts
```
