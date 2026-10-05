import json
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest

from teamsrec_transcribe.config import (Config, TranscribeSettings, VoiceprintSettings, load_config,
                                        with_overrides)

VP_ON = VoiceprintSettings(enabled=True)  # voice prints are opt-in; tests that need them say so
from teamsrec_transcribe.export import to_srt, to_txt
from teamsrec_transcribe.importer import derive_metadata
from teamsrec_transcribe.prompt import build_prompt
from teamsrec_transcribe.providers.base import Segment
from teamsrec_transcribe.pipeline import do_transcribe
from teamsrec_transcribe.recording import (Recording, RecordingError, is_sidecar, make_stem, resolve_recording,
                                          slugify)
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
    # SPEAKER_00 overlaps Jana for 10 s of its 11 s -> mapping; the segment at 60 s (no video) follows
    mapping = apply_video_timeline(segs, tl)
    assert mapping.get("SPEAKER_00") == "Jana"
    assert [s.speaker for s in segs] == ["Jana", "Petr", "Jana", "SPEAKER_02", "Jana"]
    # a short highlight must not own a long label: 12 s of Jana on a 30-minute SPEAKER_05
    from teamsrec_transcribe.speakers import video_label_mapping
    long = [Segment(i * 60, i * 60 + 55, "x", "SPEAKER_05") for i in range(30)]
    assert video_label_mapping(long, VideoTimeline(fps=2, speakers={"Jana": [[0, 12]]}, clusters=[])) == {}
    # direct-only pass leaves labels alone; the fallback pass maps them afterwards
    segs2 = _segs()
    original = [s.speaker for s in segs2]
    assert apply_video_timeline(segs2, tl, fallback=False) == {}
    assert segs2[4].speaker == "SPEAKER_00"
    from teamsrec_transcribe.speakers import apply_video_fallback
    segs2[3].speaker = "Jiří"  # the mic named SPEAKER_02 in between: untouched by the fallback
    apply_video_fallback(segs2, tl, original)
    assert [s.speaker for s in segs2] == ["Jana", "Petr", "Jana", "Jiří", "Jana"]


def test_manual_names_and_speaker_list():
    segs = _segs()
    apply_manual_names(segs, {"SPEAKER_02": "Ivan"})
    assert speaker_list(segs) == ["SPEAKER_00", "SPEAKER_01", "Ivan"]


# ---------------------------------------------------------------- export

def test_txt_and_srt():
    segs = [Segment(0.5, 2.25, "Ahoj.", "Jana"), Segment(3661, 3662.5, "Konec", None)]
    txt = to_txt(segs, title="T", header={"language": "cs"})
    assert txt.splitlines()[0] == "# T"
    assert "[00:00:00] Jana: Ahoj." in txt and "[01:01:01] ?: Konec" in txt  # ? = nobody assigned
    srt = to_srt(segs)
    assert "00:00:00,500 --> 00:00:02,250\nJana: Ahoj." in srt
    assert "01:01:01,000 --> 01:01:02,500\n?: Konec" in srt


# ---------------------------------------------------------------- config

def test_config_load_and_overrides(tmp_path):
    p = tmp_path / "teamsrec.toml"
    p.write_text('[recordings]\nout_dir = "D:/meetings"\n[transcribe]\nlanguage = "sk"\nglossary = ["WFMS"]\nunknown = 1\n',
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg.out_dir == Path("D:/meetings") and cfg.transcribe.language == "sk" and cfg.transcribe.glossary == ("WFMS",)
    assert cfg.transcribe.languages == ("cs", "sk", "en")
    p.write_text('[transcribe]\nlanguages = ["cs", "en"]\n', encoding="utf-8")
    assert load_config(p).transcribe.languages == ("cs", "en")
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


def test_mic_residue_of_a_gated_headset_is_not_the_user(tmp_path):
    """A headset with a noise gate sends digital silence between words and a faint residue (-85 dBFS) while
    the others talk: that residue is far above the floor but must not count as the user speaking."""
    import wave
    import numpy as np
    from teamsrec_transcribe.mic_speakers import apply_mic_track
    sr = 16000
    t = np.arange(sr * 60) / sr
    sig = np.zeros_like(t)
    for a, b in [(0, 10), (30, 40)]:  # the user
        sig[a * sr:b * sr] = 0.3 * np.sin(2 * np.pi * 220 * t[a * sr:b * sr])
    for a, b in [(10, 30), (40, 60)]:  # the others: only a gated residue reaches the mic
        sig[a * sr:b * sr] = 10 ** (-85 / 20) * np.sin(2 * np.pi * 330 * t[a * sr:b * sr])
    wav = tmp_path / "mic.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(4); w.setframerate(sr)  # 32-bit: the residue survives quantisation
        w.writeframes((sig * 2147483647).astype(np.int32).tobytes())
    segs = [Segment(0, 10, "a", "SPEAKER_00"), Segment(10, 30, "b", "SPEAKER_01"),
            Segment(30, 40, "c", "SPEAKER_00"), Segment(40, 60, "d", "SPEAKER_02")]
    assert apply_mic_track(segs, wav, "Jan Novák") == {"SPEAKER_00": "Jan Novák"}
    assert [s.speaker for s in segs] == ["Jan Novák", "SPEAKER_01", "Jan Novák", "SPEAKER_02"]


def test_config_user_name(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[user]\nname = " Jan Novák "\n[recordings]\nout_dir = "D:/m"\n', encoding="utf-8")
    assert load_config(p).user_name == "Jan Novák"
    assert Config().user_name == "" and not Config().voiceprints.enabled and 0 < Config().voiceprints.threshold < 1
    p.write_text('[voiceprints]\nenabled = true\n', encoding="utf-8")
    assert load_config(p).voiceprints.enabled  # opt in in the config
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
    z = ppl.ensure("Jiří")                      # from the mic track: first name only
    assert (z.id, z.first, z.last) == ("jiri", "Jiří", "")
    z2 = ppl.ensure("Jiří Dvořák")             # the typed full name completes the same person
    assert z2 is z and z.last == "Dvořák" and len(ppl.people) == 1
    assert ppl.find("Jiří Dvořák") is z and ppl.find("Jiří") is z
    pv = ppl.ensure("Karel Horák"); pv.nick = "Kája"
    assert ppl.ensure("Karol Horák") is pv and "Karol Horák" in pv.aliases   # spelling variant, not a new person
    assert ppl.find("Karol Horák") is pv and ppl.display("Karol Horák") == "Kája"
    other = ppl.ensure("Jiří Novák")            # a different Jiří now has to be a new person
    assert other is not z and len(ppl.people) == 3
    # merge: aliases/nick carried over, speakers.json rewritten, voice prints moved
    dup = ppl.ensure("J. Dvořák"); dup.nick = "Jirka"
    rec = _make_transcribed(tmp_path)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": dup.id, "SPEAKER_01": other.id})
    changed = ppl.merge(z.id, dup.id, [rec])
    assert [r.stem for r in changed] == [rec.stem] and ppl.get(dup.id) is None
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": z.id, "SPEAKER_01": other.id}
    assert z.nick == "Jirka" and "J. Dvořák" in z.aliases and ppl.find("J. Dvořák") is z
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
    # cap per person (distinct prints, otherwise they are skipped as adding nothing)
    for i in range(15):
        vp.enroll("x", [1.0 if j == i else 0.0 for j in range(15)], f"r{i}", "S")
    assert vp.count("x") == 10
    # remap: two labels renamed to the same name are averaged, untouched labels keep their key
    emb = {"SPEAKER_00": a, "SPEAKER_01": [0.0, 0.0, 1.0, 0.0], "SPEAKER_02": b}
    before = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00", "SPEAKER_02"]
    after = ["Jiří", "Jiří", "Jiří", "SPEAKER_02"]
    out = remap_embeddings(emb, before, after)
    assert set(out) == {"Jiří", "SPEAKER_02"} and abs(out["Jiří"][0] - out["Jiří"][2]) < 1e-6
    assert abs(sum(x * x for x in out["Jiří"]) - 1) < 1e-6
    # enrol from a transcript after naming
    vp2 = Voiceprints.load(tmp_path / "v2")
    segs = [{"start": 0, "end": 40, "speaker": "Jiří"}, {"start": 40, "end": 45, "speaker": "SPEAKER_02"}]
    n = enroll_from_recording(vp2, {"speaker_embeddings": out, "diarize_model": "m", "segments": segs},
                              {"Jiří": "jiri", "SPEAKER_02": "petr", "SPEAKER_09": "nobody"}, "rec9", min_seconds=30)
    assert n == 1 and vp2.count("jiri") == 1 and vp2.count("petr") == 0  # 5 s of speech is not enrolled


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
        rv = _json.loads(urllib.request.urlopen(base + f"/api/recordings/{rec.stem}").read())
        assert len(rv["speakers"]) == 3
        wav = urllib.request.urlopen(base + f"/api/recordings/{rec.stem}/clip?start=0&end=2").read()
        assert wav[:4] == b"RIFF"
        body = _json.dumps({"names": {"SPEAKER_01": "Petr"}, "summary": False, "title": "Nový název"}).encode()
        req = urllib.request.Request(base + f"/api/recordings/{rec.stem}/names", data=body, method="PUT",
                                     headers={"Content-Type": "application/json"})
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
        body = _json.dumps({"names": {"SPEAKER_00": "Peter Svoboda"}, "summary": False}).encode()
        urllib.request.urlopen(urllib.request.Request(base + f"/api/recordings/{rec.stem}/names", data=body, method="PUT",
                                                      headers={"Content-Type": "application/json"})).read()
        body = _json.dumps({"keep": "petr", "drop": "peter-svoboda", "stem": rec.stem}).encode()
        res = _json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/people/merge", data=body,
                                                                        headers={"Content-Type": "application/json"})).read())
        assert [p["id"] for p in res["people"]] == ["petr"] and "Peter Svoboda" in res["people"][0]["aliases"]
        det = _json.loads(urllib.request.urlopen(base + "/api/people/petr").read())
        assert det["id"] == "petr" and det["recordings"] == [rec.stem] and det["prints"] == []
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/api/people/nobody")
        assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "petr"}  # the page always sends the full mapping
        bad = urllib.request.Request(base + "/api/recordings/nope")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad)
    finally:
        srv.shutdown(); srv.server_close()


def test_untranscribed_recording_is_offered_for_processing(tmp_path, monkeypatch):
    from teamsrec_transcribe.web import review as rv
    cfg = Config(out_dir=tmp_path)
    rec = Recording.load(_make_recording(tmp_path))  # sidecar + mix, no transcript
    d = rv.build_review(cfg, rec)
    assert d["transcribed"] is False and d["has_mix"] and d["speakers"] == []
    calls = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))  # the child process
    st = rv.ReviewState(cfg)
    assert st.run_process(rec)
    import time
    for _ in range(50):
        if not st.status()["busy"]:
            break
        time.sleep(0.02)
    assert calls and calls[0][-3:] == ["run-job", "process", rec.stem], "processing runs as a child process"
    assert calls[0][:2] == ["--out-dir", str(tmp_path)], "with the server's folder"
    assert st.status()["message"] == "zpracováno" and st.status()["job"] == "process"
    ev = st.status()["events"]
    assert [e["text"] for e in ev][-1].startswith("zpracováno") and ev[-1]["reload"] is True
    assert any("zpracování spuštěno" in e["text"] for e in ev), "the page must see that a job started"

    # "přepsat znovu od nuly": queued, nothing changes yet (the names go when the job starts, in run-job)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": "petr-svoboda"})
    assert st.run_process(rec, force=True)
    for _ in range(50):
        if not st.status()["busy"]:
            break
        time.sleep(0.02)
    assert calls[-1][-4:] == ["run-job", "process", rec.stem, "--force"] and rec.speakers_path.exists()


def test_run_job_from_scratch_drops_the_names_when_it_starts(tmp_path, monkeypatch):
    """The manual names and the cached window analysis must not survive a new diarization – dropped by the job
    itself, so a recording waiting in the queue keeps them and can be cancelled without loss."""
    from typer.testing import CliRunner
    from teamsrec_transcribe import cli, pipeline as pl
    rec = _make_transcribed(tmp_path)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": "petr-svoboda"})
    rec.write_json(rec.speakers_video_path, {"format": 1, "source": "teams-screen", "fps": 2, "speakers": {}})
    seen = []
    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False: seen.append(
        (force, r.speakers_path.exists(), r.speakers_video_path.exists())))
    res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem, "--force"])
    assert res.exit_code == 0, res.output
    assert seen == [(True, False, False)]


def test_choose_language_restricts_to_expected():
    from teamsrec_transcribe.providers.whisperx_provider import choose_language
    votes = [{"ru": 0.23, "sk": 0.20, "cs": 0.15, "en": 0.05}, {"sk": 0.6, "cs": 0.3}, {"cs": 0.5, "sk": 0.4}]
    assert choose_language(votes, ("cs", "sk", "en")) == "sk"      # ru ignored, sk 1.20 vs cs 0.95
    assert choose_language(votes, ("en",)) == "en"
    assert choose_language([], ("cs",)) is None and choose_language([{"ru": 1.0}], ("cs",)) is None


def test_silent_recording_is_not_transcribed_and_latest_skips_it(tmp_path):
    from teamsrec_transcribe.recording import latest_recording
    cfg = Config(out_dir=tmp_path)
    _make_recording(tmp_path, stem="2026-09-21_0900_archi-week-plan", source="live", audio_silent=True)
    good = Recording.load(_make_recording(tmp_path, stem="2026-09-20_1000_tydenni-sync", source="live"))
    assert latest_recording(tmp_path).stem == good.stem  # newest is silent -> skipped
    silent = resolve_recording("2026-09-21_0900_archi-week-plan", tmp_path)
    with pytest.raises(RecordingError, match="audio_silent"):
        do_transcribe(cfg, silent)


def test_onsite_recording_skips_mic_naming(tmp_path, monkeypatch):
    from teamsrec_transcribe import pipeline as pl
    from teamsrec_transcribe.providers.base import ProviderResult
    cfg = Config(out_dir=tmp_path, user_name="Já")
    rec = _make_transcribed(tmp_path)  # real 30 s wav as the mix
    rec.sidecar["source"] = "onsite"
    rec.sidecar["tracks"] = {"mic": {"file": rec.mix_path.name, "sample_rate": 16000, "channels": 1}}
    rec.save_sidecar()
    calls = []
    monkeypatch.setattr(pl, "apply_mic_track", lambda *a, **k: calls.append(a) or {})
    class P:
        name = "fake"
        def transcribe(self, audio, **k):
            return ProviderResult(segments=[Segment(0, 40, "ahoj", "SPEAKER_00")], language="cs", provider="fake", provider_version="0", model="m")
    monkeypatch.setattr(pl, "get_provider", lambda name: P())
    pl.do_transcribe(cfg, rec, force=True)
    assert calls == [] and rec.read_json(rec.transcript_path)["speakers"] == ["SPEAKER_00"]


def test_video_names_whole_voice_groups_only_after_the_voices(tmp_path, monkeypatch):
    """2026-10-05: the highlighted tile no longer names single replies (a live window keeps the previous speaker
    highlighted, which made mixed groups); it names whole voice groups, and only those no voice print found."""
    from teamsrec_transcribe import pipeline as pl
    from teamsrec_transcribe.providers.base import ProviderResult
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    segs = [Segment(0, 6, "a", "SPEAKER_00"), Segment(6, 8, "b", "SPEAKER_01"),   # Jana's tile still lit at 6-8
            Segment(8, 20, "c", "SPEAKER_00"), Segment(20, 30, "d", "SPEAKER_01")]
    rec.write_json(rec.speakers_video_path, {"format": 1, "source": "teams-screen", "fps": 2,
                                             "speakers": {"Jana Nováková": [[0.0, 20.0]], "Petr Svoboda": [[20.0, 30.0]]}})

    class P:
        name = "fake"

        def transcribe(self, audio, **k):
            return ProviderResult(segments=[Segment(x.start, x.end, x.text, x.speaker) for x in segs], language="cs",
                                  provider="fake", provider_version="0", model="m",
                                  speaker_embeddings={"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]})
    monkeypatch.setattr(pl, "get_provider", lambda name: P())
    # the voice knows SPEAKER_01 is Jana; the video would call SPEAKER_00 Jana too and SPEAKER_01 Petr
    monkeypatch.setattr(pl, "_voiceprints_step", lambda cfg, rec, emb, dur, mic, model: (
        rec.write_json(rec.speakers_path, {"SPEAKER_01": "jana-novakova"}) or {"SPEAKER_01": {"person": "jana-novakova", "score": 0.9}}))
    people = pl.People.load(tmp_path, "nick")
    people.ensure("Jana Nováková")
    people.save()
    pl.do_transcribe(cfg, rec, force=True)
    out = rec.read_json(rec.transcript_path)
    speakers = [x["speaker"] for x in out["segments"]]
    assert speakers[1] == speakers[3] == "SPEAKER_01", "no single reply renamed by the highlighted tile"
    assert speakers[0] == speakers[2] == "SPEAKER_00", "Jana is SPEAKER_01 by voice: the video does not make a second Jana"
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_01": "jana-novakova"}

    rec.speakers_path.unlink()  # no voice print knows anybody: the video names both groups, whole
    monkeypatch.setattr(pl, "_voiceprints_step", lambda *a: {})
    pl.do_transcribe(cfg, rec, force=True)
    speakers = [x["speaker"] for x in rec.read_json(rec.transcript_path)["segments"]]
    assert speakers == ["Jana Nováková", "Petr Svoboda", "Jana Nováková", "Petr Svoboda"]


def test_summary_refuses_empty_transcript(tmp_path):
    from teamsrec_transcribe.pipeline import do_summarize
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)  # a handful of words
    with pytest.raises(Exception, match="nothing to summarize"):
        do_summarize(cfg, rec, force=True)


def test_recognize_voices_from_stored_embeddings(tmp_path):
    from teamsrec_transcribe.people import People
    from teamsrec_transcribe.pipeline import recognize_voices
    from teamsrec_transcribe.voiceprints import Voiceprints
    cfg = Config(out_dir=tmp_path, voiceprints=VP_ON)
    rec = _make_transcribed(tmp_path)
    with pytest.raises(Exception):
        recognize_voices(cfg, rec)  # no embeddings stored
    data = rec.read_json(rec.transcript_path)
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0, 0.0], "SPEAKER_01": [0.0, 1.0, 0.0]}
    data["segments"][0]["end"] = 40  # SPEAKER_00 talks long enough, SPEAKER_01 (8 s) does not
    rec.write_json(rec.transcript_path, data)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.ensure("Jana Nováková"); ppl.save()
    vp = Voiceprints.load(tmp_path)
    vp.enroll("petr-svoboda", [0.98, 0.1, 0.0], "other", "X"); vp.enroll("jana-novakova", [0.0, 0.99, 0.1], "other", "Y"); vp.save()
    m = recognize_voices(cfg, rec)
    assert list(m) == ["SPEAKER_00"] and m["SPEAKER_00"]["person"] == "petr-svoboda"
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "petr-svoboda"}
    t = rec.read_json(rec.transcript_path)
    assert "SPEAKER_00" in t["voice_matches"] and t["speaker_sources"][-2:] == ["voiceprint", "diarization"]
    assert "Petr: Dobrý den" in rec.file(".txt").read_text(encoding="utf-8")
    assert recognize_voices(cfg, rec) == {}  # nothing new the second time
    # a close-but-not-close-enough print becomes a hint on the page instead of an assignment
    from teamsrec_transcribe.web.review import build_review
    data = rec.read_json(rec.transcript_path)
    data["speaker_embeddings"]["SPEAKER_01"] = [0.3, 0.8, 0.0]  # ~0.85 to jana but only 8 s of speech
    rec.write_json(rec.transcript_path, data)
    sp = {s["label"]: s for s in build_review(cfg, rec)["speakers"]}
    assert sp["SPEAKER_01"]["voice_hint"]["name"] == "Jana Nováková" and sp["SPEAKER_01"]["voice_hint"]["why"] == "krátká promluva"
    assert sp["SPEAKER_00"]["voice_hint"] is None  # already assigned


def test_merge_same_person_folds_labels_and_keeps_distinct_prints(tmp_path):
    from teamsrec_transcribe.people import People
    from teamsrec_transcribe.pipeline import merge_same_person, same_person_groups
    from teamsrec_transcribe.voiceprints import Voiceprints
    cfg = Config(out_dir=tmp_path, voiceprints=VP_ON)
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path); ppl.ensure("Jana Nováková"); ppl.save()
    data = rec.read_json(rec.transcript_path)
    data["segments"][0]["end"] = 40  # both labels speak long enough to be worth a print
    data["segments"][3]["end"] = 60
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0, 0.0], "Jana Nováková": [0.6, 0.8, 0.0],
                                  "SPEAKER_01": [0.0, 0.0, 1.0]}
    data["voice_matches"] = {"Jana Nováková": {"person": "jana-novakova", "score": 0.9}}
    rec.write_json(rec.transcript_path, data)
    # the video named one label, the user named the other one: one person, two cards
    rec.write_json(rec.speakers_path, {"SPEAKER_00": "jana-novakova", "Jana Nováková": "jana-novakova",
                                       "SPEAKER_01": "petr-svoboda"})
    assert same_person_groups(rec, People.load(tmp_path)) == {"jana-novakova": ["SPEAKER_00", "Jana Nováková"]}

    res = merge_same_person(cfg, rec)
    assert res["merged"] == 1 and res["prints"] == 2  # both embeddings differ enough to be worth keeping
    t = rec.read_json(rec.transcript_path)
    speakers = {s["speaker"] for s in t["segments"]}
    assert speakers == {"SPEAKER_00", "SPEAKER_01"}  # the label with the most speech won
    assert t["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    assert "Jana Nováková" not in t["speaker_embeddings"] and "Jana Nováková" not in t["voice_matches"]
    assert t["merged_speakers"][0]["kept"] == "SPEAKER_00" and t["merged_speakers"][0]["merged"] == ["Jana Nováková"]
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "jana-novakova", "SPEAKER_01": "petr-svoboda"}
    assert Voiceprints.load(tmp_path).count("jana-novakova") == 2
    assert "Jana: Rozpočet je hotový" in rec.file(".txt").read_text(encoding="utf-8")
    assert merge_same_person(cfg, rec)["merged"] == 0  # nothing left to do

    # and it can be taken back without transcribing again
    from teamsrec_transcribe.pipeline import unmerge_speakers
    assert unmerge_speakers(cfg, rec) == 1
    t = rec.read_json(rec.transcript_path)
    assert sorted({s["speaker"] for s in t["segments"]}) == ["Jana Nováková", "SPEAKER_00", "SPEAKER_01"]
    assert t["merged_speakers"] == [] and not any("merged_from" in s for s in t["segments"])
    assert rec.read_json(rec.speakers_path)["Jana Nováková"] == "jana-novakova"
    with pytest.raises(RecordingError, match="nothing to undo"):
        unmerge_speakers(cfg, rec)


def test_mic_named_segments_survive_the_video_pass():
    """Teams never outlines the local user's own tile, so the highlight must not overwrite the mic track."""
    from teamsrec_transcribe.speakers import apply_video_timeline
    from teamsrec_transcribe.video_speakers import VideoTimeline
    tl = VideoTimeline(fps=2.0, speakers={"Michal Bartoš": [[0, 60]]}, clusters=[])
    segs = [Segment(0, 10, "já mluvím", "Jiří Dvořák"),   # named by the microphone a moment ago
            Segment(10, 20, "a teď on", "SPEAKER_01"),
            Segment(20, 30, "mimo zvýraznění", "SPEAKER_02")]
    segs[2].start, segs[2].end = 120, 130
    apply_video_timeline(segs, tl, fallback=False, keep_named=True)
    assert [s.speaker for s in segs] == ["Jiří Dvořák", "Michal Bartoš", "SPEAKER_02"]
    # without keep_named the highlight wins, as it did for imported recordings
    segs2 = [Segment(0, 10, "já mluvím", "Jiří Dvořák")]
    apply_video_timeline(segs2, tl, fallback=False)
    assert segs2[0].speaker == "Michal Bartoš"


def test_video_names_that_match_no_participant_are_dropped():
    from teamsrec_transcribe.pipeline import keep_video_names
    found = {"Tomáš Beneš": [[0, 10]], "Mihoy Bardtnbnsc": [[10, 3000]], "onen Boork": [[20, 25]]}
    # the meeting has participants: only what OCR snapped onto one of them survives
    kept = keep_video_names(found, ["Tomáš Beneš", "Michal Bartoš", "Jiří Dvořák"])
    assert list(kept) == ["Tomáš Beneš"]
    # no participants (imported recording): anything name-shaped is kept, as before ("onen Boork" too:
    # without somebody to compare against there is nothing better to go on)
    kept = keep_video_names(found, [])
    assert list(kept) == ["Tomáš Beneš", "Mihoy Bardtnbnsc", "onen Boork"]


def test_a_person_can_opt_out_of_voice_recognition(tmp_path):
    """Voice prints are biometric data: whoever does not want them gets none, and the old ones go right away."""
    from teamsrec_transcribe.pipeline import enroll_names
    from teamsrec_transcribe.people import People
    from teamsrec_transcribe.voiceprints import Voiceprints
    from teamsrec_transcribe.web.review import save_people
    cfg = Config(out_dir=tmp_path, voiceprints=VP_ON)
    rec = _make_transcribed(tmp_path)
    data = rec.read_json(rec.transcript_path)
    data["segments"][0]["end"] = 40
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0, 0.0]}
    rec.write_json(rec.transcript_path, data)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.save()
    assert enroll_names(cfg, rec, {"SPEAKER_00": "petr-svoboda"}) == 1

    rows = People.load(tmp_path).to_json()
    rows[0]["voiceprint"] = False                      # Petr said no
    out = save_people(cfg, rows)
    assert out[0]["voiceprint"] is False and out[0]["prints"] == 0
    assert Voiceprints.load(tmp_path).count("petr-svoboda") == 0, "his prints are deleted at once"
    assert People.load(tmp_path).get("petr-svoboda").voiceprint is False, "and the wish is remembered"
    other = _make_transcribed(tmp_path, stem="2026-09-05_1000_dalsi")
    d2 = other.read_json(other.transcript_path)
    d2["segments"][0]["end"] = 40
    d2["speaker_embeddings"] = {"SPEAKER_00": [0.9, 0.1, 0.0]}
    other.write_json(other.transcript_path, d2)
    assert enroll_names(cfg, other, {"SPEAKER_00": "petr-svoboda"}) == 0, "no new prints either"


def test_voice_prints_are_off_unless_switched_on(tmp_path):
    from teamsrec_transcribe.pipeline import enroll_names
    from teamsrec_transcribe.people import People
    cfg = Config(out_dir=tmp_path)                     # a fresh install that was not asked
    rec = _make_transcribed(tmp_path)
    data = rec.read_json(rec.transcript_path)
    data["segments"][0]["end"] = 40
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0, 0.0]}
    rec.write_json(rec.transcript_path, data)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.save()
    assert enroll_names(cfg, rec, {"SPEAKER_00": "petr-svoboda"}) == 0
    assert not (tmp_path / "_speakers" / "voiceprints.json").exists()


class _FakeResponse:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def _cloud_setup(monkeypatch, tmp_path, env):
    import requests
    from teamsrec_transcribe.providers import cloud
    monkeypatch.setenv(*env)
    fake = tmp_path / "upload.webm"

    def compress(audio, max_bytes, **k):  # the provider deletes the upload afterwards, so make a new one each time
        fake.write_bytes(b"x")
        return fake
    monkeypatch.setattr(cloud, "compress_for_upload", compress)
    for mod in ("teamsrec_transcribe.providers.openai_provider", "teamsrec_transcribe.providers.elevenlabs_provider"):
        import importlib
        monkeypatch.setattr(importlib.import_module(mod), "compress_for_upload", compress)
    sent = {}

    def post(url, headers=None, data=None, files=None, timeout=None):
        sent.update(url=url, headers=headers, data=data, file=files["file"][0])
        return sent["reply"]
    monkeypatch.setattr(requests, "post", post)
    return sent


def test_elevenlabs_words_become_segments_with_speakers(monkeypatch, tmp_path):
    from teamsrec_transcribe.providers import get_provider
    sent = _cloud_setup(monkeypatch, tmp_path, ("ELEVENLABS_API_KEY", "k-el"))
    words = [{"text": "Dobrý", "start": 0.0, "end": 0.4, "type": "word", "speaker_id": "speaker_1"},
             {"text": " ", "start": 0.4, "end": 0.45, "type": "spacing", "speaker_id": "speaker_1"},
             {"text": "den.", "start": 0.45, "end": 0.9, "type": "word", "speaker_id": "speaker_1"},
             {"text": "(smích)", "start": 1.0, "end": 1.2, "type": "audio_event", "speaker_id": "speaker_2"},
             {"text": "Ahoj", "start": 1.3, "end": 1.6, "type": "word", "speaker_id": "speaker_2"}]
    sent["reply"] = _FakeResponse({"language_code": "ces", "language_probability": 0.97, "words": words})
    res = get_provider("elevenlabs").transcribe(tmp_path / "a.wav", language=None, prompt=None,
                                                settings=TranscribeSettings(), diarize=True)
    assert sent["headers"] == {"xi-api-key": "k-el"} and sent["data"]["model_id"] == "scribe_v2"
    assert sent["data"]["diarize"] == "true" and "language_code" not in sent["data"]
    assert res.language == "cs" and res.speaker_embeddings is None
    assert [(s.speaker, s.text) for s in res.segments] == [("SPEAKER_00", "Dobrý den."), ("SPEAKER_01", "Ahoj")]
    assert [w.word for w in res.segments[0].words] == ["Dobrý", "den."]


def test_openai_diarized_segments_and_errors(monkeypatch, tmp_path):
    from teamsrec_transcribe.providers import ProviderError, get_provider
    sent = _cloud_setup(monkeypatch, tmp_path, ("TEAMSREC_OPENAI_API_KEY", "k-oa"))
    monkeypatch.setenv("OPENAI_API_KEY", "not-this-one")
    sent["reply"] = _FakeResponse({"segments": [
        {"id": "1", "start": 0.0, "end": 2.0, "text": "Ahoj všichni.", "speaker": "A", "type": "transcript.text.segment"},
        {"id": "2", "start": 2.1, "end": 3.0, "text": "Čau.", "speaker": "B", "type": "transcript.text.segment"},
        {"id": "3", "start": 3.1, "end": 4.0, "text": "Jdeme na to.", "speaker": "A", "type": "transcript.text.segment"}],
        "usage": {"type": "duration", "seconds": 4}})
    res = get_provider("openai").transcribe(tmp_path / "a.wav", language="cs", prompt="glosář",
                                            settings=TranscribeSettings(), diarize=True)
    assert sent["headers"] == {"Authorization": "Bearer k-oa"}, "the teamsrec-specific key wins"
    assert sent["data"] == {"model": "gpt-4o-transcribe-diarize", "response_format": "diarized_json",
                            "chunking_strategy": "auto"}, "the diarize model takes neither language nor prompt"
    assert [s.speaker for s in res.segments] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert res.language == "cs" and res.segments[1].text == "Čau."
    sent["reply"] = _FakeResponse({"error": {"message": "bad key"}}, status=401)
    with pytest.raises(ProviderError, match="OpenAI 401"):
        get_provider("openai").transcribe(tmp_path / "a.wav", language=None, prompt=None,
                                          settings=TranscribeSettings(), diarize=True)


def test_cloud_provider_without_a_key_says_which_variable(monkeypatch, tmp_path):
    from teamsrec_transcribe.providers import ProviderError, get_provider
    for v in ("TEAMSREC_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(ProviderError, match="TEAMSREC_ELEVENLABS_API_KEY"):
        get_provider("elevenlabs").transcribe(tmp_path / "a.wav", language=None, prompt=None,
                                              settings=TranscribeSettings(), diarize=True)


def test_invented_replies_are_recognised():
    from teamsrec_transcribe.providers.whisperx_provider import is_hallucination
    for junk in ("Ďakujem za pozornosť.", "Děkuji za pozornost!", "Dakujem vám za pozornost", "Konec.",
                 "www.hradeckralove.org", "Titulky vytvořil JohnyX", "Thank you for watching!"):
        assert is_hallucination(junk), junk
    for real in ("Ďakujem za pozornosť, a teraz k rozpočtu na budúci rok.", "Konec sprintu je v piatok.",
                 "Dobrý ráno.", "A to znamená, že já ten princip budu rolovat na všechny repa."):
        assert not is_hallucination(real), real
    assert is_hallucination("Pojmy ČNES, ÚJKN 2016", level_db=-36.4, median_db=-26.6), "short and far too quiet"
    assert not is_hallucination("Pojmy ČNES, ÚJKN 2016", level_db=-19.0, median_db=-26.6)
    assert not is_hallucination("A tohle je dlouhá věta, kterou někdo řekl hodně potichu do mikrofonu.",
                                level_db=-40.0, median_db=-26.6), "long replies are speech even when quiet"


def test_speaker_language_choice():
    from teamsrec_transcribe.providers.whisperx_provider import choose_speaker_languages
    votes = {"SPEAKER_00": [{"sk": .7, "cs": .25}], "SPEAKER_01": [{"cs": 1.0}, {"cs": .9, "sk": .1}],
             "SPEAKER_02": [{"cs": .45, "sk": .4}], "SPEAKER_03": []}
    assert choose_speaker_languages(votes, "sk", ("cs", "sk", "en")) == {"SPEAKER_01": "cs"}
    assert choose_speaker_languages(votes, "cs", ("cs", "sk", "en")) == {"SPEAKER_00": "sk"}


def test_purge_audio_deletes_only_finished_old_recordings(tmp_path):
    from teamsrec_transcribe.pipeline import do_transcribe, purge_audio
    cfg = Config(out_dir=tmp_path)
    done = _make_transcribed(tmp_path)                                  # 2026-09-04, speakers unnamed so far
    rec2 = _make_transcribed(tmp_path, stem="2026-09-05_1000_dalsi")    # stays unfinished
    for r in (done, rec2):
        r.summary_path.write_text("# zápis\n", encoding="utf-8")
        r.file("_screen1.mp4").write_bytes(b"v" * 10)
    done.write_json(done.speakers_path, {"SPEAKER_00": "petr", "SPEAKER_01": "jana"})
    now = datetime(2026, 12, 31)
    assert [r["stem"] for r in purge_audio(cfg, 90, dry_run=True, now=now)] == [done.stem]
    assert done.mix_path.exists(), "a dry run deletes nothing"
    assert purge_audio(cfg, 200, now=now) == [], "younger than the limit"
    out = purge_audio(cfg, 90, now=now)
    assert out[0]["files"] == [f"{done.stem}_mix.wav", f"{done.stem}_screen1.mp4"] and out[0]["bytes"] > 0
    assert not done.mix_path.exists() and not done.file("_screen1.mp4").exists()
    for kept in (done.transcript_path, done.summary_path, done.speakers_path, done.stem_path.with_suffix(".json")):
        assert kept.exists(), kept.name
    assert Recording.load(done.stem_path.with_suffix(".json")).sidecar["audio_purged"] == "2026-12-31"
    assert rec2.mix_path.exists(), "unnamed speakers still need their audio samples"
    with pytest.raises(RecordingError, match="purge-audio"):
        do_transcribe(cfg, Recording.load(done.stem_path.with_suffix(".json")), force=True)
    with pytest.raises(RecordingError):
        purge_audio(cfg, 0)


def _serve(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer
    from teamsrec_transcribe.web.review import ReviewState, _handler
    cfg = Config(out_dir=tmp_path)
    state, ref = ReviewState(cfg), {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state, ref))
    ref["server"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state, f"http://127.0.0.1:{srv.server_address[1]}"


def _call(base, method, path, body=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_rest_api_routes_errors_and_openapi(tmp_path):
    from urllib.parse import quote
    from teamsrec_transcribe.web.review import ROUTES
    rec = _make_transcribed(tmp_path)
    srv, state, base = _serve(tmp_path)
    try:
        code, rows = _call(base, "GET", "/api/recordings")
        assert code == 200 and rows[0]["stem"] == rec.stem
        code, r = _call(base, "GET", f"/api/recordings/{rec.stem}")
        assert code == 200 and {s["label"] for s in r["speakers"]} >= {"SPEAKER_00", "Jana Nováková"}
        code, r = _call(base, "PUT", f"/api/recordings/{rec.stem}/names",
                        {"names": {"SPEAKER_00": {"first": "Petr", "last": "Svoboda", "nick": "", "display": ""}}})
        assert code == 200 and r["written"] == {"SPEAKER_00": "petr-svoboda"}
        # a label with a space and diacritics travels in the path
        code, r = _call(base, "DELETE", f"/api/recordings/{rec.stem}/speakers/{quote('Jana Nováková')}")
        assert code == 200 and r["removed"] == 1
        code, r = _call(base, "GET", f"/api/recordings/{rec.stem}/docs/{rec.stem}.txt")
        assert code == 200 and "Petr" in r["text"]
        assert _call(base, "GET", "/api/nothing-here")[0] == 404
        assert _call(base, "POST", f"/api/recordings/{rec.stem}")[0] == 405, "known path, wrong method"
        code, r = _call(base, "GET", "/api/recordings/2026-01-01_0000_nic")
        assert code == 400 and "not found" in r["error"]
        code, spec = _call(base, "GET", "/api/openapi.json")
        assert code == 200 and spec["openapi"].startswith("3.1")
        documented = {path for path in spec["paths"]}
        for method, pattern, name in ROUTES:
            if pattern == r"/":
                continue
            as_doc = (pattern.replace("(?P<stem>[^/]+)", "{stem}").replace("(?P<file>[^/]+)", "{file}")
                      .replace("(?P<label>[^/]+)", "{label}").replace("(?P<pid>[^/]+)", "{id}")
                      .replace("(?P<doc>[^/]+)", "{doc}").replace("(?P<name>[^/]+)", "{name}")
                      .replace("(?P<job>[^/]+)", "{job}").replace("\\.", "."))
            assert as_doc in documented, f"{method} {as_doc} missing in openapi.py"
            assert method.lower() in spec["paths"][as_doc], f"{method} {as_doc} not documented"
    finally:
        srv.shutdown()


def test_server_sent_events_stream_and_replay(tmp_path):
    import http.client
    rec = _make_transcribed(tmp_path)
    srv, state, base = _serve(tmp_path)
    port = srv.server_address[1]
    state.event("dřívější událost", "ok")

    def read_frames(resp, count):
        frames, cur = [], {}
        while len(frames) < count:
            line = resp.fp.readline().decode("utf-8").rstrip("\n")
            if not line:
                if cur:
                    frames.append(cur); cur = {}
                continue
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            cur[key] = value
        return frames

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/events")
        resp = conn.getresponse()
        assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
        hello, old = read_frames(resp, 2)
        assert hello["event"] == "hello" and json.loads(old["data"])["text"] == "dřívější událost"
        assert json.loads(old["data"])["replay"] is True, "history on connect only fills the list"
        _call(base, "PUT", f"/api/recordings/{rec.stem}/names", {"names": {}})
        (live,) = read_frames(resp, 1)
        data = json.loads(live["data"])
        assert live["event"] == "log" and live["id"] == str(data["n"]) and "uloženo" in data["text"]
        conn.close()
        # a reconnect with Last-Event-ID gets only what it missed
        state.event("zmeškaná", "ok")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/events", headers={"Last-Event-ID": live["id"]})
        resp = conn.getresponse()
        hello, missed = read_frames(resp, 2)
        assert json.loads(missed["data"])["text"] == "zmeškaná" and json.loads(missed["data"])["replay"] is False
        conn.close()
    finally:
        srv.shutdown()


def test_help_documents_come_from_the_docs_folder():
    from teamsrec_transcribe.web.review import help_doc
    for name in ("user-guide", "install", "privacy"):
        d = help_doc(name)
        assert d["doc"] == name and d["text"].startswith("#"), name
    with pytest.raises(RecordingError, match="unknown help document"):
        help_doc("../README")


def test_voiceprint_skips_a_print_that_adds_nothing(tmp_path):
    from teamsrec_transcribe.voiceprints import Voiceprints
    vp = Voiceprints.load(tmp_path)
    assert vp.enroll("petr", [1.0, 0.0, 0.0], "rec1", "SPEAKER_00")
    assert not vp.enroll("petr", [0.99, 0.02, 0.0], "rec2", "SPEAKER_00")  # same voice, same conditions
    assert vp.enroll("petr", [0.8, 0.6, 0.0], "rec3", "SPEAKER_00")        # another room, worth keeping
    assert vp.count("petr") == 2


def test_remove_speaker_drops_segments_and_reexports(tmp_path):
    from teamsrec_transcribe.pipeline import remove_speaker
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    rec.write_json(rec.speakers_path, {"SPEAKER_01": "petr"})
    assert remove_speaker(cfg, rec, "SPEAKER_01") == 2
    data = rec.read_json(rec.transcript_path)
    assert [s["speaker"] for s in data["segments"]] == ["SPEAKER_00", "Jana Nováková", "SPEAKER_00"]
    assert data["speakers"] == ["SPEAKER_00", "Jana Nováková"] and data["removed_speakers"][0]["label"] == "SPEAKER_01"
    assert rec.read_json(rec.speakers_path) == {}
    assert "SPEAKER_01" not in rec.file(".txt").read_text(encoding="utf-8")
    assert remove_speaker(cfg, rec, "SPEAKER_01") == 0
    # segments without any speaker show up as UNKNOWN on the page and can be removed under that name
    data = rec.read_json(rec.transcript_path)
    data["segments"].append({"start": 30, "end": 31, "text": "Přidáváme CJ.", "speaker": None})
    rec.write_json(rec.transcript_path, data)
    assert remove_speaker(cfg, rec, "UNKNOWN") == 1
    assert all(s.get("speaker") for s in rec.read_json(rec.transcript_path)["segments"])


def test_tile_outline_detection():
    pytest.importorskip("scipy")  # the [video] extra; CI installs the base package only
    import numpy as np
    from teamsrec_transcribe.video_speakers import _tile_label_box, _tile_outlines
    img = np.full((540, 960, 3), 40, dtype=np.uint8)
    # a hollow accent-coloured rectangle = the speaking tile; a filled block = a button, not a tile
    for x in range(480, 905):
        img[52:54, x] = (122, 122, 205); img[287:289, x] = (116, 116, 159)
    for y in range(52, 289):
        img[y, 480:482] = (98, 104, 143); img[y, 903:905] = (122, 122, 205)
    img[10:30, 700:760] = (122, 122, 205)
    tiles = _tile_outlines(img)
    assert len(tiles) == 1 and tiles[0][0] <= 482 and tiles[0][2] >= 903 and tiles[0][1] <= 54 and tiles[0][3] >= 287
    lx0, ly0, lx1, ly1 = _tile_label_box(tiles[0])
    assert lx0 < 500 and ly1 <= 289 and ly0 >= 255 and lx1 <= 482 + 240


def test_merge_timelines_and_shift():
    from teamsrec_transcribe.video_speakers import merge_timelines
    a = VideoTimeline(fps=2, speakers={"Jana": [[0, 5], [10, 12]]}, clusters=[{"box": [1]}])
    b = VideoTimeline(fps=2, speakers={"Jana": [[4, 8]], "Petr": [[1, 2]]}, clusters=[])
    m = merge_timelines([a, b.shifted(100), VideoTimeline(fps=2, speakers={"Jana": [[4, 8]]}, clusters=[])])
    assert m.speakers == {"Jana": [[0, 8], [10, 12], [104, 108]], "Petr": [[101, 102]]}  # overlap merged, shift kept
    assert m.to_json()["source"] == "teams-screen" and m.clusters == [{"box": [1]}]
    assert merge_timelines([None, None]) is None
    assert VideoTimeline.from_json(m.to_json()).speakers == m.speakers


def test_screen_analysis_writes_merged_timeline(tmp_path, monkeypatch):
    from teamsrec_transcribe import pipeline as pl
    from teamsrec_transcribe.people import People
    cfg = Config(out_dir=tmp_path)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.save()
    rec = Recording.load(_make_recording(tmp_path, screens=[
        {"file": "2026-09-04_1400_tydenni-sync_screen1.mp4", "fps": 2, "width": 1600, "height": 900, "start_offset_s": 0.0, "titles": ["Sync | Microsoft Teams"]},
        {"file": "2026-09-04_1400_tydenni-sync_screen2.mp4", "fps": 2, "width": 1600, "height": 900, "start_offset_s": 30.0, "titles": ["Galerie"]},
        {"file": "missing_screen3.mp4", "fps": 2, "start_offset_s": 0.0}]))
    for n in ("_screen1.mp4", "_screen2.mp4"):
        rec.file(n).write_bytes(b"x")
    seen = []
    def fake_analyze(path, *, fps, names, width, height):
        seen.append((path.name, names, width))
        if path.name.endswith("_screen1.mp4"):
            return VideoTimeline(fps=fps, speakers={"Petr Svoboda": [[0, 10]]}, clusters=[])
        return VideoTimeline(fps=fps, speakers={"Jana Nováková": [[0, 5]]}, clusters=[])
    monkeypatch.setattr(pl, "analyze_screen", fake_analyze)
    tl = pl.do_video(cfg, rec)
    assert [s[0][-12:] for s in seen] == ["_screen1.mp4", "_screen2.mp4"]
    assert "Petr Svoboda" in seen[0][1] and "Jana Nováková" in seen[0][1]  # registry + participants as OCR candidates
    assert tl.speakers == {"Petr Svoboda": [[0, 10]], "Jana Nováková": [[30, 35]]}
    assert rec.read_json(rec.speakers_video_path)["source"] == "teams-screen"
    assert pl.is_meeting_screen(["Schůzka s: Petr | Microsoft Teams"]) and pl.is_meeting_screen([])
    assert not pl.is_meeting_screen(["Calendar | Microsoft Teams"]) and not pl.is_meeting_screen(["Chat | Archi | Microsoft Teams"])
    # a main-window capture and OCR noise are ignored
    rec2 = Recording.load(_make_recording(tmp_path, stem="2026-09-04_1500_druha", screens=[
        {"file": "2026-09-04_1500_druha_screen1.mp4", "fps": 2, "start_offset_s": 0.0, "titles": ["Calendar | Microsoft Teams"]},
        {"file": "2026-09-04_1500_druha_screen2.mp4", "fps": 2, "start_offset_s": 0.0, "titles": ["WFMS | Microsoft Teams"]}]))
    rec2.file("_screen1.mp4").write_bytes(b"x"); rec2.file("_screen2.mp4").write_bytes(b"x")
    monkeypatch.setattr(pl, "analyze_screen", lambda path, **k: VideoTimeline(fps=2, speakers={"Develonment": [[0, 5]], "Petr Svoboda": [[5, 9]]}, clusters=[]))
    assert pl.do_video(cfg, rec2).speakers == {"Petr Svoboda": [[5, 9]]}
    assert pl._looks_like_a_name("Petr Svoboda") and pl._looks_like_a_name("Jana Nováková-Černá")
    assert not pl._looks_like_a_name("Petr") and not pl._looks_like_a_name("x1 y2") and not pl._looks_like_a_name("Nahrávání 12:30")


def test_outlook_pick_meeting_and_fields():
    from datetime import datetime as dt
    from teamsrec_transcribe.outlook import calendar_fields, candidates_at, pick_meeting
    items = [{"subject": "Archi week plan", "start": dt(2026, 9, 14, 8, 30), "end": dt(2026, 9, 14, 9, 15), "teams": True,
              "attendees": ["Jana Nováková", "Petr Svoboda"], "organizer": "Jana Nováková"},
             {"subject": "B", "start": dt(2026, 9, 14, 9, 15), "end": dt(2026, 9, 14, 9, 30), "teams": False, "attendees": []},
             {"subject": "DeepSource", "start": dt(2026, 9, 14, 9, 20), "end": dt(2026, 9, 14, 9, 40), "teams": True, "attendees": []}]
    assert pick_meeting(items, dt(2026, 9, 14, 8, 22)) == (items[0], "time")   # 8 min early
    assert pick_meeting(items, dt(2026, 9, 14, 9, 17))[0]["subject"] == "B"   # inside B beats A's grace
    assert pick_meeting(items, dt(2026, 9, 14, 9, 25))[0]["subject"] == "DeepSource"   # inside both: Teams first
    assert pick_meeting(items, dt(2026, 9, 14, 12, 0)) == (None, "")
    # the Teams window / file title settles parallel meetings and ad-hoc calls
    assert pick_meeting(items, dt(2026, 9, 14, 9, 25), "Archi week plan | Microsoft Teams") == (items[0], "title")
    assert pick_meeting(items, dt(2026, 9, 14, 9, 25), "Deep Source") == (items[2], "title")
    assert pick_meeting(items, dt(2026, 9, 14, 9, 25), "Připojení ke schůzce")[1] == "time"
    assert pick_meeting(items, dt(2026, 9, 14, 9, 25), "Schůzka s: Petr")[1] == "time"
    assert [c["subject"] for c in candidates_at(items, dt(2026, 9, 14, 9, 25))] == ["DeepSource", "B", "Archi week plan"]
    f = calendar_fields(dict(items[0], match="title"))
    assert f["participants"] == [{"name": "Jana Nováková", "source": "calendar"}, {"name": "Petr Svoboda", "source": "calendar"}]
    assert f["calendar"]["source"] == "outlook" and f["calendar"]["start"] == "2026-09-14T08:30"
    assert f["calendar"]["match"] == "title" and f["calendar"]["status"] == "auto"


def test_meeting_link_edits(tmp_path, monkeypatch):
    from datetime import datetime as dt
    from teamsrec_transcribe import pipeline as pl
    from teamsrec_transcribe import outlook
    cfg = Config(out_dir=tmp_path, calendar_outlook=True)
    rec = _make_transcribed(tmp_path)
    rec.sidecar.update(outlook.calendar_fields({"subject": "Týdenní sync", "organizer": "Jana Nováková", "match": "time",
                                                "start": dt(2026, 9, 4, 14, 0), "end": dt(2026, 9, 4, 14, 30), "attendees": ["Jana Nováková"]}))
    rec.sidecar["participants"].append({"name": "Host Ručně", "source": "manual"})
    rec.sidecar["title_source"] = "calendar"
    rec.save_sidecar()
    info = pl.meeting_info(cfg, rec)
    assert info["calendar"]["match"] == "time" and info["calendar"]["status"] == "auto" and info["title_source"] == "calendar"
    assert [p["source"] for p in info["participants"]] == ["calendar", "manual"]
    pl.set_meeting_link(cfg, rec, "confirm")
    assert Recording.load(rec.sidecar_path).sidecar["calendar"]["status"] == "confirmed"
    rec = pl.set_meeting_link(cfg, rec, "detach")
    sc = Recording.load(rec.sidecar_path).sidecar
    assert "calendar" not in sc and sc["participants"] == [{"name": "Host Ručně", "source": "manual"}] and sc["title_source"] == "manual"
    monkeypatch.setattr(outlook, "candidates_for", lambda at, window_s=3600: [
        {"subject": "Plánování Q4", "start": "2026-09-04T14:30", "end": "2026-09-04T15:00", "teams": True,
         "attendees": ["Petr Svoboda"], "organizer": "Petr Svoboda"}])
    rec = pl.set_meeting_link(cfg, rec, "attach", {"subject": "Plánování Q4", "start": "2026-09-04T14:30"})
    assert rec.stem == "2026-09-04_1400_planovani-q4" and rec.title == "Plánování Q4"
    sc = rec.sidecar
    assert sc["calendar"]["status"] == "confirmed" and sc["calendar"]["match"] == "manual"
    assert sc["participants"] == [{"name": "Petr Svoboda", "source": "calendar"}] and sc["title_source"] == "calendar"
    with pytest.raises(Exception):
        pl.set_meeting_link(cfg, rec, "attach", {"subject": "nope", "start": "2026-09-04T14:30"})


def test_config_calendar_flag(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[calendar]\noutlook = true\n', encoding="utf-8")
    assert load_config(p).calendar_outlook is True and Config().calendar_outlook is False


def test_automatic_names_do_not_reach_the_shared_registry(tmp_path, monkeypatch):
    """A name from the video or from a voice match is a guess: no person, no voice print until it is saved."""
    from teamsrec_transcribe.people import People
    from teamsrec_transcribe.pipeline import _voiceprints_step
    from teamsrec_transcribe.voiceprints import Voiceprints
    cfg = Config(out_dir=tmp_path, user_name="Jan Novák", voiceprints=VP_ON)
    rec = _make_transcribed(tmp_path)
    ppl = People.load(tmp_path); ppl.ensure("Petr Svoboda"); ppl.save()
    vp = Voiceprints.load(tmp_path)
    vp.enroll("petr-svoboda", [0.99, 0.1, 0.0], "older-recording", "X"); vp.save()
    emb = {"SPEAKER_00": [1.0, 0.0, 0.0], "Jan Novák": [0.0, 1.0, 0.0], "Jana Nováková": [0.0, 0.0, 1.0]}
    durations = {"SPEAKER_00": 120.0, "Jan Novák": 300.0, "Jana Nováková": 200.0}
    matches = _voiceprints_step(cfg, rec, emb, durations, {"SPEAKER_02": "Jan Novák"}, "m")
    assert list(matches) == ["SPEAKER_00"] and matches["SPEAKER_00"]["person"] == "petr-svoboda"
    assert rec.read_json(rec.speakers_path) == {"SPEAKER_00": "petr-svoboda"}  # shown on the page, undoable
    after = Voiceprints.load(tmp_path)
    assert after.count("petr-svoboda") == 1, "the match itself must not become a print"
    assert after.count("jan-novak") == 0, "not even the user's own microphone label"
    assert [p.id for p in People.load(tmp_path).people] == ["petr-svoboda"], "no people invented from labels"


def test_saving_a_name_confirms_it_and_stores_the_print(tmp_path):
    from teamsrec_transcribe.voiceprints import Voiceprints
    from teamsrec_transcribe.web.review import build_review, save_names
    cfg = Config(out_dir=tmp_path, voiceprints=VP_ON)
    rec = _make_transcribed(tmp_path)
    data = rec.read_json(rec.transcript_path)
    data["segments"][0]["end"] = 40  # long enough for a print
    data["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0, 0.0]}
    data["voice_matches"] = {"SPEAKER_00": {"person": "petr-svoboda", "score": 0.81}}
    rec.write_json(rec.transcript_path, data)
    rec.write_json(rec.speakers_path, {"SPEAKER_00": "petr-svoboda"})
    sp = {s["label"]: s for s in build_review(cfg, rec)["speakers"]}
    assert sp["SPEAKER_00"]["confirmed"] is False  # recognised by voice, nobody has said yes yet
    assert Voiceprints.load(tmp_path).count("petr-svoboda") == 0

    save_names(cfg, rec, {"SPEAKER_00": {"first": "Petr", "last": "Svoboda", "nick": "", "display": ""}})
    assert Voiceprints.load(tmp_path).count("petr-svoboda") == 1  # confirmed -> kept for the next meetings
    sp = {s["label"]: s for s in build_review(cfg, rec)["speakers"]}
    assert sp["SPEAKER_00"]["confirmed"] is True
    assert rec.read_json(rec.transcript_path)["voice_matches"] == {}


def test_rename_recording_moves_folder_and_fixes_references(tmp_path):
    from teamsrec_transcribe.pipeline import rename_recording
    from teamsrec_transcribe.voiceprints import Voiceprints
    from teamsrec_transcribe.web.review import build_review, save_title
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    old_stem, old_dir = rec.stem, rec.dir
    rec.sidecar["screens"] = [{"file": f"{old_stem}_screen1.mp4", "fps": 2, "start_offset_s": 0.0, "titles": []}]
    rec.save_sidecar(); rec.file("_screen1.mp4").write_bytes(b"x")
    (old_dir / f"{old_stem}.summary.md").write_text("<!-- x -->\n# Týdenní sync\n\n## Shrnutí\n", encoding="utf-8")
    (old_dir / f"{old_stem}.summary.claude-opus-5.md").write_text("<!-- x -->\n# Týdenní sync\n", encoding="utf-8")
    vp = Voiceprints.load(tmp_path); vp.enroll("jana", [1.0, 0.0], old_stem, "Jana Nováková"); vp.save()
    assert save_title(cfg, rec, "  Týdenní   sync ") is rec  # unchanged after whitespace normalisation
    new = rename_recording(cfg, rec, "Plánování Q4 / rozpočet")
    assert new.stem == "2026-09-04_1400_planovani-q4-rozpocet" and not old_dir.exists() and new.dir.exists()
    names = sorted(p.name for p in new.dir.iterdir())
    assert names == sorted([f"{new.stem}.json", f"{new.stem}_mix.wav", f"{new.stem}.transcript.json", f"{new.stem}_screen1.mp4",
                            f"{new.stem}.summary.md", f"{new.stem}.summary.claude-opus-5.md"])
    again = Recording.load(new.sidecar_path)
    assert again.title == "Plánování Q4 / rozpočet" and again.sidecar["slug"] == "planovani-q4-rozpocet"
    assert again.mix_path.name == f"{new.stem}_mix.wav" and again.mix_path.exists()
    assert again.sidecar["screens"][0]["file"] == f"{new.stem}_screen1.mp4" and (new.dir / again.sidecar["screens"][0]["file"]).exists()
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


def test_recording_docs_and_read_doc(tmp_path):
    from teamsrec_transcribe.web.review import read_doc, recording_docs
    rec = _make_transcribed(tmp_path)
    assert recording_docs(rec) == {"transcript": None, "summaries": [
        {"file": f"{rec.stem}.summary.md", "label": "hlavní", "provider": "", "model": "", "created": "", "main": True}],
        "transcripts": []}
    (rec.dir / f"{rec.stem}.elevenlabs.txt").write_text("x", encoding="utf-8")
    assert recording_docs(rec)["transcripts"] == [{"file": f"{rec.stem}.elevenlabs.txt", "label": "přepis (elevenlabs)"}]
    (rec.dir / f"{rec.stem}.elevenlabs.txt").unlink()
    rec.file(".txt").write_text("# T\n[00:00:00] Jana: Ahoj\n", encoding="utf-8")
    (rec.dir / f"{rec.stem}.summary.claude-opus-5-5.md").write_text(
        "<!-- teamsrec-transcribe summary | anthropic: claude-opus-5-5 | created: 2026-09-30T10:00:00 | tokens: 1 in / 2 out -->\n# T\n",
        encoding="utf-8")
    d = recording_docs(rec)
    assert d["transcript"] == f"{rec.stem}.txt" and [s["label"] for s in d["summaries"]] == ["hlavní", "claude-opus-5-5"]
    assert d["summaries"][1] == {"file": f"{rec.stem}.summary.claude-opus-5-5.md", "label": "claude-opus-5-5",
                                 "provider": "anthropic", "model": "claude-opus-5-5", "created": "2026-09-30T10:00:00",
                                 "main": False}
    from teamsrec_transcribe.web.review import delete_summary
    for bad in (f"{rec.stem}.txt", f"{rec.stem}.json", "../x.summary.md", f"{rec.stem}.summary.nothere.md"):
        with pytest.raises(Exception):
            delete_summary(rec, bad)
    delete_summary(rec, f"{rec.stem}.summary.claude-opus-5-5.md")
    assert [s["label"] for s in recording_docs(rec)["summaries"]] == ["hlavní"] and rec.file(".txt").exists()
    assert "Jana: Ahoj" in read_doc(rec, f"{rec.stem}.txt")
    for bad in ("../x.md", "other.md", f"{rec.stem}.json", f"{rec.stem}.nothere.md"):
        with pytest.raises(Exception):
            read_doc(rec, bad)


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


def test_review_stays_up_while_a_page_holds_its_event_stream(tmp_path, monkeypatch):
    """A hidden or minimized window throttles its 15 s pings; an open /api/events stream still counts as a page
    (2026-10-02: the desktop window lost its server and did not show new recordings)."""
    import http.client
    import json as _json
    import threading
    import time
    from urllib.parse import urlparse
    from teamsrec_transcribe.web import review as rv
    monkeypatch.setattr(rv, "SSE_KEEPALIVE_S", 0.3)  # a closed page is noticed at the next keepalive write
    cfg = Config(out_dir=tmp_path)
    lock = tmp_path / "lock.json"
    t = threading.Thread(target=lambda: rv.serve(cfg, None, open_browser=False, idle_s=1.0, lock=lock), daemon=True)
    t.start()
    for _ in range(50):
        if lock.exists():
            break
        time.sleep(0.05)
    u = urlparse(_json.loads(lock.read_text(encoding="utf-8"))["url"])
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    assert resp.status == 200
    time.sleep(3.0)                      # three idle periods without a single ping
    assert t.is_alive()
    resp.close(); conn.close()           # the page goes away: the server stops after a failed keepalive + idle
    t.join(timeout=15)
    assert not t.is_alive()


def test_clean_headings_strips_copied_instructions():
    from teamsrec_transcribe.summarize import HEADINGS, clean_headings
    raw = ("## Shrnutí — 5 to 10 sentences: purpose of the meeting.\nText.\n"
           "## Témata: one bullet per topic\n* a\n## Úkoly — a table\n| Kdo | Úkol |\n## Pojmy\n* WFMS\n## Mluvčí\n| x |")
    got = clean_headings(raw, HEADINGS["cs"])
    assert got.splitlines()[0] == "## Shrnutí"
    assert "## Témata\n" in got and "## Úkoly\n" in got and "## Pojmy\n" in got and "## Mluvčí\n" in got
    assert "5 to 10" not in got and "| Kdo | Úkol |" in got
    assert len(HEADINGS["cs"]) == len(HEADINGS["en"]) == 7


def test_long_transcript_is_summarized_in_parts(tmp_path, monkeypatch):
    from teamsrec_transcribe import summarize as sm
    from teamsrec_transcribe.config import SummarizeSettings
    from teamsrec_transcribe.llm import LLMResult
    rec = Recording.load(_make_recording(tmp_path))
    segs = [Segment(i * 10, i * 10 + 9, "Věta číslo %d o rozpočtu a termínech projektu." % i, "Jana Nováková" if i % 2 else "Petr Svoboda")
            for i in range(400)]
    calls = []
    def fake_complete(system, user, settings):
        calls.append((system, user))
        return LLMResult(text="## Shrnutí\nx\n## Témata\n* t\n## Rozhodnutí\n1. r\n## Úkoly\n| a |\n## Otevřené otázky\n-\n## Pojmy\n-\n## Mluvčí\n| – | Jana Nováková | |",
                         model="m", input_tokens=10, output_tokens=5)
    monkeypatch.setattr(sm, "complete", fake_complete)
    out = sm.summarize(rec, segs, {"language": "cs"}, SummarizeSettings(provider="ollama", model="m", ollama_max_ctx=8192))
    assert len(calls) >= 3 and "part 1 of" in calls[0][0] and "You merge partial" in calls[-1][0]
    assert "=== Part 1/" in calls[-1][1] and out.count("## Shrnutí") == 1 and "tokens: " in out
    chunks = sm.split_segments(segs, 3000)
    assert len(chunks) > 1 and sum(len(c) for c in chunks) == 400 and all(c for c in chunks)
    # short transcripts and cloud models stay single-shot
    calls.clear()
    sm.summarize(rec, segs[:20], {"language": "cs"}, SummarizeSettings(provider="ollama", model="m", ollama_max_ctx=8192))
    assert len(calls) == 1
    calls.clear()
    sm.summarize(rec, segs, {"language": "cs"}, SummarizeSettings(provider="anthropic", model="m"))
    assert len(calls) == 1


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


def test_speaker_languages_show_a_second_pass_in_another_language():
    from teamsrec_transcribe.web.review import speaker_languages
    segs = [Segment(0, 20, "Dobrý den", "SPEAKER_00", language="cs"), Segment(20, 30, "Ahoj", "SPEAKER_00", language="cs"),
            Segment(30, 60, "Dobrý deň", "SPEAKER_01"), Segment(60, 62, "ok", "SPEAKER_01", language="en"),
            Segment(62, 80, "Takže", "SPEAKER_02", language="sk"), Segment(80, 90, "So", "SPEAKER_02", language="en")]
    langs = speaker_languages(segs, "sk")
    assert langs["SPEAKER_00"] == {"language": "cs", "own": True, "mixed": False}
    assert langs["SPEAKER_01"] == {"language": "sk", "own": False, "mixed": False}, "2 s of English is not mixed"
    assert langs["SPEAKER_02"] == {"language": "sk", "own": False, "mixed": True}
    assert speaker_languages([Segment(0, 5, "x", "A")], None)["A"] == {"language": "?", "own": False, "mixed": False}


def test_errors_logged_during_a_job_reach_the_page_history(tmp_path):
    import logging
    import time as _time
    from teamsrec_transcribe.web.review import ReviewState
    state = ReviewState(Config(out_dir=tmp_path))
    plog = logging.getLogger("teamsrec_transcribe.pipeline")

    def job():
        plog.error("%s: compare summary %s failed: %s", "s1", "anthropic:claude-x", "model not found")
        plog.info("not an error")

    assert state.run_job("process", job, "zpracováno", "s1")
    for _ in range(100):
        if not state.busy and any(e["job_end"] for e in state.events):
            break
        _time.sleep(0.02)
    texts = [(e["level"], e["text"]) for e in state.events]
    assert ("err", "s1: compare summary anthropic:claude-x failed: model not found") in texts
    assert not any("not an error" in t for _, t in texts)
    assert texts[-1] == ("ok", "zpracováno: s1"), "the job itself still finished"
    plog.error("after the job")  # the handler is gone
    assert not any("after the job" in t for _, t in [(e["level"], e["text"]) for e in state.events])


def test_the_capture_status_file_is_read_and_changes_reach_the_pages(tmp_path):
    import os as _os
    import threading as _threading
    from teamsrec_transcribe.web.review import ReviewState, _capture_watch, capture_status
    f = tmp_path / "teamsrec-capture.json"
    assert capture_status(f) == {"running": False, "recording": False}, "no file: no capture app"
    rec = {"app": "teamsrec-capture", "pid": _os.getpid(), "running": True, "recording": True, "title": "Plánování",
           "stem": "2026-09-30_1827_planovani", "source": "onsite", "started": "2026-09-30T18:27:05"}
    f.write_text(json.dumps(rec), encoding="utf-8")
    assert capture_status(f)["recording"] is True and capture_status(f)["title"] == "Plánování"
    f.write_text(json.dumps({**rec, "pid": 999999}), encoding="utf-8")
    assert capture_status(f) == {"running": False, "recording": False}, "a crashed app shows nothing"

    state = ReviewState(Config(out_dir=tmp_path))
    q = state.subscribe()
    stop = _threading.Event()
    f.write_text(json.dumps(rec), encoding="utf-8")
    t = _threading.Thread(target=_capture_watch, args=(state, f, 0.05, stop), daemon=True)
    t.start()
    first = q.get(timeout=2)
    assert first["type"] == "capture" and first["recording"] is True
    assert state.status()["capture"]["title"] == "Plánování"
    f.write_text(json.dumps({**rec, "recording": False, "title": None, "stem": None}), encoding="utf-8")
    items = [q.get(timeout=2) for _ in range(3)]
    stop.set()
    assert any(i.get("type") == "capture" and i["recording"] is False for i in items)
    ended = [i for i in state.events if "nahrávání skončilo" in i["text"]]
    assert ended and ended[0]["reload"] is True and ended[0]["stem"] == "2026-09-30_1827_planovani"


def test_unassigned_replies_are_given_out_one_by_one_and_are_no_speaker_to_name(tmp_path):
    from teamsrec_transcribe.pipeline import assign_segments, is_finished
    from teamsrec_transcribe.web.review import build_review, list_recordings, unresolved_labels
    rec = _make_transcribed(tmp_path)
    data = rec.read_json(rec.transcript_path)
    data["segments"][0]["speaker"] = None  # "Dobrý den…" at 0 s: no diarization turn
    data["segments"][3]["speaker"] = None  # "Rozpočet je hotový…" at 12 s
    data["speakers"] = ["UNKNOWN", "SPEAKER_00", "SPEAKER_01", "Jana Nováková"]
    rec.write_json(rec.transcript_path, data)
    r = build_review(Config(out_dir=tmp_path), rec)
    un = next(s for s in r["speakers"] if s["unassigned"])
    assert [x["at"] for x in un["replies"]] == ["00:00:00", "00:00:12"], "every unassigned reply, in order"
    assert "UNKNOWN" not in unresolved_labels(rec), "not a speaker to name"
    assert list_recordings(Config(out_dir=tmp_path))[0]["unresolved"] == 2  # SPEAKER_00 and SPEAKER_01 only
    with pytest.raises(RecordingError):
        assign_segments(Config(out_dir=tmp_path), rec, [{"start": 0, "speaker": "SPEAKER_99"}])
    assert assign_segments(Config(out_dir=tmp_path), rec, [{"start": 0.0, "speaker": "SPEAKER_00"}]) == 1
    data = rec.read_json(rec.transcript_path)
    assert data["segments"][0]["speaker"] == "SPEAKER_00" and data["segments"][0]["assigned"] == "manual"
    assert "UNKNOWN" in data["speakers"], "one is still unassigned"
    assert "[00:00:12] ?: Rozpočet je hotový" in rec.file(".txt").read_text(encoding="utf-8")
    assign_segments(Config(out_dir=tmp_path), rec, [{"start": 12, "speaker": "Jana Nováková"}])
    assert "UNKNOWN" not in rec.read_json(rec.transcript_path)["speakers"]
    assert not any(s["unassigned"] for s in build_review(Config(out_dir=tmp_path), rec)["speakers"])


def test_the_mic_takes_an_unassigned_reply_when_the_user_spoke(tmp_path):
    import wave
    import numpy as np
    from teamsrec_transcribe.mic_speakers import apply_mic_track
    sr = 16000
    t = np.arange(sr * 30) / sr
    sig = np.zeros_like(t)
    sig[0:5 * sr] = 0.3 * np.sin(2 * np.pi * 220 * t[0:5 * sr])  # the user, 0-5 s
    wav = tmp_path / "mic.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    segs = [Segment(0, 4, "Ahoj", None), Segment(10, 14, "Dobrý den", None), Segment(20, 30, "x", "SPEAKER_01")]
    apply_mic_track(segs, wav, "Jan Novák")
    assert [s.speaker for s in segs] == ["Jan Novák", None, "SPEAKER_01"], "only where the mic was on"


def test_background_jobs_queue_instead_of_refusing_and_say_what_runs(tmp_path):
    import threading as _threading
    import time as _time
    from teamsrec_transcribe.web.review import ReviewState
    state = ReviewState(Config(out_dir=tmp_path))
    gate, order = _threading.Event(), []

    def first():
        gate.wait(5); order.append("first")

    a = state.run_job("process", first, "zpracováno", "s1", start="s1: zpracování spuštěno")
    b = state.run_job("summary", lambda: order.append("second"), "zápis přegenerován", "s2", start="s2: zápis se generuje")
    assert a == (1, 1) and b == (2, 2), "the second waits, it is not refused"
    for _ in range(100):
        if state.jobs_state()["current"]:
            break
        _time.sleep(0.01)
    js = state.jobs_state()
    assert js["current"]["id"] == 1 and js["current"]["text"] == "s1: zpracování spuštěno"
    assert [q["id"] for q in js["queue"]] == [2]
    assert state.status()["busy"] is True and state.status()["jobs"]["queue"][0]["stem"] == "s2"
    gate.set()
    for _ in range(200):
        if not state.busy:
            break
        _time.sleep(0.01)
    assert order == ["first", "second"], "in order, one at a time"
    ends = [(e["job"], e["text"]) for e in state.events if e["job_end"]]
    assert ends == [(1, "zpracováno: s1"), (2, "zápis přegenerován: s2")]
    js = state.jobs_state()
    assert (js["current"], js["queue"], js["held"], js["asking"]) == (None, [], False, False)


def test_a_recording_stops_the_running_job_which_resumes_after_it(tmp_path, monkeypatch, _no_real_ollama_unload):
    """The GPU must not break the recorded sound: a job's child process is stopped when teamsrec-capture starts
    recording (when_recording = ask, and no page to ask: stop), goes back to the front of the queue, and runs
    again once the recording has ended."""
    import subprocess as _sp
    import sys as _sys
    import time as _time
    from teamsrec_transcribe.web import review as rv
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv)
        code = "import time; time.sleep(30)" if len(runs) == 1 else "pass"  # the first run is long, the rerun short
        p = _sp.Popen([_sys.executable, "-c", code])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(rv, "run_cli", cli)
    state = rv.ReviewState(Config(out_dir=tmp_path))
    job, _ = state.run_job("process", ["run-job", "process", "s1"], "zpracováno", "s1", start="s1: zpracování spuštěno")
    for _ in range(200):
        if state.running and state.running.get("proc"):
            break
        _time.sleep(0.02)
    state.set_capture({"running": True, "recording": True, "title": "Porada", "stem": "s2"})
    for _ in range(300):
        js = state.jobs_state()
        if not js["current"] and js["queue"]:
            break
        _time.sleep(0.02)
    js = state.jobs_state()
    assert js["held"] is True and [q["id"] for q in js["queue"]] == [job], "stopped and back in the queue"
    assert any("přerušeno kvůli nahrávání" in e["text"] for e in state.events)
    assert not any(e["job_end"] for e in state.events), "a stopped job did not fail"
    _time.sleep(0.3)
    assert _no_real_ollama_unload == ["gemma4:31b"], "the summary model leaves the GPU for the meeting"
    assert len(runs) == 1, "nothing starts while recording"
    state.set_capture({"running": True, "recording": False})
    for _ in range(300):
        if not state.busy:
            break
        _time.sleep(0.02)
    assert len(runs) == 2, "it ran again after the recording"
    ends = [e for e in state.events if e["job_end"]]
    assert len(ends) == 1 and ends[0]["job"] == job and ends[0]["level"] == "ok"


def test_a_waiting_job_can_go_next_or_stop_the_running_one(tmp_path, monkeypatch):
    """"jako další" moves a waiting job to the front; "hned" also stops the running job, which goes right behind it
    and starts over."""
    import subprocess as _sp
    import sys as _sys
    import time as _time
    import pytest as _pytest
    from teamsrec_transcribe.web import review as rv
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv[-1])
        long = argv[-1] == "a" and runs.count("a") == 1  # the first run of "a" is long: it gets stopped
        p = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)" if long else "pass"])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(rv, "run_cli", cli)
    state = rv.ReviewState(Config(out_dir=tmp_path))
    ids = {s: state.run_job("process", ["run-job", "process", s], "zpracováno", s)[0] for s in ("a", "b", "c")}
    for _ in range(200):
        if state.running and state.running.get("proc"):
            break
        _time.sleep(0.02)
    assert [q["stem"] for q in state.jobs_state()["queue"]] == ["b", "c"]
    assert "jako další" in state.move_job(ids["c"])
    assert [q["stem"] for q in state.jobs_state()["queue"]] == ["c", "b"]
    with _pytest.raises(ValueError):
        state.move_job(ids["a"])  # running, not waiting
    assert "pokračuje po něm" in state.move_job(ids["c"], now=True)
    for _ in range(300):
        if not state.busy:
            break
        _time.sleep(0.02)
    assert runs == ["a", "c", "a", "b"], "c at once, the stopped a right behind it, then b"
    assert any("přerušeno kvůli přednostnímu zpracování" in e["text"] for e in state.events)
    ends = [e for e in state.events if e["job_end"]]
    assert len(ends) == 3 and all(e["level"] == "ok" for e in ends), "the stopped run is not a failure"


def test_a_job_asked_for_during_a_recording_waits_for_its_end(tmp_path, monkeypatch):
    """Nothing ran when the recording started, so nothing was stopped – but a job asked for during it must not
    start either (the GPU would break the recorded sound); with when_recording = continue it runs."""
    import time as _time
    from teamsrec_transcribe.web import review as rv
    runs = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: runs.append(argv[-1]))
    state = rv.ReviewState(Config(out_dir=tmp_path))
    state.set_capture({"running": True, "recording": True, "title": "Porada"})
    assert state.jobs_state()["held"] is True
    state.run_job("process", ["run-job", "process", "s1"], "zpracováno", "s1", start="s1: zpracování spuštěno")
    _time.sleep(0.3)
    assert runs == [] and [q["stem"] for q in state.jobs_state()["queue"]] == ["s1"]
    assert any("čeká na konec nahrávání" in e["text"] for e in state.events)
    state.set_capture({"running": True, "recording": False})
    for _ in range(200):
        if not state.busy:
            break
        _time.sleep(0.02)
    assert runs == ["s1"], "it ran after the recording"

    cont = rv.ReviewState(Config(out_dir=tmp_path, transcribe=TranscribeSettings(when_recording="continue")))
    cont.set_capture({"running": True, "recording": True, "title": "Porada"})
    cont.run_job("process", ["run-job", "process", "s2"], "zpracováno", "s2")
    for _ in range(200):
        if not cont.busy:
            break
        _time.sleep(0.02)
    assert runs == ["s1", "s2"], "continue: it runs during the recording"


def test_cancel_drops_a_waiting_job_and_stops_the_running_one_for_good(tmp_path, monkeypatch):
    import subprocess as _sp
    import sys as _sys
    import time as _time
    from teamsrec_transcribe.web import review as rv
    runs = []

    def cli(argv, on_proc, on_line):
        runs.append(argv[-1])
        p = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)" if argv[-1] == "a" else "pass"])
        on_proc(p)
        if p.wait() != 0:
            raise RuntimeError("killed")
    monkeypatch.setattr(rv, "run_cli", cli)
    state = rv.ReviewState(Config(out_dir=tmp_path))
    ids = {s: state.run_job("process", ["run-job", "process", s], "zpracováno", s)[0] for s in ("a", "b", "c")}
    for _ in range(200):
        if state.running and state.running.get("proc"):
            break
        _time.sleep(0.02)
    assert "vyřazeno z fronty" in state.cancel_job(ids["b"])
    assert [q["stem"] for q in state.jobs_state()["queue"]] == ["c"]
    assert "ruší" in state.cancel_job(ids["a"])
    for _ in range(300):
        if not state.busy:
            break
        _time.sleep(0.02)
    assert runs == ["a", "c"], "b never ran, a was not run again"
    ends = {e["job"]: e for e in state.events if e["job_end"]}
    assert "zrušeno" in ends[ids["a"]]["text"] and ends[ids["a"]]["level"] == "info", "cancelled is not a failure"
    assert "zrušeno" in ends[ids["b"]]["text"] and ends[ids["c"]]["level"] == "ok"
    import pytest as _pytest
    with _pytest.raises(ValueError):
        state.cancel_job(ids["c"])  # finished: nothing to cancel


def test_with_a_page_open_the_recording_asks_first(tmp_path, monkeypatch):
    from teamsrec_transcribe.web import review as rv
    import threading as _threading
    gate = _threading.Event()
    state = rv.ReviewState(Config(out_dir=tmp_path))
    q = state.subscribe()  # a page is open
    state.run_job("summary", lambda: gate.wait(5), "zápis přegenerován", "s1")
    state.set_capture({"running": True, "recording": True, "title": "Porada"})
    assert state.jobs_state()["asking"] is True and state.held is False, "asks, does not stop yet"
    state.keep_running()
    assert state.jobs_state()["asking"] is False and state.held is False
    gate.set()
    state.unsubscribe(q)


def test_nearest_voice_group_is_a_hint_from_0_45():
    from teamsrec_transcribe.web.review import GROUP_HINT_MIN, _nearest_group
    emb = {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.8, 0.6], "SPEAKER_02": [0.0, 1.0], "SPEAKER_03": [0.7, 0.71]}
    secs = {"SPEAKER_00": 60.0, "SPEAKER_01": 40.0, "SPEAKER_02": 30.0}  # SPEAKER_03 has no replies left (merged)
    assert _nearest_group("SPEAKER_00", emb, secs) == {"label": "SPEAKER_01", "score": 0.8}
    assert _nearest_group("SPEAKER_02", emb, secs) == {"label": "SPEAKER_01", "score": 0.6}
    assert _nearest_group("SPEAKER_02", {"SPEAKER_02": [0.0, 1.0], "SPEAKER_00": [1.0, 0.05]}, secs) is None
    assert GROUP_HINT_MIN == 0.45


def test_a_recording_is_not_recognised_by_its_own_prints(tmp_path):
    from teamsrec_transcribe.voiceprints import Voiceprints
    vp = Voiceprints.load(tmp_path)
    vp.enroll("jana", [1.0, 0.0], "2026-10-01_0900_a", "SPEAKER_00")
    vp.enroll("petr", [0.6, 0.8], "2026-09-01_0900_b", "SPEAKER_01")
    assert vp.scores([1.0, 0.0])[0][0] == "jana"
    ranked = vp.scores([1.0, 0.0], exclude_stem="2026-10-01_0900_a")  # Jana's only print is from this recording
    assert [pid for pid, _ in ranked] == ["petr"]
    assert vp.recognize({"SPEAKER_00": [1.0, 0.0]}, 0.5, 0.1, exclude_stem="2026-10-01_0900_a") == {"SPEAKER_00": ("petr", 0.6)}


def test_replies_of_a_mixed_group_move_to_another_speaker_a_new_one_or_unassigned(tmp_path):
    from teamsrec_transcribe.pipeline import NEW_SPEAKER, assign_segments, speaker_replies
    cfg = Config(out_dir=tmp_path)
    rec = _make_transcribed(tmp_path)
    assert [r["start"] for r in speaker_replies(rec, "SPEAKER_00")] == [0, 20]
    n = assign_segments(cfg, rec, [{"start": 0, "from": "SPEAKER_00", "speaker": "SPEAKER_01"},
                                   {"start": 20, "from": "SPEAKER_00", "speaker": NEW_SPEAKER},
                                   {"start": 4, "from": "SPEAKER_01", "speaker": NEW_SPEAKER},  # the same new one
                                   {"start": 12, "from": "Jana Nováková", "speaker": "UNKNOWN"}])
    assert n == 4
    data = rec.read_json(rec.transcript_path)
    who = {s["start"]: s["speaker"] for s in data["segments"]}
    assert who == {0: "SPEAKER_01", 4: "SPEAKER_02", 5: "SPEAKER_01", 12: None, 20: "SPEAKER_02"}
    assert data["speakers"] == ["SPEAKER_01", "SPEAKER_02", "UNKNOWN"], "emptied groups go, the new one is there"
    assert [s.get("assigned") for s in data["segments"]] == ["manual", "manual", None, None, "manual"]
    assert "SPEAKER_02" in rec.file(".txt").read_text(encoding="utf-8"), "exports regenerated"
    with pytest.raises(RecordingError):
        assign_segments(cfg, rec, [{"start": 5, "from": "SPEAKER_01", "speaker": "SPEAKER_77"}])


def test_cloud_only_run_uses_cloud_services_and_needs_their_keys(tmp_path, monkeypatch):
    from teamsrec_transcribe import fasttrack, settings
    from teamsrec_transcribe.config import SummarizeSettings
    from teamsrec_transcribe.fasttrack import cloud_only
    from teamsrec_transcribe.web import review as rv
    cfg = Config(out_dir=tmp_path, summarize=SummarizeSettings(compare=("ollama:qwen3:8b", "anthropic:claude-opus-5-5")))
    fast = cloud_only(cfg)
    assert (fast.transcribe.provider, fast.summarize.provider, fast.summarize.model) == ("elevenlabs", "anthropic", "claude-opus-5-5")
    assert fast.summarize.compare == ("anthropic:claude-opus-5-5",), "nothing on the local GPU"
    assert fast.video.enabled is False and cfg.transcribe.provider == "whisperx", "the config itself stays"
    keys = {"anthropic": "k"}
    monkeypatch.setattr(settings, "get_secret", lambda name: keys.get(name, ""))
    ready = fasttrack.cloud_ready(cfg)
    assert not ready["ok"] and "elevenlabs" in ready["missing"]
    rec = _make_transcribed(tmp_path)
    state = rv.ReviewState(cfg)
    with pytest.raises(ValueError, match="klíče"):
        state.run_process(rec, cloud=True)
    keys["elevenlabs"] = "k"
    calls = []
    monkeypatch.setattr(rv, "run_cli", lambda argv, on_proc, on_line: calls.append(argv))
    state.run_process(rec, force=True, cloud=True)
    import time as _time
    for _ in range(100):
        if calls:
            break
        _time.sleep(0.02)
    assert calls[0][-3:] == [rec.stem, "--force", "--cloud"]


def test_run_job_cloud_flag_switches_the_services(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from teamsrec_transcribe import cli, pipeline as pl
    rec = _make_transcribed(tmp_path)
    seen = []
    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False: seen.append(
        (cfg.transcribe.provider, cfg.summarize.provider, cfg.video.enabled)))
    res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem, "--cloud"])
    assert res.exit_code == 0, res.output
    assert seen == [("elevenlabs", "anthropic", False)]


def test_fasttrack_voices_come_from_the_local_voice_that_overlaps_each_cloud_group(tmp_path, monkeypatch):
    """Fast-track post-processing: the cloud keeps the groups, the local diarization lends their voices."""
    from teamsrec_transcribe import fasttrack, pipeline as pl
    from teamsrec_transcribe.providers import whisperx_provider as wp
    segs = [{"start": 0, "end": 10, "text": "a", "speaker": "speaker_0"},
            {"start": 10, "end": 20, "text": "b", "speaker": "speaker_1"},
            {"start": 20, "end": 30, "text": "c", "speaker": "speaker_1"}]
    turns = [(0, 10, "SPEAKER_00"), (10, 22, "SPEAKER_01"), (22, 30, "SPEAKER_02")]  # speaker_1 is two local voices
    emb = {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0], "SPEAKER_02": [0.6, 0.8]}
    voices, mixed = fasttrack.group_voices(segs, turns, emb)
    assert voices == {"speaker_0": [1.0, 0.0], "speaker_1": [0.0, 1.0]}
    assert mixed == {"speaker_1": {"voices": ["SPEAKER_01", "SPEAKER_02"], "share": 0.4}}

    rec = _make_transcribed(tmp_path)
    data = rec.read_json(rec.transcript_path)
    assert not fasttrack.needs_voices(data), "a local transcript has its own voices"
    data.update(provider="elevenlabs", segments=segs, speakers=["speaker_0", "speaker_1"])
    rec.write_json(rec.transcript_path, data)
    assert fasttrack.needs_voices(data)
    pytest.importorskip("whisperx")  # add_voices loads the audio with whisperx (the [whisperx] extra)
    import pandas as pd
    monkeypatch.setattr(wp, "diarize_audio", lambda wav, model, device: (
        pd.DataFrame([{"start": a, "end": b, "speaker": v} for a, b, v in turns]), emb))
    seen = []
    monkeypatch.setattr(pl, "recognize_voices", lambda cfg, r: seen.append(r.stem) or {})
    from teamsrec_transcribe.config import VoiceprintSettings
    out = fasttrack.add_voices(Config(out_dir=tmp_path, voiceprints=VoiceprintSettings(enabled=True)), rec)
    assert out == {"groups": 2, "matches": 0, "mixed": ["speaker_1"]}
    stored = rec.read_json(rec.transcript_path)
    assert stored["speaker_embeddings"]["speaker_0"] == [1.0, 0.0] and not fasttrack.needs_voices(stored)
    assert seen == [rec.stem], "the standard voice recognition ran"


def test_a_job_records_how_long_each_part_took(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from teamsrec_transcribe import cli, pipeline as pl, timings
    from teamsrec_transcribe.web.review import build_review
    rec = _make_transcribed(tmp_path)

    def fake_process(cfg, r, force=False):
        with timings.step("přepis (fake)", {"transcribe_s": 1.5, "align_s": 0.5, "note": "x"}):
            pass
        with timings.step("zápis (ollama m)"):
            pass
    monkeypatch.setattr(pl, "do_process", fake_process)
    for _ in range(2):
        res = CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem, "--force"])
        assert res.exit_code == 0, res.output
    runs = timings.latest(rec, 5)
    assert len(runs) == 2 and runs[0]["job"] == "process --force" and runs[0]["ok"] is True
    assert [s["step"] for s in runs[0]["steps"]] == ["přepis (fake)", "zápis (ollama m)"]
    assert runs[0]["steps"][0]["parts"] == {"transcribe_s": 1.5, "align_s": 0.5}, "numbers only"
    assert build_review(Config(out_dir=tmp_path), rec)["timings"][0]["job"] == "process --force"

    monkeypatch.setattr(pl, "do_process", lambda cfg, r, force=False: (_ for _ in ()).throw(RuntimeError("boom")))
    CliRunner().invoke(cli.app, ["--out-dir", str(tmp_path), "run-job", "process", rec.stem])
    assert timings.latest(rec, 5)[0]["ok"] is False, "a failed job is recorded too"
    with timings.step("outside a job"):  # a CLI command: measured and logged, nothing written
        pass
    assert len(timings.latest(rec, 10)) == 3


def test_a_cloud_request_that_hangs_is_tried_once_more_then_fails(tmp_path, monkeypatch):
    import requests
    from teamsrec_transcribe.providers import cloud
    from teamsrec_transcribe.providers.base import ProviderError
    up = tmp_path / "a.webm"
    up.write_bytes(b"x")
    calls = []

    def hang(url, **kw):
        calls.append(kw["timeout"])
        raise requests.Timeout("read timed out")
    monkeypatch.setattr(requests, "post", hang)
    with pytest.raises(ProviderError, match="did not answer"):
        cloud.post_with_retry("https://x", headers={}, data={}, upload=up, timeout=(30.0, 400.0), vendor="ElevenLabs")
    assert calls == [(30.0, 400.0), (30.0, 400.0)]
    assert cloud.request_timeout(tmp_path / "missing.wav") == (30.0, 1200.0)
