"""Which quality levels ("renditions") to make from a source video.

Levels go by the LONG side of the picture (1920 / 1280 / 854, i.e. 1080p /
720p / 480p for 16:9), so wide cinema videos (1920x816) keep their shape and
portrait phone videos (1080x1920) get the same treatment turned sideways.
We never upscale. Bitrate follows picture area and always gets lower as the
picture gets smaller, so the player's automatic quality switching behaves.
"""

from dataclasses import dataclass

STANDARD_LONG = (1920, 1280, 854)
FULL_HD_AREA = 1920 * 1080
FULL_HD_KBPS = 5000
MIN_KBPS = 500
AUDIO_KBPS = 128


@dataclass(frozen=True)
class Rung:
    width: int
    height: int
    video_kbps: int

    @property
    def label(self) -> str:
        return f"{min(self.width, self.height)}p"


def _even(n: float) -> int:
    return max(2, round(n / 2) * 2)


def bitrate_for(width: int, height: int) -> int:
    """~5 Mbit/s for 1920x1080, scaled by picture area (to the power 0.75, since
    bigger pictures compress better), rounded to 100 kbit/s."""
    kbps = FULL_HD_KBPS * ((width * height) / FULL_HD_AREA) ** 0.75
    return max(MIN_KBPS, round(kbps / 100) * 100)


def pick_rungs(src_width: int, src_height: int) -> list[Rung]:
    portrait = src_height > src_width
    long_side, short_side = (src_height, src_width) if portrait else (src_width, src_height)

    top = min(long_side, STANDARD_LONG[0])
    longs = [top] + [s for s in STANDARD_LONG if s < top * 0.9]

    rungs: list[Rung] = []
    for long_ in longs:
        lg, sh = _even(long_), _even(short_side * long_ / long_side)
        w, h = (sh, lg) if portrait else (lg, sh)
        kbps = bitrate_for(w, h)
        if rungs and kbps >= rungs[-1].video_kbps:  # strictly decreasing
            kbps = rungs[-1].video_kbps - 100
        rungs.append(Rung(width=w, height=h, video_kbps=max(MIN_KBPS, kbps)))
    return rungs
