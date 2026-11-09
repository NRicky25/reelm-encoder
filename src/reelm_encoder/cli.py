"""reelm-encoder: turn a video into HLS and put it on Cloudflare R2.

uv run reelm-encoder check                 # is everything set up?
uv run reelm-encoder encode episode1.mp4   # encode + upload, prints the link
uv run reelm-encoder encode clip.mp4 --no-upload --keep   # just encode locally
uv run reelm-encoder delete <video-id>     # remove a video from R2
"""

import argparse
import shutil
import sys
import time
import uuid
from pathlib import Path

from reelm_encoder.config import Settings, get_settings
from reelm_encoder.encode import EncodeError, build_command, encode
from reelm_encoder.ladder import pick_rungs
from reelm_encoder.probe import ProbeError, probe
from reelm_encoder.r2 import R2, R2Error
from reelm_encoder.tools import ToolMissingError, find_tool, nvenc_available


def _bar(fraction: float, width: int = 30) -> str:
    filled = int(fraction * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def cmd_check(settings: Settings) -> int:
    ok = True
    try:
        ffmpeg = find_tool("ffmpeg", settings.ffmpeg_dir)
        find_tool("ffprobe", settings.ffmpeg_dir)
        print(f"✓ ffmpeg: {ffmpeg}")
        print(
            "✓ NVIDIA GPU encoding (NVENC) available"
            if nvenc_available(ffmpeg)
            else "• No NVIDIA GPU encoding: will use the CPU (slower)"
        )
    except ToolMissingError as e:
        print(f"✗ {e}")
        ok = False
    if not settings.r2_ready:
        print(f"✗ R2 not configured: set {', '.join(settings.missing_r2())} in .env")
        return 1
    try:
        R2.from_settings(settings).check()
        print(f"✓ R2 bucket '{settings.r2_bucket}' reachable")
        print(f"  public URL: {settings.r2_public_url}")
    except R2Error as e:
        print(f"✗ {e}")
        ok = False
    return 0 if ok else 1


def cmd_encode(args: argparse.Namespace, settings: Settings) -> int:
    upload = not args.no_upload
    try:
        ffmpeg = find_tool("ffmpeg", settings.ffmpeg_dir)
        ffprobe = find_tool("ffprobe", settings.ffmpeg_dir)
        r2 = R2.from_settings(settings) if upload else None
        if r2:
            r2.check()  # fail before spending minutes encoding
        source = probe(ffprobe, Path(args.input))
    except (ToolMissingError, ProbeError, R2Error) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    gpu = not args.cpu and nvenc_available(ffmpeg)
    rungs = pick_rungs(source.width, source.height)
    video_id = uuid.uuid4().hex
    out_dir = (Path(args.out) if args.out else settings.work_dir) / video_id

    size = f"{source.width}x{source.height}"
    audio = "" if source.has_audio else ", no audio"
    print(f"Source:  {source.path.name}  {size}, {_fmt_time(source.duration)}{audio}")
    print(f"Quality: {', '.join(r.label for r in rungs)}  ({'NVIDIA GPU' if gpu else 'CPU'})")
    print(f"Video id: {video_id}")

    started = time.monotonic()

    def show(fraction: float) -> None:
        elapsed = time.monotonic() - started
        eta = f" ~{_fmt_time(elapsed / fraction - elapsed)} left" if 0.02 < fraction < 1 else ""
        print(f"\rEncoding  {_bar(fraction)} {fraction:6.1%}{eta}   ", end="", flush=True)

    try:
        encode(build_command(ffmpeg, source, rungs, gpu=gpu), out_dir, source.duration, show)
    except (EncodeError, KeyboardInterrupt) as e:
        print(f"\n✗ {'Cancelled' if isinstance(e, KeyboardInterrupt) else e}", file=sys.stderr)
        shutil.rmtree(out_dir, ignore_errors=True)
        return 1
    print(f"\n✓ Encoded in {_fmt_time(time.monotonic() - started)}  ->  {out_dir}")

    if r2 is None:
        print("\nNot uploaded (--no-upload). Files are in the folder above.")
        return 0

    def show_upload(done: int, total: int) -> None:
        print(f"\rUploading {_bar(done / total)} {done}/{total} files   ", end="", flush=True)

    try:
        url = r2.upload_video(out_dir, video_id, show_upload)
    except (R2Error, KeyboardInterrupt) as e:
        print(f"\n✗ {'Cancelled' if isinstance(e, KeyboardInterrupt) else e}", file=sys.stderr)
        print(f"  The encoded files are kept in {out_dir}", file=sys.stderr)
        return 1
    if not args.keep:
        shutil.rmtree(out_dir, ignore_errors=True)

    print("\n✓ Uploaded\n")
    print(f"  Stream URL:  {url}")
    print(f"  Duration:    {round(source.duration)} seconds")
    print(f"  Video id:    {video_id}   (to remove it: reelm-encoder delete {video_id})")
    print("\nNext, in the Reelm admin:")
    print("  1. Videos -> New: provider 'external', status 'ready', paste the Stream URL,")
    print(f"     duration {round(source.duration)}, then Save.")
    print("  2. Open the episode or movie, choose that video,")
    print(f"     set Duration to {round(source.duration)}, then Save.")
    return 0


def cmd_delete(args: argparse.Namespace, settings: Settings) -> int:
    if not args.yes:
        answer = input(f"Delete every file of video {args.video_id} from R2? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing deleted.")
            return 0
    try:
        count = R2.from_settings(settings).delete_video(args.video_id)
    except R2Error as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"✓ Deleted {count} files" if count else "Nothing found for that video id.")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # progress bars use non-ASCII characters
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="reelm-encoder", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="check ffmpeg, GPU encoding and R2 access")
    p = sub.add_parser("encode", help="encode a video to HLS and upload it to R2")
    p.add_argument("input", help="video file (mp4, mkv, mov, ...)")
    p.add_argument("--cpu", action="store_true", help="don't use the NVIDIA GPU")
    p.add_argument("--no-upload", action="store_true", help="only encode, don't upload")
    p.add_argument("--keep", action="store_true", help="keep the encoded files after uploading")
    p.add_argument("--out", help="folder for encoded files (default: ./output)")
    d = sub.add_parser("delete", help="delete a video's files from R2")
    d.add_argument("video_id")
    d.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    args = parser.parse_args(argv)
    settings = get_settings()
    if args.command == "check":
        return cmd_check(settings)
    if args.command == "encode":
        return cmd_encode(args, settings)
    return cmd_delete(args, settings)
