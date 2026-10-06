"""Command line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

from . import __version__, ff
from .manifest import Manifest, effective_rotation, is_trimmed
from .util import human_bytes, human_seconds, setup_logging

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Compress, re-orient and trim Pixel videos (PXL_*.mp4) without visible quality loss.")
console = Console()


def _version(value: bool) -> None:
    if value:
        console.print(f"pxlsqueeze {__version__}")
        raise typer.Exit()


@app.callback()
def main(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True,
                                      help="Show the installed version and exit.")) -> None:
    """Compress, re-orient and trim Pixel videos (PXL_*.mp4) without visible quality loss."""

DirArg = typer.Argument(..., exists=True, file_okay=False, dir_okay=True, resolve_path=True,
                        help="Folder containing PXL_*.mp4 files.")
FFmpegOpt = typer.Option(None, "--ffmpeg", help="Path to ffmpeg (or its folder) if it isn't on PATH.")


ShortOpt = typer.Option(None, "--short-seconds",
                        help="Clips this long or shorter are suggested for the Trash (default 3).")


def _open(directory: Path, ffmpeg: Optional[str], codec: Optional[str] = None, crf: Optional[int] = None,
          preset: Optional[str] = None, include_variants: bool = False, verbose: bool = False,
          short_seconds: Optional[float] = None) -> Manifest:
    try:
        ff.set_binaries(ffmpeg)
        ff.check_version()
    except ff.FFmpegError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e
    import sys

    from .lock import FolderBusy, acquire
    from .pipeline import recover_false_positives

    command = next((a for a in sys.argv[1:] if not a.startswith("-")), "run")  # the subcommand
    try:
        acquire(directory, command)
    except FolderBusy as e:
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(3) from e
    setup_logging(directory, verbose)
    m = Manifest(directory)
    if codec:
        m.settings["codec"] = codec
    c = m.settings["codec"]
    if crf is not None:
        m.settings["crf"][c] = crf
    if preset:
        m.settings["preset"][c] = preset
    if short_seconds is not None:
        m.settings["short_clip_seconds"] = short_seconds
    m.cleanup_partials()
    new = m.discover(include_variants=include_variants)
    fixed = recover_false_positives(m)
    m.save()
    if new:
        console.print(f"Found {len(new)} new video(s).")
    if fixed:
        console.print(f"{len(fixed)} video(s) that failed a faulty check in an earlier version will be checked again.")
    return m


def _progress() -> Progress:
    return Progress(TextColumn("{task.description}"), BarColumn(), TextColumn("{task.percentage:>5.1f}%"),
                    TimeElapsedColumn(), TimeRemainingColumn(), console=console)


def do_analyze(m: Manifest, use_clip: bool = True) -> None:
    from .pipeline import analyze_item, pending_analysis

    ids = pending_analysis(m)
    if not ids:
        return
    with _progress() as p:
        task = p.add_task("Analyzing", total=len(ids))
        for i in ids:
            p.update(task, description=f"Analyzing {i}")
            it = analyze_item(m, i, use_clip=use_clip)
            if it["status"] == "error":
                console.print(f"[red]{i}: {it['error']}[/red]")
            p.advance(task)


def do_encode(m: Manifest) -> None:
    from .pipeline import encode_and_verify, pending_encode, pending_verify, verify_only

    for i in pending_verify(m):  # resumed runs: encoded but never checked
        it = verify_only(m, i)
        if it["status"] == "verified":
            console.print(f"{i}: checked, ok")

    ids = pending_encode(m)
    if not ids:
        console.print("Nothing to encode.")
        return
    with _progress() as p:
        overall = p.add_task("All videos", total=len(ids))
        for i in ids:
            t = p.add_task(i, total=1.0)
            it = encode_and_verify(m, i, lambda f, t=t: p.update(t, completed=f))
            p.remove_task(t)
            p.advance(overall)
            enc = it.get("encode") or {}
            q = enc.get("quality") or {}
            if it["status"] == "verified":
                flag = " [yellow](check quality)[/yellow]" if q.get("flag") else ""
                console.print(f"{i}: {human_bytes(it['probe']['size'])} → {human_bytes(enc['size'])} "
                              f"({100 * (1 - enc['ratio']):.0f}% smaller), {q.get('metric', '').upper()} "
                              f"{q.get('mean')}{flag}")
            elif it["status"] == "skipped":
                console.print(f"[dim]{i}: skipped, only {100 * (1 - enc.get('ratio', 1)):.0f}% smaller[/dim]")
            else:
                console.print(f"[red]{i}: {it.get('error')}[/red]")


@app.command()
def run(directory: Path = DirArg,
        codec: Optional[str] = typer.Option(None, help="hevc (default) or av1."),
        crf: Optional[int] = typer.Option(None, help="Quality level; lower is better. Default 20 (hevc) / 28 (av1)."),
        preset: Optional[str] = typer.Option(None, help="Encoder preset (hevc: slow; av1: 5)."),
        review_first: bool = typer.Option(False, "--review-first", help="Open the review UI before encoding."),
        no_review: bool = typer.Option(False, "--no-review", help="Don't open the review UI at the end."),
        no_clip: bool = typer.Option(False, "--no-clip", help="Skip the optional CLIP orientation fallback."),
        include_variants: bool = typer.Option(False, "--include-variants", help="Also process PXL_*.XX.mp4 files."),
        port: int = typer.Option(8765, help="Review UI port."),
        short_seconds: Optional[float] = ShortOpt,
        ffmpeg: Optional[str] = FFmpegOpt,
        verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    """Analyze, encode, verify, then open the review UI."""
    m = _open(directory, ffmpeg, codec, crf, preset, include_variants, verbose, short_seconds)
    do_analyze(m, use_clip=not no_clip)
    if not review_first:
        do_encode(m)
    if not no_review:
        from .server import serve
        serve(m, port)


@app.command()
def analyze(directory: Path = DirArg, no_clip: bool = typer.Option(False, "--no-clip"),
            include_variants: bool = typer.Option(False, "--include-variants"),
            reanalyze: bool = typer.Option(False, "--reanalyze", help="Redo analysis for all videos (keeps your manual changes)."),
            ffmpeg: Optional[str] = FFmpegOpt) -> None:
    """Probe videos and detect rotation and speech (no encoding)."""
    m = _open(directory, ffmpeg, include_variants=include_variants)
    if reanalyze:
        for it in m:
            if it["status"] not in ("finalized",) and m.src_path(it).exists():
                m.update(it["id"], save=False, status="pending")
        m.save()
    do_analyze(m, use_clip=not no_clip)
    status(directory, ffmpeg)


@app.command()
def encode(directory: Path = DirArg, codec: Optional[str] = typer.Option(None), crf: Optional[int] = typer.Option(None),
           preset: Optional[str] = typer.Option(None), ffmpeg: Optional[str] = FFmpegOpt) -> None:
    """Encode (or re-encode) every analyzed video whose output is missing or out of date."""
    m = _open(directory, ffmpeg, codec, crf, preset)
    do_analyze(m)
    do_encode(m)


@app.command()
def review(directory: Path = DirArg, port: int = typer.Option(8765), no_browser: bool = typer.Option(False, "--no-browser"),
           short_seconds: Optional[float] = ShortOpt, ffmpeg: Optional[str] = FFmpegOpt) -> None:
    """Open the review UI (works at any stage)."""
    from .server import serve

    m = _open(directory, ffmpeg, short_seconds=short_seconds)
    serve(m, port, open_browser=not no_browser)


@app.command()
def calibrate(directory: Path = DirArg, samples: int = typer.Option(4, help="How many videos to sample."),
              crfs: str = typer.Option("18,20,22,24", help="Comma-separated CRF values to try."),
              codec: Optional[str] = typer.Option(None), ffmpeg: Optional[str] = FFmpegOpt) -> None:
    """Try several quality levels on 10 s segments and show size vs quality."""
    from . import calibrate as cal
    from .pipeline import analyze_item

    m = _open(directory, ffmpeg, codec)
    for it in m:
        if it.get("probe") is None and it["status"] == "pending":
            try:
                m.update(it["id"], probe=ff.probe(m.src_path(it)))
            except ff.FFmpegError:
                pass
    table = Table(title=f"Calibration ({m.settings['codec']})")
    for col in ("video", "source", "CRF", "size vs source", "quality mean", "quality 1% low"):
        table.add_column(col)
    with console.status("Encoding samples…"):
        for r in cal.run(m, [int(x) for x in crfs.split(",")], samples):
            if "error" in r:
                table.add_row(r["id"], "", str(r["crf"]), "[red]failed[/red]", r["error"][:40], "")
                continue
            src = r["vcodec"] + (f" {r['hdr'].upper()}" if r.get("hdr") else "")
            style = "yellow" if r.get("q_flag") else ""
            table.add_row(r["id"], src, str(r["crf"]), f"{100 * r['ratio']:.0f}%",
                          f"[{style}]{r['q_metric']} {r['q_mean']}[/{style}]" if style else f"{r['q_metric']} {r['q_mean']}",
                          str(r["q_p1"]))
    console.print(table)
    console.print("Pick the highest CRF whose quality isn't flagged, then use it with "
                  "[bold]pxlsqueeze run DIR --crf N[/bold] (it's remembered for this folder).")


@app.command()
def finalize(directory: Path = DirArg,
             permanent: bool = typer.Option(False, "--permanent", help="Delete permanently instead of moving to Trash."),
             yes: bool = typer.Option(False, "--yes", help="Skip the typed confirmation."),
             ffmpeg: Optional[str] = FFmpegOpt) -> None:
    """Trash originals of approved videos and videos marked for the Trash (--permanent deletes instead)."""
    from .finalize import finalize as do_finalize
    from .finalize import summary

    m = _open(directory, ffmpeg)
    s = summary(m)
    if s["count"] == 0:
        console.print("Nothing to finalize. Approve videos, or mark them for the Trash, in the review UI first.")
        raise typer.Exit(0)
    where = "permanently delete" if permanent else "move to the Trash"
    parts = []
    if s["approved"]:
        parts.append(f"{s['approved']} original(s) replaced by their new file")
    if s["trash"]:
        parts.append(f"{s['trash']} video(s) marked for the Trash, with no copy kept")
    console.print(f"This will {where}: {' and '.join(parts)}. Frees about {human_bytes(s['freed_bytes'])}.")
    if not yes:
        typed = typer.prompt(f"Type {s['count']} to confirm")
        if typed.strip() != str(s["count"]):
            console.print("Cancelled.")
            raise typer.Exit(1)
    res = do_finalize(m, permanent=permanent)
    console.print(f"Finalized {len(res['done'])} video(s). Log: {res['log']}")
    for f in res["failed"]:
        console.print(f"[yellow]Kept {f['id']}: {f['reason']}[/yellow]")


@app.command()
def status(directory: Path = DirArg, ffmpeg: Optional[str] = FFmpegOpt) -> None:
    """Show a table of every video and where it is in the pipeline."""
    m = Manifest(directory)
    table = Table()
    table.add_column("video", no_wrap=True, min_width=22)
    for col in ( "length", "status", "rotation", "trim", "size", "quality", "notes"):
        table.add_column(col)
    for it in m:
        p = it.get("probe") or {}
        enc = it.get("encode") or {}
        q = enc.get("quality") or {}
        rot = effective_rotation(it) if p else 0
        rot_s = f"{rot}°" + ("" if it["rotation"].get("override") is None else " (manual)") if rot or it["rotation"].get("override") is not None else "–"
        trim_s = "–"
        if p and is_trimmed(it):
            from .manifest import effective_trim
            s, e = effective_trim(it)
            trim_s = f"{human_seconds(s)}–{human_seconds(e)}"
        size = (f"{human_bytes(p.get('size'))} → {human_bytes(enc.get('size'))}" if enc.get("size")
                else human_bytes(p.get("size")))
        status_s = it["status"] + (f" / {it['decision']}" if it.get("decision") else "")
        from .manifest import is_short
        short_note = "short clip, suggest Trash" if is_short(it, m) and not it.get("decision") else ""
        table.add_row(it["id"], human_seconds(p.get("duration")), status_s, rot_s, trim_s, size,
                      f"{q.get('metric', '')} {q.get('mean', '')}".strip() or "–",
                      (it.get("error") or "; ".join([short_note, *(it.get("warnings") or [])]).strip("; "))[:60])
    console.print(table)
    from .manifest import project_summary
    s = project_summary(m)
    line = f"{s['total']} videos: {s['encoded']} encoded"
    if s["trashed"]:
        line += f", {s['trashed']} trashed"
    if s["kept"]:
        line += f", {s['kept']} kept as is"
    line += f", {s['todo']} to go. {human_bytes(s['freed_bytes'])} freed"
    if s["pending_bytes"]:
        line += f", {human_bytes(s['pending_bytes'])} more once finalized"
    console.print(line + ".")


@app.command()
def doctor(ffmpeg: Optional[str] = FFmpegOpt) -> None:
    """Check ffmpeg features and optional models."""
    from .models import model_path

    try:
        ff.set_binaries(ffmpeg)
    except ff.FFmpegError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e
    ok = "[green]yes[/green]"
    no = "[yellow]no[/yellow]"
    console.print(f"pxlsqueeze {__version__}")
    console.print(f"ffmpeg: {ff.version()}")
    v = ff.version_tuple()
    console.print(f"  version ≥ 6.1: {ok if v >= ff.MIN_VERSION else '[red]no — please upgrade[/red]'}")
    console.print(f"  libx265 (HEVC): {ok if ff.has_encoder('libx265') else '[red]no[/red]'}")
    console.print(f"  libsvtav1 (AV1, optional): {ok if ff.has_encoder('libsvtav1') else no}")
    console.print(f"  libx264 (review previews): {ok if ff.has_encoder('libx264') else no}")
    console.print(f"  libvmaf (quality check): {ok if ff.has_filter('libvmaf') else no + ' — SSIM will be used instead'}")
    console.print(f"Face model (YuNet): {ok if model_path('yunet') else no + ' — Haar cascade fallback'}")
    console.print(f"Speech model (Silero VAD): {ok if model_path('silero_vad') else no + ' — loudness fallback'}")
    try:
        import open_clip  # noqa: F401
        console.print(f"CLIP orientation fallback: {ok}")
    except ImportError:
        console.print(f"CLIP orientation fallback: {no} (pip install 'pxlsqueeze\\[orient-ml]')")


if __name__ == "__main__":
    app()
