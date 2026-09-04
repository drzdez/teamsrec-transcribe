# Využitelnost řešení na různém hardwaru

Zhodnocení, ne plán. Vychází z měření v `lab/FINDINGS.md` (2026-09-04) a z veřejně známých vlastností použitých
knihoven. Čísla pro jiný hardware jsou **odhady** z poměrů výkonu, ne měření.

## Co vlastně zatěžuje hardware

Capture (.NET, nahrávání WASAPI) je zanedbatelné a poběží na čemkoli s Windows. Veškerá zátěž je v transkripci,
která má čtyři fáze. Změřeno na RTX 5090 Laptop (24 GB VRAM) pro 70 min záznamu:

| Fáze | Knihovna | Čas | VRAM (odhad) | Poznámka |
|---|---|---|---|---|
| ASR large-v3, float16 | faster-whisper / CTranslate2 | 61 s | ~3,5 GB | jediná fáze, kde záleží na modelu a přesnosti výpočtu |
| zarovnání (word timestamps) | wav2vec2 přes torch | 40 s | ~1 GB | volitelné, bez něj segmenty po 30 s |
| diarizace | pyannote community-1 | 60 s | ~1,5 GB | volitelné; u záznamů s videem nahraditelné analýzou videa |
| mluvčí z videa | numpy + easyocr | ~240 s | ~0,5 GB (OCR) | téměř celé na CPU (dekódování videa), na GPU nezávislé |

Špička VRAM celého řetězce je kolem 5 až 6 GB. Vše je 20× rychlejší než reálný čas, takže jakýkoli hardware,
který je i 10× pomalejší, je pro noční nebo pozaďové zpracování stále v pohodě.

## NVIDIA RTX a jiné NVIDIA karty

CTranslate2 i torch běží na všech kartách od Turingu (RTX 20xx, compute capability 7.5) nahoru, Pascal (GTX 10xx)
funguje, ale bez rychlého float16. Balíčky cu128 vyžadují driver 570+; pro starší karty jde použít i cu124/cu126.

| Karta | Typ | VRAM | Odhad ASR 70 min | Doporučené nastavení |
|---|---|---|---|---|
| RTX 5090 Laptop (změřeno) | Blackwell | 24 GB | 1 min | large-v3, float16, batch 16 |
| RTX 4090 / 4080 desktop | Ada | 16–24 GB | 1 min nebo méně | totéž |
| RTX 4070 / 4060 laptop | Ada | 8 GB | 2–3 min | large-v3, float16, batch 8 |
| RTX 3060 / 3050 | Ampere | 6–12 GB | 3–4 min | large-v3, float16 nebo int8_float16, batch 4–8 |
| RTX 2060 / 2070 | Turing | 6–8 GB | 4–6 min | large-v3, int8_float16 |
| GTX 1650 / 1060 | Pascal | 4–6 GB | 8–15 min | large-v3 int8 nebo medium, batch 2; zarovnání a diarizaci zvážit vypnout |
| GeForce MX, 2 GB | libovolný | 2 GB | – | GPU nepoužitelná, jako CPU níže |

Praktické minimum je **4 GB VRAM s int8** (large-v3 int8 má ~1,6 GB), pohodlné **6 GB**, bez kompromisů **8 GB**.
Konfigurace se dá volit automaticky podle zjištěné VRAM a compute capability; dnes je natvrdo v laboratorním skriptu.

## Počítače bez NVIDIA GPU

**Windows laptop s Intel/AMD grafikou (jen CPU).** CTranslate2 má dobrou CPU cestu (int8, AVX2/AVX-512).
Orientačně na 8jádrovém moderním CPU:

| Model | Odhad ASR 70 min | Kvalita |
|---|---|---|
| large-v3 int8 | 40–90 min | plná |
| large-v3-turbo int8 | 12–25 min | mírně horší na termíny, s promptem přijatelná |
| medium int8 | 15–30 min | znatelně horší čeština/slovenština |
| small int8 | 5–8 min | pro zápis nedostačující |

Diarizace pyannote na CPU je zhruba v reálném čase (70 min ≈ 40–80 min). U importovaných Teams záznamů ji
analýza videa nahradí zcela, což je na slabém hardwaru největší úspora. Zarovnání na CPU je v řádu 10 min.
Závěr: **na CPU je řešení použitelné pro dávkové zpracování** (přes noc, po schůzce na pozadí), ne pro rychlý přepis.

**AMD Radeon.** ROCm je jen pro Linux a CTranslate2 ho nepodporuje; na Windows se AMD chová jako CPU.
Existují cesty přes ONNX Runtime s DirectML (whisper v ONNX), ale to je jiný stack než WhisperX.

**Intel Arc / Core Ultra NPU.** OpenVINO umí Whisper (optimum-intel, whisper.cpp), opět jiný stack. Bez diarizace.

**Apple Silicon (Mac).** Nejlepší ne-NVIDIA cesta. `mlx-whisper` s large-v3 běží na M2 Pro a výš zhruba
5–10× rychleji než reálný čas, pyannote na MPS/CPU je pomalejší. WhisperX tam neběží, byl by to samostatný provider,
jak už předpokládal původní návrh.

## Cloud a vzdálený výpočet

Tři odlišné varianty, s různým dopadem na soukromí záznamů:

1. **Vlastní vzdálený worker.** Stejné CLI na jiném vlastním stroji s GPU (domácí server, druhý PC). Capture uloží
   nahrávku do sdílené složky (OneDrive, SMB, syncthing), worker ji zpracuje a výsledky vrátí vedle ní. Data neopouští
   vlastní infrastrukturu. Prototyp s tím počítal (POST_HOOK).
2. **Pronajatá GPU** (RunPod, Vast.ai, Lambda, Azure NC řada). Stejný kontejner jako lokálně, 70 min záznamu za
   ~3 min a jednotky korun. Záznam se posílá třetí straně, model ale zůstává pod kontrolou.
3. **Hotové API pro přepis.** Azure AI Speech (batch, diarizace, cs/sk/en, evropské regiony), ElevenLabs Scribe,
   OpenAI, AssemblyAI, Deepgram. Nulové nároky na hardware, nulová údržba modelů, záznam i přepis u poskytovatele.
   Většina nemá slovník pojmů srovnatelný s `initial_prompt`, výsledek na termínech bude horší.

## Jak zvýšit využitelnost (možné směry, neplánováno)

Architektura už s tím počítá: transkripce je provider za rozhraním, capture je od hardwaru nezávislé. Rozšíření
na další hardware znamená přidat provider, ne měnit řetězec.

- **Automatická volba konfigurace** podle zjištěné GPU/VRAM: model, compute type, batch, zapnutí zarovnání a diarizace.
- **CPU provider** (faster-whisper int8) se stejným výstupem, jako nouzová cesta bez GPU.
- **Vypnutelná diarizace** tam, kde jsou mluvčí z videa nebo z mikrofonní stopy; na slabém hardwaru největší efekt.
- **Vzdálený worker** přes sdílenou složku, stejné CLI, jen jiný stroj.
- **Cloud API provider** pro stroje bez GPU a bez ochoty čekat; volba per nahrávka podle citlivosti.
- **Apple provider** přes mlx-whisper, pokud se objeví Mac.
- **Benchmark příkaz** (`bench`), který na daném stroji změří fáze na krátkém vzorku a navrhne konfiguraci.
- **Kontejner** s CUDA stackem pro pronajaté GPU i vlastní worker, aby instalace (torch cu128 vs. whisperx pin)
  nebyla ruční.
