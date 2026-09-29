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
            "/api/help/{doc}": {"get": _op("A guide: user-guide | install | privacy", tags=("server",), params=[
                {"name": "doc", "in": "path", "required": True, "schema": {"enum": ["user-guide", "install", "privacy"]}}])},
            "/api/status": {"get": _op("Background job state and the recent events", tags=("server",))},
            "/api/events": {"get": _op(
                "Server-Sent Events: `event: log` with {n, at, text, level, stem, reload, busy, job_end}; the last events are "
                "replayed on connect, and after a reconnect from Last-Event-ID", tags=("server",),
                responses={"200": {"description": "text/event-stream", "content": {"text/event-stream": {}}}})},
            "/api/openapi.json": {"get": _op("This document", tags=("server",))},
            "/api/ping": {"post": _op("Heartbeat; the server stops ~90 s after the last one", tags=("server",))},
            "/api/quit": {"post": _op("Stop the server (refused while a job runs)", tags=("server",))},
        },
        "components": {"responses": {"Error": {"description": "Error",
                                               "content": {"application/json": {"schema": _obj(error=s)}}}}},
    }
