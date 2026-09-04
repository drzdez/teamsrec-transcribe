"""Build the ASR initial prompt from what we know about the meeting (title, participants, glossary).

Whisper's prompt is a soft hint: it steers spelling of names and domain terms and the general style.
Keep it short (the model sees ~224 tokens of it).
"""

from __future__ import annotations

from .recording import Recording


def build_prompt(rec: Recording, glossary: tuple[str, ...] | list[str]) -> str | None:
    parts: list[str] = []
    title = rec.title.strip()
    if title and title != rec.stem:
        parts.append(f"Schůzka: {title}.")
    names = rec.participants
    if names:
        parts.append("Účastníci: " + ", ".join(names) + ".")
    if glossary:
        parts.append("Pojmy: " + ", ".join(dict.fromkeys(g.strip() for g in glossary if g.strip())) + ".")
    return " ".join(parts) if parts else None
