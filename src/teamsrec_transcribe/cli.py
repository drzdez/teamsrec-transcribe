"""teamsrec-transcribe command line."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .config import DEFAULT_TOML, Config, default_config_path, load_config, with_overrides
from .media import ffmpeg_available

app = typer.Typer(help="Transcription, speaker attribution and exports for teamsrec recordings.",
                  no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
log = logging.getLogger("teamsrec_transcribe")


def _errors(fn):
    """Turn expected failures into a one-line error and exit code 1 instead of a traceback."""
    import functools
    from .media import MediaError
    from .providers.base import ProviderError
    from .recording import RecordingError

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (RecordingError, MediaError, ProviderError, RuntimeError) as e:
            typer.secho(f"error: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
    return wrapper


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "urllib3", "huggingface_hub", "torch", "pyannote", "speechbrain"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _cfg(ctx: typer.Context) -> Config:
    return ctx.obj


@app.callback()
def main(ctx: typer.Context,
         config: Optional[Path] = typer.Option(None, "--config", "-c", help="TOML config file"),
         out_dir: Optional[Path] = typer.Option(None, "--out-dir", "-o", help="recordings folder"),
         verbose: bool = typer.Option(False, "--verbose", "-v")):
    _setup_logging(verbose)
    cfg = load_config(config)
    ctx.obj = with_overrides(cfg, out_dir=out_dir.expanduser() if out_dir else None)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise typer.BadParameter("use YYYY-MM-DD HH:MM")


def _names(value: Optional[str]) -> Optional[list[str]]:
    return [n.strip() for n in value.split(",") if n.strip()] if value else None


@app.command("import")
@_errors
def import_cmd(ctx: typer.Context, file: Path = typer.Argument(..., exists=True, dir_okay=False),
               title: Optional[str] = typer.Option(None, help="meeting title (default: from file name)"),
               start: Optional[str] = typer.Option(None, help="start time YYYY-MM-DD HH:MM (default: from file)"),
               language: Optional[str] = typer.Option(None, help="expected language, e.g. cs, sk, en"),
               participants: Optional[str] = typer.Option(None, help="comma-separated names (helps OCR and the ASR prompt)"),
               video: Optional[bool] = typer.Option(None, "--video/--no-video", help="analyse Teams video for speakers"),
               force: bool = typer.Option(False, help="re-import even if it exists")):
    """Bring an audio/video file into the recordings folder (creates the sidecar)."""
    from .pipeline import do_import
    _require_ffmpeg()
    rec = do_import(_cfg(ctx), file, title=title, start=_parse_dt(start), language=language,
                    participants=_names(participants), video=video, force=force)
    typer.echo(rec.stem_path)


@app.command()
@_errors
def video(ctx: typer.Context, target: str = typer.Argument(..., help="stem or any file of the recording"),
          participants: Optional[str] = typer.Option(None, help="comma-separated names for OCR matching")):
    """(Re)run the Teams video analysis for an imported recording."""
    from .pipeline import do_video, resolve_target
    _require_ffmpeg()
    rec = resolve_target(_cfg(ctx), target, allow_import=False)
    if participants:
        rec.sidecar["participants"] = [{"name": n} for n in _names(participants) or []]
        rec.save_sidecar()
    tl = do_video(_cfg(ctx), rec)
    typer.echo(rec.speakers_video_path if tl else "no speaker labels found in video")


@app.command()
@_errors
def transcribe(ctx: typer.Context, target: str = typer.Argument(..., help="stem, recording file, or an ad-hoc media file"),
               provider: Optional[str] = typer.Option(None),
               language: Optional[str] = typer.Option(None, help="auto | cs | sk | en"),
               model: Optional[str] = typer.Option(None),
               compute_type: Optional[str] = typer.Option(None, help="float16 | int8_float16 | int8"),
               batch_size: Optional[int] = typer.Option(None),
               diarize: Optional[bool] = typer.Option(None, "--diarize/--no-diarize"),
               align: Optional[bool] = typer.Option(None, "--align/--no-align"),
               device: Optional[str] = typer.Option(None, help="cuda | cpu"),
               force: bool = typer.Option(False, help="overwrite an existing transcript")):
    """Transcribe a recording (imports ad-hoc files first) and write .transcript.json + .txt/.srt."""
    from .pipeline import do_export, do_transcribe, resolve_target
    _require_ffmpeg()
    cfg = with_overrides(_cfg(ctx), **{"transcribe.provider": provider, "transcribe.language": language,
                                       "transcribe.model": model, "transcribe.compute_type": compute_type,
                                       "transcribe.batch_size": batch_size, "transcribe.align": align,
                                       "transcribe.device": device})
    rec = resolve_target(cfg, target)
    do_transcribe(cfg, rec, force=force, diarize=diarize)
    for p in do_export(cfg, rec):
        typer.echo(p)


@app.command()
@_errors
def export(ctx: typer.Context, target: str, txt: bool = typer.Option(True), srt: bool = typer.Option(True)):
    """Regenerate .txt / .srt from the transcript (applies speakers.json names)."""
    from .pipeline import do_export, resolve_target
    rec = resolve_target(_cfg(ctx), target, allow_import=False)
    for p in do_export(_cfg(ctx), rec, txt=txt, srt=srt):
        typer.echo(p)


@app.command("label-speakers")
@_errors
def label_speakers(ctx: typer.Context, target: str):
    """Interactively map SPEAKER_XX labels to names (writes .speakers.json, regenerates exports)."""
    from .pipeline import do_export, load_segments, resolve_target
    cfg = _cfg(ctx)
    rec = resolve_target(cfg, target, allow_import=False)
    data, segs = load_segments(rec)
    existing = rec.read_json(rec.speakers_path) if rec.speakers_path.exists() else {}
    labels = [s for s in data.get("speakers", []) if s.startswith("SPEAKER_") or s == "UNKNOWN"]
    if not labels:
        typer.echo("all speakers already have names")
        return
    for label in labels:
        sample = next((s.text for s in segs if s.speaker == label and len(s.text) > 40), "")
        secs = sum(s.end - s.start for s in segs if s.speaker == label)
        typer.echo(f"\n{label}  ({secs/60:.1f} min)  e.g.: {sample[:120]}")
        name = typer.prompt("  name (empty = keep)", default=existing.get(label, ""), show_default=True)
        if name.strip():
            existing[label] = name.strip()
    rec.write_json(rec.speakers_path, existing)
    for p in do_export(cfg, rec):
        typer.echo(p)


@app.command()
@_errors
def summarize(ctx: typer.Context, target: str,
              model: Optional[str] = typer.Option(None, help="Claude model id"),
              language: Optional[str] = typer.Option(None, help="language of the minutes, e.g. cs, en"),
              force: bool = typer.Option(False, help="overwrite an existing summary")):
    """Write meeting minutes (summary, decisions, action items) to .summary.md via the Claude API."""
    from .pipeline import do_summarize, resolve_target
    cfg = with_overrides(_cfg(ctx), **{"summarize.model": model, "summarize.language": language})
    rec = resolve_target(cfg, target, allow_import=False)
    typer.echo(do_summarize(cfg, rec, force=force))


@app.command()
@_errors
def process(ctx: typer.Context, target: Optional[str] = typer.Argument(None, help="stem, file, or `latest`; default: inbox + all pending"),
            latest: bool = typer.Option(False, "--latest", help="import the inbox, then process only the newest recording"),
            force: bool = typer.Option(False)):
    """Import the inbox, then transcribe + export + summarize every recording that is missing them (or just the target)."""
    from .pipeline import do_process, do_process_inbox, pending_recordings, resolve_target
    from .recording import latest_recording
    _require_ffmpeg()
    cfg = _cfg(ctx)
    if latest and not target:
        for r in do_process_inbox(cfg):
            typer.echo(f"imported {r.stem}")
        target = latest_recording(cfg.out_dir).stem
        typer.echo(f"latest: {target}")
    if target:
        rec = resolve_target(cfg, target)
        do_process(cfg, rec, force=force)
        typer.echo(rec.stem_path)
        return
    imported = do_process_inbox(cfg)
    for r in imported:
        typer.echo(f"imported {r.stem}")
    todo = pending_recordings(cfg)
    if not todo:
        typer.echo("nothing to do")
    for rec in todo:
        try:
            do_process(cfg, rec, force=force)
            typer.echo(f"done {rec.stem}")
        except Exception as e:
            log.error("%s failed: %s", rec.stem, e)


@app.command("list")
def list_cmd(ctx: typer.Context):
    """List recordings and what has been produced for each."""
    from .recording import iter_recordings
    for rec in iter_recordings(_cfg(ctx).out_dir):
        flags = "".join(f for f, p in (("A", rec.mix_path and rec.mix_path.exists()), ("V", rec.speakers_video_path.exists()),
                                       ("T", rec.transcript_path.exists()), ("S", rec.summary_path.exists())) if p)
        typer.echo(f"{rec.stem:60s} {rec.source:8s} {flags:4s} {rec.title}")


@app.command("config")
def config_cmd(ctx: typer.Context, init: bool = typer.Option(False, help="write a default config file if none exists")):
    """Show the effective configuration (or create the default file with --init)."""
    cfg = _cfg(ctx)
    path = default_config_path()
    if init:
        if path.exists():
            typer.echo(f"exists: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_TOML.format(out_dir=str(cfg.out_dir).replace("\\", "/")), encoding="utf-8")
            typer.echo(f"written: {path}")
        return
    typer.echo(f"config file: {cfg.source_path or f'(none, defaults; would read {path})'}")
    typer.echo(f"out_dir:     {cfg.out_dir}")
    typer.echo(f"transcribe:  {cfg.transcribe}")
    typer.echo(f"video:       {cfg.video}")
    typer.echo(f"ffmpeg:      {'ok' if ffmpeg_available() else 'NOT FOUND'}")
    typer.echo(f"version:     {__version__}")


def _require_ffmpeg() -> None:
    if not ffmpeg_available():
        raise typer.BadParameter("ffmpeg/ffprobe not found on PATH (winget install Gyan.FFmpeg, or set TEAMSREC_FFMPEG_DIR)")


if __name__ == "__main__":
    app()
