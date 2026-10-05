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

## Who is highlighted in the Teams window (2026-09-28)

Going through nine live recordings with window analysis: **the user's own tile was never highlighted** (no
`speakers_video.json` contains their name). Teams does not draw the outline around the user's own tile in their own
client, so while they speak the previous speaker stays highlighted – and the video credits their speech to that other
person. In the Archi board meeting of 2026-09-24 that meant 52 minutes of one tile highlighted while the user's
microphone was loud (median −40 dB against −73 dB for the other labels).

Consequences: the microphone track wins over the video (it is the user's own hardware); the video names only the
replies the microphone did not take. And names from name labels are used only when they match a participant of the
meeting: OCR of a live window read "Michal Bartoš" as "Mihoy Bardtnbnsc" and "Mihoy Bardtnongg", which a score
difference cannot reliably correct (0.46 vs 0.43 for another participant), so for such a name `SPEAKER_XX` is better.

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

## Voice prints – calibration (2026-09-11)

Script `lab/voiceprints_calib.py`: diarization of all recordings in OUT_DIR with `return_embeddings=True` (pyannote
community-1 through whisperx), labels named by overlap with the existing transcript, cosine similarity of all pairs
(recording, speaker). Three recordings (12 s, 29 min, 10 min), 8 labels.

| pair | similarity |
|---|---|
| Jiří 29 min (microphone) × Jiří 10 min (microphone) | 0.94 |
| Jiří 12 s × Jiří 29 / 10 min | 0.43 / 0.44 |
| different people, named (Jiří × Kája, Jiří × Michal, Kája × Michal) | max 0.36, mean 0.24 |
| unnamed SPEAKER_02 (24 s, 29-min recording) × Jiří 10 min | 0.56 |

Conclusions: a long enough stretch of the same person's speech gives a match well above different people; short
stretches (under ~30 s) give an unreliable embedding both ways (0.43 for the same person, 0.56 for a stranger's voice).
Set: `threshold = 0.60`, `margin = 0.10`, `min_seconds = 30` (shorter labels are neither compared nor stored).
Revisit after 10+ recordings with more people.

Storing prints (2026-09-28): a print whose similarity to a stored one is ≥ `NEAR_DUPLICATE = 0.95` is dropped – the same
voice in the same conditions gave 0.94 (row above), so above 0.95 a sample adds nothing new and only takes one of the
ten slots. Merging speakers (`merge_same_person`) therefore stores the samples of all merged labels – the ones made in
other conditions get through and improve recognition.

Addendum after 7 recordings (10 people with prints): the 0.60 threshold just rejected two correct candidates (0.597
with a lead of 0.29, 0.58 with a lead of 0.15), strangers' voices stay below 0.36. Threshold lowered to 0.55;
candidates between 0.40 and the threshold are shown on the page as "nejpodobnější hlas" (the most similar voice) with a
button to confirm, but are not assigned by themselves.

## Local minutes – context vs. VRAM (2026-09-11)

gemma4:31b (20 GB) on an RTX 5090 Laptop with 24 GB: with `num_ctx` 38,343 (a 70-minute transcript, 64k characters)
Ollama put 12 % of the model on the CPU (`ollama ps`: 12%/88% CPU/GPU) and the minutes did not finish within the
30-minute timeout. With `num_ctx` ≈ 20,000 the model is 100 % on the GPU. Hence `ollama_max_ctx = 20480`, and
transcripts over ~23k characters are summarised in parts (each part its own minutes with the same headings, then
merged). A 44-minute meeting: 2 parts + merge, 18k tokens in / 4.3k out. During a concurrent Teams meeting the GPU is
shared and the parts take several times longer.

## Mixed speaker groups and voices per reply (2026-10-05)

"Peter Hirko" in the 86-minute "Diskusia 2 blocker" had 23 replies of several voices. The cause was the window video:
it named single replies by the highlighted tile, and a live window keeps the previous speaker highlighted. In 9 of 12
recent meetings the video made such named groups next to the voice groups (e.g. "Pavol Orosz" next to SPEAKER_01,
which the voice print recognised as Pavol at 0.93). Since then the video names whole voice groups only, after the
voice prints, and never a person the voice found elsewhere.

Voices per reply (`lab/reply_voices.py`: the diarization's embedding model on each reply of 1.5–10 s, 636 replies in
18 s on the GPU) do not separate people reliably: replies of one group are 0.45–0.57 alike, different groups 0.20–0.35.
They also live in another space than the stored prints (the diarization returns group embeddings after its own
transform), so they cannot be compared with people. Not used.

Group embeddings of one meeting (25 meetings): the same person split in two 0.25–0.71 (median 0.45), different people
0.07–0.54 (median 0.25). So "hlasem podobná skupina" from 0.45 is a hint to listen to, never an automatic merge.

## Microphone activity threshold (2026-09-30)

A Sony WH-1000XM6 connected directly over Bluetooth gates its microphone: almost digital silence between words (floor
−104 dBFS) and a −80…−90 dB residue while the others talk. "Active" as floor + 10 dB then counted that residue as the
user speaking (the others' segments had the mic "active" 61 % of the time, everyone ended up under the user's name).
An absolute minimum of −55 dBFS fixes it: the others' segments 5 %, the user's 92 %; on the dongle recordings 11–12 %
(was 28–34 %), the user's 86–88 %.
