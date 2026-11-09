"""Turning one video file into HLS (several qualities, cut into short chunks).

One ffmpeg run decodes the source once, scales it to every quality level and
writes:

    out_dir/master.m3u8           <- the link the player uses
    out_dir/v1080p/index.m3u8     <- one playlist per quality
    out_dir/v1080p/seg_00000.ts   <- ~6 second chunks
    out_dir/v720p/...             <- lower qualities

Keyframes are forced at every chunk boundary so all qualities line up and the
player can switch between them smoothly.
"""

import subprocess
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

from reelm_encoder.ladder import AUDIO_KBPS, Rung
from reelm_encoder.probe import Source

SEGMENT_SECONDS = 6
MASTER = "master.m3u8"


class EncodeError(RuntimeError):
    pass


def build_command(
    ffmpeg: str,
    source: Source,
    rungs: list[Rung],
    *,
    gpu: bool,
    segment_seconds: int = SEGMENT_SECONDS,
) -> list[str]:
    """The ffmpeg command. Run it with cwd = the output folder (paths are relative)."""
    n = len(rungs)
    splits = "".join(f"[s{i}]" for i in range(n))
    scales = ";".join(
        f"[s{i}]scale={r.width}:{r.height}:flags=lanczos,format=yuv420p[v{i}]"
        for i, r in enumerate(rungs)
    )
    cmd = [
        ffmpeg, "-hide_banner", "-y", "-nostdin", "-loglevel", "error",
        "-nostats", "-progress", "pipe:1",
        "-i", str(source.path.resolve()),
        "-filter_complex", f"[0:v]split={n}{splits};{scales}",
    ]  # fmt: skip
    for i in range(n):
        cmd += ["-map", f"[v{i}]"]
        if source.has_audio:
            cmd += ["-map", "0:a:0"]

    codec = "h264_nvenc" if gpu else "libx264"
    cmd += ["-c:v", codec, "-profile:v", "high"]
    if gpu:
        cmd += ["-preset:v", "p5", "-tune:v", "hq", "-rc:v", "vbr", "-forced-idr", "1"]
    else:
        cmd += ["-preset:v", "veryfast", "-sc_threshold", "0"]
    for i, r in enumerate(rungs):
        cmd += [
            f"-b:v:{i}", f"{r.video_kbps}k",
            f"-maxrate:v:{i}", f"{int(r.video_kbps * 1.1)}k",
            f"-bufsize:v:{i}", f"{int(r.video_kbps * 1.5)}k",
        ]  # fmt: skip
    cmd += ["-force_key_frames", f"expr:gte(t,n_forced*{segment_seconds})"]
    if source.has_audio:
        cmd += ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ac", "2", "-ar", "48000"]

    stream_map = " ".join(
        f"v:{i},a:{i},name:{r.label}" if source.has_audio else f"v:{i},name:{r.label}"
        for i, r in enumerate(rungs)
    )
    cmd += [
        "-f", "hls",
        "-hls_time", str(segment_seconds),
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", "v%v/seg_%05d.ts",
        "-master_pl_name", MASTER,
        "-var_stream_map", stream_map,
        "v%v/index.m3u8",
    ]  # fmt: skip
    return cmd


def parse_progress(line: str, duration: float) -> float | None:
    """Fraction done (0..1) from one line of `ffmpeg -progress` output, if it has one."""
    key, _, value = line.strip().partition("=")
    if key == "progress" and value == "end":
        return 1.0
    if key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit():
        seconds = int(value) / 1_000_000  # both keys are in microseconds
        return max(0.0, min(1.0, seconds / duration)) if duration > 0 else None
    return None


def encode(
    command: list[str],
    out_dir: Path,
    duration: float,
    on_progress: Callable[[float], None] = lambda _f: None,
) -> Path:
    """Run ffmpeg, reporting progress. Returns the master playlist path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stderr_tail: deque[str] = deque(maxlen=30)
    proc = subprocess.Popen(
        command,
        cwd=out_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def drain_stderr() -> None:  # read errors in parallel so ffmpeg never blocks
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())

    reader = threading.Thread(target=drain_stderr, daemon=True)
    reader.start()
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            fraction = parse_progress(line, duration)
            if fraction is not None:
                on_progress(fraction)
        code = proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    reader.join(timeout=5)
    master = out_dir / MASTER
    if code != 0 or not master.is_file():
        details = "\n".join(stderr_tail) or f"exit code {code}"
        raise EncodeError(f"ffmpeg failed:\n{details}")
    return master
