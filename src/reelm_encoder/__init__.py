"""Encode videos to HLS (1080p / 720p / 480p) for Reelm."""

import sys


def main() -> None:
    from reelm_encoder.cli import main as run

    sys.exit(run())
