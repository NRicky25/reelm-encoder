from pathlib import Path

import pytest

from reelm_encoder.ladder import pick_rungs
from reelm_encoder.probe import ProbeError, parse_probe


def sizes(w, h):
    return [(r.width, r.height, r.video_kbps) for r in pick_rungs(w, h)]


def test_full_hd_gets_three_levels():
    assert sizes(1920, 1080) == [(1920, 1080, 5000), (1280, 720, 2800), (854, 480, 1400)]


def test_wide_cinema_keeps_its_shape():
    assert sizes(1920, 816) == [(1920, 816, 5000), (1280, 544, 2800), (854, 362, 1400)]


def test_never_upscales_and_4k_is_capped():
    assert sizes(1280, 720)[0][:2] == (1280, 720)
    assert sizes(3840, 2160)[0][:2] == (1920, 1080)
    small = sizes(640, 360)
    assert small == [(640, 360, small[0][2])] and small[0][2] >= 600


def test_odd_sizes_become_even():
    for w, h, _ in sizes(1001, 563):
        assert w % 2 == 0 and h % 2 == 0


def test_close_sizes_are_not_duplicated():
    widths = [w for w, _, _ in sizes(1366, 768)]
    assert widths == [1366, 854]  # 1280 is too close to 1366 to be worth it


def probe_info(**video):
    return {
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080} | video,
            {"codec_type": "audio"},
        ],
        "format": {"duration": "61.5"},
    }


def test_parse_probe():
    s = parse_probe(Path("a.mp4"), probe_info())
    assert (s.width, s.height, s.duration, s.has_audio) == (1920, 1080, 61.5, True)


def test_rotated_phone_video_is_portrait():
    s = parse_probe(Path("a.mp4"), probe_info(side_data_list=[{"rotation": -90}]))
    assert (s.width, s.height) == (1080, 1920)


def test_cover_art_is_not_the_video():
    info = probe_info()
    info["streams"].insert(
        0, {"codec_type": "video", "width": 600, "height": 600, "disposition": {"attached_pic": 1}}
    )
    assert parse_probe(Path("a.mp4"), info).width == 1920


def test_no_video_or_length_is_an_error():
    with pytest.raises(ProbeError, match="no video"):
        parse_probe(
            Path("a.mp3"), {"streams": [{"codec_type": "audio"}], "format": {"duration": "3"}}
        )
    with pytest.raises(ProbeError, match="how long"):
        parse_probe(Path("a.mp4"), probe_info() | {"format": {}})
