"""Meeting minutes (summary + action items) from the transcript via an LLM: local Ollama or the Claude API."""

from __future__ import annotations

import logging
from datetime import datetime

from .config import SummarizeSettings
from .export import to_txt
from .llm import OUTPUT_RESERVE, LLMResult, complete
from .providers.base import Segment
from .recording import Recording

log = logging.getLogger(__name__)

LANG_NAMES = {"cs": "in Czech (česky)", "sk": "in Slovak (slovensky)", "en": "in English", "de": "in German"}
HEADINGS = {
    "cs": ["Shrnutí", "Témata", "Rozhodnutí", "Úkoly", "Otevřené otázky", "Pojmy", "Mluvčí"],
    "sk": ["Zhrnutie", "Témy", "Rozhodnutia", "Úlohy", "Otvorené otázky", "Pojmy", "Rečníci"],
    "en": ["Summary", "Topics", "Decisions", "Action items", "Open questions", "Terms", "Speakers"],
    "de": ["Zusammenfassung", "Themen", "Entscheidungen", "Aufgaben", "Offene Fragen", "Begriffe", "Sprecher"],
}
TASK_COLS = {"cs": "Kdo | Úkol | Termín | Zdroj", "sk": "Kto | Úloha | Termín | Zdroj", "de": "Wer | Aufgabe | Termin | Quelle"}
SPEAKER_COLS = {"cs": "Označení | Jméno | Poznámka", "sk": "Označenie | Meno | Poznámka", "de": "Kennung | Name | Anmerkung"}

# The headings are listed on their own, the instructions separately: smaller models otherwise copy the
# instruction text into the heading ("## Shrnutí — 5 to 10 sentences: ...").
SYSTEM_PROMPT = """You write meeting minutes from raw transcripts of business meetings.

The transcript comes from automatic speech recognition with speaker attribution. Expect recognition errors,
mixed Czech/Slovak/English, filler words and cross-talk; reconstruct the intended meaning, never invent facts.

Speakers: when the transcript gives a real name, always use that name. Labels like SPEAKER_01 are people whose
name is not known; in the minutes refer to them by that exact label, consistently, and never replace a label with
a guessed name. The header may list which label belongs to which name.

Write the minutes {language}, regardless of the language(s) spoken in the transcript; quote names and terms as
they are. Use Markdown with exactly these seven level-2 headings, verbatim and nothing else on the heading line,
in this order:

## {h0}
## {h1}
## {h2}
## {h3}
## {h4}
## {h5}
## {h6}

Content of the sections:
1. {h0}: 5 to 10 sentences, purpose of the meeting and what was concluded.
2. {h1}: one bullet per topic discussed, with the key points and who argued what (2–4 sub-bullets each).
3. {h2}: numbered list of decisions actually made. If none, say so.
4. {h3}: a table with the columns {cols} (owner, task, due date if mentioned, source timestamp hh:mm:ss).
   Only tasks someone agreed to do; attribute to the person who took them, not who proposed them. "?" for unknown owners.
5. {h4}: issues raised but not resolved.
6. {h5}: short glossary of project-specific names, acronyms and systems as they were used (max 10).
7. {h6}: a table with the columns {speaker_cols}, one row per speaker: label as used in the transcript (or "–" when
   the transcript already gives the name), the name (or "?" when unknown), and for unknown speakers a short note
   on their role in the meeting so they can be identified later (e.g. "led the meeting, presented the build").
   The note is the one place for a likely identity, always with the evidence: e.g. "probably Jan: addressed by
   that name at 00:20:06 and answered". No evidence in the transcript, no guess.

Do not add a document title, date line or participant line above the first heading, no preamble, no closing
remarks. Keep names, product names and acronyms exactly as spelled in the transcript. Be concrete: prefer the
actual numbers, names and dates from the discussion over generic phrasing."""


CHARS_PER_TOKEN = 2.2  # Czech/Slovak on Gemma, generous

PART_NOTE = """

This is part {i} of {n} of the same meeting ({span}). Write the sections for this part only; a section with
nothing in this part says so in one line. Keep the timestamps."""

MERGE_PROMPT = """You merge partial meeting minutes into the final minutes. The parts were written from consecutive
stretches of one transcript, in order, all with the same seven level-2 headings. Produce one document {language}
with exactly these headings, verbatim and nothing else on the heading line, in this order:

## {h0}
## {h1}
## {h2}
## {h3}
## {h4}
## {h5}
## {h6}

Rules: merge duplicates (the same topic, decision, task or term mentioned in several parts becomes one item),
keep the order of the meeting, keep every timestamp and name exactly as written, keep the {h3} table format
({cols}) and the {h6} table format ({speaker_cols}). Drop lines that only say a part had nothing for a section.
Do not add a document title, preamble or closing remarks. Do not invent anything that is not in the parts."""


def build_user_message(rec: Recording, segments: list[Segment], header: dict) -> str:
    meta = "\n".join(f"{k}: {v}" for k, v in header.items() if v)
    body = to_txt(segments)
    return f"Meeting: {rec.title}\n{meta}\n\nTranscript:\n\n{body}"


def _hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def split_segments(segments: list[Segment], max_chars: int) -> list[list[Segment]]:
    """Consecutive chunks whose transcript text stays under max_chars (at least one segment each)."""
    chunks: list[list[Segment]] = []
    cur: list[Segment] = []
    size = 0
    for seg in segments:
        line = len(seg.text) + len(seg.speaker or "") + 14
        if cur and size + line > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(seg)
        size += line
    if cur:
        chunks.append(cur)
    return chunks


def chunk_budget_chars(system: str, settings: SummarizeSettings) -> int | None:
    """How much transcript fits one local-model call; None = no limit (cloud model)."""
    if settings.provider != "ollama":
        return None
    tokens = settings.ollama_max_ctx - OUTPUT_RESERVE - int(len(system) / CHARS_PER_TOKEN) - 512
    return max(int(tokens * CHARS_PER_TOKEN), 4000)


def _map_reduce(rec: Recording, chunks: list[list[Segment]], header: dict, settings: SummarizeSettings,
                system: str, h: list[str], language: str, cols: str, speaker_cols: str) -> LLMResult:
    parts: list[str] = []
    tokens_in = tokens_out = 0
    for i, chunk in enumerate(chunks, 1):
        span = f"{_hms(chunk[0].start)}–{_hms(chunk[-1].end)}"
        log.info("%s: part %d/%d (%s, %d segments)", rec.stem, i, len(chunks), span, len(chunk))
        r = complete(system + PART_NOTE.format(i=i, n=len(chunks), span=span),
                     build_user_message(rec, chunk, header), settings)
        parts.append(f"=== Part {i}/{len(chunks)} ({span}) ===\n{clean_headings(r.text, h)}")
        tokens_in += r.input_tokens or 0
        tokens_out += r.output_tokens or 0
    merge_system = MERGE_PROMPT.format(language=language, cols=cols, speaker_cols=speaker_cols,
                                       **{f"h{i}": h[i] for i in range(len(h))})
    meta = "\n".join(f"{k}: {v}" for k, v in header.items() if v)
    log.info("%s: merging %d parts", rec.stem, len(parts))
    r = complete(merge_system, f"Meeting: {rec.title}\n{meta}\n\n" + "\n\n".join(parts), settings)
    return LLMResult(text=r.text, model=r.model, input_tokens=tokens_in + (r.input_tokens or 0),
                     output_tokens=tokens_out + (r.output_tokens or 0), truncated=r.truncated)


def summarize(rec: Recording, segments: list[Segment], header: dict, settings: SummarizeSettings) -> str:
    language = LANG_NAMES.get(settings.language, settings.language)
    h = HEADINGS.get(settings.language, HEADINGS["en"])
    cols = TASK_COLS.get(settings.language, "Owner | Task | Due | Source")
    speaker_cols = SPEAKER_COLS.get(settings.language, "Label | Name | Note")
    system = SYSTEM_PROMPT.format(language=language, cols=cols, speaker_cols=speaker_cols,
                                  **{f"h{i}": h[i] for i in range(len(h))})
    user = build_user_message(rec, segments, header)
    log.info("%s: summarizing with %s/%s (%d chars of transcript)", rec.stem, settings.provider, settings.model, len(user))

    budget = chunk_budget_chars(system, settings)
    if budget is not None and len(user) > budget:
        chunks = split_segments(segments, budget)
        log.info("%s: transcript too long for one local-model context (%d > %d chars), %d parts + merge",
                 rec.stem, len(user), budget, len(chunks))
        result = _map_reduce(rec, chunks, header, settings, system, h, language, cols, speaker_cols)
    else:
        result = complete(system, user, settings)
    if result.truncated:
        log.warning("%s: summary was cut off by the output limit", rec.stem)
    log.info("%s: summary done, tokens in=%s out=%s", rec.stem, result.input_tokens, result.output_tokens)

    front = (f"<!-- teamsrec-transcribe summary | {settings.provider}: {result.model} | "
             f"created: {datetime.now().replace(microsecond=0).isoformat()} | "
             f"tokens: {result.input_tokens} in / {result.output_tokens} out -->\n")
    return front + f"# {rec.title}\n\n" + clean_headings(result.text, h) + "\n"


def clean_headings(text: str, headings: list[str]) -> str:
    """Safety net for models that append the instruction to the heading: `## Shrnutí — 5 to 10 ...` -> `## Shrnutí`."""
    out = []
    for line in text.strip().splitlines():
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            for hd in headings:
                if body == hd or body.lower().startswith(hd.lower()) and body[len(hd):len(hd) + 1] in (" ", ":", "—", "-", "–", "("):
                    line = f"## {hd}"
                    break
        out.append(line)
    return "\n".join(out)
