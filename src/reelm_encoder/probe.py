"""Reading a source video's size, length and streams with ffprobe."""

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10 / Dolby Vision base) and HLG


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    path: Path
    width: int  # as displayed (after rotation)
    height: int
    duration: float  # seconds
    has_audio: bool
    video_index: int = 0  # ffmpeg stream index of the real video (not cover art)
    audio_index: int | None = None  # the audio track to use
    fps: float = 24.0
    hdr: bool = False


def probe(ffprobe: str, path: Path) -> Source:
    if not path.is_file():
        raise ProbeError(f"File not found: {path}")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )  # fmt: skip
    if result.returncode != 0:
        raise ProbeError(f"ffprobe couldn't read {path.name}: {result.stderr.strip()[-500:]}")
    try:
        info = json.loads(result.stdout or "{}")
    except ValueError as e:
        raise ProbeError(f"ffprobe gave unreadable output for {path.name}") from e
    return parse_probe(path, info)


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            value = float(Fraction(stream.get(key) or "0/1"))
        except (ValueError, ZeroDivisionError):
            continue
        if 1 <= value <= 240:
            return value
    return 24.0


def _pick_audio(streams: list[dict]) -> dict | None:
    """The main audio track: the default one, else the one with most channels, else the first."""
    audio = [s for s in streams if s.get("codec_type") == "audio" and s.get("codec_name")]
    if not audio:
        return None
    default = [s for s in audio if s.get("disposition", {}).get("default")]
    pool = default or audio
    return max(pool, key=lambda s: int(s.get("channels") or 0))


def parse_probe(path: Path, info: dict) -> Source:
    streams = info.get("streams", [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")  # skip cover art
            and s.get("width")
            and s.get("height")
        ),
        None,
    )
    if video is None:
        raise ProbeError(f"{path.name} has no video stream.")
    width, height = int(video["width"]), int(video["height"])
    # Phone videos are often stored sideways with a "rotate" flag (ffmpeg turns them upright).
    rotation = 0
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = abs(int(side["rotation"])) % 180
    if rotation == 90:
        width, height = height, width
    try:
        duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    except ValueError:
        duration = 0
    if duration <= 0:
        raise ProbeError(f"Couldn't tell how long {path.name} is.")
    audio = _pick_audio(streams)
    return Source(
        path=path,
        width=width,
        height=height,
        duration=duration,
        has_audio=audio is not None,
        video_index=int(video.get("index", 0)),
        audio_index=None if audio is None else int(audio.get("index", 0)),
        fps=_fps(video),
        hdr=video.get("color_transfer") in HDR_TRANSFERS,
    )
