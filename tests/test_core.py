import json
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest

from teamsrec_transcribe.config import Config, TranscribeSettings, load_config, with_overrides
from teamsrec_transcribe.export import to_srt, to_txt
from teamsrec_transcribe.importer import derive_metadata
from teamsrec_transcribe.prompt import build_prompt
from teamsrec_transcribe.providers.base import Segment
from teamsrec_transcribe.recording import Recording, is_sidecar, make_stem, resolve_recording, slugify
from teamsrec_transcribe.speakers import apply_manual_names, apply_video_timeline, speaker_list
from teamsrec_transcribe.video_speakers import VideoTimeline


# ---------------------------------------------------------------- naming

def test_slugify_strips_diacritics_and_limits():
    assert slugify("Týdenní sync: WFMS / NOTAM") == "tydenni-sync-wfms-notam"
    assert slugify("!!!") == "recording"
    assert len(slugify("a" * 100)) == 60


def test_make_stem():
    assert make_stem(datetime(2026, 9, 4, 14, 0, 12), "Týdenní sync") == "2026-09-04_1400_tydenni-sync"


# ---------------------------------------------------------------- import metadata

def test_teams_name_pattern(tmp_path):
    f = tmp_path / "WFMS Future Version-20260903_133158-Meeting Recording.mp4"
    f.write_bytes(b"x")
    m = derive_metadata(f, creation_time=datetime(2020, 1, 1))
    assert m.title == "WFMS Future Version"
    assert m.start == datetime(2026, 9, 3, 13, 31, 58)
    assert m.source == "teams-name"


def test_container_then_file_fallback(tmp_path):
    f = tmp_path / "hlasovka.m4a"
    f.write_bytes(b"x")
    m = derive_metadata(f, creation_time=datetime(2026, 8, 1, 9, 30))
    assert (m.title, m.start, m.source) == ("hlasovka", datetime(2026, 8, 1, 9, 30), "container")
    m = derive_metadata(f, creation_time=None)
    assert m.title == "hlasovka" and m.source == "file" and m.start.year >= 2020


# ---------------------------------------------------------------- recording / sidecar

def _make_recording(out_dir: Path, stem="2026-09-04_1400_tydenni-sync", **extra) -> Path:
    d = out_dir / "2026" / "09" / stem
    d.mkdir(parents=True)
    sidecar = {"format": 1, "app": "t", "app_version": "0", "title": "Týdenní sync", "slug": "tydenni-sync",
               "source": "import", "start": "2026-09-04T14:00:00", "end": "2026-09-04T14:30:00", "duration_s": 1800,
               "stop_reason": "n/a", "tracks": {}, "mix": {"file": f"{stem}_mix.wav", "sample_rate": 16000, "channels": 1},
               "participants": [{"name": "Jana Nováková"}, {"name": "Petr Svoboda"}], **extra}
    (d / f"{stem}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    (d / f"{stem}_mix.wav").write_bytes(b"RIFF")
    return d / f"{stem}.json"


def test_is_sidecar_distinguishes_derived_files():
    assert is_sidecar(Path("2026-09-04_1400_tydenni-sync.json"))
    assert not is_sidecar(Path("2026-09-04_1400_tydenni-sync.transcript.json"))
    assert not is_sidecar(Path("2026-09-04_1400_tydenni-sync.speakers.json"))
    assert not is_sidecar(Path("random.json"))


def test_resolve_by_stem_file_and_path(tmp_path):
    sc = _make_recording(tmp_path)
    assert resolve_recording("2026-09-04_1400_tydenni-sync", tmp_path).stem == "2026-09-04_1400_tydenni-sync"
    assert resolve_recording(sc, tmp_path).title == "Týdenní sync"
    assert resolve_recording(sc.with_name("2026-09-04_1400_tydenni-sync_mix.wav"), tmp_path).stem_path == sc.with_suffix("")
    assert resolve_recording(sc.parent, tmp_path).stem == "2026-09-04_1400_tydenni-sync"  # the folder itself
    rec = Recording.load(sc)
    assert rec.mix_path.name.endswith("_mix.wav")
    assert rec.participants == ["Jana Nováková", "Petr Svoboda"]
    assert rec.transcript_path.name == "2026-09-04_1400_tydenni-sync.transcript.json"


def test_latest_keyword(tmp_path):
    _make_recording(tmp_path, stem="2026-09-04_1400_tydenni-sync")
    _make_recording(tmp_path, stem="2026-09-04_1630_pozdejsi")
    assert resolve_recording("latest", tmp_path).stem == "2026-09-04_1630_pozdejsi"
    with pytest.raises(Exception):
        resolve_recording("latest", tmp_path / "empty")


def test_unsupported_format_rejected(tmp_path):
    sc = _make_recording(tmp_path, format=2)
    with pytest.raises(Exception):
        Recording.load(sc)


# ---------------------------------------------------------------- prompt

def test_prompt_from_title_participants_glossary(tmp_path):
    rec = Recording.load(_make_recording(tmp_path))
    p = build_prompt(rec, ("WFMS", "NOTAM", "WFMS"))
    assert p == "Schůzka: Týdenní sync. Účastníci: Jana Nováková, Petr Svoboda. Pojmy: WFMS, NOTAM."


# ---------------------------------------------------------------- speakers

def _segs():
    return [Segment(0, 5, "a", "SPEAKER_00"), Segment(5, 10, "b", "SPEAKER_01"),
            Segment(10, 15, "c", "SPEAKER_00"), Segment(15, 16, "d", "SPEAKER_02"), Segment(60, 61, "e", "SPEAKER_00")]


def test_video_timeline_names_segments_and_maps_fallback():
    tl = VideoTimeline(fps=2, speakers={"Jana": [[0, 5], [10, 15]], "Petr": [[5, 10]]}, clusters=[])
    segs = _segs()
    # SPEAKER_00 overlaps Jana for 10 s -> mapping; segment at 60 s (no video) falls back to Jana
    mapping = apply_video_timeline(segs, tl)
    assert mapping == {"SPEAKER_00": "Jana", "SPEAKER_01": "Petr"} or mapping.get("SPEAKER_00") == "Jana"
    assert [s.speaker for s in segs] == ["Jana", "Petr", "Jana", "SPEAKER_02", "Jana"]


def test_manual_names_and_speaker_list():
    segs = _segs()
    apply_manual_names(segs, {"SPEAKER_02": "Ivan"})
    assert speaker_list(segs) == ["SPEAKER_00", "SPEAKER_01", "Ivan"]


# ---------------------------------------------------------------- export

def test_txt_and_srt():
    segs = [Segment(0.5, 2.25, "Ahoj.", "Jana"), Segment(3661, 3662.5, "Konec", None)]
    txt = to_txt(segs, title="T", header={"language": "cs"})
    assert txt.splitlines()[0] == "# T"
    assert "[00:00:00] Jana: Ahoj." in txt and "[01:01:01] Konec" in txt
    srt = to_srt(segs)
    assert "00:00:00,500 --> 00:00:02,250\nJana: Ahoj." in srt
    assert "01:01:01,000 --> 01:01:02,500\nKonec" in srt


# ---------------------------------------------------------------- config

def test_config_load_and_overrides(tmp_path):
    p = tmp_path / "teamsrec.toml"
    p.write_text('[recordings]\nout_dir = "D:/meetings"\n[transcribe]\nlanguage = "sk"\nglossary = ["WFMS"]\nunknown = 1\n',
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg.out_dir == Path("D:/meetings") and cfg.transcribe.language == "sk" and cfg.transcribe.glossary == ("WFMS",)
    cfg2 = with_overrides(cfg, **{"transcribe.language": "cs", "transcribe.model": None, "out_dir": Path("x")})
    assert cfg2.transcribe.language == "cs" and cfg2.transcribe.model == "large-v3" and cfg2.out_dir == Path("x")
    assert load_config(tmp_path / "missing.toml") == Config(source_path=None)
    assert TranscribeSettings().diarize_model.startswith("pyannote/")


# ---------------------------------------------------------------- summarize input

def test_summary_user_message_contains_transcript(tmp_path):
    from teamsrec_transcribe.summarize import SYSTEM_PROMPT, build_user_message
    rec = Recording.load(_make_recording(tmp_path))
    segs = [Segment(0, 2, "Začneme.", "Jana Nováková"), Segment(2, 5, "Úkol vezmu já.", "Petr Svoboda")]
    msg = build_user_message(rec, segs, {"start": "2026-09-04T14:00:00", "language": "cs", "participants": None})
    assert msg.startswith("Meeting: Týdenní sync\nstart: 2026-09-04T14:00:00\nlanguage: cs\n")
    assert "[00:00:02] Petr Svoboda: Úkol vezmu já." in msg
    assert "{language}" in SYSTEM_PROMPT and "{h3}" in SYSTEM_PROMPT and "{h6}" in SYSTEM_PROMPT


def test_mic_track_names_the_user(tmp_path):
    import wave
    import numpy as np
    from teamsrec_transcribe.mic_speakers import apply_mic_track
    sr = 16000
    t = np.arange(sr * 40) / sr
    sig = np.zeros_like(t)
    hot = [(0, 5), (10, 15), (20, 22), (30, 40)]  # the user talks here
    for a, b in hot:
        sig[int(a * sr):int(b * sr)] = 0.3 * np.sin(2 * np.pi * 220 * t[int(a * sr):int(b * sr)])
    sig += np.random.default_rng(0).normal(0, 0.002, len(t))  # room noise
    wav = tmp_path / "mic.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    segs = [Segment(0, 5, "a", "SPEAKER_00"), Segment(5, 10, "b", "SPEAKER_01"), Segment(10, 15, "c", "SPEAKER_00"),
            Segment(15, 20, "d", "SPEAKER_01"), Segment(20, 22, "e", "SPEAKER_02"), Segment(25, 28, "f", "Jana"),
            Segment(30, 40, "g", "SPEAKER_00")]
    mapping = apply_mic_track(segs, wav, "Jan Novák")
    assert mapping == {"SPEAKER_00": "Jan Novák"}
    # SPEAKER_02's single 2 s segment sits fully inside mic activity -> the user too; Jana untouched
    assert [s.speaker for s in segs] == ["Jan Novák", "SPEAKER_01", "Jan Novák", "SPEAKER_01", "Jan Novák", "Jana", "Jan Novák"]


def test_config_user_name(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[user]\nname = " Jan Novák "\n[recordings]\nout_dir = "D:/m"\n', encoding="utf-8")
    assert load_config(p).user_name == "Jan Novák"
    assert Config().user_name == "" and Config().voiceprints.enabled and 0 < Config().voiceprints.threshold < 1
    p.write_text('[voiceprints]\nenabled = false\nthreshold = 0.7\n', encoding="utf-8")
    v = load_config(p).voiceprints
    assert (v.enabled, v.threshold, v.margin, v.min_seconds) == (False, 0.7, 0.1, 30)


def _make_transcribed(tmp_path, stem="2026-09-04_1400_tydenni-sync"):
    import wave
    import numpy as np
    sc = _make_recording(tmp_path, stem=stem)
    rec = Recording.load(sc)
    sr = 16000
    t = np.arange(sr * 30) / sr
    sig = (0.3 * np.sin(2 * np.pi * 330 * t) * 32767).astype(np.int16)
    with wave.open(str(rec.mix_path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(sig.tobytes())
    segs = [{"start": 0, "end": 4, "text": "Dobrý den, začneme s programem.", "speaker": "SPEAKER_00"},
            {"start": 4, "end": 5, "text": "Ano.", "speaker": "SPEAKER_01"},
            {"start": 5, "end": 12, "text": "Já bych rád probral rozpočet na příští kvartál.", "speaker": "SPEAKER_01"},
            {"start": 12, "end": 20, "text": "Rozpočet je hotový, pošlu ho zítra.", "speaker": "Jana Nováková"},
            {"start": 20, "end": 28, "text": "Ještě k termínům.", "speaker": "SPEAKER_00"}]
    rec.write_json(rec.transcript_path, {"format": 1, "language": "cs", "speaker_sources": ["mic", "diarization"],
                                         "speakers": ["SPEAKER_00", "SPEAKER_01", "Jana Nováková"], "segments": segs})
    rec.summary_path.write_text("# T\n\n## Mluvčí\n| Označení | Jméno | Poznámka |\n|---|---|---|\n"
                                "| SPEAKER_00 | ? | vedl schůzku |\n| SPEAKER_01 | ? | pravděpodobně Petr: osloven |\n",
                                encoding="utf-8")
    return rec


def test_people_display_and_matching(tmp_path):
    from teamsrec_transcribe.people import People, Person
    ppl = People.load(tmp_path, "nick")
    petr = ppl.ensure("Petr Svoboda"); petr.nick = "Péťa"
    jana = ppl.ensure("Jana Nováková"); jana.display = "full"
    ppl.ensure("Jan Novák")
    ppl.save()
    ppl = People.load(tmp_path, "nick")
    assert [p.id for p in ppl.people] == ["petr-svoboda", "jana-novakova", "jan-novak"]
    assert ppl.display("petr-svoboda") == "Péťa" and ppl.display("Petr Svoboda") == "Péťa"
    assert ppl.display("jana-novakova") == "Jana Nováková"  # per-person override
    assert ppl.display("Jan Novák") == "Jan"                 # nick mode without nickname -> first name
    assert ppl.display("SPEAKER_00") == "SPEAKER_00" and ppl.display("Neznámý Host") == "Neznámý Host"
    assert People.load(tmp_path, "full").display("petr-svoboda") == "Petr Svoboda"
    assert ppl.find("petr svoboda").id == "petr-svoboda" and ppl.find("Svoboda Petr").id == "petr-svoboda"
    assert ppl.find("Jana") is not None and ppl.find("Jan").id == "jan-novak"
    segs = [Segment(0, 1, "a", "petr-svoboda"), Segment(1, 2, "b", "SPEAKER_01"), Segment(2, 3, "c", "Jana Nováková")]
    assert ppl.apply(segs) == {"petr-svoboda": "Péťa"}
    assert [s.speaker for s in segs] == ["Péťa", "SPEAKER_01", "Jana Nováková"]
    ppl.ensure("Petr Novotný")  # two Petrs without nicknames would both print "Petr" -> full names
    ppl.people[0].nick = ""
    segs = [Segment(0, 1, "a", "petr-svoboda"), Segment(1, 2, "b", "petr-novotny"), Segment(2, 3, "c", "jan-novak")]
    ppl.apply(segs)
    assert [s.speaker for s in segs] == ["Petr Svoboda", "Petr Novotný", "Jan"]
    ppl.replace_all([{"id": "petr-svoboda", "first": "Petr", "last": "Svoboda", "nick": "", "display": "first"},
                     {"first": "", "last": "", "nick": ""}, {"first": "Eva", "last": "", "nick": "Evka"}])
    assert [p.id for p in ppl.people] == ["petr-svoboda", "eva"] and ppl.display("petr-svoboda") == "Petr"
    assert ppl.display("eva") == "Evka"
    assert Person.from_text("Petr Svoboda", {"petr-svoboda"}).id == "petr-svoboda-2"


def test_people_dedup_and_merge(tmp_path):
    from teamsrec_transcribe.people import People
    ppl = People.load(tmp_path, "nick")
    z = ppl.ensure("Zdeněk")                      # from the mic track: first name only
    assert (z.id, z.first, z.last) == ("zdenek", "Zdeněk", "")
    z2 = ppl.ensure("Zdeněk Zdražil")             # the typed full name completes the same person
    assert z2 is z and z.last == "Zdražil" and len(ppl.people) == 1
    assert ppl.find("Zdeněk Zdražil") is z and ppl.find("Zdeněk") is z
    other = ppl.ensure("Zdeněk Novák")            # a different Zdeněk now has to be a new person
    assert other is not z and len(ppl.people) == 2
    # merge: aliases/nick carried over, speakers.json rewritten, voice prints moved
    dup = ppl.ensure("Z. Zdražil"); dup.nick = "Zdenda"
    rec = _make_transcribed(tmp_path)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": dup.id, "SPEAKER_01": other.id})
    changed = ppl.merge(z.id, dup.id, [rec])
    assert [r.stem for r in changed] == [rec.stem] and ppl.get(dup.id) is None
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": z.id, "SPEAKER_01": other.id}
    assert z.nick == "Zdenda" and "Z. Zdražil" in z.aliases and ppl.find("Z. Zdražil") is z
    with pytest.raises(ValueError):
        ppl.merge(z.id, "nobody")


def test_voiceprints_registry_and_recognition(tmp_path):
    from teamsrec_transcribe.voiceprints import Voiceprints, enroll_from_recording, remap_embeddings
    a, b = [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]
    near_a = [0.95, 0.3, 0.0, 0.0]
    vp = Voiceprints.load(tmp_path)
    assert vp.enroll("petr", a, "rec1", "SPEAKER_00", "m") and not vp.enroll("petr", a, "rec1", "SPEAKER_00")
    assert vp.enroll("jana", b, "rec1", "SPEAKER_01")
    vp.save()
    vp = Voiceprints.load(tmp_path)
    assert vp.model == "m" and vp.count("petr") == 1 and vp.people["petr"][0]["v"] == [1.0, 0.0, 0.0, 0.0]
    got = vp.recognize({"SPEAKER_03": near_a, "SPEAKER_04": [0.7, 0.7, 0.0, 0.0], "SPEAKER_05": [0.0, 0.0, 1.0, 0.0],
                        "SPEAKER_06": near_a},
                       threshold=0.55, margin=0.1, durations={"SPEAKER_03": 60, "SPEAKER_04": 60, "SPEAKER_05": 60, "SPEAKER_06": 5},
                       min_seconds=30)
    assert got["SPEAKER_03"][0] == "petr" and got["SPEAKER_03"][1] > 0.9
    assert "SPEAKER_04" not in got  # tie between petr and jana: no margin
    assert "SPEAKER_05" not in got  # nobody similar
    assert "SPEAKER_06" not in got  # spoke too little for a reliable embedding
    vp.rename("jana", "jana-novakova")
    assert vp.count("jana") == 0 and vp.count("jana-novakova") == 1
    vp.forget("petr")
    assert vp.count("petr") == 0
    # cap per person
    for i in range(15):
        vp.enroll("x", [1.0, float(i) / 10, 0.0, 0.0], f"r{i}", "S")
    assert vp.count("x") == 10
    # remap: two labels renamed to the same name are averaged, untouched labels keep their key
    emb = {"SPEAKER_00": a, "SPEAKER_01": [0.0, 0.0, 1.0, 0.0], "SPEAKER_02": b}
    before = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00", "SPEAKER_02"]
    after = ["Zdeněk", "Zdeněk", "Zdeněk", "SPEAKER_02"]
    out = remap_embeddings(emb, before, after)
    assert set(out) == {"Zdeněk", "SPEAKER_02"} and abs(out["Zdeněk"][0] - out["Zdeněk"][2]) < 1e-6
    assert abs(sum(x * x for x in out["Zdeněk"]) - 1) < 1e-6
    # enrol from a transcript after naming
    vp2 = Voiceprints.load(tmp_path / "v2")
    segs = [{"start": 0, "end": 40, "speaker": "Zdeněk"}, {"start": 40, "end": 45, "speaker": "SPEAKER_02"}]
    n = enroll_from_recording(vp2, {"speaker_embeddings": out, "diarize_model": "m", "segments": segs},
                              {"Zdeněk": "zdenek", "SPEAKER_02": "petr", "SPEAKER_09": "nobody"}, "rec9", min_seconds=30)
    assert n == 1 and vp2.count("zdenek") == 1 and vp2.count("petr") == 0  # 5 s of speech is not enrolled


def test_review_data_clip_and_save(tmp_path):
    from teamsrec_transcribe.web.review import build_review, clip_wav, list_recordings, save_names, unresolved_labels
    cfg = Config(out_dir=tmp_path, user_name="Já")
    rec = _make_transcribed(tmp_path)
    assert unresolved_labels(rec) == ["SPEAKER_00", "SPEAKER_01"]
    rv = build_review(cfg, rec)
    assert [s["label"] for s in rv["speakers"]] == ["SPEAKER_00", "SPEAKER_01", "Jana Nováková"]  # by time
    s0 = rv["speakers"][0]
    assert s0["unresolved"] and s0["name"] == "" and s0["hint"] == "vedl schůzku" and s0["seconds"] == 12
    assert rv["speakers"][1]["hint"].startswith("pravděpodobně Petr")
    assert 1 <= len(s0["samples"]) <= 3 and s0["samples"][0]["end"] - s0["samples"][0]["start"] <= 8
    assert rv["speakers"][2]["name"] == "Jana Nováková" and not rv["speakers"][2]["unresolved"]
    assert "Já" in rv["known_names"] and "Petr Svoboda" in rv["known_names"]  # user + participants
    assert rv["has_mix"] and rv["speaker_sources"] == ["mic", "diarization"]
    clip = clip_wav(rec.mix_path, 1.0, 3.0)
    assert clip[:4] == b"RIFF" and 2 * 16000 * 2 - 100 < len(clip) < 2 * 16000 * 2 + 100
    rows = list_recordings(cfg)
    assert rows[0]["stem"] == rec.stem and rows[0]["unresolved"] == 2
    written = save_names(cfg, rec, {"SPEAKER_00": " Petr Svoboda ", "SPEAKER_01": "", "Jana Nováková": "Jana Nováková"})
    assert written == {"SPEAKER_00": "petr-svoboda"}  # a person was created from the typed name
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "petr-svoboda"}
    assert "Petr: Dobrý den" in rec.file(".txt").read_text(encoding="utf-8")  # default display: nick -> first
    assert unresolved_labels(rec) == ["SPEAKER_01"]
    assert list_recordings(cfg)[0]["unresolved"] == 1
    rv = build_review(cfg, rec)
    assert rv["speakers"][0]["name"] == "Petr Svoboda" and rv["speakers"][0]["person"]["shown"] == "Petr"
    assert "Petr Svoboda" in rv["known_names"] and rv["people"][0]["id"] == "petr-svoboda"
    from teamsrec_transcribe.web.review import save_people
    save_people(cfg, [{"id": "petr-svoboda", "first": "Petr", "last": "Svoboda", "nick": "Péťa"}], rec.stem)
    assert "Péťa: Dobrý den" in rec.file(".txt").read_text(encoding="utf-8")
    assert build_review(cfg, rec)["speakers"][0]["fields"] == {"first": "Petr", "last": "Svoboda", "nick": "Péťa", "display": ""}
    # structured save: fields update a known person (display override) and create a new one from nick only
    written = save_names(cfg, rec, {"SPEAKER_00": {"first": "Petr", "last": "Svoboda", "nick": "Péťa", "display": "full"},
                                    "SPEAKER_01": {"first": "", "last": "", "nick": "Šéf", "display": ""}})
    assert written == {"SPEAKER_00": "petr-svoboda", "SPEAKER_01": "sef"}
    txt = rec.file(".txt").read_text(encoding="utf-8")
    assert "Petr Svoboda: Dobrý den" in txt and "Šéf: Já bych rád" in txt
    assert build_review(cfg, rec)["speakers"][1]["fields"]["nick"] == "Šéf"


def test_review_http_roundtrip(tmp_path):
    import json as _json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from teamsrec_transcribe.web.review import ReviewState, _handler
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    ref = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(ReviewState(cfg), ref))
    ref["server"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert "teamsrec" in page and "@ts-check" in page
        rows = _json.loads(urllib.request.urlopen(base + "/api/recordings").read())
        assert rows[0]["stem"] == rec.stem
        rv = _json.loads(urllib.request.urlopen(base + f"/api/recording?stem={rec.stem}").read())
        assert len(rv["speakers"]) == 3
        wav = urllib.request.urlopen(base + f"/api/clip?stem={rec.stem}&start=0&end=2").read()
        assert wav[:4] == b"RIFF"
        body = _json.dumps({"stem": rec.stem, "names": {"SPEAKER_01": "Petr"}, "summary": False, "title": "Nový název"}).encode()
        req = urllib.request.Request(base + "/api/save", data=body, headers={"Content-Type": "application/json"})
        res = _json.loads(urllib.request.urlopen(req).read())
        assert res["ok"] and res["written"] == {"SPEAKER_01": "petr"}
        assert res["stem"] == "2026-09-04_1400_novy-nazev" and res["title"] == "Nový název"
        moved = resolve_recording(res["stem"], tmp_path)
        assert moved.title == "Nový název" and not rec.dir.exists()
        assert "# Nový název" in moved.file(".txt").read_text(encoding="utf-8")
        rec = moved  # the rest of the test works with the renamed recording
        ppl = _json.loads(urllib.request.urlopen(base + "/api/people").read())
        assert ppl["people"][0]["first"] == "Petr" and ppl["modes"] == ["first", "full", "nick"]
        assert ppl["people"][0]["prints"] == 0
        body = _json.dumps({"stem": rec.stem, "names": {"SPEAKER_00": "Peter Svoboda"}, "summary": False}).encode()
        urllib.request.urlopen(urllib.request.Request(base + "/api/save", data=body, headers={"Content-Type": "application/json"})).read()
        body = _json.dumps({"keep": "petr", "drop": "peter-svoboda", "stem": rec.stem}).encode()
        res = _json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/people/merge", data=body,
                                                                        headers={"Content-Type": "application/json"})).read())
        assert [p["id"] for p in res["people"]] == ["petr"] and "Peter Svoboda" in res["people"][0]["aliases"]
        det = _json.loads(urllib.request.urlopen(base + "/api/person?id=petr").read())
        assert det["id"] == "petr" and det["recordings"] == [rec.stem] and det["prints"] == []
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/api/person?id=nobody")
        assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "petr"}  # the page always sends the full mapping
        bad = urllib.request.Request(base + "/api/recording?stem=nope")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad)
    finally:
        srv.shutdown(); srv.server_close()


def test_rename_recording_moves_folder_and_fixes_references(tmp_path):
    from teamsrec_transcribe.pipeline import rename_recording
    from teamsrec_transcribe.voiceprints import Voiceprints
    from teamsrec_transcribe.web.review import build_review, save_title
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    old_stem, old_dir = rec.stem, rec.dir
    (old_dir / f"{old_stem}.summary.md").write_text("<!-- x -->\n# Týdenní sync\n\n## Shrnutí\n", encoding="utf-8")
    (old_dir / f"{old_stem}.summary.claude-opus-5.md").write_text("<!-- x -->\n# Týdenní sync\n", encoding="utf-8")
    vp = Voiceprints.load(tmp_path); vp.enroll("jana", [1.0, 0.0], old_stem, "Jana Nováková"); vp.save()
    assert save_title(cfg, rec, "  Týdenní   sync ") is rec  # unchanged after whitespace normalisation
    new = rename_recording(cfg, rec, "Plánování Q4 / rozpočet")
    assert new.stem == "2026-09-04_1400_planovani-q4-rozpocet" and not old_dir.exists() and new.dir.exists()
    names = sorted(p.name for p in new.dir.iterdir())
    assert names == sorted([f"{new.stem}.json", f"{new.stem}_mix.wav", f"{new.stem}.transcript.json",
                            f"{new.stem}.summary.md", f"{new.stem}.summary.claude-opus-5.md"])
    again = Recording.load(new.sidecar_path)
    assert again.title == "Plánování Q4 / rozpočet" and again.sidecar["slug"] == "planovani-q4-rozpocet"
    assert again.mix_path.name == f"{new.stem}_mix.wav" and again.mix_path.exists()
    assert again.summary_path.read_text(encoding="utf-8").splitlines()[1] == "# Plánování Q4 / rozpočet"
    assert Voiceprints.load(tmp_path).people["jana"][0]["stem"] == new.stem
    assert build_review(cfg, again)["title"] == "Plánování Q4 / rozpočet"
    assert resolve_recording("latest", tmp_path).stem == new.stem
    # same slug, different wording: only the title text changes, nothing moves
    same = rename_recording(cfg, again, "Plánování Q4, rozpočet")
    assert same.stem == new.stem and Recording.load(new.sidecar_path).title == "Plánování Q4, rozpočet"
    # a collision is refused
    _make_recording(tmp_path, stem="2026-09-04_1400_jina")
    with pytest.raises(Exception):
        rename_recording(cfg, again, "Jiná")


def test_person_detail_lists_prints_with_samples(tmp_path):
    from teamsrec_transcribe.people import People
    from teamsrec_transcribe.voiceprints import Voiceprints
    from teamsrec_transcribe.web.review import forget_print, person_detail
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.save()
    vp = Voiceprints.load(tmp_path)
    vp.enroll("petr-svoboda", [1.0, 0.0, 0.0], rec.stem, "SPEAKER_01", "m")
    vp.enroll("petr-svoboda", [0.0, 1.0, 0.0], "2020-01-01_0000_gone", "SPEAKER_00", "m")
    vp.save()
    d = person_detail(cfg, "petr-svoboda")
    assert d["shown"] == "Petr" and d["model"] == "m" and len(d["prints"]) == 2
    p0 = d["prints"][0]
    assert p0["title"] == "Týdenní sync" and p0["seconds"] == 8 and p0["has_mix"]
    assert [s["text"][:7] for s in p0["samples"]] == ["Já bych"]  # the 1 s "Ano." is too short for a sample
    assert d["prints"][1]["title"] is None and d["prints"][1]["samples"] == []  # recording no longer there
    assert forget_print(cfg, "petr-svoboda", rec.stem, "SPEAKER_01") == 1
    assert len(person_detail(cfg, "petr-svoboda")["prints"]) == 1
    assert forget_print(cfg, "petr-svoboda", None, None) == 1
    assert person_detail(cfg, "petr-svoboda")["prints"] == []


def test_review_single_instance_and_idle_exit(tmp_path):
    import json as _json
    import threading
    import time
    import urllib.request
    from teamsrec_transcribe.web import review as rv
    cfg = Config(out_dir=tmp_path)
    _make_transcribed(tmp_path)
    lock = tmp_path / "lock.json"
    assert rv.running_instance(cfg, lock) is None  # no lock file
    lock.write_text(_json.dumps({"url": "http://127.0.0.1:1/", "pid": 0, "out_dir": str(tmp_path)}), encoding="utf-8")
    assert rv.running_instance(cfg, lock) is None  # stale lock: nobody answers -> ignored
    lock.unlink()
    result = {}
    t = threading.Thread(target=lambda: result.update(url=rv.serve(cfg, None, open_browser=False, idle_s=1.0, lock=lock)),
                         daemon=True)
    t.start()
    for _ in range(50):
        if lock.exists():
            break
        time.sleep(0.05)
    info = _json.loads(lock.read_text(encoding="utf-8"))
    assert rv.running_instance(cfg, lock) == info["url"]                     # answers -> reuse
    assert rv.running_instance(Config(out_dir=tmp_path / "other"), lock) is None  # different folder -> not ours
    assert rv.serve(cfg, None, open_browser=False, lock=lock) == info["url"]  # second call just returns the URL
    urllib.request.urlopen(info["url"] + "api/recordings").read()             # a page request starts the idle clock
    t.join(timeout=10)
    assert not t.is_alive() and result["url"] == info["url"]                  # exited ~1 s after the last request
    assert not lock.exists()


def test_clean_headings_strips_copied_instructions():
    from teamsrec_transcribe.summarize import HEADINGS, clean_headings
    raw = ("## Shrnutí — 5 to 10 sentences: purpose of the meeting.\nText.\n"
           "## Témata: one bullet per topic\n* a\n## Úkoly — a table\n| Kdo | Úkol |\n## Pojmy\n* WFMS\n## Mluvčí\n| x |")
    got = clean_headings(raw, HEADINGS["cs"])
    assert got.splitlines()[0] == "## Shrnutí"
    assert "## Témata\n" in got and "## Úkoly\n" in got and "## Pojmy\n" in got and "## Mluvčí\n" in got
    assert "5 to 10" not in got and "| Kdo | Úkol |" in got
    assert len(HEADINGS["cs"]) == len(HEADINGS["en"]) == 7


def test_summarize_settings_defaults_and_unknown_provider():
    from teamsrec_transcribe.config import SummarizeSettings
    from teamsrec_transcribe.llm import LLMError, complete
    s = SummarizeSettings()
    assert s.provider == "ollama" and s.model.startswith("gemma4") and s.ollama_url.startswith("http")
    from teamsrec_transcribe.config import load_config
    cfgfile = Path(__file__).parent / "_cmp.toml"
    cfgfile.write_text('[summarize]\ncompare = ["anthropic:claude-opus-5"]\n', encoding="utf-8")
    try:
        assert load_config(cfgfile).summarize.compare == ("anthropic:claude-opus-5",)
    finally:
        cfgfile.unlink()
    with pytest.raises(LLMError):
        complete("sys", "user", SummarizeSettings(provider="nope"))


def test_anthropic_key_comes_from_teamsrec_variable(monkeypatch):
    from teamsrec_transcribe import llm
    from teamsrec_transcribe.config import SummarizeSettings
    from teamsrec_transcribe.llm import LLMError, complete
    assert llm.API_KEY_ENV == "TEAMSREC_ANTHROPIC_API_KEY"
    monkeypatch.delenv("TEAMSREC_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-bogus")  # ours must win over the generic one
    monkeypatch.setenv("TEAMSREC_ANTHROPIC_API_KEY", "sk-ant-ours")
    import anthropic
    seen = {}
    def fake(api_key=None, **kw):
        seen["key"] = api_key
        raise TypeError("stop here")
    monkeypatch.setattr(anthropic, "Anthropic", fake)
    with pytest.raises(LLMError):
        complete("sys", "user", SummarizeSettings(provider="anthropic", model="claude-opus-5"))
    assert seen["key"] == "sk-ant-ours"
