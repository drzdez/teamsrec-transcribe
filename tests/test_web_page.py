"""The review page (web/index.html) in a headless DOM (jsdom) against the real review server: every
tests/web/*.test.mjs gets a fresh server on the recordings folder built here. Needs Node.js and `npm ci` in
tests/web; skipped without them."""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from teamsrec_transcribe.people import People
from teamsrec_transcribe.recording import Recording
from test_core import _make_recording, _make_transcribed, _serve

WEB = Path(__file__).parent / "web"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE or not (WEB / "node_modules" / "jsdom").is_dir(),
                                reason="page tests need Node.js and `npm ci` in tests/web")

SUMMARY = """<!-- teamsrec-transcribe summary | ollama: test -->
# Týdenní sync

## Shrnutí
Krátká schůzka o **rozpočtu** a `termínech`. Podklady: [plán](https://example.org/plan),
[klikni sem](javascript:alert(1)) a [data](data:text/html,x).

## Úkoly
| Kdo | Úkol | Termín |
|---|---|---|
| Petr | Poslat rozpočet | zítra |

## Mluvčí
| Označení | Jméno | Poznámka |
|---|---|---|
| SPEAKER_00 | ? | vedl schůzku |
| SPEAKER_01 | ? | pravděpodobně Petr: osloven |
"""


def _folder(out: Path) -> None:
    """Three recordings (two of the same meeting, one of them not transcribed yet) and two known people."""
    rec = _make_transcribed(out)
    rec.summary_path.write_text(SUMMARY, encoding="utf-8")
    rec.file(".txt").write_text("# Týdenní sync\n\n[00:00:00] SPEAKER_00: Dobrý den, začneme <b>programem</b>.\n"
                                "[00:00:12] Jana Nováková: Rozpočet je hotový.\n", encoding="utf-8")
    _make_recording(out, stem="2026-09-03_0900_archi-board", title="Archi board", start="2026-09-03T09:00:00")
    old = _make_transcribed(out, stem="2026-09-02_0900_archi-board")
    sc = old.dir / f"{old.stem}.json"
    sidecar = json.loads(sc.read_text(encoding="utf-8"))
    sidecar.update(title="Archi board", slug="archi-board", start="2026-09-02T09:00:00", end="2026-09-02T09:30:00")
    sc.write_text(json.dumps(sidecar), encoding="utf-8")
    people = People.load(out, "nick")
    people.ensure("Petr Svoboda").nick = "Péťa"
    people.ensure("Jana Nováková")
    people.save()


def _fake_process(cfg, rec: Recording, force: bool = False) -> None:
    """Stands in for transcription: a short job that leaves a transcript, so the page sees busy -> done. A new
    transcript from scratch takes longer, so a page test can act on the jobs queued behind it."""
    time.sleep(2.0 if force else 0.6)
    rec.write_json(rec.transcript_path, {
        "format": 1, "language": "cs", "speaker_sources": ["diarization"], "speakers": ["SPEAKER_00"],
        "segments": [{"start": 0, "end": 5, "text": "Nová nahrávka je přepsaná.", "speaker": "SPEAKER_00"}]})


@pytest.mark.parametrize("script", sorted(p.name for p in WEB.glob("*.test.mjs")))
def test_review_page(script, tmp_path, monkeypatch):
    from teamsrec_transcribe import pipeline, settings
    from teamsrec_transcribe.web import review
    from test_settings import TOML, FakeVault
    _folder(tmp_path)
    # the settings page edits a throw-away config and a fake key vault, never the real ones
    cfg_file = tmp_path / "teamsrec.toml"
    cfg_file.write_text(TOML, encoding="utf-8")
    if script != "wizard.test.mjs":  # the setup wizard would cover the page; its own test sees it
        (tmp_path / "setup-done").write_text("", encoding="utf-8")
    else:  # a fresh install: no name yet (an existing configuration does not get the wizard by itself)
        cfg_file.write_text(TOML.replace('name = "Jan Novák"  # your name', 'name = ""  # your name'), encoding="utf-8")
    from teamsrec_transcribe import setup
    monkeypatch.setattr(setup, "gpu", lambda: {"name": "NVIDIA GeForce RTX 4070 Laptop GPU", "vram_gb": 8})
    monkeypatch.setattr(setup, "ollama_running", lambda url: False)
    monkeypatch.setattr(settings, "hf_login_token", lambda: "")
    monkeypatch.setattr(setup, "hf_access", lambda repo, token: {"ok": token == "hf_good", "message": "ok" if token == "hf_good" else "Token neplatí"})
    monkeypatch.setenv("TEAMSREC_CONFIG", str(cfg_file))
    vault = FakeVault()
    monkeypatch.setattr(settings, "_keyring", lambda: vault)
    known = {"on": False}  # like the real ones: nothing known until asked live once

    def ollama_models(url, live=False):
        known["on"] = known["on"] or live
        return ["gemma4:31b"] if known["on"] else None
    monkeypatch.setattr(settings, "ollama_models", ollama_models)  # no network in page tests
    monkeypatch.setattr(settings, "claude_models", lambda live=False: ["claude-opus-5-5"] if known["on"] or live else None)
    for secret in settings.SECRETS.values():
        for var in secret.env:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(review, "do_process", _fake_process)
    monkeypatch.setattr(review, "do_summarize", lambda cfg, rec, force=False: time.sleep(1.0))
    monkeypatch.setattr(review, "do_summarize_compare", lambda cfg, rec, force=False: [])

    def fake_summarize_as(cfg, rec, provider, model):
        time.sleep(0.3)
        path = pipeline.summary_path_for(cfg, rec, provider, model)
        stamp = f"<!-- teamsrec-transcribe summary | {provider}: {model} | created: 2026-09-30T10:00:00 -->"
        path.write_text(stamp + "\n# Zápis od " + model + "\n\nText.\n", encoding="utf-8")
        return path
    monkeypatch.setattr(review, "summarize_as", fake_summarize_as)

    def fake_run_cli(argv, on_proc, on_line):  # the jobs' child process, played by the fakes above
        args = argv[argv.index("run-job") + 1:]
        cfg = review.Config(out_dir=tmp_path)
        if args[0] == "replies":  # the replies' voices: a unit vector per reply instead of the GPU model
            from teamsrec_transcribe import replies
            for r in replies.recordings_of(cfg, args[1]):
                vecs = {replies._key(seg["start"]): [1.0] + [0.0] * 255 for seg in replies.replies_of(r)}
                replies.cache_path(r).write_text(json.dumps({"format": 1, "model": cfg.transcribe.diarize_model,
                                                             "replies": vecs}), encoding="utf-8")
            return
        rec = review.resolve_recording(args[1], tmp_path)
        if args[0] == "process":
            review.do_process(cfg, rec, force="--force" in args)
        elif args[0] == "summary":
            review.do_summarize(cfg, rec, force=True)
        else:
            review.summarize_as(cfg, rec, args[args.index("--provider") + 1], args[args.index("--model") + 1])
    monkeypatch.setattr(review, "run_cli", fake_run_cli)
    srv, state, base = _serve(tmp_path)
    capture_file = tmp_path / "teamsrec-capture.json"  # the test's own capture status, never the real one
    stop = threading.Event()
    threading.Thread(target=review._capture_watch, args=(state, capture_file, 0.1, stop), daemon=True).start()
    try:
        run = subprocess.run([NODE, "--test", "--test-reporter=spec", str(WEB / script)], cwd=WEB,
                             env={**os.environ, "TEAMSREC_REVIEW_URL": base, "TEAMSREC_TEST_CONFIG": str(cfg_file),
                                  "TEAMSREC_TEST_CAPTURE": str(capture_file), "TEAMSREC_TEST_PID": str(os.getpid())},
                             capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=120)
    finally:
        stop.set()
        srv.shutdown()
    assert run.returncode == 0, run.stdout + run.stderr
