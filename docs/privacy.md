# Privacy: voice prints and what leaves the PC

teamsrec records meetings and recognises the people in them. Part of that is colleagues' personal data; a voice print
is moreover biometric data. Here is what is stored, where, why, and how anyone can opt out. The last section is a note
that can be sent to colleagues as it is.

## Voice print

- **What it is:** 256 numbers describing the timbre of a voice (an embedding from the pyannote diarization
  `speaker-diarization-community-1`). No speech can be played or put together from a print; no audio is stored in it.
- **Where it is:** only in the file `<recordings folder>\_speakers\voiceprints.json` on the PC of whoever records. It is
  not sent anywhere.
- **What for:** so that later recordings know who speaks, and the transcript and minutes show a name instead of
  `SPEAKER_03`.
- **When it is made:** only when the user **confirms** on the review page (or with `label-speakers`) that a voice
  belongs to a person. The application's guesses (a name label from the video, a match with a print) are not stored
  permanently.
- **How many:** at most 10 prints per person; almost identical ones are dropped.
- **Default:** off. It is turned on in the settings (Nastavení → Hlasové otisky, `[voiceprints] enabled = true` in
  `%APPDATA%\teamsrec\teamsrec.toml`); `teamsrec-transcribe config --init` asks during installation.

## How to opt out

Whoever does not want to be recognised by voice just tells the person who records. They untick the **Hlas** column for
that name on the review page's **Lidé** tab and click **Uložit lidi**:

- all of that person's prints are **deleted at once**,
- no new ones are made, not even when a name is confirmed (the wish is remembered in `people.json`),
- the transcripts keep their name where someone typed it by hand; otherwise `SPEAKER_XX`.

The same from the command line: `teamsrec-transcribe people forget-voice <id>` deletes the prints (the permanent
opt-out is the choice on the page). The whole `voiceprints.json` can be deleted at any time; the application works on
without recognition.

## What leaves the PC

- **Prints never.** Recognising speakers by voice runs only locally.
- **Audio only with cloud transcription.** By default (`[transcribe] provider = "whisperx"`) the transcription runs
  locally on the graphics card and the audio does not leave the PC. With `provider = "openai"` or `"elevenlabs"` – or
  the command `compare-transcribe --provider …` – **the audio of the whole meeting** (compressed) is sent to OpenAI or
  ElevenLabs. That has to be chosen deliberately and colleagues told.
- **"Co nejrychleji (cloud)"** on the review page sends both: the audio to the cloud transcription service and the
  transcript text to Claude, for that one recording; the page asks before it starts.
- **The transcript text only for minutes through Claude:** when the summary service is Claude (`provider = "anthropic"`)
  or Claude is among the comparison summaries (`compare`), the transcript text (with the speakers' names) is sent to
  Anthropic's Claude API to write the minutes. With `provider = "ollama"` and an empty `compare` nothing leaves.
- **API keys** entered on the settings page are stored encrypted in the Windows Credential Manager of your account and
  are only sent to the service they belong to.
- **The calendar** (Outlook) is read locally through COM; nothing is sent.
- **The review page** runs on 127.0.0.1 only; it is not reachable from the network.

## Note for colleagues

The note below is in Czech, as sent to the team; adapt the bracketed sentences to the settings in use.

> Ahoj, schůzky si nahrávám kvůli zápisu (teamsrec, běží jen na mém počítači). Přepis dělám lokálně; aby
> v něm byla jména, pamatuje si aplikace hlasové otisky lidí, které jsem u nahrávky ručně pojmenoval –
> 256 čísel popisujících hlas, žádný zvuk, uložené jen u mě a nikam se neposílají.
> [Zápis z přepisu nechávám napsat i přes Claude API, tam jde text přepisu.]
> [Přepis dělá služba OpenAI / ElevenLabs, tam jde i zvuk schůzky.]
> Kdybys nechtěl/a být poznáván/a po hlase, dej mi vědět – otisky smažu a nové už nevzniknou.

In English:

> Hi, I record meetings to write up the minutes (teamsrec, it runs only on my PC). The transcript is made locally; to
> put names in it, the application keeps voice prints of the people I named by hand in a recording – 256 numbers
> describing a voice, no audio, stored only with me and never sent anywhere.
> [The minutes are also written through the Claude API, which receives the transcript text.]
> [The transcript is made by OpenAI / ElevenLabs, which receives the meeting's audio.]
> If you would rather not be recognised by voice, let me know – I will delete the prints and no new ones will be made.
