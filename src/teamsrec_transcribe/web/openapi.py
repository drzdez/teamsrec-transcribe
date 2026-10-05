"""OpenAPI 3.1 description of the review server's REST API (served at /api/openapi.json).

Hand-written on purpose: the server is a small stdlib HTTP server, and this file is the contract a different
client (or a later .NET server) can be built against. Keep it in step with web/review.py's ROUTES.
"""

from __future__ import annotations

STEM = {"name": "stem", "in": "path", "required": True, "schema": {"type": "string"},
        "description": "recording stem, e.g. 2026-09-24_1600_archi-board"}
OK = {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}},
      "400": {"$ref": "#/components/responses/Error"}}


def _op(summary: str, *, params=(), body: dict | None = None, responses=None, tags=("recordings",)) -> dict:
    op = {"summary": summary, "tags": list(tags), "responses": responses or OK}
    if params:
        op["parameters"] = list(params)
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return op


def _obj(**props) -> dict:
    return {"type": "object", "properties": props}


def spec(version: str) -> dict:
    s, b, o, a = {"type": "string"}, {"type": "boolean"}, {"type": "object"}, {"type": "array"}
    person_fields = _obj(first=s, last=s, nick=s, display=s)
    return {
        "openapi": "3.1.0",
        "info": {"title": "teamsrec review", "version": version,
                 "description": "Local review page API (127.0.0.1 only). Recordings live in the recordings folder; "
                                "this API reads and edits their files. Errors: {\"error\": \"...\"} with status 400/404/409."},
        "paths": {
            "/api/recordings": {"get": _op("Recordings, newest first, with processing state", params=[
                {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 30}}])},
            "/api/recordings/{stem}": {"get": _op("Everything the page shows for one recording", params=[STEM])},
            "/api/recordings/{stem}/clip": {"get": _op("A WAV clip of the 16 kHz mix (max 8 s)", params=[
                STEM, {"name": "start", "in": "query", "schema": {"type": "number"}},
                {"name": "end", "in": "query", "schema": {"type": "number"}}],
                responses={"200": {"description": "audio/wav", "content": {"audio/wav": {}}}})},
            "/api/recordings/{stem}/docs/{file}": {"get": _op("Text of a transcript (.txt) or summary (.md)", params=[
                STEM, {"name": "file", "in": "path", "required": True, "schema": s}])},
            "/api/recordings/{stem}/names": {"put": _op(
                "Save the speaker names (and the title); regenerates the exports, optionally the summary",
                params=[STEM], body=_obj(names={"type": "object", "additionalProperties": {"oneOf": [s, person_fields]}},
                                         title=s, summary=b))},
            "/api/recordings/{stem}/process": {"post": _op(
                "Transcribe + export + summarize in the background; force = from scratch (drops manual names)",
                params=[STEM], body=_obj(force=b))},
            "/api/recordings/{stem}/recognize": {"post": _op("Match unnamed speakers against the voice prints",
                                                             params=[STEM])},
            "/api/recordings/{stem}/segments/assign": {"post": _op(
                "Move replies to a speaker one by one: unassigned ones, or with `from` replies of another speaker; "
                "speaker '@new' = a new one, 'UNKNOWN' = unassigned; exports regenerated",
                params=[STEM], body=_obj(segments={"type": "array", "items": _obj(start={"type": "number"}, speaker=s,
                                                                                  **{"from": s})}))},
            "/api/recordings/{stem}/summaries": {"post": _op(
                "Write a summary with this provider/model in the background (the configured one = the main "
                "summary, any other = <stem>.summary.<model>.md); the end is an event with job_end",
                params=[STEM], body=_obj(provider={"enum": ["ollama", "anthropic"]}, model=s))},
            "/api/recordings/{stem}/summaries/{file}": {"delete": _op(
                "Delete one summary of the recording (<stem>.summary*.md only)",
                params=[STEM, {"name": "file", "in": "path", "required": True, "schema": s}])},
            "/api/summary-models": {"get": _op(
                "live=1 asks Ollama and the Claude API now; otherwise the last known lists. " +
                "Models for summaries: {ollama: [...], anthropic: [...], default: {provider, model}}; each entry "
                "{value, note, local}", tags=("settings",))},
            "/api/recordings/{stem}/speakers/merge": {"post": _op("Fold the labels of one person into one speaker",
                                                                  params=[STEM])},
            "/api/recordings/{stem}/speakers/unmerge": {"post": _op("Undo the merges that remember their origin",
                                                                    params=[STEM])},
            "/api/recordings/{stem}/speakers/{label}": {"delete": _op(
                "Drop all segments of one speaker (noise turned into text)",
                params=[STEM, {"name": "label", "in": "path", "required": True, "schema": s}])},
            "/api/recordings/{stem}/meeting": {"post": _op("Calendar link: confirm | detach | attach", params=[STEM],
                                                           body=_obj(action={"enum": ["confirm", "detach", "attach"]},
                                                                     candidate=o))},
            "/api/recordings/{stem}/meeting/candidates": {"get": _op("Nearby Outlook items to link instead",
                                                                     params=[STEM])},
            "/api/people": {
                "get": _op("The people registry with voice-print counts", tags=("people",)),
                "put": _op("Replace the registry (opted-out people lose their prints)", tags=("people",),
                           body=_obj(people=a, stem=s))},
            "/api/people/merge": {"post": _op("Fold one person into another everywhere", tags=("people",),
                                              body=_obj(keep=s, drop=s, stem=s))},
            "/api/people/{id}": {"get": _op("One person with their voice prints", tags=("people",), params=[
                {"name": "id", "in": "path", "required": True, "schema": s}])},
            "/api/people/{id}/voiceprints": {"delete": _op(
                "Delete one print (stem + label) or all prints of the person", tags=("people",), params=[
                    {"name": "id", "in": "path", "required": True, "schema": s},
                    {"name": "stem", "in": "query", "schema": s}, {"name": "label", "in": "query", "schema": s}])},
            "/api/settings": {
                "get": _op("Settings of both apps: sections with fields (type, choices, value, default), the state "
                           "of the API keys (env | vault | missing, never the value), the audio inputs of this PC",
                           tags=("settings",)),
                "put": _op("Validate and write changed values into teamsrec.toml (comments kept); returns the changed "
                           "keys, whether a restart is needed, and the settings again", tags=("settings",),
                           body=_obj(values={"type": "object", "description": "{\"section.key\": value}"}))},
            "/api/secrets/{name}": {
                "put": _op("Store an API key in the Windows Credential Manager", tags=("settings",),
                           params=[{"name": "name", "in": "path", "required": True,
                                    "schema": {"enum": ["anthropic", "openai", "elevenlabs"]}}], body=_obj(value=s)),
                "delete": _op("Remove a stored API key (a key in the environment stays)", tags=("settings",),
                              params=[{"name": "name", "in": "path", "required": True,
                                       "schema": {"enum": ["anthropic", "openai", "elevenlabs"]}}])},
            "/api/settings/models": {"get": _op(
                "The model lists asked live - Ollama on this PC, the Claude models of the key (takes seconds; "
                "GET /api/settings sends the last known ones)", tags=("settings",))},
            "/api/jobs/pause": {"post": _op("Stop the running job because a recording runs; it goes back to the "
                                            "front of the queue and the queue waits for the recording's end",
                                            tags=("server",))},
            "/api/jobs/continue": {"post": _op("Let the jobs run during the recording (answers the question)",
                                               tags=("server",))},
            "/api/recordings/{stem}/title": {"put": _op(
                "Rename the meeting only (folder and files follow); returns the new stem", params=[STEM],
                body=_obj(title=s))},
            "/api/jobs/{job}/next": {"post": _op("Move a waiting job to the front: it runs right after the current one",
                                                 tags=("server",), params=[{"name": "job", "in": "path", "required": True,
                                                                             "schema": {"type": "integer"}}])},
            "/api/recordings/{stem}/speakers/{label}/replies": {"get": _op(
                "All replies of one speaker (to go through them and move some to another speaker)",
                params=[STEM, {"name": "label", "in": "path", "required": True, "schema": {"type": "string"}}])},
            "/api/recordings/{stem}/voices": {"post": _op(
                "Fast-track post-processing: local diarization lends voice embeddings to the groups of a cloud "
                "transcript, then voice recognition runs (background job)", params=[STEM])},
            "/api/recordings/{stem}/summaries/{file}/export": {
                "get": _op("The folder remembered for this meeting name and the name of the copy", params=[STEM, {
                    "name": "file", "in": "path", "required": True, "schema": {"type": "string"}}]),
                "post": _op("Save a copy of the minutes into a folder and remember it for the meeting name",
                            params=[STEM, {"name": "file", "in": "path", "required": True, "schema": {"type": "string"}}],
                            body=_obj(folder=s))},
            "/api/system/pick-folder": {"post": _op("Open the Windows folder dialog on this PC; '' when cancelled",
                                                    tags=("settings",), body=_obj(initial=s))},
            "/api/jobs/{job}/cancel": {"post": _op("Drop a waiting job, or stop the running one for good",
                                                   tags=("server",), params=[{"name": "job", "in": "path", "required": True,
                                                                               "schema": {"type": "integer"}}])},
            "/api/jobs/{job}/now": {"post": _op("Run a waiting job at once: the current job stops and runs again from "
                                                "the beginning right after it", tags=("server",),
                                                params=[{"name": "job", "in": "path", "required": True,
                                                         "schema": {"type": "integer"}}])},
            "/api/system/sound-settings": {"post": _op("Open the Windows sound dialog (mmsys.cpl)", tags=("settings",))},
            "/api/help/{doc}": {"get": _op("A guide: user-guide | install | privacy", tags=("server",), params=[
                {"name": "doc", "in": "path", "required": True, "schema": {"enum": ["user-guide", "install", "privacy"]}}])},
            "/api/status": {"get": _op("Background job state and the recent events", tags=("server",))},
            "/api/events": {"get": _op(
                "Server-Sent Events: `event: log` with {n, at, text, level, stem, reload, busy, job_end}, and "
                "`event: capture` {running, recording, title, stem, source, started} when teamsrec-capture starts or "
                "stops recording, `event: jobs` {current, queue} when a background job starts, ends or is queued "
                "(both also in `hello` and /api/status); job events carry `job` (the id POST .../process and "
                ".../summaries return with `position`); the last events are "
                "replayed on connect, and after a reconnect from Last-Event-ID", tags=("server",),
                responses={"200": {"description": "text/event-stream", "content": {"text/event-stream": {}}}})},
            "/api/openapi.json": {"get": _op("This document", tags=("server",))},
            "/api/ping": {"post": _op("Heartbeat; the server stops ~90 s after the last one", tags=("server",))},
            "/api/quit": {"post": _op("Stop the server (refused while a job runs)", tags=("server",))},
        },
        "components": {"responses": {"Error": {"description": "Error",
                                               "content": {"application/json": {"schema": _obj(error=s)}}}}},
    }
