"""LLM backends for the summary: local Ollama (default), the Claude API (Anthropic SDK), or OpenAI.

The OpenAI one speaks the Chat Completions API that most providers copy (Gemini, Mistral, OpenRouter, LM Studio,
vLLM…), so another service is only another `openai_url`; only OpenAI itself is verified so far."""

from __future__ import annotations

import json
import logging
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
    if settings.provider == "openai":
        return _openai(system, user, settings)
    raise LLMError(f"unknown summarize provider {settings.provider!r} (ollama | anthropic | openai)")


# ---------------------------------------------------------------- OpenAI (cloud; the Chat Completions API)

OPENAI_URL = "https://api.openai.com/v1"


def openai_key(url: str) -> str:
    """OpenAI's own key for api.openai.com; another compatible service (not verified yet, so not offered on the
    page) takes TEAMSREC_LLM_API_KEY."""
    import os
    from .settings import get_secret
    return get_secret("openai") if "api.openai.com" in url else os.environ.get("TEAMSREC_LLM_API_KEY", "").strip()


def _openai(system: str, user: str, settings: SummarizeSettings) -> LLMResult:
    import requests
    url = (settings.openai_url or OPENAI_URL).rstrip("/")
    key = openai_key(url)
    if not key and "api.openai.com" in url:
        raise LLMError("no OpenAI key: enter it in Settings on the review page (Klíče API), or set OPENAI_API_KEY")
    body = {"model": settings.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": 16000}
    try:
        r = requests.post(f"{url}/chat/completions", json=body, timeout=settings.ollama_timeout_s,
                          headers={"Authorization": f"Bearer {key}"} if key else {})
    except requests.RequestException as e:
        raise LLMError(f"cannot reach {url}: {e}") from e
    if r.status_code in (401, 403):
        raise LLMError(f"the key was refused by {url} (HTTP {r.status_code}) – check it in Settings")
    if r.status_code == 404:
        raise LLMError(f"model {settings.model!r} not found at {url}")
    if r.status_code == 429:
        raise LLMError(f"rate limit or no credit at {url}, try again later")
    if not r.ok:
        raise LLMError(f"{url}: HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    if not text:
        raise LLMError(f"{url}: empty response ({choice.get('finish_reason')})")
    usage = data.get("usage") or {}
    return LLMResult(text=text, model=data.get("model", settings.model), input_tokens=usage.get("prompt_tokens"),
                     output_tokens=usage.get("completion_tokens"), truncated=choice.get("finish_reason") == "length")


def openai_models(url: str, key: str) -> list[str]:
    """Chat models the key can use at that address (listing costs nothing and sends no text)."""
    import requests
    r = requests.get(f"{url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=5)
    r.raise_for_status()
    ids = [m.get("id", "") for m in r.json().get("data", [])]
    if "api.openai.com" in url:  # the list holds every OpenAI model: keep the ones that write text
        skip = ("audio", "realtime", "transcribe", "tts", "search", "image", "embedding", "moderation", "instruct")
        ids = [i for i in ids if i.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")) and not any(s in i for s in skip)]
    return sorted(ids, reverse=True)


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
        if e.code == 404:  # 2026-10-06: Ollama's model folder was moved, every local minutes failed in 2 s
            raise LLMError(f"Ollama nemá model {settings.model} – stáhněte ho (ollama pull {settings.model}) nebo "
                           f"zkontrolujte složku modelů v nastavení Ollamy") from e
        raise LLMError(f"ollama: HTTP {e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"Ollama neběží ({settings.ollama_url}: {e.reason}) – spusťte ji") from e
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
        from .settings import get_secret
        key = get_secret("anthropic") or None  # environment, then the key stored on the settings page
        client = anthropic.Anthropic(api_key=key)  # None -> SDK defaults (ANTHROPIC_API_KEY, `ant auth login`)
        with client.messages.stream(
            model=settings.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        ) as stream:
            response = stream.get_final_message()
    except (anthropic.AuthenticationError, TypeError) as e:  # TypeError: SDK found no credentials at all
        raise LLMError(f"no valid Claude credentials: enter the key in Settings on the review page, or set "
                       f"{API_KEY_ENV} (or run `ant auth login`)") from e
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


def ollama_pull(url: str, model: str, progress) -> None:
    """Download a model into the local Ollama (POST /api/pull, streamed). `progress(done_bytes, total_bytes, status)` is
    called as the layers come in; raises LLMError when Ollama refuses or is not running."""
    base = url.rstrip("/").replace("//localhost", "//127.0.0.1")
    req = urllib.request.Request(base + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    done: dict[str, int] = {}
    total: dict[str, int] = {}
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise LLMError(f"Ollama: {msg['error']}")
                digest = msg.get("digest")
                if digest and msg.get("total"):
                    total[digest] = int(msg["total"])
                    done[digest] = int(msg.get("completed") or 0)
                progress(sum(done.values()), sum(total.values()), str(msg.get("status") or ""))
    except urllib.error.HTTPError as e:
        raise LLMError(f"Ollama: HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"Ollama neběží ({url}: {e.reason}) – spusťte ji") from e
