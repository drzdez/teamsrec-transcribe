# Running on different hardware

An assessment, not a plan. It is based on the measurements in `lab/FINDINGS.md` (2026-09-04) and on publicly known
properties of the libraries used. The figures for other hardware are **estimates** from performance ratios, not
measurements. Other operating systems: `teamsrec-capture/docs/cross-platform-design.md`.

## What actually loads the hardware

Capture (.NET, WASAPI recording) is negligible and runs on anything with Windows. All the load is in transcription,
which has four phases. Measured on an RTX 5090 Laptop (24 GB VRAM) for a 70-minute recording:

| Phase | Library | Time | VRAM (estimate) | Note |
|---|---|---|---|---|
| ASR large-v3, float16 | faster-whisper / CTranslate2 | 61 s | ~3.5 GB | the only phase where the model and the compute precision matter |
| alignment (word timestamps) | wav2vec2 through torch | 40 s | ~1 GB | optional; without it segments of 30 s |
| diarization | pyannote community-1 | 60 s | ~1.5 GB | optional; for recordings with video replaceable by the video analysis |
| speakers from video | numpy + easyocr | ~240 s | ~0.5 GB (OCR) | almost all on the CPU (video decoding), independent of the GPU |

The VRAM peak of the whole chain is around 5 to 6 GB. Everything is 20× faster than real time, so any hardware that is
even 10× slower is still fine for overnight or background processing.

## NVIDIA RTX and other NVIDIA cards

CTranslate2 and torch run on every card from Turing (RTX 20xx, compute capability 7.5) up; Pascal (GTX 10xx) works, but
without fast float16. The cu128 packages need driver 570+; for older cards cu124/cu126 can be used.

| Card | Generation | VRAM | Estimated ASR, 70 min | Recommended settings |
|---|---|---|---|---|
| RTX 5090 Laptop (measured) | Blackwell | 24 GB | 1 min | large-v3, float16, batch 16 |
| RTX 4090 / 4080 desktop | Ada | 16–24 GB | 1 min or less | the same |
| RTX 4070 / 4060 laptop | Ada | 8 GB | 2–3 min | large-v3, float16, batch 8 |
| RTX 3060 / 3050 | Ampere | 6–12 GB | 3–4 min | large-v3, float16 or int8_float16, batch 4–8 |
| RTX 2060 / 2070 | Turing | 6–8 GB | 4–6 min | large-v3, int8_float16 |
| GTX 1650 / 1060 | Pascal | 4–6 GB | 8–15 min | large-v3 int8 or medium, batch 2; consider turning off alignment and diarization |
| GeForce MX, 2 GB | any | 2 GB | – | GPU unusable, as CPU below |

The practical minimum is **4 GB VRAM with int8** (large-v3 int8 takes ~1.6 GB), comfortable is **6 GB**, without
compromises **8 GB**. The settings could be chosen automatically from the detected VRAM and compute capability; today
they are set in the configuration.

## PCs without an NVIDIA GPU

**A Windows laptop with Intel/AMD graphics (CPU only).** CTranslate2 has a good CPU path (int8, AVX2/AVX-512). Roughly,
on a modern 8-core CPU:

| Model | Estimated ASR, 70 min | Quality |
|---|---|---|
| large-v3 int8 | 40–90 min | full |
| large-v3-turbo int8 | 12–25 min | slightly worse on terms, acceptable with a prompt |
| medium int8 | 15–30 min | noticeably worse Czech/Slovak |
| small int8 | 5–8 min | not enough for minutes |

pyannote diarization on the CPU runs at about real time (70 min ≈ 40–80 min). For imported Teams recordings the video
analysis replaces it completely, which is the biggest saving on weak hardware. Alignment on the CPU takes around 10 min.
Conclusion: **on a CPU the solution is usable for batch processing** (overnight, in the background after a meeting),
not for a quick transcript.

**AMD Radeon.** ROCm is Linux-only and CTranslate2 does not support it; on Windows AMD behaves like a CPU. There are
routes through ONNX Runtime with DirectML (whisper in ONNX), but that is a different stack from WhisperX.

**Intel Arc / Core Ultra NPU.** OpenVINO can run Whisper (optimum-intel, whisper.cpp), again a different stack, without
diarization.

**Apple Silicon (Mac).** The best non-NVIDIA route. `mlx-whisper` with large-v3 runs roughly 5–10× faster than real time
on an M2 Pro and up; pyannote on MPS/CPU is slower. WhisperX does not run there; it would be a separate provider, as the
original design already assumed.

## Cloud and remote compute

Three distinct options, with a different impact on the privacy of the recordings:

1. **Your own remote worker.** The same CLI on another machine of your own with a GPU (a home server, a second PC).
   Capture saves the recording to a shared folder (OneDrive, SMB, syncthing), the worker processes it and puts the
   results next to it. The data never leaves your own infrastructure. The prototype allowed for this (POST_HOOK).
2. **A rented GPU** (RunPod, Vast.ai, Lambda, Azure NC series). The same container as locally, 70 minutes of recording
   in ~3 min for a few cents. The recording goes to a third party, but the model stays under your control.
3. **A ready-made transcription API.** Azure AI Speech (batch, diarization, cs/sk/en, European regions), ElevenLabs
   Scribe, OpenAI, AssemblyAI, Deepgram. No hardware requirements, no model maintenance; recording and transcript stay
   with the provider. Most have no glossary comparable to `initial_prompt`, so terms come out worse. OpenAI and
   ElevenLabs are implemented (`[transcribe] provider`); measured comparison in the user guide.

## How to make it run on more hardware (possible directions, not planned)

The architecture already allows for it: transcription is a provider behind an interface, capture is independent of the
hardware. Supporting more hardware means adding a provider, not changing the chain.

- **Automatic choice of settings** from the detected GPU/VRAM: model, compute type, batch, alignment and diarization on
  or off.
- **A CPU provider** (faster-whisper int8) with the same output, as a fallback without a GPU.
- **Diarization that can be turned off** where the speakers come from the video or the microphone track; the biggest
  effect on weak hardware (already a setting).
- **A remote worker** through a shared folder: the same CLI, just another machine.
- **Cloud API providers** for machines without a GPU and without patience; chosen per recording by sensitivity (done
  for OpenAI and ElevenLabs).
- **An Apple provider** through mlx-whisper, should a Mac turn up.
- **A benchmark command** (`bench`) that measures the phases on a short sample on the machine and suggests settings.
- **A container** with the CUDA stack for rented GPUs and your own worker, so that the installation (torch cu128 vs.
  the whisperx pin) is not manual.
