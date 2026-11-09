"""Which quality levels ("renditions") to make from a source video.

Sizes go by width, so wide cinema videos (e.g. 1920x816) keep their shape.
We never upscale: the top level is the source width, capped at 1920.
"""

from dataclasses import dataclass

# width -> video bitrate (kbit/s), roughly what streaming sites use for H.264
STANDARD = {1920: 5000, 1280: 2800, 854: 1400}
MAX_WIDTH = 1920
AUDIO_KBPS = 128


@dataclass(frozen=True)
class Rung:
    width: int
    height: int
    video_kbps: int

    @property
    def label(self) -> str:
        return f"{self.height}p"


def _even(n: float) -> int:
    return max(2, round(n / 2) * 2)


def bitrate_for(width: int) -> int:
    if width in STANDARD:
        return STANDARD[width]
    # scale with picture area, rounded to 100 kbit/s
    kbps = STANDARD[MAX_WIDTH] * (width / MAX_WIDTH) ** 2
    return max(600, round(kbps / 100) * 100)


def pick_rungs(src_width: int, src_height: int) -> list[Rung]:
    top = min(_even(src_width), MAX_WIDTH)
    widths = [top] + [w for w in sorted(STANDARD, reverse=True) if w < top * 0.9]
    return [
        Rung(width=w, height=_even(src_height * w / src_width), video_kbps=bitrate_for(w))
        for w in widths
    ]
