# teamsrec-transcribe – uživatelská příručka

Nástroj přepisuje nahrávky schůzek, určí, kdo kdy mluvil, a uloží přepis vedle nahrávky. Pracuje nad jedním
adresářem nahrávek (výchozí `D:\meetings`), kam ukládá jak teamsrec-capture, tak importy z Teams záznamů.

## 1. Instalace (Windows, NVIDIA GPU)

Jednorázově:

1. **Nástroje:** `winget install astral-sh.uv Gyan.FFmpeg GitHub.cli` (gh jen pro vývoj).
2. **Zdrojáky a prostředí:**
   ```
   git clone https://github.com/drzdez/teamsrec-transcribe D:\projects\teamsrec-transcribe
   cd D:\projects\teamsrec-transcribe
   uv sync --extra all
   ```
   Stáhne Python 3.12, torch s CUDA 12.8 (~3 GB) a vše ostatní. Potřebuje NVIDIA driver 570 nebo novější.
3. **HuggingFace** (modely pro rozpoznání mluvčích jsou za licencí):
   - registrace na huggingface.co, na stránce https://huggingface.co/pyannote/speaker-diarization-community-1
     kliknout „Agree and access repository“,
   - přihlášení: `.venv\Scripts\hf.exe auth login` (kód do prohlížeče). Token se uloží do profilu, nic dalšího.
4. **Model pro zápisy.** Výchozí je lokální model přes Ollamu, přepis tak nikdy neopustí počítač:
   ```
   winget install Ollama.Ollama
   ollama pull gemma4:31b        # 20 GB, vejde se do 24 GB VRAM
   ```
   Alternativa je Claude API (kvalitnější zápis, přepis odchází do cloudu Anthropic): v konfiguraci
   `provider = "anthropic"`, `model = "claude-opus-5"` a klíč z https://console.anthropic.com/ jako proměnná
   prostředí `setx TEAMSREC_ANTHROPIC_API_KEY sk-ant-...` (vlastní jméno schválně: obecnou `ANTHROPIC_API_KEY`
   by našly i jiné nástroje Anthropic, třeba Claude Code, a nabízely by účtování přes ni; tuhle čte jen teamsrec).
   Max subskripce claude.ai se na API nevztahuje, v Console se
   předplácí kredit; zápis ze 70minutové schůzky vyšel na 36 tis. vstupních a 7 tis. výstupních tokenů, tedy asi 0,35 USD.
5. **Konfigurace:** `bin\teamsrec-transcribe.cmd config --init` založí `%APPDATA%\teamsrec\teamsrec.toml`
   a zeptá se na vaše jméno. To se uloží jako `[user] name` a používá se u živých nahrávek: mikrofonní stopa
   je jen váš hlas, takže vaše repliky dostanou jméno automaticky, bez hádání. Prázdné jméno = vypnuto.
   Dále upravte `out_dir` a `glossary` (viz kapitola 5).
6. **Příkaz odkudkoli:** přidejte `D:\projects\teamsrec-transcribe\bin` do PATH (Nastavení → proměnné prostředí),
   nebo volejte `bin\teamsrec-transcribe.cmd` plnou cestou. Spouštěč sám najde ffmpeg z WinGetu.

Kontrola: `teamsrec-transcribe config` musí ukázat `ffmpeg: ok` a správný `out_dir`.

Při prvním přepisu se stáhnou modely (~5 GB: whisper large-v3, zarovnání pro daný jazyk, pyannote, OCR).
První běh je tedy o několik minut delší.

## 2. Denní použití

### Záznam schůzky z Teams (nejčastější)

1. V Teams otevřete záznam schůzky a stáhněte ho (soubor `<Název>-YYYYMMDD_HHMMSS-Meeting Recording.mp4`).
2. Přesuňte ho do `D:\meetings\_inbox\`.
3. Spusťte
   ```
   teamsrec-transcribe process
   ```
   Import vezme název a čas ze jména souboru, z videa určí, kdo kdy mluvil, přepíše zvuk a uloží výsledky.
   Zpracovaný soubor se přesune do `_inbox\done\`.

70 minut záznamu trvá zhruba 2 minuty (video) + 3,5 minuty (přepis) na RTX 5090. Na slabší kartě úměrně déle.

### Nejnovější nahrávka jedním kliknutím

Zástupce **teamsrec – zpracovat poslední** na ploše (nebo `bin\teamsrec-process-latest.cmd`) importuje, co je
ve schránce `_inbox`, a pak zpracuje jen nejnovější nahrávku: přepis, export a zápis. Okno zůstane otevřené, aby šel
přečíst výsledek. Totéž z příkazové řádky: `teamsrec-transcribe process --latest`.

Slovo `latest` funguje místo stemu ve všech příkazech, např. `teamsrec-transcribe label-speakers latest`
nebo `teamsrec-transcribe summarize latest --force`.

### Jeden konkrétní soubor

```
teamsrec-transcribe transcribe "C:\stažené\Porada-20260904_090000-Meeting Recording.mp4"
```

Soubor bez sidecaru se importuje automaticky. Původní soubor zůstane, kde je.

### Libovolné audio nebo video (hlasovka, nahrávka z telefonu, jiný nástroj)

```
teamsrec-transcribe import "C:\hlasovky\schuzka.m4a" --title "Schůzka s dodavatelem" --start "2026-09-04 09:00" --participants "Jana Nováková,Petr Svoboda"
teamsrec-transcribe process 2026-09-04_0900_schuzka-s-dodavatelem
```

Bez `--title` a `--start` se použije čas z metadat souboru, případně čas změny souboru a název souboru.
Seznam účastníků zlepší přepis jmen a pomůže OCR při čtení jmenovek z videa.

### Živé nahrávky z teamsrec-capture

U živých nahrávek (stopy `sys` + `mic`) dostane váš hlas jméno z `[user] name` v konfiguraci: ta část
diarizace, která se kryje s aktivitou mikrofonu, jste vy. Ostatní účastníci zůstávají `SPEAKER_XX`, dokud je
nepojmenujete přes `label-speakers`. U přehrávání (*Record playback*) mikrofonní stopa není, jméno se nepoužije.

V plánu jsou hlasové otisky: koho jednou pojmenujete, toho další nahrávky poznají po hlase samy. Do té doby
se vyplatí po každé schůzce s novým kolegou spustit `label-speakers`, jména se pak uplatní i v zápisu.

Nahrávky pořízené prototypem `teamsrec-capture/legacy/teamsrec.py` (živý hovor, *Record now*, *Record playback*)
leží už ve správném adresáři. `teamsrec-transcribe process` je zpracuje spolu s ostatními. Označení „já“ podle mikrofonní stopy zatím není hotové, mluvčí dá diarizace.

## 3. Výstupy

Každá nahrávka má vlastní složku `D:\meetings\RRRR\MM\<stem>\` (stem = `2026-09-03_1331_wfms-future-version`,
tedy datum, čas a název schůzky). Všechny soubory v ní nesou stem v názvu, takže zůstávají jednoznačné i po
přeposlání. Ve složce vznikne:

| Soubor | Obsah |
|---|---|
| `<stem>.summary.md` | zápis ze schůzky: shrnutí, témata, rozhodnutí, úkoly (kdo / co / termín / čas v záznamu), otevřené otázky, pojmy, mluvčí |
| `<stem>.txt` | čitelný přepis: hlavička (název, začátek, délka, jazyk, mluvčí) a řádky `[hh:mm:ss] Jméno: text` |
| `<stem>.srt` | titulky, lze pustit vedle videa |
| `<stem>.transcript.json` | úplný přepis se slovy a časy, vstup pro další zpracování |
| `<stem>.speakers_video.json` | kdo kdy mluvil podle videa (jen importy Teams s videem) |
| `<stem>.speakers.json` | ruční přejmenování mluvčích (kapitola 4) |
| `<stem>.json` | sidecar: metadata nahrávky |
| `<stem>_mix.wav` | zvuk, který se přepisoval (mono 16 kHz) |

Přehled všeho: `teamsrec-transcribe list` (písmena A = audio, V = mluvčí z videa, T = přepis, S = shrnutí).

## 4. Pojmenování mluvčích

U záznamů Teams s videem dostanou mluvčí jména automaticky. U čistě zvukových nahrávek zůstanou jako
`SPEAKER_00`, `SPEAKER_01`… Přejmenujte je jednou:

```
teamsrec-transcribe label-speakers 2026-09-04_0900_schuzka-s-dodavatelem
```

Ke každému mluvčímu se ukáže délka mluvení a ukázka věty, zadáte jméno. Uloží se do `<stem>.speakers.json`
a přegenerují se `.txt` a `.srt`. Ruční jména mají vždy přednost.

## 4b. Zápis ze schůzky

`process` vytvoří zápis automaticky (lokálně přes Ollamu, nebo přes Claude API podle konfigurace). Ručně nebo znovu:

```
teamsrec-transcribe summarize <stem>                 # česky, model z konfigurace
teamsrec-transcribe summarize <stem> --language en   # anglicky
teamsrec-transcribe summarize <stem> --force         # přepsat existující
teamsrec-transcribe summarize <stem> --provider anthropic --force   # jednorázově přes Claude
teamsrec-transcribe summarize <stem> --compare                       # navíc zápisy z `compare` v konfiguraci
```

Pro porovnání modelů nastavte v konfiguraci `compare = ["anthropic:claude-opus-5"]`: `process` pak vedle hlavního
`<stem>.summary.md` (lokální model) uloží i `<stem>.summary.claude-opus-5.md`. Až se rozhodnete, seznam vyprázdněte.

Zápis používá jména všude, kde je přepis zná (z videa Teams, z mikrofonní stopy nebo z `label-speakers`); neznámé
lidi nechává v textu jako `SPEAKER_XX`. Poslední sekce **Mluvčí** je tabulka označení / jméno / poznámka: u neznámých
mluvčích model popíše jejich roli na schůzce („vedl schůzku, představil build“) a smí tam dát i tip na jméno, vždy
s důkazem z přepisu („pravděpodobně Jan: osloven v 00:20:06 a odpověděl“). Bez důkazu tip nedává.
Vyplatí se tedy nejdřív `label-speakers` a pak `summarize --force`, nebo zápis přečíst, podle sekce Mluvčí přiřadit
jména a `summarize --force` spustit znovu. Úkoly v zápisu odkazují na čas v záznamu, dají se ověřit v `.txt` nebo `.srt`.
S Ollamou zůstává vše na počítači. S Claude API odchází do cloudu text přepisu, nikdy zvuk ani video.
Zápis z lokálního modelu trvá na RTX 5090 zhruba 2 až 3 minuty a GPU je po tu dobu obsazená.

## 5. Konfigurace

Soubor `%APPDATA%\teamsrec\teamsrec.toml`. Aktuální hodnoty zobrazí `teamsrec-transcribe config`.

```toml
[recordings]
out_dir = "D:/meetings"

[transcribe]
language = "auto"        # auto | cs | sk | en – jazyk se pozná z prvních 30 s; při smíšené schůzce lze vynutit
model = "large-v3"       # nejpřesnější; large-v3-turbo je rychlejší a horší na odborné pojmy
compute_type = "float16" # 8 GB VRAM a víc; 4–6 GB: "int8_float16"
batch_size = 16          # 4–8 na kartách s méně pamětí
align = true             # časy slov; vypnout na slabém hardwaru
diarize = true           # rozpoznání mluvčích; vypnout, když stačí video nebo na slabém hardwaru
glossary = ["WFMS", "NOTAM", "Eurocontrol", "BPMN", "Entra ID"]   # vaše pojmy, zkratky, názvy produktů

[video]
enabled = true
fps = 2
```

**Slovník (`glossary`) se vyplatí udržovat.** Bez něj model zkratku WFMS psal jako „VFMS“ ve 30 případech ze 49,
se slovníkem 0×. Přidávejte názvy systémů, projektů a zkratky, které se na schůzkách opakují.

Každé nastavení jde jednorázově přepsat z příkazové řádky, např.
`teamsrec-transcribe transcribe <stem> --language cs --no-diarize --force`.

## 6. Když něco nejde

| Projev | Příčina a řešení |
|---|---|
| `ffmpeg/ffprobe not found` | ffmpeg není v PATH. Použijte `bin\teamsrec-transcribe.cmd`, nebo nastavte `TEAMSREC_FFMPEG_DIR` na složku `bin` ffmpegu. |
| `CUDA is not available to torch` | Starý NVIDIA driver (potřeba 570+), nebo se nainstaloval CPU torch. `uv sync --extra all` znovu; ověření `uv run python -c "import torch;print(torch.cuda.is_available())"`. |
| `ollama: cannot reach` / `model not found` | Ollama neběží nebo model není stažený: `ollama pull gemma4:31b`. Přepis a export fungují i bez zápisu. |
| `no valid Claude credentials` | `provider = "anthropic"` bez `TEAMSREC_ANTHROPIC_API_KEY`. Krok 4 instalace. |
| `GatedRepoError` / 403 u pyannote | Nepřijaté podmínky modelu nebo chybí přihlášení. Krok 3 instalace. |
| Jména z videa jsou zkomolená | OCR nezná diakritiku. Zadejte `--participants` při importu; jména se dohledají podle podobnosti. `teamsrec-transcribe video <stem> --participants "..."` analýzu zopakuje. |
| Video nedalo žádné mluvčí | Záznam nemá rozložení Teams se jmenovkami (jiný nástroj, jen sdílený obsah). Mluvčí dá diarizace, pojmenujte je ručně. |
| Přepis je v jiném jazyce, než se mluvilo | Detekce z prvních 30 s selhala (úvodní ticho, angličtina na začátku). `--language cs` a `--force`. |
| Přepis existuje a nic se neděje | Hotové nahrávky se přeskakují. `--force` přepíše. |
| Málo paměti GPU | `compute_type = "int8_float16"`, `batch_size = 4`, případně `align = false`. Viz `docs/hardware-portability.md`. |

Podrobný výpis: přepínač `-v` před příkazem (`teamsrec-transcribe -v process`).

## 7. Co zatím chybí

- Označení „já“ z mikrofonní stopy u živých nahrávek.
- Jazyk per mluvčí u smíšených česko-slovensko-anglických schůzek (dnes jeden jazyk na nahrávku).
- Běh bez NVIDIA GPU a cloudoví poskytovatelé.
- Mazání audia po přepisu (`purge-audio`).
