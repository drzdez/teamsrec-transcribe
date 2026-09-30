"""Settings page back end: the TOML is edited in place (comments kept), values are checked, keys never leak."""
import json
import tomllib

import pytest

from teamsrec_transcribe import settings
from teamsrec_transcribe.config import load_config

TOML = '''[user]
name = "Jan Novák"  # your name

[capture]
onsite_mic = "Pole mikrofonu (Intel® # 2)"  # a '#' inside the string is not a comment

[transcribe]
provider = "whisperx"
glossary = [
    "WFMS",   # multi-line array
    "NOTAM",
]
batch_size = 16

[summarize]
model = "gemma4:31b"         # ollama tag
'''


class FakeVault:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise KeyError(name)
        del self.store[(service, name)]


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    p = tmp_path / "teamsrec.toml"
    p.write_text(TOML, encoding="utf-8")
    monkeypatch.setenv("TEAMSREC_CONFIG", str(p))
    return p


@pytest.fixture
def vault(monkeypatch):
    v = FakeVault()
    monkeypatch.setattr(settings, "_keyring", lambda: v)
    for s in settings.SECRETS.values():
        for var in s.env:
            monkeypatch.delenv(var, raising=False)
    return v


def test_values_come_from_the_file_or_the_defaults(cfg_file):
    v = settings.read_values(cfg_file)
    assert v["user.name"] == "Jan Novák"
    assert v["capture.onsite_mic"] == "Pole mikrofonu (Intel® # 2)"
    assert v["transcribe.glossary"] == ["WFMS", "NOTAM"]
    assert v["transcribe.diarize"] is True and v["voiceprints.enabled"] is False  # defaults
    assert v["capture.onsite_offer"] == "never"


def test_saving_keeps_comments_order_and_other_keys(cfg_file):
    res = settings.save({"user.name": "Jana Nováková", "capture.onsite_mic": "Mikrofon (USB)",
                         "transcribe.glossary": ["WFMS", "NOTAM", "BPMN"], "transcribe.batch_size": "8",
                         "summarize.model": "gemma4:31b", "voiceprints.enabled": True,
                         "retention.audio_days": 30}, cfg_file)
    assert res == {"changed": ["capture.onsite_mic", "retention.audio_days", "transcribe.batch_size",
                               "transcribe.glossary", "user.name", "voiceprints.enabled"], "restart": False}
    text = cfg_file.read_text(encoding="utf-8")
    assert 'name = "Jana Nováková"  # your name' in text
    assert "onsite_mic = \"Mikrofon (USB)\"  # a '#' inside the string is not a comment" in text
    assert 'glossary = ["WFMS", "NOTAM", "BPMN"]' in text and "multi-line array" not in text
    assert "batch_size = 8" in text
    assert 'model = "gemma4:31b"         # ollama tag' in text, "an unchanged value is not rewritten"
    assert text.index("[voiceprints]") > text.index("[summarize]"), "a new section goes at the end"
    data = tomllib.loads(text)
    assert data["voiceprints"]["enabled"] is True and data["retention"]["audio_days"] == 30
    cfg = load_config(cfg_file)
    assert cfg.user_name == "Jana Nováková" and cfg.transcribe.batch_size == 8
    assert settings.save({"user.name": "Jana Nováková"}, cfg_file)["changed"] == []


def test_a_new_key_goes_into_its_section(cfg_file):
    settings.save({"transcribe.diarize": False}, cfg_file)
    data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["transcribe"]["diarize"] is False and data["transcribe"]["batch_size"] == 16
    assert settings.save({"recordings.out_dir": "E:/rec"}, cfg_file)["restart"] is True


@pytest.mark.parametrize("key,value,msg", [
    ("transcribe.provider", "azure", "neznámá hodnota"),
    ("transcribe.batch_size", "osm", "není číslo"),
    ("transcribe.batch_size", 2.5, "celé číslo"),
    ("retention.audio_days", -1, "záporné"),
    ("video.enabled", "ano", "ano/ne"),
    ("user.name", "a\nb", "jeden řádek"),
    ("nothing.here", 1, "neznámé nastavení"),
])
def test_bad_values_are_refused_and_nothing_is_written(cfg_file, key, value, msg):
    before = cfg_file.read_text(encoding="utf-8")
    with pytest.raises(settings.SettingsError, match=msg):
        settings.save({"user.name": "Změna", key: value}, cfg_file)
    assert cfg_file.read_text(encoding="utf-8") == before


def test_a_list_can_come_as_lines(cfg_file):
    settings.save({"summarize.compare": "anthropic:claude-opus-5\n\n  ollama:gemma4:31b "}, cfg_file)
    assert settings.read_values(cfg_file)["summarize.compare"] == ["anthropic:claude-opus-5", "ollama:gemma4:31b"]


def test_keys_come_from_the_environment_first_then_the_vault(vault, monkeypatch):
    assert settings.get_secret("openai") == ""
    settings.set_secret("openai", " sk-test-123 ")
    assert vault.store[("teamsrec", "openai")] == "sk-test-123"
    assert settings.get_secret("openai") == "sk-test-123"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert settings.get_secret("openai") == "sk-from-env", "a key set with setx wins"
    states = {s["name"]: s for s in settings.secret_states()}
    assert states["openai"]["state"] == "env" and states["openai"]["env"] == "OPENAI_API_KEY"
    assert states["anthropic"]["state"] == "missing"
    assert "sk-" not in json.dumps(states), "a key value never leaves the vault"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert settings.delete_secret("openai") is True and settings.delete_secret("openai") is False
    assert settings.get_secret("openai") == ""
    with pytest.raises(settings.SettingsError):
        settings.set_secret("openai", "two words")
    with pytest.raises(settings.SettingsError):
        settings.set_secret("github", "x")


def test_cloud_providers_and_claude_use_the_stored_key(vault):
    from teamsrec_transcribe.providers.base import ProviderError
    from teamsrec_transcribe.providers.cloud import api_key
    with pytest.raises(ProviderError, match="Settings"):
        api_key("elevenlabs", "ELEVENLABS_API_KEY")
    settings.set_secret("elevenlabs", "el-key")
    assert api_key("elevenlabs", "ELEVENLABS_API_KEY") == "el-key"


def test_settings_over_http(tmp_path, cfg_file, vault):
    from test_core import _call, _serve
    srv, state, base = _serve(tmp_path)
    try:
        assert state.settings_path == cfg_file
        code, d = _call(base, "GET", "/api/settings")
        assert code == 200 and d["path"] == str(cfg_file)
        keys = {f["key"]: f for s in d["sections"] for f in s["fields"]}
        assert keys["transcribe.provider"]["choices"] == ["whisperx", "openai", "elevenlabs"]
        assert keys["user.name"]["value"] == "Jan Novák"
        code, r = _call(base, "PUT", "/api/settings", {"values": {"summarize.provider": "anthropic",
                                                                  "voiceprints.threshold": "0,6"}})
        assert code == 200 and r["changed"] == ["summarize.provider", "voiceprints.threshold"]
        assert state.cfg.summarize.provider == "anthropic" and state.cfg.voiceprints.threshold == 0.6
        assert state.cfg.out_dir == tmp_path, "the running server keeps its folder"
        code, r = _call(base, "PUT", "/api/settings", {"values": {"summarize.provider": "gpt"}})
        assert code == 400 and "neznámá hodnota" in r["error"]
        code, r = _call(base, "PUT", "/api/secrets/anthropic", {"value": "sk-ant-secret"})
        assert code == 200 and {s["name"]: s["state"] for s in r["secrets"]}["anthropic"] == "vault"
        assert "sk-ant-secret" not in json.dumps(r) and "sk-ant-secret" not in json.dumps(_call(base, "GET", "/api/settings")[1])
        code, r = _call(base, "DELETE", "/api/secrets/anthropic")
        assert code == 200 and r["removed"] is True
    finally:
        srv.shutdown()


def test_suggestions_mark_what_is_on_this_pc_and_what_would_download(cfg_file, vault, monkeypatch, tmp_path):
    hub = tmp_path / "hub"
    (hub / "models--Systran--faster-whisper-large-v3").mkdir(parents=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    monkeypatch.setattr(settings, "_cache", {})
    monkeypatch.setattr(settings, "ollama_models", lambda url: ["gemma4:31b"])
    monkeypatch.setattr(settings, "claude_models", lambda: ["claude-opus-5-5"])
    settings.save({"summarize.model": "qwen9:70b", "summarize.compare": ["ollama:llama9:8b", "anthropic:claude-opus-5-5"]},
                  cfg_file)
    d = settings.suggestions(settings.read_values(cfg_file))
    whisper = {o["value"]: o for o in d["whisper"]}
    assert whisper["large-v3"]["local"] is True and whisper["large-v3"]["note"] == "v počítači"
    assert whisper["medium"]["local"] is False and "stáhne se" in whisper["medium"]["note"]
    ollama = {o["value"]: o for o in d["summary"]["ollama"]}
    assert ollama["gemma4:31b"]["local"] is True
    assert ollama["qwen9:70b"]["local"] is False and "ollama pull qwen9:70b" in ollama["qwen9:70b"]["note"]
    assert ollama["llama9:8b"]["local"] is False, "a comparison model that is not pulled is flagged too"
    assert d["summary"]["anthropic"] == [{"value": "claude-opus-5-5", "note": "cloud (Claude API)", "local": None}]
    assert {o["value"] for o in d["compare"]} >= {"anthropic:claude-opus-5-5", "ollama:gemma4:31b"}
