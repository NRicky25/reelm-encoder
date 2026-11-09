"""Encode videos to HLS and upload them to Cloudflare R2 for Reelm."""

import sys


def main() -> None:
    from reelm_encoder.cli import main as run

    sys.exit(run())
