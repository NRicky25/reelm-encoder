"""reelm-encoder: turn a video into HLS (1080p / 720p / 480p) for Reelm.

uv run reelm-encoder app                   # open the desktop window
uv run reelm-encoder check                 # is ffmpeg / the GPU ready?
uv run reelm-encoder encode episode1.mp4   # encode into ./output (or --out FOLDER)
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

from reelm_encoder.config import Settings, get_settings
from reelm_encoder.encode import EncodeError, build_command, encode
from reelm_encoder.jobs import INCOMPLETE, new_output_dir, unique_folder_name
from reelm_encoder.ladder import pick_rungs
from reelm_encoder.probe import ProbeError, probe
from reelm_encoder.tools import ToolMissingError, find_tool, nvenc_available


def _bar(fraction: float, width: int = 30) -> str:
    filled = int(fraction * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def cmd_check(settings: Settings) -> int:
    try:
        ffmpeg = find_tool("ffmpeg", settings.ffmpeg_dir)
        find_tool("ffprobe", settings.ffmpeg_dir)
    except ToolMissingError as e:
        print(f"✗ {e}")
        return 1
    print(f"✓ ffmpeg: {ffmpeg}")
    print(
        "✓ NVIDIA GPU encoding (NVENC) available"
        if nvenc_available(ffmpeg)
        else "• No NVIDIA GPU encoding: will use the CPU (slower)"
    )
    return 0


def cmd_encode(args: argparse.Namespace, settings: Settings) -> int:
    try:
        ffmpeg = find_tool("ffmpeg", settings.ffmpeg_dir)
        ffprobe = find_tool("ffprobe", settings.ffmpeg_dir)
        source = probe(ffprobe, Path(args.input))
    except (ToolMissingError, ProbeError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    gpu = not args.cpu and nvenc_available(ffmpeg)
    rungs = pick_rungs(source.width, source.height)
    root = Path(args.out) if args.out else settings.work_dir
    try:
        out_dir = new_output_dir(root, unique_folder_name(source.path))
    except OSError as e:
        print(f"✗ Can't write to {root}: {e}", file=sys.stderr)
        return 1
    (out_dir / INCOMPLETE).touch()

    size = f"{source.width}x{source.height}"
    audio = "" if source.has_audio else ", no audio"
    print(f"Source:  {source.path.name}  {size}, {_fmt_time(source.duration)}{audio}")
    print(f"Quality: {', '.join(r.label for r in rungs)}  ({'NVIDIA GPU' if gpu else 'CPU'})")

    started = time.monotonic()

    def show(fraction: float) -> None:
        elapsed = time.monotonic() - started
        eta = f" ~{_fmt_time(elapsed / fraction - elapsed)} left" if 0.02 < fraction < 1 else ""
        print(f"\rEncoding  {_bar(fraction)} {fraction:6.1%}{eta}   ", end="", flush=True)

    try:
        command = build_command(ffmpeg, source, rungs, gpu=gpu)
        encode(command, out_dir, source.duration, show, expected_variants=len(rungs))
    except (EncodeError, KeyboardInterrupt) as e:
        print(f"\n✗ {'Cancelled' if isinstance(e, KeyboardInterrupt) else e}", file=sys.stderr)
        shutil.rmtree(out_dir, ignore_errors=True)
        return 1
    (out_dir / INCOMPLETE).unlink(missing_ok=True)
    print(f"\n✓ Encoded in {_fmt_time(time.monotonic() - started)}  ->  {out_dir}\n")

    link = settings.video_link(out_dir.name)
    print(f"  Folder:    {out_dir.name}")
    print(f"  Duration:  {round(source.duration)} seconds")
    if link:
        print(f"  Link:      {link}   (once uploaded)")
    print("\nNext:")
    print(f"  1. Upload the folder '{out_dir.name}' into your bucket under videos/.")
    print("  2. Reelm admin -> Videos -> New: provider 'external', status 'ready',")
    print(f"     paste the link and duration {round(source.duration)}, then Save.")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # progress bars use non-ASCII characters
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="reelm-encoder", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("app", help="open the desktop window")
    sub.add_parser("check", help="check ffmpeg and GPU encoding")
    p = sub.add_parser("encode", help="encode a video to HLS in a new folder")
    p.add_argument("input", help="video file (mp4, mkv, mov, ...)")
    p.add_argument("--cpu", action="store_true", help="don't use the NVIDIA GPU")
    p.add_argument("--out", help="folder to put the result in (default: <project>/output)")

    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        if args.command == "app":
            from reelm_encoder.desktop import run

            return run()
        if args.command == "check":
            return cmd_check(settings)
        return cmd_encode(args, settings)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
