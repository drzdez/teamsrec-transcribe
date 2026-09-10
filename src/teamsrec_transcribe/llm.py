"""LLM backends for the summary: local Ollama (default) or the Claude API (Anthropic SDK)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import SummarizeSettings

log = logging.getLogger(__name__)

# Our own variable name on purpose: a plain ANTHROPIC_API_KEY is picked up by every Anthropic tool on the
# machine (Claude Code asks whether to bill against it), this one is seen by teamsrec only.
API_KEY_ENV = "TEAMSREC_ANTHROPIC_API_KEY"


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    truncated: bool = False


class LLMError(RuntimeError):
    pass


def complete(system: str, user: str, settings: SummarizeSettings) -> LLMResult:
    if settings.provider == "ollama":
        return _ollama(system, user, settings)
    if settings.provider == "anthropic":
        return _anthropic(system, user, settings)
    raise LLMError(f"unknown summarize provider {settings.provider!r} (ollama | anthropic)")


# ---------------------------------------------------------------- Ollama (local)

OUTPUT_RESERVE = 8192  # tokens kept free for the answer


def _ollama(system: str, user: str, settings: SummarizeSettings, num_ctx: int | None = None) -> LLMResult:
    # Ollama's default context window is small; size it to the transcript. Czech/Slovak tokenizes at roughly
    # 2.5 chars/token on Gemma, so estimate generously and keep room for the answer.
    approx_prompt = int((len(system) + len(user)) / 2.2)
    if num_ctx is None:
        num_ctx = min(max(16384, approx_prompt + OUTPUT_RESERVE), settings.ollama_max_ctx)
    body = {
        "model": settings.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "think": settings.ollama_think,
        "options": {"num_ctx": num_ctx, "temperature": 0.2, "num_predict": OUTPUT_RESERVE},
    }
    req = urllib.request.Request(f"{settings.ollama_url.rstrip('/')}/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    log.info("ollama: %s, num_ctx=%d (~%d prompt tokens estimated)", settings.model, num_ctx, approx_prompt)
    try:
        with urllib.request.urlopen(req, timeout=settings.ollama_timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:300]
        if e.code == 404:
            raise LLMError(f"ollama: model {settings.model!r} not found, run `ollama pull {settings.model}`") from e
        raise LLMError(f"ollama: HTTP {e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"ollama: cannot reach {settings.ollama_url} ({e.reason}); is Ollama running?") from e
    text = (data.get("message") or {}).get("content", "").strip()
    prompt_tokens, out_tokens = data.get("prompt_eval_count") or 0, data.get("eval_count") or 0
    truncated = data.get("done_reason") == "length"
    if truncated and prompt_tokens + out_tokens >= num_ctx - 256 and num_ctx < settings.ollama_max_ctx:
        # the context window, not the answer limit, was the bottleneck: retry once with room to spare
        bigger = min(prompt_tokens + OUTPUT_RESERVE + 2048, settings.ollama_max_ctx)
        log.info("ollama: context %d too small for %d prompt tokens, retrying with %d", num_ctx, prompt_tokens, bigger)
        return _ollama(system, user, settings, num_ctx=bigger)
    if not text:
        raise LLMError("ollama: empty response")
    return LLMResult(text=text, model=data.get("model", settings.model),
                     input_tokens=prompt_tokens, output_tokens=out_tokens, truncated=truncated)


# ---------------------------------------------------------------- Anthropic (cloud)

def _anthropic(system: str, user: str, settings: SummarizeSettings) -> LLMResult:
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise LLMError("the anthropic package is not installed") from e
    try:
        key = os.environ.get(API_KEY_ENV) or None
        client = anthropic.Anthropic(api_key=key)  # None -> SDK defaults (ANTHROPIC_API_KEY, `ant auth login`)
        with client.messages.stream(
            model=settings.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        ) as stream:
            response = stream.get_final_message()
    except (anthropic.AuthenticationError, TypeError) as e:  # TypeError: SDK found no credentials at all
        raise LLMError(f"no valid Claude credentials: set {API_KEY_ENV} (or run `ant auth login`)") from e
    except anthropic.NotFoundError as e:
        raise LLMError(f"model {settings.model!r} not found for this account") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Claude API rate limit hit, try again later") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"cannot reach the Claude API: {e}") from e
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise LLMError(f"model refused to summarize ({getattr(details, 'category', None)})")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return LLMResult(text=text, model=response.model, input_tokens=response.usage.input_tokens,
                     output_tokens=response.usage.output_tokens, truncated=response.stop_reason == "max_tokens")
