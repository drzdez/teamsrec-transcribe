import pytest


@pytest.fixture(autouse=True)
def _no_real_ollama_unload(monkeypatch):
    """A recording pause frees the GPU by unloading the Ollama model; tests must never touch a real Ollama."""
    from teamsrec_transcribe.web import review
    calls = []
    monkeypatch.setattr(review, "_unload_ollama", lambda cfg: calls.append(cfg.summarize.model))
    return calls


@pytest.fixture(autouse=True)
def _no_real_ollama_lookup(monkeypatch):
    """The page asks the local Ollama whether the minutes model is there; tests must not depend on this PC."""
    from teamsrec_transcribe import settings
    monkeypatch.setattr(settings, "ollama_has", lambda url, model, **kw: True)
