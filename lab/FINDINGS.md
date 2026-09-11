# Lab findings — WhisperX on a real Teams recording (2026-09-04)

Sample: Teams meeting recording, 70 min, 5 participants, Slovak (with Czech-leaning listeners), shared screen for ~28 min.
Hardware: RTX 5090 Laptop (24 GB), torch 2.8.0+cu128, whisperx 3.8.6, ctranslate2 4.8.2, pyannote.audio 4.0.7.

## Transcription configurations

| config | model | language | prompt | ASR s | total s | WFMS ok / as "VFMS" | notes |
|---|---|---|---|---|---|---|---|
| base | large-v3 | cs (forced) | – | 62 | 177 | 19 / 30 | Slovak speech rendered as Czech–Slovak mix |
| prompt-domain | large-v3 | cs (forced) | yes | 64 | 176 | 71 / 0 | prompt fixes domain terms completely |
| turbo | large-v3-turbo | cs (forced) | – | 25 | 130 | 0 / 65 | 2.5× faster ASR, terms broken without prompt |
| turbo-prompt | large-v3-turbo | cs (forced) | yes | 25 | 132 | 69 / 2 | writes Slovak more faithfully than large-v3+cs |
| auto-language-prompt | large-v3 | auto → sk | yes | 62 | 130* | 70 / 0 | native Slovak; *no alignment in this run |
| sk-prompt | large-v3 | sk (forced) | yes | 61 | 130* | 70 / 0 | identical to auto |
| final-auto-prompt | large-v3 | auto → sk | yes | 61 | 314** | 70 / 0 | **recommended**; 580 segments, 579 named from video; **first run includes sk align-model download (~145 s) |

Total = load + ASR + alignment (~40 s) + diarization (~62 s). Everything is 20–25× faster than real time.

## Conclusions

1. **Domain prompt is mandatory.** `initial_prompt` with the meeting title + participant names + a user term list
   turned "VFMS" into "WFMS" in 100 % of cases. Build it from the sidecar (`title`, `participants`) and a
   user-maintained glossary.
2. **Detect language, don't force `cs`.** Forcing Czech on Slovak speech produces a hybrid. Auto-detect on the first
   30 s picked `sk` correctly. For mixed CS/SK meetings a later refinement is per-speaker language detection
   (transcribe each speaker's audio separately); not needed for v1.
3. **large-v3 over turbo.** Turbo saves ~40 s per hour of audio, but total time is dominated by alignment + diarization,
   and turbo is slightly worse on terms. Keep `large-v3`, `float16`, `batch 16`, `beam 5`.
4. **Alignment models exist for both cs and sk** (`comodoro/wav2vec2-xls-r-300m-{cs,sk}`), so word timestamps and fine
   segments work for both languages.
5. **Diarization (pyannote community-1)** found 5 real speakers + 2 splinter clusters (< 1 min) + 1 UNKNOWN.
   Against the video ground truth it agrees on **93 % of segments**. `pyannote/speaker-diarization-3.1` was not
   tested (separate HF licence).

## Active speaker from Teams video (`video_speakers.py`)

Teams recordings highlight the active speaker's name label with the accent colour (~RGB 96,98,166), both in the
gallery and in the participant strip next to a shared screen (plus a presenter label bottom-left).

Method: sample frames at 2 fps (960×540), mask accent colour, connected components sized like a label, cluster by
position, drop static clusters (UI elements of a shared screen never toggle), OCR one representative frame per cluster
(easyocr), fuzzy-match to the participant list, merge into per-name intervals.

Result on the sample: 5 names, talk time 32.8 / 19.5 / 10.0 / 7.0 / 6.5 min; **588 of 590 transcript segments got a
name directly from the video**, 2 via diarization fallback. Processing ≈ 4 min (frame decode dominates; CPU).

Design consequences:
- `import` of a Teams recording with video should run this analysis; names come from OCR, optionally corrected by the
  `participants` list. Output `speakers_video.json` (name → intervals) is a **higher-priority speaker source than
  diarization**; diarization remains for audio-only recordings (live capture, playback without tiles visible).
- For live capture the same idea could work on screenshots of the Teams window, but that is a later experiment.

## Decisions (2026-09-04)

1. **One pipeline, several speaker sources.** ASR is identical for every recording. Speaker attribution picks sources by
   availability, not by file extension: video timeline → mic track → diarization (always run, as fallback). Participants
   not visible in the video stay `SPEAKER_XX` until named via `label-speakers`.
2. **Language per speaker (v2).** Meetings mix Czech, Slovak and English. v1: auto-detect once per recording, overridable
   by `--language` / sidecar. v2: after speakers are known, detect each speaker's language from their longest stretches,
   run ASR once per language present (≤ 3 runs, ~1 min each on the 5090) and stitch segments by speaker; each segment
   carries `language`. Alignment models exist for cs, sk and en.
3. **Recommended ASR config:** `large-v3`, `float16`, batch 16, beam 5, alignment on, `initial_prompt` built from
   title + participants + user glossary, diarization `pyannote/speaker-diarization-community-1`.

## Environment notes

- `whisperx` pins `torch~=2.8`; installing it pulls CPU torch. Reinstall `torch==2.8.0 torchaudio==2.8.0
  torchvision==0.23.0` from the cu128 index afterwards. See `README.md`.
- WhisperX shells out to `ffmpeg`; the WinGet install is not on PATH inside Git Bash — use the package `bin` dir.
- HuggingFace: `hf auth login` (token in `~/.cache/huggingface/token`) + accept terms of
  `pyannote/speaker-diarization-community-1`.

## Hlasové otisky – kalibrace (2026-09-11)

Skript `lab/voiceprints_calib.py`: diarizace všech nahrávek v OUT_DIR s `return_embeddings=True`
(pyannote community-1 přes whisperx), označení pojmenována překryvem s existujícím přepisem, kosinová
podobnost všech dvojic (nahrávka, mluvčí). Tři nahrávky (12 s, 29 min, 10 min), 8 označení.

| dvojice | podobnost |
|---|---|
| Zdeněk 29 min (mikrofon) × Zdeněk 10 min (mikrofon) | 0.94 |
| Zdeněk 12 s × Zdeněk 29 / 10 min | 0.43 / 0.44 |
| různí lidé, pojmenovaní (Zdeněk × Ori, Zdeněk × Miro, Ori × Miro) | max 0.36, průměr 0.24 |
| nepojmenované SPEAKER_02 (24 s, 29 min nahrávka) × Zdeněk 10 min | 0.56 |

Závěry: dostatečně dlouhá promluva téhož člověka dává shodu vysoko nad různými lidmi; krátké promluvy
(pod ~30 s) dávají nespolehlivý embedding na obě strany (0.43 pro téhož člověka, 0.56 pro cizí hlas).
Nastaveno: `threshold = 0.60`, `margin = 0.10`, `min_seconds = 30` (kratší označení se neporovnávají
ani neukládají). Přehodnotit po 10+ nahrávkách s více lidmi.
