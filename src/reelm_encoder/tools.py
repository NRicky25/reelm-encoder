"""Finding ffmpeg / ffprobe and checking for NVIDIA hardware encoding."""

import os
import shutil
import subprocess
from functools import cache
from pathlib import Path


class ToolMissingError(RuntimeError):
    pass


def _winget_candidates(name: str) -> list[Path]:
    """`winget install Gyan.FFmpeg` puts ffmpeg here without always adding it to PATH."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
    found = [base / "Links" / f"{name}.exe"]
    packages = base / "Packages"
    if packages.is_dir():
        found += sorted(packages.glob(f"*FFmpeg*/*/bin/{name}.exe"), reverse=True)
    return found


def find_tool(name: str, folder: Path | None = None) -> str:
    """Path to ffmpeg or ffprobe: FFMPEG_DIR, then PATH, then the winget install folder."""
    candidates: list[Path] = []
    if folder:
        candidates += [folder / f"{name}.exe", folder / name]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    if on_path := shutil.which(name):
        return on_path
    for candidate in _winget_candidates(name):
        if candidate.is_file():
            return str(candidate)
    raise ToolMissingError(
        f"{name} not found. Install it (winget install Gyan.FFmpeg) or set FFMPEG_DIR in .env."
    )


@cache
def nvenc_available(ffmpeg: str) -> bool:
    """True if this machine can encode H.264 on an NVIDIA GPU (NVENC)."""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=black:s=256x256:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True,
            timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
