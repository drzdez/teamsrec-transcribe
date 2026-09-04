"""Meeting summary + action items from the transcript via the Claude API (Anthropic SDK)."""

from __future__ import annotations

import logging
from datetime import datetime

from .config import SummarizeSettings
from .export import to_txt
from .providers.base import Segment
from .recording import Recording

log = logging.getLogger(__name__)

LANG_NAMES = {"cs": "česky", "sk": "slovensky", "en": "in English", "de": "auf Deutsch"}

SYSTEM_PROMPT = """You write meeting minutes from raw transcripts of business meetings.

The transcript comes from automatic speech recognition with speaker attribution. Expect recognition errors,
mixed Czech/Slovak/English, filler words and cross-talk; reconstruct the intended meaning, never invent facts.
Speaker names in the transcript are reliable when they are real names; labels like SPEAKER_01 are unknown people.

Write the minutes {language}. Use Markdown with exactly these sections (translate the headings to that language):

1. Summary — 5 to 10 sentences: purpose of the meeting and what was concluded.
2. Topics — one bullet per topic discussed, with the key points and who argued what (2–4 sub-bullets each).
3. Decisions — numbered list of decisions actually made. If none, say so.
4. Action items — a table: Owner | Task | Due (if mentioned) | Source (timestamp hh:mm:ss). Only tasks someone
   agreed to do; attribute to the person who took them, not who proposed them. Use "?" for unknown owners.
5. Open questions — issues raised but not resolved.
6. Terms — short glossary of project-specific names, acronyms and systems as they were used (max 10).

Keep names, product names and acronyms exactly as spelled in the transcript. Be concrete: prefer the actual
numbers, names and dates from the discussion over generic phrasing. Do not add a preamble or closing remarks."""


def build_user_message(rec: Recording, segments: list[Segment], header: dict) -> str:
    meta = "\n".join(f"{k}: {v}" for k, v in header.items() if v)
    body = to_txt(segments)
    return f"Meeting: {rec.title}\n{meta}\n\nTranscript:\n\n{body}"


def summarize(rec: Recording, segments: list[Segment], header: dict, settings: SummarizeSettings) -> str:
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("the anthropic package is not installed") from e

    try:
        client = anthropic.Anthropic()  # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login` profile
    except TypeError as e:  # SDK: "Could not resolve authentication method"
        raise RuntimeError("no Claude credentials: set ANTHROPIC_API_KEY (or run `ant auth login`)") from e
    language = LANG_NAMES.get(settings.language, settings.language)
    system = SYSTEM_PROMPT.format(language=language)
    user = build_user_message(rec, segments, header)
    log.info("%s: summarizing with %s (%d chars of transcript)", rec.stem, settings.model, len(user))

    try:
        with client.messages.stream(
            model=settings.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        ) as stream:
            response = stream.get_final_message()
    except (anthropic.AuthenticationError, TypeError) as e:  # TypeError: SDK found no credentials at all
        raise RuntimeError("no valid Claude credentials: set ANTHROPIC_API_KEY (or run `ant auth login`)") from e
    except anthropic.NotFoundError as e:
        raise RuntimeError(f"model {settings.model!r} not found for this account") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError("Claude API rate limit hit, try again later") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError(f"cannot reach the Claude API: {e}") from e

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise RuntimeError(f"model refused to summarize ({getattr(details, 'category', None)})")
    if response.stop_reason == "max_tokens":
        log.warning("%s: summary hit max_tokens, output may be truncated", rec.stem)
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    usage = response.usage
    log.info("%s: summary done, tokens in=%s out=%s", rec.stem, usage.input_tokens, usage.output_tokens)

    front = (f"<!-- teamsrec-transcribe summary | model: {response.model} | "
             f"created: {datetime.now().replace(microsecond=0).isoformat()} | "
             f"tokens: {usage.input_tokens} in / {usage.output_tokens} out -->\n")
    return front + f"# {rec.title}\n\n" + text + "\n"
