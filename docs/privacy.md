# Soukromí: hlasové otisky a co opouští počítač

teamsrec nahrává schůzky a pozná v nich lidi. Část z toho jsou osobní údaje kolegů, hlasový otisk je navíc
biometrický údaj. Tady je, co se ukládá, kde, proč a jak se z toho kdokoli vyvlékne. Poslední oddíl je text,
který se dá kolegům rovnou poslat.

## Hlasový otisk

- **Co to je:** 256 čísel, které popisují zabarvení hlasu (embedding z diarizace pyannote
  `speaker-diarization-community-1`). Z otisku se nedá přehrát ani složit řeč; zvuk se do něj neukládá.
- **Kde je:** jen v souboru `<složka nahrávek>\_speakers\voiceprints.json` na počítači toho, kdo nahrává.
  Nikam se neposílá.
- **K čemu:** aby další nahrávky poznaly, kdo mluví, a v přepisu i zápisu stálo jméno místo `SPEAKER_03`.
- **Kdy vzniká:** jen když uživatel na kontrolní stránce (nebo `label-speakers`) **potvrdí**, že daný hlas patří
  dané osobě. Odhady aplikace (jmenovka z videa, shoda s otiskem) se trvale neukládají.
- **Kolik:** nejvýš 10 otisků na osobu, skoro shodné se zahazují.
- **Výchozí stav:** vypnuto. Zapíná se v `%APPDATA%\teamsrec\teamsrec.toml` (`[voiceprints] enabled = true`);
  `teamsrec-transcribe config --init` se na to při instalaci zeptá.

## Jak se vyvléknout

Kdo si nepřeje být poznáván po hlase, stačí říct tomu, kdo nahrává. Ten na kontrolní stránce v záložce
**Lidé** zruší u jeho jména zaškrtnutí ve sloupci **Hlas** a dá **Uložit lidi**:

- všechny jeho otisky se **hned smažou**,
- nové už nevzniknou ani při potvrzení jména (přání se pamatuje v `people.json`),
- v přepisech zůstane jeho jméno tam, kde ho někdo ručně zadal; jinak `SPEAKER_XX`.

Totéž z příkazové řádky: `teamsrec-transcribe people forget-voice <id>` smaže otisky (vyvléknutí natrvalo je
ta volba na stránce). Celý soubor `voiceprints.json` lze kdykoli smazat, aplikace funguje dál bez rozpoznávání.

## Co opouští počítač

- **Otisky nikdy.** Rozpoznávání mluvčích po hlase běží jen lokálně.
- **Zvuk jen s cloudovým přepisem.** Ve výchozím stavu (`[transcribe] provider = "whisperx"`) běží přepis lokálně
  na grafické kartě a zvuk počítač neopouští. S `provider = "openai"` nebo `"elevenlabs"` – nebo příkazem
  `compare-transcribe --provider …` – se **zvuk celé schůzky** (zkomprimovaný) posílá službě OpenAI nebo
  ElevenLabs. To je třeba vědomě zvolit a kolegům říct.
- **Text přepisu jen při zápisu přes Claude**: když je v `[summarize]` nastaven `provider = "anthropic"` nebo
  je Claude v `compare`, pošle se text přepisu (se jmény mluvčích) do Claude API společnosti Anthropic, aby
  z něj vznikl zápis. S `provider = "ollama"` a prázdným `compare` nic neodchází.
- **Kalendář** (Outlook) se čte lokálně přes COM, nic se neposílá.

## Text pro kolegy

> Ahoj, schůzky si nahrávám kvůli zápisu (teamsrec, běží jen na mém počítači). Přepis dělám lokálně; aby
> v něm byla jména, pamatuje si aplikace hlasové otisky lidí, které jsem u nahrávky ručně pojmenoval –
> 256 čísel popisujících hlas, žádný zvuk, uložené jen u mě a nikam se neposílají.
> [Zápis z přepisu nechávám napsat i přes Claude API, tam jde text přepisu.]
> [Přepis dělá služba OpenAI / ElevenLabs, tam jde i zvuk schůzky.]
> Kdybys nechtěl/a být poznáván/a po hlase, dej mi vědět – otisky smažu a nové už nevzniknou.
