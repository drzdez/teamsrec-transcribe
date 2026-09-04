"""Lab runner: transcribe one audio file with WhisperX under a named configuration.

Usage:
  python run_whisperx.py <audio> --name <config-name> [--model large-v3] [--compute float16]
        [--batch 16] [--no-diarize] [--diarize-model pyannote/speaker-diarization-community-1]
        [--min-speakers N] [--max-speakers N] [--no-align] [--vad-onset 0.5] [--vad-offset 0.363]
        [--initial-prompt "..."]

Writes to out/<name>/: transcript.json (raw whisperx result), transcript.txt (speaker-labelled),
transcript.srt, and run.json (config + timings).
"""
import argparse
import gc
import json
import time
from datetime import timedelta
from pathlib import Path

import torch
import whisperx


def fmt_ts(sec: float) -> str:
    td = timedelta(seconds=sec)
    h, rem = divmod(td.seconds + td.days * 86400, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{td.microseconds // 1000:03d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--compute", default="float16")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--language", default="cs")
    ap.add_argument("--no-align", action="store_true")
    ap.add_argument("--no-diarize", action="store_true")
    ap.add_argument("--diarize-model", default="pyannote/speaker-diarization-community-1")
    ap.add_argument("--min-speakers", type=int)
    ap.add_argument("--max-speakers", type=int)
    ap.add_argument("--vad-onset", type=float, default=0.500)
    ap.add_argument("--vad-offset", type=float, default=0.363)
    ap.add_argument("--initial-prompt")
    ap.add_argument("--beam", type=int, default=5)
    args = ap.parse_args()
    if args.language in ("", "auto"):
        args.language = None  # whisperx detects on the first 30 s

    out = Path(__file__).parent / "out" / args.name
    out.mkdir(parents=True, exist_ok=True)
    device = "cuda"
    timings = {}

    t = time.time()
    audio = whisperx.load_audio(args.audio)
    duration = len(audio) / 16000
    timings["load_audio_s"] = round(time.time() - t, 1)

    asr_options = {"beam_size": args.beam}
    if args.initial_prompt:
        asr_options["initial_prompt"] = args.initial_prompt
    t = time.time()
    model = whisperx.load_model(args.model, device, compute_type=args.compute, language=args.language,
                                asr_options=asr_options,
                                vad_options={"vad_onset": args.vad_onset, "vad_offset": args.vad_offset})
    timings["load_model_s"] = round(time.time() - t, 1)

    t = time.time()
    result = model.transcribe(audio, batch_size=args.batch, language=args.language)
    timings["transcribe_s"] = round(time.time() - t, 1)
    del model; gc.collect(); torch.cuda.empty_cache()

    if not args.no_align:
        t = time.time()
        lang = args.language or result["language"]
        result["language"] = lang
        align_model, meta = whisperx.load_align_model(language_code=lang, device=device)
        result = whisperx.align(result["segments"], align_model, meta, audio, device, return_char_alignments=False)
        timings["align_s"] = round(time.time() - t, 1)
        del align_model; gc.collect(); torch.cuda.empty_cache()

    if not args.no_diarize:
        t = time.time()
        from whisperx.diarize import DiarizationPipeline
        dia = DiarizationPipeline(model_name=args.diarize_model, device=device)
        kw = {}
        if args.min_speakers: kw["min_speakers"] = args.min_speakers
        if args.max_speakers: kw["max_speakers"] = args.max_speakers
        dia_segments = dia(audio, **kw)
        result = whisperx.assign_word_speakers(dia_segments, result)
        timings["diarize_s"] = round(time.time() - t, 1)
        result["speakers"] = sorted({s.get("speaker", "UNKNOWN") for s in result["segments"]})

    timings["audio_duration_s"] = round(duration, 1)
    timings["total_s"] = round(sum(v for k, v in timings.items() if k.endswith("_s") and k != "audio_duration_s"), 1)
    timings["realtime_factor"] = round(duration / timings["total_s"], 1)

    (out / "transcript.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    lines, srt = [], []
    for i, seg in enumerate(result["segments"], 1):
        spk = seg.get("speaker", "")
        text = seg["text"].strip()
        lines.append(f"[{fmt_ts(seg['start'])[:8]}] {spk + ': ' if spk else ''}{text}")
        srt.append(f"{i}\n{fmt_ts(seg['start'])} --> {fmt_ts(seg['end'])}\n{spk + ': ' if spk else ''}{text}\n")
    (out / "transcript.txt").write_text("\n".join(lines), encoding="utf-8")
    (out / "transcript.srt").write_text("\n".join(srt), encoding="utf-8")
    (out / "run.json").write_text(json.dumps({"args": vars(args), "timings": timings,
                                              "segments": len(result["segments"]),
                                              "speakers": result.get("speakers")}, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    print(json.dumps(timings, indent=1))
    print("segments:", len(result["segments"]), "speakers:", result.get("speakers"))


if __name__ == "__main__":
    main()
