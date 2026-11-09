import re
import subprocess
from pathlib import Path

import pytest

from reelm_encoder.encode import build_command, encode, parse_progress
from reelm_encoder.ladder import pick_rungs
from reelm_encoder.probe import Source, probe
from reelm_encoder.tools import ToolMissingError, find_tool, nvenc_available

try:
    FFMPEG, FFPROBE = find_tool("ffmpeg"), find_tool("ffprobe")
except ToolMissingError:
    FFMPEG = FFPROBE = None
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


def src(has_audio=True, **kw):
    defaults = {"video_index": 0, "audio_index": 1 if has_audio else None, "fps": 25.0}
    return Source(Path("in.mp4"), 1920, 1080, 120.0, has_audio, **(defaults | kw))


def test_command_maps_every_level():
    cmd = build_command("ffmpeg", src(), pick_rungs(1920, 1080), gpu=True)
    joined = " ".join(cmd)
    assert "h264_nvenc" in cmd and "libx264" not in cmd
    assert cmd[cmd.index("-var_stream_map") + 1] == (
        "v:0,a:0,name:1080p v:1,a:1,name:720p v:2,a:2,name:480p"
    )
    assert cmd.count("0:1") == 3, "the chosen audio track, once per quality"
    assert "-b:v:2" in cmd and "scale=854:480" in joined
    assert "expr:gte(t,n_forced*6)" in cmd  # chunks of all qualities line up
    assert cmd[cmd.index("-g") + 1] == "150", "keyframes only on chunk boundaries (25 fps x 6 s)"
    assert cmd[cmd.index("-colorspace") + 1] == "bt709"
    assert "zscale" not in joined, "no tone-mapping for normal video"


def test_command_uses_the_probed_streams_and_tone_maps_hdr():
    cmd = build_command(
        "ffmpeg", src(video_index=2, audio_index=4, hdr=True), pick_rungs(1920, 1080), gpu=False
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.startswith("[0:2]zscale=") and "tonemap=hable" in graph
    assert cmd.count("0:4") == 3


def test_command_without_audio_or_gpu():
    cmd = build_command("ffmpeg", src(has_audio=False), pick_rungs(1280, 720), gpu=False)
    assert "libx264" in cmd and "-c:a" not in cmd
    assert not any(a.startswith("0:") for a in cmd), "no audio mapped"
    assert cmd[cmd.index("-var_stream_map") + 1] == "v:0,name:720p v:1,name:480p"


def write_hls(folder: Path, variants: list[str], *, finished: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    master = folder / "master.m3u8"
    master.write_text("#EXTM3U\n" + "".join(f"#EXT-X-STREAM-INF:X\n{v}\n" for v in variants))
    for v in variants:
        (folder / v).parent.mkdir(parents=True, exist_ok=True)
        (folder / v).write_text("#EXTM3U\n" + ("#EXT-X-ENDLIST\n" if finished else ""))
    return master


def test_incomplete_results_are_rejected(tmp_path):
    from reelm_encoder.encode import EncodeError, check_output

    ok = write_hls(tmp_path / "ok", ["v720p/index.m3u8", "v480p/index.m3u8"])
    check_output(ok, 2)
    with pytest.raises(EncodeError, match="incomplete"):
        check_output(write_hls(tmp_path / "empty", []), 2)
    with pytest.raises(EncodeError, match="1 of 2"):
        check_output(write_hls(tmp_path / "partial", ["v720p/index.m3u8"]), 2)
    with pytest.raises(EncodeError, match="unfinished"):
        check_output(write_hls(tmp_path / "cut", ["v720p/index.m3u8"], finished=False), 1)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("out_time_us=30000000", 0.25),
        ("out_time_ms=60000000", 0.5),
        ("out_time_us=-9223372036854775807", 0.0),
        ("out_time_us=N/A", None),
        ("progress=end", 1.0),
        ("frame=12", None),
        ("out_time_us=999000000", 1.0),
    ],
)
def test_parse_progress(line, expected):
    assert parse_progress(line, 120.0) == expected


def make_clip(path: Path, seconds: int = 5, size: str = "1280x720", audio: bool = True):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=s={size}:r=24:d={seconds}"]  # fmt: skip
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:d={seconds}", "-shortest"]
    subprocess.run([*cmd, "-pix_fmt", "yuv420p", str(path)], check=True)


def run_encode(tmp_path, gpu, audio=True):
    clip = tmp_path / "clip.mp4"
    make_clip(clip, audio=audio)
    source = probe(FFPROBE, clip)
    rungs = pick_rungs(source.width, source.height)
    seen: list[float] = []
    cmd = build_command(FFMPEG, source, rungs, gpu=gpu, segment_seconds=2)
    master = encode(
        cmd, tmp_path / "out", source.duration, seen.append, expected_variants=len(rungs)
    )
    return master, rungs, seen


@needs_ffmpeg
@pytest.mark.parametrize("gpu", [False, True], ids=["cpu", "nvenc"])
def test_real_encode(tmp_path, gpu):
    if gpu and not nvenc_available(FFMPEG):
        pytest.skip("no NVIDIA GPU encoding here")
    master, rungs, seen = run_encode(tmp_path, gpu)
    text = master.read_text()
    assert text.count("#EXT-X-STREAM-INF") == len(rungs) == 2
    assert "RESOLUTION=1280x720" in text and "RESOLUTION=854x480" in text
    variants = re.findall(r"^(v\w+/index\.m3u8)$", text, re.M)
    assert variants == ["v720p/index.m3u8", "v480p/index.m3u8"]  # folders named by quality
    for v in variants:
        playlist = (master.parent / v).read_text()
        assert "#EXT-X-ENDLIST" in playlist  # complete, on-demand playlist
        segments = re.findall(r"^(seg_\d+\.ts)$", playlist, re.M)
        assert len(segments) == 3, playlist  # 5 s in 2 s chunks
        assert all((master.parent / v).parent.joinpath(s).is_file() for s in segments)
    assert seen and seen[-1] == 1.0


@needs_ffmpeg
def test_real_encode_without_audio(tmp_path):
    master, _, _ = run_encode(tmp_path, gpu=False, audio=False)
    assert "mp4a" not in master.read_text()
