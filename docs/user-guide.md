# teamsrec-transcribe – user guide

The tool transcribes meeting recordings, works out who spoke when, and saves the transcript next to the recording.
It works on one recordings folder (default `D:\meetings`), where both teamsrec-capture and imports of Teams
recordings end up.

The review page itself is in Czech; button labels and messages are quoted here as they appear on the page.

## 1. Installation (Windows, NVIDIA GPU)

Once:

1. **Tools:** `winget install astral-sh.uv Gyan.FFmpeg GitHub.cli` (gh only for development).
2. **Sources and environment:**
   ```
   git clone https://github.com/drzdez/teamsrec-transcribe D:\projects\teamsrec-transcribe
   cd D:\projects\teamsrec-transcribe
   uv sync --extra all
   ```
   This downloads Python 3.12, torch with CUDA 12.8 (~3 GB) and everything else. It needs NVIDIA driver 570 or newer.
3. **Hugging Face** (the speaker models are behind a licence):
   - sign up at huggingface.co, and on https://huggingface.co/pyannote/speaker-diarization-community-1 click
     "Agree and access repository";
   - log in: `.venv\Scripts\hf.exe auth login` (a code for the browser). The token is stored in your profile, nothing else.
4. **Model for the minutes.** The default is a local model through Ollama, so the transcript never leaves the PC:
   ```
   winget install Ollama.Ollama
   ollama pull gemma4:31b        # 20 GB, fits 24 GB VRAM
   ```
   The alternative is the Claude API (better minutes, the transcript goes to Anthropic's cloud): in the settings
   choose Claude as the summary service and a model such as `claude-opus-5-5`, and enter the key from
   https://console.anthropic.com/ under **Nastavení → Klíče API** (or set the environment variable
   `TEAMSREC_ANTHROPIC_API_KEY`; the tool's own name on purpose: a plain `ANTHROPIC_API_KEY` would be found by other
   Anthropic tools such as Claude Code, which would offer to bill against it). A claude.ai Max subscription does not
   cover the API; credit is prepaid in the Console. The minutes of a 70-minute meeting took 36k input and 7k output
   tokens, about 0.35 USD.
5. **Configuration:** `bin\teamsrec-transcribe.cmd config --init` creates `%APPDATA%\teamsrec\teamsrec.toml` and asks
   for your name and whether it may read the calendar of classic Outlook on this PC (`[calendar] outlook`). With the
   calendar each recording gets the right meeting title and the list of participants: capture when the call starts,
   transcribe when it imports a recording (by its start time). It is read locally through COM; nothing leaves the PC.
   The new Outlook (no COM) provides no calendar; then the title from the Teams window stays. Your name is stored as
   `[user] name` and used for live recordings: the microphone track is only your voice, so your replies get your name
   without guessing. An empty name turns this off. Then adjust `out_dir` and `glossary` (see chapter 5) – easiest on
   the **Nastavení** page.
6. **The command from anywhere:** add `D:\projects\teamsrec-transcribe\bin` to PATH (Settings → environment variables),
   or call `bin\teamsrec-transcribe.cmd` with its full path. The launcher finds the WinGet ffmpeg by itself.

Check: `teamsrec-transcribe config` must show `ffmpeg: ok` and the right `out_dir`.

The first transcription downloads the models (~5 GB: whisper large-v3, alignment for the language, pyannote, OCR),
so the first run takes a few minutes longer.

The review page can also run as a desktop window (`desktop/`, Tauri); see the README.

## 2. Daily use

### A Teams meeting recording (the most common case)

1. In Teams open the meeting recording and download it (file `<Title>-YYYYMMDD_HHMMSS-Meeting Recording.mp4`).
2. Move it to `D:\meetings\_inbox\`.
3. Run
   ```
   teamsrec-transcribe process
   ```
   The import takes the title and time from the file name, works out from the video who spoke when, transcribes
   the sound and saves the results. The processed file moves to `_inbox\done\`.

70 minutes of recording take about 2 minutes (video) + 3.5 minutes (transcript) on an RTX 5090; longer on a weaker card.

### The newest recording with one click

A call is recorded from its first second, as soon as Teams takes the microphone, with no clicking: the tray only
shows "Nahrávám: …". A recording can be discarded at any time from the tray icon's menu (Abort & delete). Who prefers
to be asked sets "Na začátku hovoru v Teams" in the settings (`[capture] prompt_default = "ask"`: a "Zahodit?" box,
kept without an answer within 45 s, or `"skip"`: discarded without an answer). The capture app is also watched every
5 minutes by the Windows scheduled task `teamsrec-capture`: if it crashed, it starts again (a second instance quits
by itself).

A double click on the tray icon (or a click on its "Saved …" balloon) opens the review page, on that recording.

The desktop shortcut **teamsrec – zpracovat poslední** (or `bin\teamsrec-process-latest.cmd`) imports what is in the
`_inbox` and processes it, then the newest recording: transcript, export and minutes. The window stays open so the
result can be read. The same from a terminal: `teamsrec-transcribe process --latest`.

`latest` works in place of a stem in every command, e.g. `teamsrec-transcribe label-speakers latest`
or `teamsrec-transcribe summarize latest --force`.

### One particular file

```
teamsrec-transcribe transcribe "C:\downloads\Porada-20260904_090000-Meeting Recording.mp4"
```

A file without a sidecar is imported automatically. The original file stays where it is.

### Any audio or video (a voice memo, a phone recording, another tool)

```
teamsrec-transcribe import "C:\memos\meeting.m4a" --title "Supplier meeting" --start "2026-09-04 09:00" --participants "Jana Nováková,Petr Svoboda"
teamsrec-transcribe process 2026-09-04_0900_supplier-meeting
```

Without `--title` and `--start` the time comes from the file's metadata, or its modification time, and the title from
the file name. A list of participants improves the spelling of names and helps OCR read the name labels in the video.

### Live recordings from teamsrec-capture

For live recordings capture also records the Teams windows as videos `<stem>_screen<N>.mp4`, and transcribe reads the
highlighted name labels from them just as in downloaded recordings, so the other participants get names from the
picture. Your voice gets the name from `[user] name`: the part of the diarization that coincides with microphone
activity is you. The others stay `SPEAKER_XX` until voice prints or you name them. Playback recordings
(*Record playback*) have no microphone track, so no name comes from it.

### Language per speaker and invented sentences

A mixed meeting (you speak Czech, colleagues Slovak) used to get one language for everybody, and your Czech came out
half Slovak ("Tak ja bych možná začal"). Now, after the speakers are separated, each speaker's language is detected
from their own replies (for anyone who speaks at least 20 s), and whoever clearly speaks another language from
`[transcribe] languages` than the meeting gets their replies transcribed again in their language. Such a reply has
`language: cs` in the transcript. To turn it off: `[transcribe] per_speaker_language = false`; it only applies with
`language = "auto"`. It costs about half a minute extra.

Whisper sometimes "hears" a sentence in silence, a click or music – typically "Ďakujem za pozornosť.", "Titulky
vytvořil…" or a web address. Such replies are dropped, as are short replies much quieter than the rest of the
recording (in the log: `dropped N invented replies`).

### Deleting the audio after a while

Audio takes ~0.5 GB per hour (microphone, system sound, mix, window videos). Once a meeting is finished – it has a
transcript and minutes and every speaker has a name – the audio is no longer needed:

```
teamsrec-transcribe purge-audio --older-than 90 --dry-run   # only lists what would be deleted and how much it frees
teamsrec-transcribe purge-audio --older-than 90             # deletes (asks first)
```

`_sys.wav`, `_mic.wav`, `_mix.wav` and `_screen*.mp4` are deleted; transcript, subtitles, minutes, names and the
sidecar stay, and the sidecar notes `audio_purged`. Recordings where someone is still unnamed stay whole (the page
needs voice samples for them). Automatically after every `process`: `[retention] audio_days = 90` (default `0` =
keep). Deleted audio cannot be transcribed again.

### Transcription in the cloud (OpenAI, ElevenLabs)

The default transcription is local WhisperX on the graphics card. Next to it there are two cloud services:

| `[transcribe] provider` | model (`openai_model` / `elevenlabs_model`) | what it does |
|---|---|---|
| `whisperx` | `large-v3` | local, glossary in the prompt, language detection, voice prints |
| `openai` | `gpt-4o-transcribe-diarize` | speakers A/B/…, no glossary, no language, no word times, no voice prints |
| `elevenlabs` | `scribe_v2` | speakers, word times, language; no glossary and no voice prints |

**The cloud sends the meeting's audio out** (see [privacy.md](privacy.md)). Enter the key under **Nastavení** on the
review page (it is stored in the Windows Credential Manager), or in the environment variable
`TEAMSREC_OPENAI_API_KEY` / `TEAMSREC_ELEVENLABS_API_KEY` (or the usual `OPENAI_API_KEY` / `ELEVENLABS_API_KEY`), which
wins. The audio is compressed before it is sent (Opus; OpenAI has a 25 MB limit).

To try it without changing the settings: `teamsrec-transcribe compare-transcribe <recording> --provider openai` writes
`<stem>.openai.txt` next to the main transcript (which stays as it is); on the page it is under the Přepis tab, in the
selector on the right. The first comparison (stand-up of 29 Sep, Slovak, 16 min):

| | time | sample ("Jiří, ty tam asi nemáš updates…", "v štvrtok ste mali Archiboard?", "mrknem") |
|---|---|---|
| WhisperX (local) | ~1.5 min | name ✓, "Štátok ste mali Archiboard?" ✗, "mrknem" ✓ |
| ElevenLabs `scribe_v2` | 37 s | "Jiri" ✓, "v štvrtok ste mali Archibord?" ✓, "mrknem" ✓; also writes "uhm" and repetitions, longer replies |
| OpenAI `gpt-4o-transcribe-diarize` | 3 min | "Zdaj niekdy" ✗, "čo tak ste mali ArchiveBot" ✗, "mrtnem" ✗ |

ElevenLabs was the most accurate, WhisperX close behind, free and without sending the audio anywhere; OpenAI was
clearly worse.

### Voice prints

Voice prints are biometric data of colleagues: they are **off** by default (`[voiceprints] enabled`, `config --init`
asks), and whoever does not want to be recognised by voice gets the tick in the Hlas column of the Lidé tab removed –
their prints are deleted at once and no new ones are made. What is stored, where, what leaves the PC, and a note for
colleagues: [privacy.md](privacy.md).

Whom you have named once (on the page or with `label-speakers`) is recognised by voice in later recordings: **confirming**
a name stores a voice print from the diarization in `_speakers\voiceprints.json` (only on this PC, at most 10 prints per
person, and only ones that add something new – an almost identical sample is dropped).

What the application only guessed – a name label from the video, your voice from the microphone track, a match with a
print – **is not stored permanently**: it stays with that meeting and the card shows "nepotvrzeno". Such a name gets
into neither the shared list of people nor the prints until you save it, so a mistake is not learned (a misread name
label from the video would otherwise create a person and a print). The voice sample stays in the recording's
transcript, so it can be confirmed later.

In a new recording each unknown speaker is compared with the prints, and when the match is high enough and clearly the
best, it gets the name at once. On the page this shows in green as "poznáno po hlase: Karel Horák (shoda 0.72) – …";
correct a wrong match by changing the name. Settings `[voiceprints]`: `enabled`, `threshold` (the match needed),
`margin` (the lead over the second best). Delete one person's prints with
`teamsrec-transcribe people forget-voice <id>`; the whole `voiceprints.json` can be deleted at any time.

An older recording with unknown speakers can be compared with the prints collected since, without a new transcript:
on the page the button "Zkusit poznat neznámé po hlase" above the cards, in a terminal
`teamsrec-transcribe recognize <stem>` (without a stem it goes through all recordings; `--summary` regenerates the
minutes where it recognised somebody).

A voice print is a colleague's biometric data. It stays local and only saves naming the speakers by hand every time;
tell the team, just as about the recording itself.

A recording without sound: when the audio device delivers nothing (a headset asleep, a dongle switched to another
profile), capture marks it `audio_silent` in the sidecar. Such a recording is not transcribed (`transcribe` stops
with a message) and `latest` skips it, so "process the newest" takes the newest recording where something can be heard.

An on-site meeting without Teams: in the tray menu *Record on-site meeting (microphone only)*. Only the microphone is
recorded (the on-site microphone setting is part of the device name, on a laptop the microphone array; it must be
enabled in Windows); title and participants come from the calendar; it ends with *Stop & keep*. Everybody is on one
microphone, so your voice is not named from it; names come from voice prints and the diarization, the rest you add on
the page.

## 3. Output

Every recording has its own folder `D:\meetings\YYYY\MM\<stem>\` (stem = `2026-09-03_1331_wfms-future-version`: date,
time and meeting title). Every file in it carries the stem in its name, so it stays unambiguous when forwarded. The
folder gets:

| File | Content |
|---|---|
| `<stem>.summary.md` | the minutes: summary, topics, decisions, action items (who / what / due / time in the recording), open questions, terms, speakers |
| `<stem>.summary.<model>.md` | minutes from another model (comparison) |
| `<stem>.txt` | readable transcript: header (title, start, length, language, speakers) and lines `[hh:mm:ss] Name: text`; `?:` = a reply the diarization gave nobody |
| `<stem>.srt` | subtitles, to play next to the video |
| `<stem>.transcript.json` | the full transcript with words and times, input for further processing |
| `<stem>.speakers_video.json` | who spoke when according to the video (only Teams imports with video and live window videos) |
| `<stem>.speakers.json` | manual names of the speakers (chapter 4) |
| `<stem>.json` | sidecar: the recording's metadata |
| `<stem>_mix.wav` | the audio that was transcribed (mono 16 kHz) |

Overview of everything: `teamsrec-transcribe list` (letters A = audio, V = speakers from video, T = transcript,
S = summary).

## 4. Naming the speakers

Teams recordings with video get names automatically. Audio-only recordings keep `SPEAKER_00`, `SPEAKER_01`… Name them
once:

```
teamsrec-transcribe label-speakers 2026-09-04_0900_supplier-meeting
```

For each speaker it shows how long they spoke and a sample sentence; you type the name. It is stored in
`<stem>.speakers.json` and `.txt` and `.srt` are regenerated. Manual names always win.

## 4a. Reviewing the speakers on the page (the recommended way)

The desktop shortcut **teamsrec – zkontrolovat mluvčí** (or `bin\teamsrec-review-latest.cmd`, the command
`teamsrec-transcribe review latest`, or a double click on the tray icon) opens a local page. It runs only on your PC
(address 127.0.0.1) and sends nothing anywhere. The shortcut "zpracovat poslední" opens it by itself after processing
when somebody in the recording is still unnamed.

For each speaker the page shows: the label or name, how long they spoke, ▶ buttons with three voice samples, the two
longest replies, the language they were transcribed in, and a hint from the minutes (role in the meeting, possibly a
guess at the name with evidence). On the right are the fields first name, last name, nickname and the choice "v zápisu";
below them a preview of how the person will appear in the transcript and the minutes. "vybrat známou osobu…" fills the
fields from the registry, "stejná osoba jako…" copies them from another label in the same recording. The recording can
be switched at the top.

Saving creates a person in the registry (`_speakers\people.json`, the **Lidé** tab at the top), or adds the nickname and
choice to a known person. What goes into the transcript and the minutes: first name, full name, or nickname. The default
is `[people] display` (default `nick` = nickname, and the first name for whoever has none). The assignment in a
recording points at the person, so a changed nickname or choice shows at the next export without assigning again.

- Bottom left there is a flag: green "vše uloženo", orange "neuložené změny". Speakers recognised by voice or from the
  video are already in the transcript and the minutes; only what you change on the page needs saving.
- **Uložit** writes `<stem>.speakers.json` and regenerates `.txt` and `.srt`.
- **Uložit a přegenerovat zápis** also creates `.summary.md` again with the names (local model, 1 to 3 minutes; it asks
  for a second click first). On a recording without a transcript it starts the whole processing. The work runs on the
  server: the page can be reloaded or closed meanwhile; "⏳ běží: …" at the bottom shows what runs and what waits.
- **The meeting title** in the header can be edited (also right after recording, before the transcript): capture takes
  it from the Teams window title, which is often "Připojení ke schůzce" or "Kompaktní zobrazení schůzky". Change and
  save it: the recording's folder and all its files are renamed (new stem `date_time_new-title`), as are the sidecar,
  the transcript header, the minutes' headings and the references in voice prints. In a terminal:
  `teamsrec-transcribe rename <stem> "New title"`.
- **Zavřít** stops the server (in the desktop window, closing the window does it). Closing the browser tab is enough
  too: the server stops by itself within two minutes, never while a job runs or waits. Starting the shortcut again
  creates no new page, it opens the running one.

Actions that run long, delete or change things (regenerate or generate minutes, process, recognise by voice, merge,
detach a meeting, delete) need a second click: the first one turns the button into "Potvrdit …" and the status line
says what will happen; **Zrušit** or 6 seconds without a second click put it back.

The **Lidé** tab: a click on "N · detail" in the Hlas column (or a double click on the row) opens a person's detail: in
which recordings they are assigned and a table of the stored prints – from which recording and label, how long they
spoke, when stored, ▶ a voice sample from that recording and ✕ to delete one print or all. Then the column
"sloučit do…": when the same person exists twice (say once with only the first name from the microphone and once with
the last name), merging rewrites the assignment in all recordings and carries over the nickname, aliases and prints
(confirm with the button that appears). The same in a terminal: `teamsrec-transcribe people list` and
`people merge <keep> <merge>`.

Top left is the recording selector and next to it a **filter**: what you type narrows the list (case and diacritics do
not matter; `archi` finds "Archi board" and "Archi standup"). When the text is a valid regular expression it is used as
one — `^archi` only titles starting with archi, `board|standup` both, `2026-09-2` by the date in the folder name. Each
recording in the list shows how far it got: `✓ hotovo` (transcript and minutes, everybody named), `◐ bez zápisu`,
`◐ 2 nepojmenovaných` (or both) and `○ bez přepisu`.

Under the selector are the filter rows. **Předvolby:** starts with `1×` (meetings with a single recording), then a
button for every meeting that repeats (with its count), the meeting with the newest recording first. **Stav:** filters by processing: `◐ nezpracované` (something is still
missing), `○ bez přepisu`, `? nepojmenované` (someone left unnamed), and `⏳ zpracovává se nebo ve frontě`. **Kdy:** `dnes`, `včera`, `týden` (this week, from Monday), `‹ týden` (last week), `měsíc`, `‹ měsíc`,
`« starší` (before last month) – the small number is how many recordings fall there, the tooltip gives the full name
and the dates – and 📅 od – do for any range (both days included); a button fills od – do with its range, so it can be
adjusted. One click shows only those recordings and opens the newest; a second click clears the filter. The rows
combine (a meeting, a state and a time). What does not fit the width scrolls sideways – drag the row with the mouse,
or use the wheel. The text filter also finds dates: `2026-09-03`, `3.9.2026`, or a regexp such as `2026-09-0[34]`. When you type the filter by hand, the
open recording does not disappear from the list (it is marked as open, outside the filter), so the selection does not
jump.

Top tabs: **Schůzka** (the selected recording), **Lidé (společné)**, **Nastavení** and **Nápověda**. Inside Schůzka
are the sub-tabs **Mluvčí** (assigning names), **Přepis** (readable transcript with time and speaker) and **Zápis**
(the minutes rendered from Markdown). The sub-tabs stay at the top while scrolling, and each keeps its own scroll
position. Text is edited in the files; the page only shows it.

**Zápis** has a tab per summary, named by the model that wrote it ("hlavní" = the configured model). Above them pick a
model (Ollama on this PC, or Claude in the cloud) and **Vygenerovat zápis**: another model adds a tab, the same model
rewrites its summary. **Přegenerovat** rewrites the open one. Tabs can be dragged to reorder; ✕ deletes that summary
(after a second click).

Above the cards is the **Schůzka** panel: where the title comes from (calendar / Teams window / manual / file), which
calendar meeting is linked and how surely ("podle názvu schůzky" is reliable, "jen podle času, ověřte" is a guess, for
example an ad-hoc call during a scheduled meeting, or two meetings at once), and the participants with their source.
Buttons: **Potvrdit spojení**, **Odpojit** (the calendar participants are removed, the title stays) and **spojit s
jinou schůzkou** (offers Outlook meetings around that time; linking rewrites the title, the participants and the
folder name). The organiser and the planned time from the calendar get into the minutes.

When the selected recording has no transcript yet, the page says so and offers the button "Ano, přepsat a zpracovat"
(transcript, export, minutes).

At the bottom is the server's latest event (what happened and when); **Historie** opens the whole list since the server
started (a click outside it or Esc closes it). The server pushes its events to the page, so walk away from a
transcription: when it is done it shows at the bottom and **the cards reload by themselves** — after regenerating you
do not look at old names. With unsaved changes the page does not reload by itself (it would overwrite them); save and
reload. While teamsrec-capture records, a red dot "Nahrávání probíhá · …" shows next to the status line.

When a recording starts while a transcription or minutes are being generated, the recorded sound can break up (the
GPU and CPU are busy). The setting "Když začne nahrávání a běží zpracování" decides: ask (a yellow-black warning tape
in the middle of the window with **Přerušit** and **Nechat běžet**; the desktop window also flashes in the taskbar;
stopped after 10 s without an answer, or at once when no page is open), stop at once, or let it run. A stopped job
goes back to the front of the queue and finishes by itself after the recording; a job asked for during a recording waits for its end too (unless the setting is to let it run). The same tape says when the page has
lost its server (open the review page again from the tray icon).

Jobs run one at a time, in order. The recordings list marks them `[⏳ zpracovává se]` or `[⏳ ve frontě, 2.]`. A recording that waits in the queue shows its place ("Je ve frontě – 2. v pořadí")
instead of the offer to transcribe it, with **Zpracovat jako další** (it moves right behind the running job) and
**Zpracovat hned** (asks first: the running job stops, goes right behind this one and starts again from the
beginning – what it had done is lost). **Zrušit zpracování** takes a waiting recording out of the queue at once; on the one being
processed it asks first and stops it for good (its work so far is lost; the recording can be processed again).

Keys: Enter = next speaker, Esc = stop playback. An empty name means keeping the label.

A card's heading is the name the speaker got (it follows what you type), with the original label from the transcript
next to it in grey – `SPEAKER_03`, or the name from the video's name label, even if OCR misread it.

The diarization sometimes splits one person into two labels (a second microphone, a long meeting, re-joining). When
you assign the same person to both, a **Sloučit podle osoby** button appears above the cards: the replies are joined
under one label (the one with the most speech), and the voice samples of both are stored as prints if they differ
enough – a recording of the same voice in other conditions is exactly what makes recognition reliable. Merging follows
the saved assignment, so save first.

A merge is written to the transcript (`merged_speakers`) and every moved reply remembers its original label, so
**Vrátit sloučení** puts it back without a new transcript; for recordings merged earlier (without that information) only
a new transcript helps.

When the speaker assignment is completely mixed up (say two people ended up under one label), there is a **Přepsat
znovu od nuly** button above the cards: a new transcript and diarization; the manual names of that recording are
dropped (they are tied to labels that will not exist after the new transcript). It takes about as long as the first
processing.

A reply the diarization gave nobody (no speaker turn covered it, for example at the very start) is on the card
**Nepřiřazeno**: each such reply with ▶ and a choice "komu patří…", or "přiřadit všechny…" for all at once, then
**Uložit přiřazení**. If your microphone was active during such a reply, it is given to you automatically.

A speaker with a few seconds of "speech" at the bottom of the list is often noise: mouse clicks, typing, breathing. The
recogniser sometimes invents a whole sentence for it, even with terms from your domain (it has them in the prompt). The
link "✕ smazat repliky tohoto mluvčího" under the name fields removes them from the transcript and the subtitles: the
first click turns it red and waits for confirmation, the second deletes and saves at once (no further Uložit needed);
regenerate the minutes if you want them without those replies. The deletion is recorded in the transcript
(`removed_speakers`); `transcribe --force` brings the replies back. `label-speakers` in a terminal does the naming
without audio.

## 4b. The minutes

`process` writes the minutes automatically (locally through Ollama, or through the Claude API, as configured). By hand
or again:

```
teamsrec-transcribe summarize <stem>                 # in Czech, model from the configuration
teamsrec-transcribe summarize <stem> --language en   # in English
teamsrec-transcribe summarize <stem> --force         # overwrite the existing one
teamsrec-transcribe summarize <stem> --provider anthropic --force   # once through Claude
teamsrec-transcribe summarize <stem> --compare                       # also the minutes from `compare` in the configuration
```

To compare models set `compare = ["anthropic:claude-opus-5-5"]` (on the page: Nastavení → Zápis → Porovnávací zápisy):
`process` then saves `<stem>.summary.claude-opus-5-5.md` next to the main `<stem>.summary.md` (local model). Once you
have decided, empty the list. Claude model ids use dashes (`claude-opus-5-5`), not dots.

The minutes use names wherever the transcript knows them (from the Teams video, the microphone track or the page);
unknown people stay `SPEAKER_XX` in the text. The last section **Mluvčí** is a table label / name / note: for unknown
speakers the model describes their role in the meeting ("led the meeting, presented the build") and may add a guess at
the name, always with evidence from the transcript ("probably Jan: addressed at 00:20:06 and answered"). Without
evidence it gives no guess. So it pays to name the speakers first and then regenerate, or to read the minutes, assign
names by the Mluvčí section and regenerate. Action items point at the time in the recording; they can be checked in the
`.txt` or `.srt`. With Ollama everything stays on the PC. With the Claude API the transcript text goes to the cloud,
never the audio or video. Minutes from the local model take about 2 to 3 minutes on an RTX 5090, and the GPU is busy
meanwhile. Long meetings (roughly over 40 minutes of dense talk) are given to the local model in parts and the results
merged, so the whole model fits into the graphics card's memory; otherwise the work spills over to the CPU and takes up
to half an hour. A cloud model gets the whole transcript at once.

## 5. Configuration

The easiest way is the **Nastavení** button at the top right of the review page: every setting of the transcription,
minutes, voice prints, retention and recording in one place, with a description and the default of each. Only what you
changed is saved, and the comments in the file stay. The recording app picks up the changes by itself; only a new
recordings folder applies after restarting the apps. Model fields are dropdowns marked "✓ v počítači" (on this PC),
"↓ stáhne se" (downloaded on first use) or cloud; the Ollama and Claude lists are fetched when such a dropdown is
opened, or with "Načíst aktuální modely pro výběr".

At the top of Nastavení are the **API keys** (Claude, OpenAI, ElevenLabs). An entered key is stored encrypted in the
Windows Credential Manager (only for your account, service `teamsrec`) and applies at once. It cannot be shown again,
only replaced or deleted. A key in an environment variable wins, so keys set with `setx` keep working. When you choose a
cloud service without a key, the page says so.

Underneath is the file `%APPDATA%\teamsrec\teamsrec.toml`. `teamsrec-transcribe config` shows the current values.

```toml
[recordings]
out_dir = "D:/meetings"

[transcribe]
language = "auto"        # auto | cs | sk | en – detected among `languages`; can be forced for a mixed meeting
model = "large-v3"       # the most accurate; large-v3-turbo is faster and worse at domain terms
compute_type = "float16" # 8 GB VRAM and more; 4–6 GB: "int8_float16"
batch_size = 16          # 4–8 on cards with less memory
align = true             # word times; turn off on weak hardware
diarize = true           # telling the speakers apart; turn off when the video is enough, or on weak hardware
glossary = ["WFMS", "NOTAM", "Eurocontrol", "BPMN", "Entra ID"]   # your terms, abbreviations, product names

[video]
enabled = true
fps = 2
```

**The glossary pays off.** Without it the model wrote the abbreviation WFMS as "VFMS" in 30 of 49 cases, with the
glossary 0 times. Add the names of systems, projects and abbreviations that keep coming up in meetings.

Every setting can be overridden once on the command line, e.g.
`teamsrec-transcribe transcribe <stem> --language cs --no-diarize --force`.

## 6. When something does not work

| Symptom | Cause and fix |
|---|---|
| `ffmpeg/ffprobe not found` | ffmpeg is not on PATH. Use `bin\teamsrec-transcribe.cmd`, or set `TEAMSREC_FFMPEG_DIR` to ffmpeg's `bin` folder. |
| `CUDA is not available to torch` | An old NVIDIA driver (570+ needed), or a CPU torch got installed. Run `uv sync --extra all` again; check with `uv run python -c "import torch;print(torch.cuda.is_available())"`. |
| `ollama: cannot reach` / `model not found` | Ollama is not running or the model is not pulled: `ollama pull gemma4:31b`. Transcript and export work without minutes too. |
| the transcript is in a nonsense language | Language detection votes between the languages in `[transcribe] languages` (default cs, sk, en) on several loud stretches. The language can be forced: `transcribe <stem> --language sk --force`. |
| `no valid Claude credentials` | Claude chosen without a key: enter it under Nastavení on the review page (or `TEAMSREC_ANTHROPIC_API_KEY`). |
| a comparison summary is missing | The model id is wrong (`claude-opus-5.5` instead of `claude-opus-5-5`); the error is in Historie. |
| `GatedRepoError` / 403 for pyannote | The model's terms are not accepted or the login is missing. Installation step 3. |
| Names from the video are garbled | OCR does not know diacritics. Give `--participants` when importing; names are matched by similarity. `teamsrec-transcribe video <stem> --participants "..."` repeats the analysis. |
| The video gave no speakers | The recording has no Teams layout with name labels (another tool, shared content only). The diarization gives the speakers; name them by hand. |
| The transcript exists and nothing happens | Finished recordings are skipped. `--force` overwrites. |
| Little GPU memory | `compute_type = "int8_float16"`, `batch_size = 4`, possibly `align = false`. See `docs/hardware-portability.md`. |
| The recorded sound breaks up during processing | Set "Když začne nahrávání a běží zpracování" to ask or stop (chapter 4a). |

Detailed output: the switch `-v` before the command (`teamsrec-transcribe -v process`).

## 7. Not there yet

- Running without an NVIDIA GPU (CPU, Apple Silicon); see `teamsrec-capture/docs/cross-platform-design.md`.
- Full-text search in the transcripts.
- An installer for a new machine (`install.ps1`).
