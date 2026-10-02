import pytest


@pytest.fixture(autouse=True)
def _no_real_ollama_unload(monkeypatch):
    """A recording pause frees the GPU by unloading the Ollama model; tests must never touch a real Ollama."""
    from teamsrec_transcribe.web import review
    calls = []
    monkeypatch.setattr(review, "_unload_ollama", lambda cfg: calls.append(cfg.summarize.model))
    return calls
