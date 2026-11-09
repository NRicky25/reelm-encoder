"""Reading a source video's size, length and streams with ffprobe."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    path: Path
    width: int
    height: int
    duration: float  # seconds
    has_audio: bool

    @property
    def aspect(self) -> float:
        return self.width / self.height


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
    return parse_probe(path, json.loads(result.stdout))


def parse_probe(path: Path, info: dict) -> Source:
    streams = info.get("streams", [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")  # skip cover art
        ),
        None,
    )
    if video is None:
        raise ProbeError(f"{path.name} has no video stream.")
    width, height = int(video["width"]), int(video["height"])
    # Phone videos are often stored sideways with a "rotate" flag.
    rotation = 0
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = abs(int(side["rotation"])) % 180
    if rotation == 90:
        width, height = height, width
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ProbeError(f"Couldn't tell how long {path.name} is.")
    return Source(
        path=path,
        width=width,
        height=height,
        duration=duration,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )
