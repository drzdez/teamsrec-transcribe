"""Meeting minutes (summary + action items) from the transcript via an LLM: local Ollama or the Claude API."""

from __future__ import annotations

import logging
from datetime import datetime

from .config import SummarizeSettings
from .export import to_txt
from .llm import complete
from .providers.base import Segment
from .recording import Recording

log = logging.getLogger(__name__)

LANG_NAMES = {"cs": "in Czech (česky)", "sk": "in Slovak (slovensky)", "en": "in English", "de": "in German"}
HEADINGS = {
    "cs": ["Shrnutí", "Témata", "Rozhodnutí", "Úkoly", "Otevřené otázky", "Pojmy"],
    "sk": ["Zhrnutie", "Témy", "Rozhodnutia", "Úlohy", "Otvorené otázky", "Pojmy"],
    "en": ["Summary", "Topics", "Decisions", "Action items", "Open questions", "Terms"],
    "de": ["Zusammenfassung", "Themen", "Entscheidungen", "Aufgaben", "Offene Fragen", "Begriffe"],
}

SYSTEM_PROMPT = """You write meeting minutes from raw transcripts of business meetings.

The transcript comes from automatic speech recognition with speaker attribution. Expect recognition errors,
mixed Czech/Slovak/English, filler words and cross-talk; reconstruct the intended meaning, never invent facts.
Speaker names in the transcript are reliable when they are real names; labels like SPEAKER_01 are unknown people.

Write the minutes {language}, regardless of the language(s) spoken in the transcript; quote names and terms as they are. Use Markdown with exactly these six level-2 headings, verbatim, in this order. Do not add a document title, date line or participant line above them; start directly with the first heading:

## {h0} — 5 to 10 sentences: purpose of the meeting and what was concluded.
## {h1} — one bullet per topic discussed, with the key points and who argued what (2–4 sub-bullets each).
## {h2} — numbered list of decisions actually made. If none, say so.
## {h3} — a table with columns {cols}: Owner | Task | Due (if mentioned) | Source (timestamp hh:mm:ss). Only tasks
   someone agreed to do; attribute to the person who took them, not who proposed them. Use "?" for unknown owners.
## {h4} — issues raised but not resolved.
## {h5} — short glossary of project-specific names, acronyms and systems as they were used (max 10).

Keep names, product names and acronyms exactly as spelled in the transcript. Be concrete: prefer the actual
numbers, names and dates from the discussion over generic phrasing. Do not add a preamble or closing remarks."""


def build_user_message(rec: Recording, segments: list[Segment], header: dict) -> str:
    meta = "\n".join(f"{k}: {v}" for k, v in header.items() if v)
    body = to_txt(segments)
    return f"Meeting: {rec.title}\n{meta}\n\nTranscript:\n\n{body}"


def summarize(rec: Recording, segments: list[Segment], header: dict, settings: SummarizeSettings) -> str:
    language = LANG_NAMES.get(settings.language, settings.language)
    h = HEADINGS.get(settings.language, HEADINGS["en"])
    cols = {"cs": "Kdo | Úkol | Termín | Zdroj", "sk": "Kto | Úloha | Termín | Zdroj"}.get(
        settings.language, "Owner | Task | Due | Source")
    system = SYSTEM_PROMPT.format(language=language, cols=cols, **{f"h{i}": h[i] for i in range(6)})
    user = build_user_message(rec, segments, header)
    log.info("%s: summarizing with %s/%s (%d chars of transcript)", rec.stem, settings.provider, settings.model, len(user))

    result = complete(system, user, settings)
    if result.truncated:
        log.warning("%s: summary was cut off by the output limit", rec.stem)
    log.info("%s: summary done, tokens in=%s out=%s", rec.stem, result.input_tokens, result.output_tokens)

    front = (f"<!-- teamsrec-transcribe summary | {settings.provider}: {result.model} | "
             f"created: {datetime.now().replace(microsecond=0).isoformat()} | "
             f"tokens: {result.input_tokens} in / {result.output_tokens} out -->\n")
    return front + f"# {rec.title}\n\n" + result.text + "\n"
